from __future__ import annotations

from types import SimpleNamespace

from nexfiscal import controller as controller_module
from nexfiscal.controller import BatchRunner
from nexfiscal.database.models import CompanyRow, RunStatus
from nexfiscal.events import EventBus, EventKind, RunControl


class _CompaniesStub:
    def __init__(self):
        self.statuses = []

    def upsert_rows(self, _rows):
        pass

    def set_status(self, usuario_key, cidade_norm, status):
        self.statuses.append((usuario_key, cidade_norm, status))


class _ExecutionsStub:
    def __init__(self, run):
        self.run = run
        self.updates = []

    def reset_interrupted(self):
        pass

    def get_or_create(self, _competence, _row):
        return self.run

    def update(self, run):
        self.updates.append((run.status, run.message))

    def list_for(self, _competence, _keys=None):
        return []


def _runner(tmp_path, monkeypatch, portal, initial_status=RunStatus.PENDENTE):
    row = CompanyRow(
        cnpj_usuario="12345678901",
        razao_social="Empresa Teste",
        senha="senha-teste",
        cidade="Cidade Teste",
    )
    run = SimpleNamespace(status=initial_status, message=None, steps={}, livro_ok=False,
                          xml_found=0, xml_downloaded=0, xml_existing=0, xml_failed=0,
                          attempts=0, etapa=None)
    runner = BatchRunner.__new__(BatchRunner)
    runner.settings = SimpleNamespace(output_root=str(tmp_path))
    runner.db = object()
    runner.cipher = object()
    runner.bus = EventBus()
    runner.events = []
    runner.bus.subscribe(runner.events.append)
    runner.control = RunControl()
    runner.registry = SimpleNamespace(resolve=lambda _city: portal)
    profile = SimpleNamespace(selectors={})
    runner.memory = SimpleNamespace(load_profile=lambda _portal: profile,
                                    save_profile=lambda _profile: None)
    runner.companies = _CompaniesStub()
    runner.execs = _ExecutionsStub(run)
    report = SimpleNamespace(to_text=lambda: "relatório")
    monkeypatch.setattr(controller_module, "ReportGenerator",
                        lambda _execs: SimpleNamespace(build=lambda _comp, _keys: report))
    return runner, row, run


def test_missing_portal_emits_company_end_and_persists_company_status(tmp_path, monkeypatch):
    runner, row, run = _runner(tmp_path, monkeypatch, portal=None)

    runner.run([row], SimpleNamespace(competence=SimpleNamespace(label="09/2026")))

    event = next(e for e in runner.events if e.kind is EventKind.COMPANY_END)
    assert event.status == RunStatus.ERRO.value
    assert event.message == "Portal não cadastrado para a cidade."
    assert event.current == event.total == 1
    assert run.status is RunStatus.ERRO
    assert runner.companies.statuses == [(row.usuario_key, row.cidade_norm, RunStatus.ERRO)]


def test_completed_company_skip_emits_progress_event(tmp_path, monkeypatch):
    runner, row, run = _runner(tmp_path, monkeypatch, portal=SimpleNamespace(url="https://portal.example"),
                               initial_status=RunStatus.CONCLUIDO)

    class FakeBrowser:
        stopped = False
        def start(self):
            return self
        def stop(self):
            self.stopped = True

    browser = FakeBrowser()
    monkeypatch.setattr(controller_module, "BrowserManager", lambda _settings: browser)

    runner.run([row], SimpleNamespace(competence=SimpleNamespace(label="09/2026")))

    event = next(e for e in runner.events if e.kind is EventKind.COMPANY_END)
    assert event.status == RunStatus.CONCLUIDO.value
    assert event.message == "Empresa já concluída nesta competência — ignorada."
    assert event.current == event.total == 1
    assert browser.stopped is True
