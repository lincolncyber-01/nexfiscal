"""Fluxo completo de UMA empresa: login -> NFS-e -> XML -> Livro Fiscal -> organização.

Regras de falha:
* erro de login/credenciais encerra a empresa com ERRO (sem novas tentativas);
* erros temporários (rede, timeout, download) são repetidos por etapa, sem refazer o CAPTCHA
  — só uma sessão expirada exige novo login (e novo CAPTCHA manual);
* a falha de uma etapa não impede a outra: o resultado vira CONCLUIDO_COM_PENDENCIA ou ERRO
  conforme a gravidade.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from ..authentication.login import LoginHandler
from ..browser.download_manager import DownloadManager
from ..browser.page_manager import PageManager
from ..config.settings import Settings
from ..database.models import CompanyRow, CompanyRun, Portal, RunStatus, Stage, StepStatus
from ..database.repositories import ExecutionRepository
from ..dates import Period
from ..events import CompanyReporter, RunControl
from ..exceptions import (CancelledError, ConfigurationError, NetworkError, NexFiscalError,
                          SessionExpiredError, StageUnavailableError, TemporaryError)
from ..fiscal_book.downloader import LivroFileStore
from ..fiscal_book.fiscal_book import FiscalBookHandler
from ..logs.logger import SECRETS
from ..nfse.nfse_consult import NfseConsult
from ..nfse.xml_downloader import XmlDownloader, XmlStats
from ..storage.organizer import OutputLayout
from .portal_memory import PortalProfile
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
        self.previous, self.period, self.layout = previous, period, layout
        self.reporter, self.execs, self.run_id, self.control = reporter, execs, run_id, control
        self.pm = PageManager(page, settings, control)
        self.engine = SelectorEngine(
            page, profile, timeout=settings.timeout_seconds, enable_ocr=settings.enable_ocr,
            assisted=settings.assisted_recognition and not settings.headless,
            assist_timeout=settings.assist_timeout_seconds,
            on_assist=lambda _spec, msg: reporter.assist(f"{row.display} ({row.cidade}): {msg}"),
            control=control, guided_learning=settings.guided_learning)
        self.dm = DownloadManager(settings.tmp_dir, settings.download_timeout_seconds)
        self.login_handler = LoginHandler(page, self.engine, self.pm, settings, reporter, control)
        self.steps: dict[str, str] = dict(previous.steps)
        self.home_url = ""
        self.cnpj = row.display

    # --------------------------------------------------------------- helpers ---
    def _set(self, stage: Stage, status: StepStatus, msg: str = "") -> None:
        self.steps[stage.value] = status.value
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
        time.sleep(min(2 ** n, 10))
        try:
            self._goto_home()
        except NexFiscalError:
            pass

    # ----------------------------------------------------------------- etapas ---
    def _xml_flow(self, paths_xml: Path, out: CompanyResult) -> None:
        self._set(Stage.NFSE, StepStatus.EM_ANDAMENTO, "Acessando consulta de NFS-e")
        outcome = NfseConsult(self.engine, self.pm, self.settings.timeout_seconds).search(self.period)
        self._set(Stage.NFSE, StepStatus.OK)
        self._log(Stage.NFSE, "OK", "Consulta executada" + (" (sem notas)" if outcome.empty else ""))
        if outcome.empty:
            self._set(Stage.XML, StepStatus.OK, "Sem notas no período")
            self._log(Stage.XML, "OK", "Nenhuma NFS-e no período")
            return
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
        out = CompanyResult(RunStatus.EM_EXECUCAO, steps=self.steps)
        try:
            paths = self.layout.paths(self.row.cidade, self.row.cnpj_usuario, self.period.competence)
        except NexFiscalError as exc:
            return self._fail(out, Stage.ORGANIZACAO, exc, "Verifique as permissões da pasta de saída.")

        need_xml = self.previous.step(Stage.XML) is not StepStatus.OK
        need_livro = self.previous.step(Stage.LIVRO) is not StepStatus.OK
        xml_state = "skip" if not need_xml else "ok"
        livro_state = "skip" if not need_livro else "ok"
        problems: list[str] = []

        # ------------------------------------------------------------ login ---
        try:
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

        # --------------------------------------------------- NFS-e + XML ---
        if need_xml:
            try:
                self._attempt("NFS-e/XML", lambda: self._xml_flow(paths.xml_dir, out))
                if out.xml.failed:
                    xml_state = "partial"
                    problems.append(f"{out.xml.failed} XML(s) com falha")
            except CancelledError:
                raise
            except Exception as exc:
                stage = Stage.XML if self.steps.get(Stage.NFSE.value) == StepStatus.OK.value else Stage.NFSE
                self._set(stage, StepStatus.ERRO, friendly_error(exc))
                self._log(stage, "ERRO", err=friendly_error(exc))
                self._error_block(stage, friendly_error(exc), "Etapa marcada para nova tentativa.")
                xml_state = "fail"
                problems.append(f"{stage.title}: {friendly_error(exc)}")
        else:
            self._set(Stage.NFSE, StepStatus.PULADO, "Já concluído")
            out.xml = XmlStats()

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
            self._set(Stage.LIVRO, StepStatus.PULADO, "Já concluído")

        # ------------------------------------------------------ organização ---
        try:
            OutputLayout.write_manifest(paths, {
                "empresa": self.cnpj, "cidade": self.row.cidade,
                "competencia": self.period.competence.label,
                "periodo": f"{self.period.start_br} a {self.period.end_br}",
                "xml": {"encontrados": out.xml.found, "baixados": out.xml.downloaded,
                        "ja_existentes": out.xml.existing, "falhas": out.xml.failed},
                "livro_fiscal": out.livro_ok, "pendencias": problems,
            })
            self._set(Stage.ORGANIZACAO, StepStatus.OK)
        except Exception as exc:  # pragma: no cover
            self._set(Stage.ORGANIZACAO, StepStatus.ERRO, friendly_error(exc))

        # --------------------------------------------------------- resultado ---
        if xml_state == "fail" and livro_state in ("fail", "unavailable"):
            out.status = RunStatus.ERRO
        elif xml_state in ("fail", "partial") or livro_state in ("fail", "unavailable"):
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
