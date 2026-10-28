"""Captura de arquivos: downloads diretos, popups e conteúdo aberto inline (XML/PDF)."""
from __future__ import annotations

import hashlib
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

from ..exceptions import DownloadError, NexFiscalError
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
        """Run a click and capture a download, popup file or file navigation.

        A click may report a Playwright error after the browser has already started a download.
        Preserve an observed file rather than discarding it with the click error.
        """
        downloads: list = []
        popups: list[Page] = []
        ctx = page.context

        def on_download(dl) -> None:  # type: ignore[no-untyped-def]
            downloads.append(dl)
            log.debug("Evento Playwright de download recebido | nome=%s", dl.suggested_filename)

        def on_page(p: Page) -> None:
            popups.append(p)
            p.on("download", on_download)

        page.on("download", on_download)
        ctx.on("page", on_page)
        url_before = page.url
        trigger_error: PlaywrightError | None = None
        try:
            try:
                trigger()
            except (PlaywrightError, NexFiscalError) as exc:
                trigger_error = exc
                navigation_started = page.url != url_before
                if not downloads and not popups and not navigation_started:
                    raise DownloadError(f"O clique não concluiu a ação de download: {exc}") from exc
                log.warning("A ação de clique falhou depois que o portal já iniciou uma exportação; examinando evento/aba/navegação observados.",
                            extra={"event": "download_trigger_error_after_event", "error_type": type(exc).__name__,
                                   "download_events": len(downloads), "popups": len(popups)})
            deadline = time.monotonic() + (timeout if timeout is not None else self.timeout)
            popup_ready_at: float | None = None
            while time.monotonic() < deadline:
                if downloads:
                    return self._save_download(downloads[0])
                if popups and self._popup_ready(popups[0]) and popup_ready_at is None:
                    popup_ready_at = time.monotonic()
                if popup_ready_at is not None and time.monotonic() - popup_ready_at >= 2.0:
                    break
                if page.url != url_before and not popups:
                    try:
                        page.wait_for_timeout(600)
                    except PlaywrightError as exc:
                        if downloads:
                            break
                        raise DownloadError("A página fechou durante a navegação de exportação.") from exc
                    if not downloads:
                        break
                try:
                    page.wait_for_timeout(100)
                except PlaywrightError as exc:
                    if downloads or popups:
                        break
                    raise DownloadError("A página foi encerrada antes de confirmar o download.") from exc
            if downloads:
                return self._save_download(downloads[0])
            if popups:
                popup_url = popups[0].url
                if not popup_url or popup_url == "about:blank":
                    raise DownloadError("O portal abriu uma aba de exportação, mas não entregou o arquivo.")
                return self._from_url(popups[0], popup_url, close=True)
            if page.url != url_before:
                captured = self._from_url(page, page.url)
                if detect_kind(read_head(captured.path)) == "html":
                    captured.path.unlink(missing_ok=True)
                    raise DownloadError("O clique não iniciou nenhum download.")
                try:
                    page.go_back()
                except PlaywrightError:
                    log.warning("Não foi possível retornar à página anterior após capturar o arquivo.", exc_info=True)
                return captured
            detail = f" Ação original: {trigger_error}" if trigger_error else ""
            raise DownloadError("O clique não iniciou nenhum download." + detail)
        finally:
            try:
                page.remove_listener("download", on_download)
            except PlaywrightError:
                log.debug("Página principal já encerrada ao limpar listener de download.")
            for popup in popups:
                try:
                    popup.remove_listener("download", on_download)
                except PlaywrightError:
                    log.debug("Popup já encerrado ao limpar listener de download.")
            try:
                ctx.remove_listener("page", on_page)
            except PlaywrightError:
                log.debug("Contexto já encerrado ao limpar listener de nova página.")

    def _save_download(self, dl) -> CapturedFile:  # type: ignore[no-untyped-def]
        name = sanitize_name(dl.suggested_filename or "download.bin") or "download.bin"
        failure = dl.failure()
        if failure:
            raise DownloadError(f"Download interrompido pelo navegador: {failure}")
        last_error: BaseException | None = None
        for attempt in range(1, 4):
            target = self._target(name)
            try:
                dl.save_as(str(target))
                failure = dl.failure()
                if failure:
                    raise DownloadError(f"Download interrompido: {failure}")
                if not target.is_file() or target.stat().st_size <= 0:
                    raise OSError("Playwright não produziu um arquivo não vazio.")
                return CapturedFile(target, name)
            except (PlaywrightError, OSError) as exc:
                last_error = exc
                target.unlink(missing_ok=True)
                try:
                    target.parent.rmdir()
                except OSError:
                    pass
                if attempt == 3:
                    break
                delay = 0.15 * (2 ** (attempt - 1))
                log.warning("Falha transitória ao salvar download Playwright; tentativa %d/3 | nome=%s | erro=%s",
                            attempt, name, exc, extra={"event": "playwright_download_save_retry",
                                                       "attempt": attempt, "download_name": name,
                                                       "delay_seconds": delay})
                time.sleep(delay)
        raise DownloadError(f"Não foi possível salvar o download '{name}' após 3 tentativas: {last_error}") from last_error

    @staticmethod
    def _popup_ready(popup: Page) -> bool:
        try:
            return popup.url not in ("", "about:blank")
        except PlaywrightError:
            return False

    def _from_url(self, page: Page, url: str, close: bool = False) -> CapturedFile:
        """Fetch inline XML/PDF with session cookies and bounded retry for transient failures."""
        body: bytes | None = None
        name = "arquivo"
        try:
            for attempt in range(1, 4):
                try:
                    resp = page.context.request.get(url, timeout=self.timeout * 1000)
                    if not resp.ok:
                        retryable_status = resp.status in (408, 425, 429) or resp.status >= 500
                        if retryable_status and attempt < 3:
                            delay = 0.2 * (2 ** (attempt - 1))
                            log.warning("HTTP transitório ao obter arquivo; tentativa %d/3 | status=%d | url=%s",
                                        attempt, resp.status, url,
                                        extra={"event": "inline_download_http_retry", "attempt": attempt,
                                               "http_status": resp.status, "delay_seconds": delay})
                            time.sleep(delay)
                            continue
                        raise DownloadError(f"O portal respondeu HTTP {resp.status} ao baixar o arquivo.")
                    body = resp.body()
                    if not body:
                        if attempt < 3:
                            delay = 0.2 * (2 ** (attempt - 1))
                            log.warning("Portal retornou conteúdo vazio; repetindo download inline | tentativa=%d/3 | url=%s",
                                        attempt, url, extra={"event": "inline_download_empty_retry",
                                                             "attempt": attempt, "delay_seconds": delay})
                            time.sleep(delay)
                            continue
                        raise DownloadError("O portal respondeu sem conteúdo para o arquivo solicitado.")
                    disp = resp.headers.get("content-disposition", "")
                    match = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", disp)
                    name = unquote(match.group(1)) if match else (Path(unquote(urlparse(url).path)).name or "arquivo")
                    break
                except PlaywrightError as exc:
                    if attempt == 3:
                        raise DownloadError(f"Não foi possível obter o arquivo após 3 tentativas: {exc}") from exc
                    delay = 0.2 * (2 ** (attempt - 1))
                    log.warning("Falha Playwright transitória ao obter arquivo inline; tentativa %d/3 | erro=%s",
                                attempt, exc, extra={"event": "inline_download_retry", "attempt": attempt,
                                                    "delay_seconds": delay})
                    time.sleep(delay)
            if not body:
                raise DownloadError("O portal não entregou um arquivo válido.")
        finally:
            if close:
                try:
                    page.close()
                except PlaywrightError:
                    log.debug("Popup já encerrado após a leitura do arquivo inline.")
        target = self._target(name)
        try:
            target.write_bytes(body)
            if not target.is_file() or target.stat().st_size != len(body) or target.stat().st_size == 0:
                raise OSError("O conteúdo recebido não foi gravado integralmente no temporário.")
        except OSError as exc:
            target.unlink(missing_ok=True)
            raise DownloadError(f"Não foi possível persistir o arquivo obtido do portal: {exc}") from exc
        return CapturedFile(target, name)

    @staticmethod
    def extract_zip(zip_path: Path, wanted_suffix: str = ".xml") -> list[Path]:
        """Extract matching ZIP entries without overwriting distinct same-named files."""
        out: list[Path] = []
        base = zip_path.parent / (zip_path.stem + "_unzipped")
        base.mkdir(exist_ok=True)
        with zipfile.ZipFile(zip_path) as zf:
            for member in zf.infolist():
                if member.is_dir() or not member.filename.lower().endswith(wanted_suffix):
                    continue
                filename = sanitize_name(Path(member.filename).name) or ("arquivo" + wanted_suffix)
                payload = zf.read(member)
                target = base / filename
                if target.exists():
                    if target.read_bytes() == payload:
                        continue
                    stem, suffix = Path(filename).stem, Path(filename).suffix
                    digest = hashlib.sha256(payload).hexdigest()[:8]
                    target = base / f"{stem}_{digest}{suffix}"
                    serial = 2
                    while target.exists() and target.read_bytes() != payload:
                        target = base / f"{stem}_{digest}_{serial}{suffix}"
                        serial += 1
                    if target.exists():
                        continue
                target.write_bytes(payload)
                out.append(target)
        return out
