"""Fluxo completo de UMA empresa: login/CAPTCHA -> XML -> Livro Fiscal -> organização.

Regras de falha:
* erro de login/credenciais encerra a empresa com ERRO (sem novas tentativas);
* erros temporários (rede, timeout, download) são repetidos por etapa, sem refazer o CAPTCHA
  — só uma sessão expirada exige novo login (e novo CAPTCHA manual);
* a falha de uma etapa não impede a outra: o resultado vira CONCLUIDO_COM_PENDENCIA ou ERRO
  conforme a gravidade.
"""
from __future__ import annotations

import logging
import re
import shutil
import time
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from ..authentication.login import LoginHandler
from ..browser.download_manager import DownloadManager
from ..browser.page_manager import PageManager
from ..config import portal_config as pc
from ..config.settings import Settings
from ..database.models import CompanyRow, CompanyRun, Portal, RunStatus, Stage, StepStatus
from ..database.repositories import ExecutionRepository
from ..dates import Period
from ..events import CompanyReporter, RunControl
from ..exceptions import (CancelledError, DownloadError, InvalidFileError,
                          NetworkError, NexFiscalError, SessionExpiredError,
                          StageUnavailableError, TemporaryError)
from ..fiscal_book.downloader import LivroFileStore
from ..fiscal_book.fiscal_book import FiscalBookHandler
from ..logs.logger import SECRETS
from ..nfse.xml_downloader import XmlDownloader, XmlStats
from ..nfse.xml_validator import XmlValidator
from ..storage.file_manager import detect_kind, read_head, move_atomic, validate_deliverable
from ..utils import sanitize_name
from ..storage.organizer import OutputLayout
from .portal_memory import PortalProfile
from .guided_action_recorder import GuidedActionRecorder
from .selector_engine import SelectorEngine

log = logging.getLogger("nexfiscal.workflow")
T = TypeVar("T")


@dataclass
class CompanyResult:
    status: RunStatus
    steps: dict[str, str] = field(default_factory=dict)
    xml: XmlStats = field(default_factory=XmlStats)
    livro_ok: bool = False
    message: str = ""
    retry_company: bool = False      # falha de infraestrutura: vale recomeçar com novo navegador


def friendly_error(exc: BaseException) -> str:
    if not isinstance(exc, (NexFiscalError, PlaywrightError, PermissionError)):
        # erro de programação: mantém o traceback (já filtrado de segredos) no log técnico
        log.error("Detalhe técnico do erro inesperado", exc_info=exc)
    if isinstance(exc, NexFiscalError):
        text = exc.message
    elif isinstance(exc, PlaywrightError):
        text = str(PageManager.classify(exc)).splitlines()[0]
    elif isinstance(exc, PermissionError):
        text = "Sem permissão para gravar arquivos na pasta de saída."
    else:
        text = f"Erro inesperado ({type(exc).__name__})."
    return SECRETS.redact(text)


