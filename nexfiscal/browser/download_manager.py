"""Captura de arquivos: downloads diretos, popups e conteúdo aberto inline (XML/PDF)."""
from __future__ import annotations

import logging
import re
import time
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from ..exceptions import DownloadError
from ..storage.file_manager import detect_kind, read_head
from ..utils import sanitize_name

log = logging.getLogger("nexfiscal.download")


@dataclass
class CapturedFile:
    path: Path                # arquivo temporário (ainda não validado)
    suggested_name: str


class DownloadManager:
    def __init__(self, tmp_dir: Path, timeout_seconds: int = 60) -> None:
        self.tmp_dir, self.timeout = tmp_dir, timeout_seconds

    def _target(self, name: str) -> Path:
        folder = self.tmp_dir / uuid.uuid4().hex[:10]
        folder.mkdir(parents=True, exist_ok=True)
        return folder / (sanitize_name(name) or "download.bin")

    def capture(self, page: Page, trigger: Callable[[], None], timeout: float | None = None) -> CapturedFile:
        """Executa ``trigger`` (clique) e devolve o arquivo resultante.

        Cobre: evento de download; popup/aba com o arquivo; navegação da própria página para o arquivo.
        """
        downloads: list = []
        popups: list[Page] = []
        ctx = page.context

        def on_download(dl) -> None:  # type: ignore[no-untyped-def]
            downloads.append(dl)

        def on_page(p: Page) -> None:
            popups.append(p)
            p.on("download", on_download)

        page.on("download", on_download)
        ctx.on("page", on_page)
        url_before = page.url
        try:
            trigger()
            deadline = time.monotonic() + (timeout if timeout is not None else self.timeout)
            popup_ready_at: float | None = None
            while time.monotonic() < deadline:
                # O evento "page" pode chegar antes do evento "download" em portais
                # que abrem uma aba transitória para iniciar a exportação. Não trate
                # a URL da nova aba como arquivo antes de dar tempo ao evento real.
                if downloads:
                    return self._save_download(downloads[0])
                if popups and self._popup_ready(popups[0]) and popup_ready_at is None:
                    popup_ready_at = time.monotonic()
                if popup_ready_at is not None and time.monotonic() - popup_ready_at >= 2.0:
                    break
                if page.url != url_before and not popups:
                    page.wait_for_timeout(600)
                    if not downloads:
                        break
                page.wait_for_timeout(100)
            if downloads:
                return self._save_download(downloads[0])
            if popups:
                popup_url = popups[0].url
                # Se a aba transitória terminou em about:blank/foi fechada, não
                # tente baixar uma URL vazia: registre uma falha recuperável.
                if not popup_url or popup_url == "about:blank":
                    raise DownloadError("O portal abriu uma aba de exportação, mas não entregou o arquivo.")
                return self._from_url(popups[0], popup_url, close=True)
            if page.url != url_before:
                captured = self._from_url(page, page.url)
                if detect_kind(read_head(captured.path)) == "html":
                    # navegação comum para outra tela do portal: não é um arquivo
                    captured.path.unlink(missing_ok=True)
                    raise DownloadError("O clique não iniciou nenhum download.")
                try:
                    page.go_back()
                except PlaywrightError:
                    pass
                return captured
            raise DownloadError("O clique não iniciou nenhum download.")
        finally:
            try:
                page.remove_listener("download", on_download)
            except Exception:
                pass
            ctx.remove_listener("page", on_page)

    # ------------------------------------------------------------------
    def _save_download(self, dl) -> CapturedFile:  # type: ignore[no-untyped-def]
        name = dl.suggested_filename or "download.bin"
        target = self._target(name)
        try:
            dl.save_as(str(target))
        except PlaywrightError as exc:
            raise DownloadError(f"Download interrompido: {exc}") from exc
        failure = dl.failure()
        if failure:
            raise DownloadError(f"Download interrompido: {failure}")
        return CapturedFile(target, name)

    @staticmethod
    def _popup_ready(popup: Page) -> bool:
        try:
            return popup.url not in ("", "about:blank")
        except PlaywrightError:
            return False

    def _from_url(self, page: Page, url: str, close: bool = False) -> CapturedFile:
        """Baixa o conteúdo de uma URL com os cookies da sessão (PDF/XML aberto no visualizador)."""
        try:
            resp = page.context.request.get(url, timeout=self.timeout * 1000)
            if not resp.ok:
                raise DownloadError(f"O portal respondeu HTTP {resp.status} ao baixar o arquivo.")
            body = resp.body()
            disp = resp.headers.get("content-disposition", "")
            m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", disp)
            name = unquote(m.group(1)) if m else (Path(unquote(urlparse(url).path)).name or "arquivo")
        except PlaywrightError as exc:
            raise DownloadError(f"Não foi possível obter o arquivo: {exc}") from exc
        finally:
            if close:
                try:
                    page.close()
                except PlaywrightError:
                    pass
        target = self._target(name)
        target.write_bytes(body)
        return CapturedFile(target, name)

    @staticmethod
    def extract_zip(zip_path: Path, wanted_suffix: str = ".xml") -> list[Path]:
        """Extrai (com proteção contra path traversal) os arquivos de um ZIP."""
        out: list[Path] = []
        base = zip_path.parent / (zip_path.stem + "_unzipped")
        base.mkdir(exist_ok=True)
        with zipfile.ZipFile(zip_path) as zf:
            for member in zf.infolist():
                if member.is_dir() or not member.filename.lower().endswith(wanted_suffix):
                    continue
                dest = base / sanitize_name(Path(member.filename).name)
                dest.write_bytes(zf.read(member))
                out.append(dest)
        return out
