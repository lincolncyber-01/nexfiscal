"""Orquestração do lote: cidades -> portais -> empresas, com retomada, retentativas e relatório."""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

from .browser.browser_manager import BrowserManager
from .config.settings import Settings
from .database.database import Database
from .database.models import CompanyRow, CompanyRun, RunStatus, Stage
from .database.repositories import CompanyRepository, ExecutionRepository
from .dates import Period
from .events import CompanyReporter, EventBus, EventKind, ProgressEvent, RunControl
from .exceptions import CancelledError, ConfigurationError, NexFiscalError
from .logs.logger import SECRETS, log_context
from .memory.portal_memory_manager import PortalMemoryManager
from .portal.portal_registry import PortalRegistry
from .portal.workflow_engine import CompanyResult, CompanyWorkflow, friendly_error
from .reports.report_generator import Report, ReportGenerator
from .security import CredentialCipher
from .storage.organizer import OutputLayout

log = logging.getLogger("nexfiscal.batch")


@dataclass
class BatchSummary:
    report: Report
    cancelled: bool = False
    keys: list[tuple[str, str]] = field(default_factory=list)


class BatchRunner:
    def __init__(self, settings: Settings, db: Database, cipher: CredentialCipher,
                 bus: EventBus | None = None, control: RunControl | None = None) -> None:
        self.settings, self.db, self.cipher = settings, db, cipher
        self.bus = bus or EventBus()
        self.control = control or RunControl()
        self.registry = PortalRegistry(db)
        self.memory = PortalMemoryManager(db)
        self.companies = CompanyRepository(db, cipher)
        self.execs = ExecutionRepository(db)

    # ------------------------------------------------------------------ API ---
    def run(self, rows: list[CompanyRow], period: Period, *, cities: set[str] | None = None,
            force: bool = False) -> BatchSummary:
        run_id = uuid.uuid4().hex[:8]
        with log_context(run_id=run_id, competence=period.competence.label):
            try:
                return self._run(rows, period, cities=cities, force=force, run_id=run_id)
            except Exception as exc:
                # Diagnose while secret values are still registered; expose only a
                # redacted domain error after this scope releases the registry.
                log.exception("Falha ao executar lote.")
                raise NexFiscalError(friendly_error(exc)) from None
            finally:
                # Also cover failures before browser startup (e.g. credential/database I/O).
                SECRETS.clear()

    def _run(self, rows: list[CompanyRow], period: Period, *, cities: set[str] | None,
             force: bool, run_id: str) -> BatchSummary:
        """Processa as empresas. ``cities`` = nomes normalizados a processar (None = todas).

        Empresas já CONCLUÍDAS na competência são puladas, a menos que ``force`` seja True —
        é isso que permite retomar um lote interrompido.
        """
        comp = period.competence.label
        selected = [r for r in rows if not cities or r.cidade_norm in cities]
        if any(not r.razao_social.strip() for r in selected):
            raise ConfigurationError(
                "Razão social ausente. Reimporte a planilha com a coluna 'Razão Social' antes de processar."
            )
        for r in selected:
            SECRETS.register(r.password)
        self.companies.upsert_rows(selected)
        self.execs.reset_interrupted()
        keys = [(r.usuario_key, r.cidade_norm) for r in selected]
        for r in selected:  # todas aparecem no relatório, inclusive as ainda não iniciadas (PENDENTE)
            self.execs.get_or_create(comp, r)
        layout = OutputLayout(self.settings.output_root)

        groups: dict[str, list[CompanyRow]] = {}
        for r in selected:
            groups.setdefault(r.cidade_norm, []).append(r)
        total = len(selected)
        log.info("Iniciando processamento.")
        log.info("%d empresas encontradas.", total)
        log.info("%d cidades identificadas.", len(groups))
        done = 0
        cancelled = False
        browser: BrowserManager | None = None
        try:
            for city_idx, (cnorm, city_rows) in enumerate(groups.items(), start=1):
                city = city_rows[0].cidade
                self._emit(EventKind.CITY_START, city=city, current=city_idx, total=len(groups))
                portal = self.registry.resolve(city)
                if portal is None:
                    for r in city_rows:
                        run = self.execs.get_or_create(comp, r)
                        run.status, run.message = RunStatus.ERRO, "Portal não cadastrado para a cidade."
                        self.execs.update(run)
                        self.companies.set_status(r.usuario_key, r.cidade_norm, run.status)
                        done += 1
                        self._emit(
                            EventKind.COMPANY_END, city=r.cidade, company=r.display,
                            competence=comp, status=run.status.value, message=run.message,
                            current=done, total=total,
                        )
                    log.error("Portal não cadastrado para %s: empresas marcadas com erro.", city)
                    continue
                log.info("Carregando portal de %s.", city)
                profile = self.memory.load_profile(portal)
                log.info("Memória do portal %s.", "encontrada" if profile.selectors else "não encontrada (reconhecimento)")
                try:
                    if browser is None:
                        browser = BrowserManager(self.settings).start()
                    for row in city_rows:
                        self.control.checkpoint()
                        done += 1
                        run = self.execs.get_or_create(comp, row)
                        if run.status is RunStatus.CONCLUIDO and not force:
                            log.info("Empresa %s já concluída — pulada.", row.display)
                            self._emit(
                                EventKind.COMPANY_END, city=row.cidade, company=row.display,
                                competence=comp, status=run.status.value,
                                message="Empresa já concluída nesta competência — ignorada.",
                                current=done, total=total,
                            )
                            continue
                        if force:  # reprocessar do zero: esquece etapas e contadores anteriores
                            run.steps, run.livro_ok = {}, False
                            run.xml_found = run.xml_downloaded = run.xml_existing = run.xml_failed = 0
                        with log_context(company=row.display, city=row.cidade):
                            self._process_company(browser, row, run, profile, portal, period, layout,
                                                  run_id, done, total, city_idx, len(groups))
                        self.memory.save_profile(profile)
                finally:
                    self.memory.save_profile(profile)
                self._emit(EventKind.CITY_END, city=city, current=city_idx, total=len(groups))
        except CancelledError:
            cancelled = True
            log.warning("Processamento cancelado. O que foi concluído está salvo; use Retomar.")
        finally:
            if browser:
                browser.stop()
        report = ReportGenerator(self.execs).build(comp, keys)
        self._emit(EventKind.BATCH_END, competence=comp, message=report.to_text())
        return BatchSummary(report, cancelled, keys)

    # -------------------------------------------------------------- empresa ---
    def _process_company(self, browser: BrowserManager, row: CompanyRow, run: CompanyRun, profile, portal,
                         period: Period, layout: OutputLayout, run_id: str, idx: int, total: int,
                         city_idx: int, city_total: int) -> None:
        comp = period.competence.label
        reporter = CompanyReporter(self.bus, row.cidade, row.display, comp)
        self.bus.emit(ProgressEvent(EventKind.COMPANY_START, row.cidade, row.display, comp,
                                    current=idx, total=total,
                                    data={"city_index": city_idx, "city_total": city_total}))
        log.info("Empresa %d/%d: %s (%s).", idx, total, row.display, row.cidade)
        result: CompanyResult | None = None
        for attempt in range(1, self.settings.max_retries + 1):
            run.status, run.attempts = RunStatus.EM_EXECUCAO, run.attempts + 1
            self.execs.update(run)
            ctx = None
            try:
                ctx, page = browser.new_page()
                wf = CompanyWorkflow(settings=self.settings, page=page, profile=profile, portal=portal,
                                     row=row, previous=run, period=period, layout=layout,
                                     reporter=reporter, execs=self.execs, run_id=run_id,
                                     control=self.control)
                result = wf.execute()
            except CancelledError:
                run.status, run.message = RunStatus.PENDENTE, "Cancelado pelo usuário"
                self.execs.update(run)
                raise
            except Exception as exc:  # falha inesperada isolada nesta empresa
                log.exception("Falha inesperada na empresa %s", row.display)
                result = CompanyResult(RunStatus.ERRO, message=friendly_error(exc),
                                       retry_company=not isinstance(exc, NexFiscalError))
            finally:
                if ctx is not None:
                    try:
                        ctx.close()
                    except Exception:
                        log.warning("Falha ao fechar contexto da empresa.", exc_info=True)
            self._apply(run, result)
            if not result.retry_company or attempt == self.settings.max_retries:
                break
            log.warning("Falha de infraestrutura; nova tentativa %d/%d com navegador limpo.",
                        attempt + 1, self.settings.max_retries)
            self.control.wait(min(2 ** attempt, 10))
        assert result is not None
        self.companies.set_status(row.usuario_key, row.cidade_norm, result.status)
        self._log_summary(row, comp, result)
        self.bus.emit(ProgressEvent(EventKind.COMPANY_END, row.cidade, row.display, comp,
                                    status=result.status.value, message=result.message,
                                    current=idx, total=total))

    def _apply(self, run: CompanyRun, res: CompanyResult) -> None:
        run.status, run.steps, run.message = res.status, dict(res.steps), res.message
        run.etapa = None
        # contadores acumulam entre tentativas parciais (a retomada não zera o que já foi baixado)
        run.xml_found = max(run.xml_found, res.xml.found)
        run.xml_downloaded += res.xml.downloaded
        run.xml_existing = max(run.xml_existing, res.xml.existing)
        run.xml_failed = res.xml.failed
        run.livro_ok = run.livro_ok or res.livro_ok
        self.execs.update(run)

    @staticmethod
    def _log_summary(row: CompanyRow, comp: str, res: CompanyResult) -> None:
        s = res.steps
        log.info("Empresa: %s | Cidade: %s | Competência: %s | Login: %s | CAPTCHA: %s | NFS-e: %s | "
                 "XML: %s | Livro Fiscal: %s | STATUS: %s", row.display, row.cidade, comp,
                 s.get(Stage.LOGIN.value, "-"), s.get(Stage.CAPTCHA.value, "-"), s.get(Stage.NFSE.value, "-"),
                 f"{res.xml.downloaded + res.xml.existing}/{res.xml.found}",
                 s.get(Stage.LIVRO.value, "-"), res.status.value)

    def _emit(self, kind: EventKind, **kw) -> None:  # type: ignore[no-untyped-def]
        self.bus.emit(ProgressEvent(kind, **kw))

    # ---------------------------------------------------------- utilitários ---
    def retry_failed_rows(self, period: Period, rows: list[CompanyRow]) -> list[CompanyRow]:
        """Empresas ERRO/PENDÊNCIA/PENDENTE na competência (para 'tentar novamente')."""
        comp = period.competence.label
        pending = {(r.usuario_key, r.cidade_norm) for r in self.execs.list_for(comp)
                   if r.status is not RunStatus.CONCLUIDO}
        return [r for r in rows if (r.usuario_key, r.cidade_norm) in pending]