class CompanyWorkflow:
    def __init__(self, *, settings: Settings, page: Page, profile: PortalProfile, portal: Portal,
                 row: CompanyRow, previous: CompanyRun, period: Period, layout: OutputLayout,
                 reporter: CompanyReporter, execs: ExecutionRepository, run_id: str,
                 control: RunControl | None = None) -> None:
        self.settings, self.page, self.portal, self.row = settings, page, portal, row
        self.profile = profile
        self.previous, self.period, self.layout = previous, period, layout
        self.reporter, self.execs, self.run_id, self.control = reporter, execs, run_id, control
        self.pm = PageManager(page, settings, control)
        self.action_recorder = GuidedActionRecorder(page) if settings.guided_learning else None
        if self.action_recorder is not None:
            self.action_recorder.start()
        self.engine = SelectorEngine(
            page, profile, timeout=settings.timeout_seconds, enable_ocr=settings.enable_ocr,
            assisted=settings.assisted_recognition and not settings.headless,
            assist_timeout=settings.assist_timeout_seconds,
            on_assist=lambda _spec, msg: reporter.assist(f"{row.display} ({row.cidade}): {msg}"),
            control=control, guided_learning=settings.guided_learning)
        self.dm = DownloadManager(settings.tmp_dir, settings.download_timeout_seconds)
        self.login_handler = LoginHandler(
            page, self.engine, self.pm, settings, reporter, control,
            guided_progress=self._has_guided_artifacts,
        )
        self.steps: dict[str, str] = dict(previous.steps)
        self.home_url = ""
        self.cnpj = row.display
        self._guided_download_dest: Path | None = None
        self._guided_download_context = None
        self._guided_download_pages: dict[int, Page] = {}
        self._guided_attach_page = None
        self._guided_download_events = 0
        self._guided_download_saved = 0
        self._guided_book_saved = 0
        self._guided_saved_xml_paths: list[Path] = []
        self._guided_saved_book_paths: list[Path] = []
        self._guided_download_failures = 0
        self._guided_baseline_files: dict[str, tuple[int, int]] = {}

    # --------------------------------------------------------------- helpers ---
    def _set(self, stage: Stage, status: StepStatus, msg: str = "") -> None:
        self.steps[stage.value] = status.value
        log.info("Etapa %s: %s | %s", stage.title, status.value, msg,
                 extra={"event": "stage_updated", "stage": stage.value, "status": status.value})
        self.reporter.stage(stage, status, msg)

    def _log(self, stage: Stage, status: str, msg: str = "", file: str = "", err: str = "") -> None:
        try:
            self.execs.log(self.run_id, self.cnpj, self.row.cidade, self.period.competence.label,
                           stage.value, status, SECRETS.redact(msg), file, SECRETS.redact(err))
        except Exception:  # pragma: no cover - log em banco nunca derruba o fluxo
            log.exception("Falha ao gravar histórico")

    def _error_block(self, stage: Stage, problem: str, action: str) -> None:
        log.error("ERRO | Empresa: %s | Cidade: %s | Etapa: %s | Problema: %s | Ação: %s",
                  self.cnpj, self.row.cidade, stage.title, problem, action)
        self.pm.screenshot_on_error(stage.value, self.settings.log_dir / "screenshots")

    def _has_guided_artifacts(self) -> bool:
        # Any verified fiscal file means the operator reached a page beyond login.
        # The manual guided stage below still waits for both required deliverables.
        return bool(self._guided_saved_xml_paths or self._guided_saved_book_paths)

    def _should_use_manual_guided_flow(self) -> bool:
        """Keep a first/unlearned fiscal route manual instead of searching/clicking automatically."""
        if not self.settings.guided_learning:
            return False
        if getattr(self.login_handler, "_manual_guided_login", False):
            return True
        return not (
            self.profile.has(pc.NFSE_XML.stage.value, pc.NFSE_XML.key)
            and self.profile.has(pc.LIVRO_DOWNLOAD.stage.value, pc.LIVRO_DOWNLOAD.key)
        )

    @staticmethod
    def _looks_like_guided_book(filename: str) -> bool:
        return bool(re.search(r"livro|fiscal|livro.?iss", filename, re.I))

    @staticmethod
    def _zip_contains_xml(path: Path) -> bool:
        try:
            with zipfile.ZipFile(path) as archive:
                return any(
                    not member.is_dir() and member.filename.lower().endswith(".xml")
                    for member in archive.infolist()
                )
        except (OSError, zipfile.BadZipFile):
            return False

    def _reconcile_guided_folder(self, dest: Path) -> None:
        """Reconcile all deliverables in the company folder, including files already present.

        Do not rely on download events or a changed timestamp: Chromium may open a
        fiscal PDF in a tab instead of emitting a download event, and a prior attempt
        may already have saved the file. This is safe because the XML and Livro Fiscal
        candidates are validated from their contents before the checklist is advanced.
        """
        if not dest.is_dir():
            return
        files = sorted((path for path in dest.iterdir() if path.is_file()),
                       key=lambda path: path.name.casefold())
        validator = XmlValidator()

        # Pass 1: establish the XML evidence first, so book classification does not
        # depend on filesystem enumeration order.
        for path in files:
            if path.suffix.lower() != ".xml":
                continue
            if validator.validate_file(path).valid and path not in self._guided_saved_xml_paths:
                self._guided_saved_xml_paths.append(path)

        # Pass 2: recognise a named Livro Fiscal or, once a valid XML exists, an
        # opaque-name PDF/Excel/CSV downloaded by a portal. ZIPs containing individual
        # XMLs must not be mistaken for the fiscal book.
        after_xml = bool(self._guided_saved_xml_paths)
        for path in files:
            if path.suffix.lower() == ".xml" or path in self._guided_saved_book_paths:
                continue
            try:
                ext = validate_deliverable(path, ("pdf", "xls", "xlsx", "csv", "zip"))
            except (InvalidFileError, OSError):
                continue
            xml_bundle = ext == "zip" and self._zip_contains_xml(path)
            looks_like_book = self._looks_like_guided_book(path.name)
            if looks_like_book or (after_xml and not xml_bundle):
                self._guided_saved_book_paths.append(path)

    def _report_guided_artifact_progress(self, dest: Path) -> bool:
        """Reflect verified files captured during manual learning in the live checklist."""
        self._reconcile_guided_folder(dest)
        validator = XmlValidator()
        xml_files = [
            p for p in self._guided_saved_xml_paths
            if p.is_file() and validator.validate_file(p).valid
        ]
        book_files: list[Path] = []
        for p in self._guided_saved_book_paths:
            try:
                validate_deliverable(p, ("pdf", "xls", "xlsx", "csv", "zip"))
                book_files.append(p)
            except (InvalidFileError, OSError):
                continue
        if xml_files:
            self.reporter.stage(Stage.XML, StepStatus.OK, f"{len(xml_files)} XML válido(s) na pasta")
        if book_files:
            self.reporter.stage(Stage.LIVRO, StepStatus.OK, "Livro Fiscal salvo e verificado")
        complete = bool(xml_files and book_files)
        if complete:
            self.reporter.stage(Stage.ORGANIZACAO, StepStatus.OK,
                                "XML e Livro Fiscal verificados na pasta da empresa")
        return complete

    def _write_guided_manifest(self, paths, out: CompanyResult, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
        OutputLayout.write_manifest(paths, {
            "empresa": self.cnpj, "razao_social": self.row.razao_social,
            "cidade": self.row.cidade, "competencia": self.period.competence.label,
            "periodo": f"{self.period.start_br} a {self.period.end_br}",
            "xml": {"encontrados": out.xml.found, "baixados": out.xml.downloaded,
                    "ja_existentes": out.xml.existing, "falhas": out.xml.failed},
            "livro_fiscal": out.livro_ok, "pendencias": problems,
        })

    def _finish_manual_guided_flow(self, paths, out: CompanyResult) -> CompanyResult:  # type: ignore[no-untyped-def]
        """Wait for operator-led downloads, then finish the company without autonomous fiscal clicks."""
        dest = paths.xml_dir
        timeout = max(self.settings.captcha_timeout_seconds, self.settings.assist_timeout_seconds)
        deadline = time.monotonic() + timeout
        complete = self._report_guided_artifact_progress(dest)
        if not self._guided_saved_book_paths:
            self._set(Stage.LIVRO, StepStatus.EM_ANDAMENTO, "Aguardando download manual do Livro Fiscal")
        while not complete and time.monotonic() < deadline:
            self.pm.checkpoint()
            try:
                # Playwright dispatches download events while this wait yields to the browser.
                self.page.wait_for_timeout(250)
            except PlaywrightError:
                log.warning("Página do portal fechada antes de concluir o aprendizado guiado.")
                break
            complete = self._report_guided_artifact_progress(dest)

        xml_files = [
            p for p in self._guided_saved_xml_paths
            if p.is_file() and XmlValidator().validate_file(p).valid
        ]
        book_files: list[Path] = []
        for path in self._guided_saved_book_paths:
            try:
                validate_deliverable(path, ("pdf", "xls", "xlsx", "csv", "zip"))
                book_files.append(path)
            except (InvalidFileError, OSError):
                continue
        out.xml = XmlStats(found=len(xml_files), downloaded=len(xml_files), files=list(xml_files))
        out.livro_ok = bool(book_files)
        problems: list[str] = []

        if xml_files:
            self._set(Stage.XML, StepStatus.OK, f"{len(xml_files)} XML válido(s) salvo(s)")
            self._log(Stage.XML, "OK", "XML do aprendizado guiado confirmado", file=str(xml_files[-1]))
        else:
            self._set(Stage.XML, StepStatus.PENDENCIA, "Nenhum XML validado foi capturado nesta execução")
            problems.append("XML não confirmado no destino configurado")
        if book_files:
            self._set(Stage.LIVRO, StepStatus.OK, "Livro Fiscal salvo e verificado")
            self._log(Stage.LIVRO, "OK", "Livro Fiscal do aprendizado guiado confirmado",
                      file=str(book_files[-1]))
        else:
            self._set(Stage.LIVRO, StepStatus.PENDENCIA,
                      "Download não reconhecido; confira o nome/formato do arquivo")
            problems.append("Livro Fiscal não confirmado no destino configurado")

        if not problems:
            self._set(Stage.ORGANIZACAO, StepStatus.OK, "Arquivos confirmados e manifesto salvo")
        else:
            self._set(Stage.ORGANIZACAO, StepStatus.PENDENCIA, "Finalização parcial; verifique os arquivos")
        try:
            self._write_guided_manifest(paths, out, problems)
        except Exception as exc:
            log.exception("Falha ao gravar manifesto do aprendizado guiado.")
            self._set(Stage.ORGANIZACAO, StepStatus.ERRO, friendly_error(exc))
            problems.append(f"Manifesto: {friendly_error(exc)}")

        if problems:
            out.status = RunStatus.CONCLUIDO_COM_PENDENCIA if xml_files or book_files else RunStatus.ERRO
            out.message = "; ".join(problems)
        else:
            out.status = RunStatus.CONCLUIDO
            out.message = "Concluído"
        return out

    def _record_guided_download_failure(self, filename: str, dest: Path, reason: str,
                                        exc: BaseException | None = None) -> None:
        """Make a failed manual download visible instead of silently continuing."""
        self._guided_download_failures += 1
        detail = SECRETS.redact(reason)
        extra = {"event": "guided_xml_download_failed", "company": self.cnpj,
                 "city": self.row.cidade, "download_name": filename,
                 "destination": str(dest), "failure_count": self._guided_download_failures}
        if exc is not None:
            log.error("Download guiado falhou | arquivo=%s | destino=%s | motivo=%s",
                      filename, dest, detail, exc_info=(type(exc), exc, exc.__traceback__), extra=extra)
        else:
            log.error("Download guiado falhou | arquivo=%s | destino=%s | motivo=%s",
                      filename, dest, detail, extra=extra)
        self._log(Stage.XML, "ERRO", f"Download guiado falhou: {detail}", file=str(dest / filename), err=detail)
        try:
            self.reporter.assist(
                f"[FALHA NO DOWNLOAD XML] {filename}: {detail} Destino esperado: {dest}. "
                "O download não foi confirmado; verifique o log e tente novamente."
            )
        except Exception:
            log.exception("Não foi possível informar a falha do download guiado à interface.", extra=extra)

    def _capture_guided_download(self, download) -> None:  # type: ignore[no-untyped-def]
        """Persist and verify manual downloads while the guided workflow is active."""
        dest = self._guided_download_dest
        if dest is None:
            log.warning("Evento de download recebido sem destino guiado ativo; evento descartado.")
            return
        self._guided_download_events += 1
        tmp = self.settings.tmp_dir / ("guided-" + uuid.uuid4().hex[:12])
        name = sanitize_name(download.suggested_filename or "download.xml") or "download.xml"
        extra = {"event": "guided_xml_download_received", "company": self.cnpj,
                 "city": self.row.cidade, "download_name": name, "destination": str(dest),
                 "event_number": self._guided_download_events}
        try:
            dest.mkdir(parents=True, exist_ok=True)
            tmp.mkdir(parents=True, exist_ok=True)
            source = tmp / name
            last_error: BaseException | None = None
            for attempt in range(1, 4):
                try:
                    download.save_as(str(source))
                    failure = download.failure()
                    if failure:
                        raise DownloadError(f"Chromium informou falha no download: {failure}")
                    last_error = None
                    break
                except (PlaywrightError, OSError) as exc:
                    last_error = exc
                    source.unlink(missing_ok=True)
                    if attempt == 3:
                        raise
                    delay = 0.15 * (2 ** (attempt - 1))
                    log.warning("Falha transitória ao copiar download guiado; tentativa %d/3 | arquivo=%s | erro=%s",
                                attempt, name, SECRETS.redact(str(exc)),
                                extra={**extra, "attempt": attempt, "delay_seconds": delay})
                    time.sleep(delay)
            if last_error is not None:
                raise last_error
            if not source.is_file() or source.stat().st_size <= 0:
                raise OSError("O evento de download terminou sem produzir um arquivo não vazio.")
            kind = detect_kind(read_head(source))
            suffix = Path(name).suffix.lower()
            book_named = self._looks_like_guided_book(name)
            book_extension = None
            if suffix in {".pdf", ".xls", ".xlsx", ".csv", ".zip"}:
                try:
                    book_extension = validate_deliverable(source, ("pdf", "xls", "xlsx", "csv", "zip"))
                except InvalidFileError:
                    book_extension = None
            # A generic PDF/Excel/CSV returned after an XML is also a book candidate:
            # many portals use opaque names such as "download.pdf" or "relatorio.xlsx".
            # Do not mistake a ZIP of individual XML invoices for the fiscal book.
            xml_bundle = kind == "zip" and book_extension != "xlsx" and self._zip_contains_xml(source)
            book_candidate = bool(
                book_extension and (
                    book_named or (self._guided_saved_xml_paths and not xml_bundle)
                )
            )
            if book_candidate:
                extension = book_extension
                target = dest / name
                if target.exists() and target.read_bytes() != source.read_bytes():
                    target = dest / f"{Path(name).stem}_{uuid.uuid4().hex[:8]}{Path(name).suffix}"
                move_atomic(source, target)
                validate_deliverable(target, ("pdf", "xls", "xlsx", "csv", "zip"))
                if target not in self._guided_saved_book_paths:
                    self._guided_saved_book_paths.append(target)
                self._guided_book_saved += 1
                self.reporter.stage(Stage.LIVRO, StepStatus.OK, f"Salvo: {target.name}")
                self._log(Stage.LIVRO, "OK", "Livro Fiscal salvo no aprendizado guiado", file=str(target))
                log.info("Livro Fiscal guiado salvo e verificado | destino=%s | bytes=%d | formato=%s",
                         target, target.stat().st_size, extension,
                         extra={**extra, "event": "guided_fiscal_book_saved",
                                "status": "saved", "bytes_written": target.stat().st_size,
                                "format": extension})
                complete = self._report_guided_artifact_progress(dest)
                if complete and self.control is not None:
                    mark_progress = getattr(self.control, "mark_guided_progress", None)
                    if callable(mark_progress):
                        mark_progress()
                return
            if kind == "zip":
                candidates = self.dm.extract_zip(source)
            elif kind == "xml":
                candidates = [source]
            else:
                raise InvalidFileError(f"O arquivo recebido não é XML nem ZIP com XML (tipo detectado: {kind}).")
            validator = XmlValidator()
            saved = 0
            invalid: list[str] = []
            xml_candidates = [p for p in candidates if p.suffix.lower() == ".xml"]
            if not xml_candidates:
                raise InvalidFileError("O arquivo ZIP/baixado não contém membros com extensão .xml.")
            for candidate in xml_candidates:
                info = validator.validate_file(candidate)
                if not info.valid:
                    invalid.append(f"{candidate.name}: {info.reason}")
                    log.warning("XML de download guiado recusado pela validação | arquivo=%s | motivo=%s",
                                candidate.name, info.reason, extra=extra)
                    continue
                # Preserve the portal's filename even when it is a consultation
                # response without individually issued invoices.
                base = candidate.name if candidate != source else name
                base = sanitize_name(base) or ("NFSe_" + info.sha256[:12] + ".xml")
                if not base.lower().endswith(".xml"):
                    base += ".xml"
                target = dest / base
                if target.exists():
                    if target.stat().st_size == candidate.stat().st_size and target.read_bytes() == candidate.read_bytes():
                        candidate.unlink(missing_ok=True)
                        saved += 1
                        log.info("Download guiado já existe e foi verificado | destino=%s | bytes=%d",
                                 target, target.stat().st_size,
                                 extra={**extra, "status": "duplicate_validated"})
                        if target not in self._guided_saved_xml_paths:
                            self._guided_saved_xml_paths.append(target)
                        self.reporter.stage(Stage.XML, StepStatus.OK, f"Já salvo: {target.name}")
                        complete = self._report_guided_artifact_progress(dest)
                        if complete and self.control is not None:
                            mark_progress = getattr(self.control, "mark_guided_progress", None)
                            if callable(mark_progress):
                                mark_progress()
                        continue
                    target = dest / (Path(base).stem + "_" + info.sha256[:8] + ".xml")
                move_atomic(candidate, target)
                if not target.is_file() or target.stat().st_size <= 0:
                    raise OSError("O arquivo de destino não passou na verificação pós-gravação.")
                saved_info = validator.validate_file(target)
                if not saved_info.valid or saved_info.sha256 != info.sha256:
                    raise InvalidFileError(f"O XML gravado difere do conteúdo validado: {target.name}.")
                saved += 1
                self._guided_download_saved += 1
                if target not in self._guided_saved_xml_paths:
                    self._guided_saved_xml_paths.append(target)
                self.reporter.stage(Stage.XML, StepStatus.OK, f"Salvo: {target.name}")
                self._log(Stage.XML, "OK", "XML salvo no aprendizado guiado", file=str(target))
                complete = self._report_guided_artifact_progress(dest)
                if complete and self.control is not None:
                    mark_progress = getattr(self.control, "mark_guided_progress", None)
                    if callable(mark_progress):
                        mark_progress()
                log.info("Download guiado salvo e verificado | destino=%s | bytes=%d | sha256=%s",
                         target, target.stat().st_size, info.sha256,
                         extra={**extra, "status": "saved", "sha256": info.sha256,
                                "bytes_written": target.stat().st_size})
                self._log(Stage.XML, "OK", "Download guiado salvo e verificado", file=str(target))
            if saved == 0:
                reason = "Nenhum XML válido foi salvo. " + ("; ".join(invalid) if invalid else "O arquivo não continha XML válido.")
                self._record_guided_download_failure(name, dest, reason)
            elif invalid:
                self._record_guided_download_failure(name, dest,
                    "Download parcial: alguns XMLs foram rejeitados: " + "; ".join(invalid))
        except (PlaywrightError, OSError, zipfile.BadZipFile, InvalidFileError, NexFiscalError) as exc:
            self._record_guided_download_failure(name, dest, str(exc), exc)
        except Exception as exc:
            # Unexpected programming errors are intentionally visible with a traceback.
            self._record_guided_download_failure(name, dest,
                f"Erro inesperado ({type(exc).__name__}): {exc}", exc)
        finally:
            try:
                shutil.rmtree(tmp)
            except FileNotFoundError:
                pass
            except OSError:
                log.warning("Falha ao limpar diretório temporário do download guiado: %s", tmp, exc_info=True)

    def _start_guided_download_capture(self, dest: Path) -> None:
        """Observe operator downloads on every page/popup during guided learning."""
        if not self.settings.guided_learning:
            return
        dest.mkdir(parents=True, exist_ok=True)
        self._guided_baseline_files = {}
        for existing in dest.iterdir():
            try:
                if existing.is_file():
                    stat = existing.stat()
                    self._guided_baseline_files[existing.name] = (stat.st_size, stat.st_mtime_ns)
            except OSError:
                continue
        self._guided_download_dest = dest
        context = self.page.context
        self._guided_download_context = context
        self._guided_download_pages = {}

        def attach(page: Page) -> None:
            key = id(page)
            if key in self._guided_download_pages:
                return
            self._guided_download_pages[key] = page
            page.on("download", self._capture_guided_download)

        for page in context.pages:
            attach(page)
        context.on("page", attach)
        self._guided_attach_page = attach
        log.info("Captura de downloads guiados ativada | destino=%s | paginas_monitoradas=%d",
                 dest, len(self._guided_download_pages),
                 extra={"event": "guided_download_capture_started", "destination": str(dest),
                        "guided_learning": True})

    def _stop_guided_download_capture(self) -> None:
        context = self._guided_download_context
        if context is None:
            return
        try:
            if self._guided_attach_page is not None:
                context.remove_listener("page", self._guided_attach_page)
        except PlaywrightError:
            log.warning("Não foi possível remover o listener de novas páginas do download guiado.", exc_info=True)
        for page in self._guided_download_pages.values():
            try:
                page.remove_listener("download", self._capture_guided_download)
            except PlaywrightError:
                log.debug("Página já encerrada durante limpeza do listener de download.")
        log.info("Captura de downloads guiados encerrada | eventos=%d | arquivos_salvos=%d | falhas=%d",
                 self._guided_download_events, self._guided_download_saved + self._guided_book_saved,
                 self._guided_download_failures,
                 extra={"event": "guided_download_capture_stopped", "download_events": self._guided_download_events,
                        "files_saved": self._guided_download_saved, "failures": self._guided_download_failures})
        self._guided_download_context = None
        self._guided_download_dest = None
        self._guided_download_pages = {}
        self._guided_attach_page = None

    def _goto_home(self) -> None:
        if self.home_url and self.page.url != self.home_url:
            self.pm.goto(self.home_url)
            if self.login_handler.looks_logged_out():
                self._relogin()

    def _login(self) -> None:
        pwd = self.row.password
        SECRETS.register(pwd)
        self.home_url = self.login_handler.login(self.portal.url, self.row.cnpj_usuario, pwd,
                                                 self.cnpj, self.row.cidade)

    def _relogin(self) -> None:
        log.warning("Sessão expirada — autenticando novamente (pode exigir novo CAPTCHA).")
        self._login()

    def _attempt(self, label: str, func: Callable[[], T]) -> T:
        """Executa a etapa com novas tentativas para erros temporários."""
        attempts = self.settings.max_retries
        for n in range(1, attempts + 1):
            try:
                return func()
            except (CancelledError, StageUnavailableError):
                raise
            except SessionExpiredError:
                if n == attempts:
                    raise
                self._relogin()
            except PlaywrightError as exc:
                converted = PageManager.classify(exc)
                if "has been closed" in str(exc) or "Target closed" in str(exc):
                    raise NetworkError("O navegador foi fechado durante o processamento.") from exc
                if not isinstance(converted, TemporaryError) or n == attempts:
                    raise converted if isinstance(converted, NexFiscalError) else NexFiscalError(
                        friendly_error(exc)) from exc
                self._backoff(label, n, friendly_error(exc))
            except TemporaryError as exc:
                if n == attempts:
                    raise
                self._backoff(label, n, exc.message)
        raise AssertionError("unreachable")  # pragma: no cover

    def _backoff(self, label: str, n: int, why: str) -> None:
        log.warning("%s: tentativa %d/%d falhou (%s). Nova tentativa em instantes.",
                    label, n, self.settings.max_retries, why)
        if self.control:
            self.control.wait(min(2 ** n, 10))
        else:
            time.sleep(min(2 ** n, 10))
        try:
            self._goto_home()
        except NexFiscalError:
            log.warning("Não foi possível recuperar a página inicial antes da nova tentativa.", exc_info=True)

    # ----------------------------------------------------------------- etapas ---
    def _xml_flow(self, paths_xml: Path, out: CompanyResult) -> None:
        """Segue diretamente para a coleta de XMLs após o login/CAPTCHA."""
        self._set(Stage.XML, StepStatus.EM_ANDAMENTO, "Baixando XMLs")
        stats = XmlDownloader(self.page, self.engine, self.pm, self.dm, self.settings,
                              self.reporter).run(paths_xml)
        out.xml = stats
        for f in stats.files:
            self._log(Stage.XML, "OK", "XML salvo", str(f))
        if stats.failed:
            self._set(Stage.XML, StepStatus.PENDENCIA, f"{stats.summary} (falhas: {stats.failed})")
            self._log(Stage.XML, "PENDENCIA", f"{stats.failed} falha(s)", err="; ".join(stats.failures[:5]))
        else:
            self._set(Stage.XML, StepStatus.OK, stats.summary)

    def execute(self) -> CompanyResult:
        """Executa o fluxo e persiste observações guiadas mesmo em falha/cancelamento."""
        try:
            return self._execute()
        finally:
            self._stop_guided_download_capture()
            self._persist_guided_actions()

    def _persist_guided_actions(self) -> None:
        if self.action_recorder is None:
            return
        try:
            events = [
                GuidedActionRecorder._validate(event)
                for event in self.action_recorder.drain()
            ]
            events = [event for event in events if event["url"].startswith(("http://", "https://"))]
        except Exception:
            log.warning("Não foi possível coletar eventos guiados; a execução segue sem gravar esta sessão.", exc_info=True)
            return
        if not events:
            return
        grouped: dict[str, list[dict]] = {}
        for event in events:
            url = event.get("url", "")
            name = GuidedActionRecorder.flow_name(url)
            if name:
                grouped.setdefault(name, []).append(event)
        for name, route_events in grouped.items():
            # Cada rota guarda a última sessão observada, evitando crescimento ilimitado
            # e mantendo as demais rotas já aprendidas para o mesmo portal.
            normalized = []
            for order, event in enumerate(route_events, start=1):
                normalized.append(dict(event, order=order))
            self.profile.set_flow(name, GuidedActionRecorder.serialize(normalized))

    def _execute(self) -> CompanyResult:
        out = CompanyResult(RunStatus.EM_EXECUCAO, steps=self.steps)
        try:
            paths = self.layout.paths(self.row.cidade, self.row.razao_social, self.period.competence)
        except NexFiscalError as exc:
            return self._fail(out, Stage.ORGANIZACAO, exc, "Verifique as permissões da pasta de saída.")

        need_xml = self.previous.step(Stage.XML) is not StepStatus.OK
        need_livro = self.previous.step(Stage.LIVRO) is not StepStatus.OK
        xml_state = "skip" if not need_xml else "ok"
        livro_state = "skip" if not need_livro else "ok"
        problems: list[str] = []

        # ------------------------------------------------------------ login ---
        try:
            self._start_guided_download_capture(paths.xml_dir)
            self._attempt("Login", self._login)
            self._log(Stage.LOGIN, "OK", "Login realizado")
        except CancelledError:
            raise
        except Exception as exc:
            infra = isinstance(exc, NetworkError)
            res = self._fail(out, Stage.LOGIN, exc, "Empresa marcada com erro." if not infra
                             else "Nova tentativa com navegador limpo.")
            res.retry_company = infra
            return res

        # In a first/unlearned fiscal route, the operator controls portal actions.
        # Wait for both verified deliverables and do not run automated selector searches.
        if self._should_use_manual_guided_flow():
            return self._finish_manual_guided_flow(paths, out)

        # Um download fiscal manual validado pode ter concluído etapas enquanto o
        # operador estava no CAPTCHA. Não refaça essas etapas nem deixe o checklist
        # em PENDENTE só porque a captura ocorreu fora do fluxo automático.
        if self._guided_saved_xml_paths:
            need_xml = False
        if self._guided_saved_book_paths:
            need_livro = False
        xml_state = "skip" if not need_xml else "ok"
        livro_state = "skip" if not need_livro else "ok"

        # --------------------------------------------------------- XML ---
        if need_xml:
            try:
                self._attempt("XML", lambda: self._xml_flow(paths.xml_dir, out))
                if out.xml.failed:
                    xml_state = "partial"
                    problems.append(f"{out.xml.failed} XML(s) com falha")
            except CancelledError:
                raise
            except Exception as exc:
                stage = Stage.XML
                self._set(stage, StepStatus.ERRO, friendly_error(exc))
                self._log(stage, "ERRO", err=friendly_error(exc))
                self._error_block(stage, friendly_error(exc), "Etapa marcada para nova tentativa.")
                xml_state = "fail"
                problems.append(f"{stage.title}: {friendly_error(exc)}")
        else:
            if self._guided_saved_xml_paths:
                out.xml = XmlStats(found=len(self._guided_saved_xml_paths),
                                   downloaded=len(self._guided_saved_xml_paths),
                                   files=list(self._guided_saved_xml_paths))
                self._set(Stage.XML, StepStatus.OK,
                          f"{len(self._guided_saved_xml_paths)} XML(s) no aprendizado guiado")
            else:
                out.xml = XmlStats()
                self._set(Stage.XML, StepStatus.PULADO, "Já concluído")

        # ---------------------------------------------------- Livro Fiscal ---
        if need_livro:
            try:
                self._goto_home()
                self._set(Stage.LIVRO, StepStatus.EM_ANDAMENTO, "Acessando Livro Fiscal")
                store = LivroFileStore(paths.livro_dir, self.row.cnpj_usuario, self.period.competence,
                                       self.settings.existing_files)
                handler = FiscalBookHandler(self.page, self.engine, self.pm, self.dm, self.settings)
                file, existed = self._attempt("Livro Fiscal", lambda: handler.run(self.period, store))
                out.livro_ok = True
                self._set(Stage.LIVRO, StepStatus.OK, "Já existente" if existed else "Baixado")
                self._log(Stage.LIVRO, "OK", "Livro Fiscal", str(file))
            except CancelledError:
                raise
            except StageUnavailableError as exc:
                livro_state = "unavailable"
                self._set(Stage.LIVRO, StepStatus.PENDENCIA, exc.message)
                self._log(Stage.LIVRO, "PENDENCIA", err=exc.message)
                self._error_block(Stage.LIVRO, exc.message, "Empresa concluída com pendência.")
                problems.append(f"Livro Fiscal: {exc.message}")
            except Exception as exc:
                livro_state = "fail"
                self._set(Stage.LIVRO, StepStatus.ERRO, friendly_error(exc))
                self._log(Stage.LIVRO, "ERRO", err=friendly_error(exc))
                self._error_block(Stage.LIVRO, friendly_error(exc), "Empresa marcada para nova tentativa.")
                problems.append(f"Livro Fiscal: {friendly_error(exc)}")
        else:
            out.livro_ok = True
            if self._guided_saved_book_paths:
                self._set(Stage.LIVRO, StepStatus.OK, "Baixado no aprendizado guiado")
                self._log(Stage.LIVRO, "OK", "Livro Fiscal salvo no aprendizado guiado",
                          str(self._guided_saved_book_paths[-1]))
            else:
                self._set(Stage.LIVRO, StepStatus.PULADO, "Já concluído")

        # ------------------------------------------------------ organização ---
        try:
            OutputLayout.write_manifest(paths, {
                "empresa": self.cnpj, "razao_social": self.row.razao_social, "cidade": self.row.cidade,
                "competencia": self.period.competence.label,
                "periodo": f"{self.period.start_br} a {self.period.end_br}",
                "xml": {"encontrados": out.xml.found, "baixados": out.xml.downloaded,
                        "ja_existentes": out.xml.existing, "falhas": out.xml.failed},
                "livro_fiscal": out.livro_ok, "pendencias": problems,
            })
            self._set(Stage.ORGANIZACAO, StepStatus.OK)
        except Exception as exc:  # pragma: no cover
            log.exception("Falha ao gravar manifesto da empresa.")
            self._set(Stage.ORGANIZACAO, StepStatus.ERRO, friendly_error(exc))
            problems.append(f"Organização: {friendly_error(exc)}")

        # --------------------------------------------------------- resultado ---
        if xml_state == "fail" and livro_state in ("fail", "unavailable"):
            out.status = RunStatus.ERRO
        elif problems or xml_state in ("fail", "partial") or livro_state in ("fail", "unavailable"):
            out.status = RunStatus.CONCLUIDO_COM_PENDENCIA
        else:
            out.status = RunStatus.CONCLUIDO
        out.message = "; ".join(problems) if problems else "Concluído"
        return out

    def _fail(self, out: CompanyResult, stage: Stage, exc: BaseException, action: str) -> CompanyResult:
        text = friendly_error(exc)
        self._set(stage, StepStatus.ERRO, text)
        self._log(stage, "ERRO", err=text)
        self._error_block(stage, text, action)
        out.status = RunStatus.ERRO
        out.message = f"{stage.title}: {text}"
        return out
