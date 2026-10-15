"""Download dos XMLs de NFS-e: validação, deduplicação e organização."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from ..browser.download_manager import DownloadManager
from ..browser.page_manager import PageManager
from ..config import portal_config as pc
from ..config.settings import Settings
from ..events import CompanyReporter
from ..exceptions import (DownloadError, InvalidFileError, NexFiscalError, SessionExpiredError,
                          TemporaryError)
from ..portal.selector_engine import SelectorEngine
from ..storage.file_manager import detect_kind, move_atomic, read_head, sha256_file
from ..utils import normalize_text, sanitize_name
from .xml_validator import XmlInfo, XmlValidator

log = logging.getLogger("nexfiscal.xml")

_STRIP_PATTERNS = [
    re.compile(r"\d{2}/\d{2}/\d{4}"), re.compile(r"\d[\d.]*,\d{2}"),
    re.compile(r"\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}"), re.compile(r"\d{11,}"),
]


@dataclass
class XmlStats:
    found: int = 0
    downloaded: int = 0
    existing: int = 0
    failed: int = 0
    files: list[Path] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        return f"{self.downloaded + self.existing}/{self.found}"


class XmlDownloader:
    def __init__(self, page: Page, engine: SelectorEngine, pm: PageManager, dm: DownloadManager,
                 settings: Settings, reporter: CompanyReporter) -> None:
        self.page, self.engine, self.pm, self.dm = page, engine, pm, dm
        self.settings, self.reporter = settings, reporter
        self.validator = XmlValidator()

    # ---------------------------------------------------------------- índice ---
    def _index_existing(self, dest: Path) -> dict[str, XmlInfo]:
        index: dict[str, XmlInfo] = {}
        for f in dest.glob("*.xml"):
            info = self.validator.validate_file(f)
            if info.valid and info.numero:
                index[info.numero.lstrip("0") or "0"] = info
                info.reason = str(f)  # reaproveita o campo para guardar o caminho
        return index

    @staticmethod
    def _row_tokens(text: str) -> tuple[set[str], str]:
        clean = text
        for pat in _STRIP_PATTERNS:
            clean = pat.sub(" ", clean)
        return {t.lstrip("0") or "0" for t in re.findall(r"\d+", clean)}, text

    def _already_have(self, loc, index: dict[str, XmlInfo]) -> bool:  # type: ignore[no-untyped-def]
        """Antes de baixar: o XML desta linha (número + data) já existe e é válido?"""
        if self.settings.existing_files == "overwrite" or not index:
            return False
        try:
            text = loc.evaluate("el => ((el.closest('tr') || el.parentElement || el).innerText || '')")
        except PlaywrightError:
            return False
        tokens, raw = self._row_tokens(text)
        for numero, info in index.items():
            if numero not in tokens:
                continue
            m = re.match(r"(\d{4})-(\d{2})-(\d{2})", info.data_emissao or "")
            if m and re.search(r"\d{2}/\d{2}/\d{4}", raw):
                if f"{m.group(3)}/{m.group(2)}/{m.group(1)}" not in raw:
                    continue
            return True
        return False

    # ---------------------------------------------------------------- fluxo ---
    def run(self, dest: Path) -> XmlStats:
        stats = XmlStats()
        index = self._index_existing(dest)
        last_signature = None
        for page_no in range(1, self.settings.max_result_pages + 1):
            self.pm.checkpoint()
            group = self.engine.find_all(pc.NFSE_XML, timeout=6 if page_no == 1 else 2, required=page_no == 1)
            if group is None:
                break
            if self.pm.contains_any(pc.SESSION_EXPIRED_PATTERNS) or self._login_form_visible():
                raise SessionExpiredError("A sessão do portal expirou durante o download dos XMLs.")
            signature = self._page_signature(group.locators[0])
            if signature == last_signature:
                break  # a paginação não avançou
            last_signature = signature
            log.info("Página %d: %d link(s) de XML.", page_no, len(group))
            for loc in group.locators:
                self._handle_link(loc, dest, index, stats)
            if not self._next_page():
                break
        log.info("XMLs — encontrados: %d | baixados: %d | já existentes: %d | falhas: %d",
                 stats.found, stats.downloaded, stats.existing, stats.failed)
        return stats

    def _handle_link(self, loc, dest: Path, index: dict[str, XmlInfo], stats: XmlStats) -> None:  # type: ignore[no-untyped-def]
        self.pm.checkpoint()
        if self._already_have(loc, index):
            stats.found += 1
            stats.existing += 1
            self.reporter.progress(stats.downloaded + stats.existing + stats.failed, stats.found)
            return
        last_error = ""
        for attempt in range(1, self.settings.max_retries + 1):
            try:
                self._download_one(loc, dest, index, stats)
                self.reporter.progress(stats.downloaded + stats.existing + stats.failed, stats.found)
                return
            except SessionExpiredError:
                raise
            except (TemporaryError, InvalidFileError) as exc:
                last_error = exc.message
                log.warning("XML: tentativa %d/%d falhou: %s", attempt, self.settings.max_retries, last_error)
                if self._session_lost():  # a falha é a sessão caída: refazer login, não insistir
                    raise SessionExpiredError("A sessão do portal expirou durante o download dos XMLs.") from exc
                self.pm.pause(2.0)
            except NexFiscalError as exc:
                last_error = exc.message
                break
        stats.found += 1
        stats.failed += 1
        stats.failures.append(last_error or "falha desconhecida")
        self.reporter.progress(stats.downloaded + stats.existing + stats.failed, stats.found)

    def _download_one(self, loc, dest: Path, index: dict[str, XmlInfo], stats: XmlStats) -> None:  # type: ignore[no-untyped-def]
        captured = self.dm.capture(self.page, lambda: self.pm.click(loc, "download XML"))
        kind = detect_kind(read_head(captured.path))
        paths = self.dm.extract_zip(captured.path) if kind == "zip" else [captured.path]
        if not paths:
            raise InvalidFileError("O ZIP baixado não contém XML.")
        for p in paths:
            self._store(p, dest, index, stats)

    def _store(self, path: Path, dest: Path, index: dict[str, XmlInfo], stats: XmlStats) -> None:
        info = self.validator.validate_file(path)
        if not info.valid:
            path.unlink(missing_ok=True)
            raise InvalidFileError(f"XML inválido: {info.reason}")
        stats.found += 1
        base = f"NFSe_{sanitize_name(info.numero)}" if info.numero else f"NFSe_{info.sha256[:12]}"
        target = dest / f"{base}.xml"
        if target.exists() and self.settings.existing_files == "skip_valid":
            if self.validator.validate_file(target).valid:
                if sha256_file(target) == info.sha256:
                    stats.existing += 1
                    path.unlink(missing_ok=True)
                    return
                target = dest / f"{base}_{info.sha256[:8]}.xml"  # mesmo número, conteúdo diferente
                if target.exists():
                    stats.existing += 1
                    path.unlink(missing_ok=True)
                    return
        move_atomic(path, target)  # conteúdo original, sem modificação
        stats.downloaded += 1
        stats.files.append(target)
        if info.numero:
            index[info.numero.lstrip("0") or "0"] = info
        log.info("XML salvo: %s", target.name)

    # --------------------------------------------------------- paginação ---
    def _page_signature(self, loc) -> str:  # type: ignore[no-untyped-def]
        try:
            return normalize_text(loc.evaluate("el => ((el.closest('table') || document.body).innerText || '')")[:400])
        except PlaywrightError:
            return ""

    def _next_page(self) -> bool:
        nxt = self.engine.find(pc.NFSE_NEXT, timeout=1.5, required=False)
        if nxt is None:
            return False
        try:
            cls = nxt.locator.evaluate("el => (el.className || '') + ' ' + (el.parentElement ? el.parentElement.className : '')")
            if re.search(r"disabled|inativ", str(cls), re.I):
                return False
        except PlaywrightError:
            pass
        self.pm.click(nxt, "próxima página")
        return True

    def _session_lost(self) -> bool:
        return bool(self.pm.contains_any(pc.SESSION_EXPIRED_PATTERNS)) or self._login_form_visible()

    def _login_form_visible(self) -> bool:
        for frame in self.page.frames:
            try:
                if frame.locator("input[type=password]:visible").count():
                    return True
            except PlaywrightError:
                continue
        return False
