"""Failure-path regression tests: never publish partial state or report false success."""
import json
import os
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from nexfiscal import cli
from nexfiscal.config.settings import SettingsStore
from nexfiscal.config.settings import Settings
from nexfiscal.database.database import Database, SCHEMA_VERSION
from nexfiscal.database.models import RunStatus, Stage, StepStatus
from nexfiscal.exceptions import ConfigurationError
from nexfiscal.memory.portal_memory_manager import PortalMemoryManager
from nexfiscal.portal import guided_learning_session
from nexfiscal.portal.guided_action_recorder import GuidedActionRecorder
from nexfiscal.portal.workflow_engine import CompanyWorkflow
from nexfiscal.storage.organizer import OutputLayout
from nexfiscal.storage.atomic import atomic_write_text


@pytest.mark.parametrize("payload", [b'{"timeout_seconds":', b'{"timeout_seconds": -1}', b'\xff'])
def test_invalid_settings_are_preserved_and_never_silently_replaced(tmp_path, payload):
    path = tmp_path / "settings.json"
    path.write_bytes(payload)
    with pytest.raises(ConfigurationError):
        SettingsStore(path).load()
    assert path.read_bytes() == payload


def test_atomic_write_syncs_through_a_writable_descriptor(tmp_path, monkeypatch):
    original_fsync = os.fsync
    synced = []

    def windows_compatible_fsync(fd):
        # A zero-byte write also rejects read-only descriptors on POSIX, letting
        # Linux exercise the Windows fsync precondition without changing bytes.
        os.write(fd, b"")
        synced.append(fd)
        original_fsync(fd)

    monkeypatch.setattr(os, "fsync", windows_compatible_fsync)
    path = tmp_path / "state.json"
    atomic_write_text(path, '{"complete": true}')
    assert path.read_text() == '{"complete": true}'
    assert len(synced) == 1


def test_failed_commit_rolls_back_and_connection_can_be_reused(tmp_path):
    db = Database(tmp_path / "test.db")
    try:
        db.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
        db.execute("CREATE TABLE child (pid INTEGER REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED)")
        with pytest.raises(sqlite3.IntegrityError):
            with db.transaction():
                db.execute("INSERT INTO child VALUES (123)")
        assert not db._conn.in_transaction
        assert db.query_one("SELECT COUNT(*) AS n FROM child")["n"] == 0
        with db.transaction():
            db.execute("INSERT INTO parent VALUES (123)")
            db.execute("INSERT INTO child VALUES (123)")
        assert db.query_one("SELECT COUNT(*) AS n FROM child")["n"] == 1
    finally:
        db.close()


def test_nested_transaction_rollback_preserves_outer_work(tmp_path):
    db = Database(tmp_path / "test.db")
    try:
        with db.transaction():
            db.execute("INSERT INTO portals(url) VALUES ('https://keep.example')")
            with pytest.raises(RuntimeError):
                with db.transaction():
                    db.execute("INSERT INTO portals(url) VALUES ('https://discard.example')")
                    raise RuntimeError("simulated interruption")
        assert [r["url"] for r in db.query("SELECT url FROM portals")] == ["https://keep.example"]
    finally:
        db.close()


def test_newer_database_schema_is_not_downgraded(tmp_path):
    path = tmp_path / "future.db"
    with sqlite3.connect(path) as conn:
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    with pytest.raises(ConfigurationError, match="versão mais recente"):
        Database(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION + 1


def test_memory_import_rolls_back_all_portals_on_write_failure(tmp_path, monkeypatch):
    path = tmp_path / "memory.json"
    path.write_text(json.dumps([
        {"url": "https://one.example", "cidades": ["Uma"], "fluxos": {"xml": ["exportar"]}},
        {"url": "https://two.example", "cidades": ["Duas"], "fluxos": {"xml": ["exportar"]}},
    ]))
    db = Database(tmp_path / "test.db")
    manager = PortalMemoryManager(db)
    save = manager.repo.save_flow
    calls = 0

    def fail_second(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise sqlite3.OperationalError("disk full")
        return save(*args)

    monkeypatch.setattr(manager.repo, "save_flow", fail_second)
    try:
        with pytest.raises(sqlite3.OperationalError):
            manager.import_json(path)
        assert db.query_one("SELECT COUNT(*) AS n FROM portals")["n"] == 0
        assert db.query_one("SELECT COUNT(*) AS n FROM portal_flows")["n"] == 0
    finally:
        db.close()


def test_learn_dispatch_does_not_clear_existing_memory(tmp_path, monkeypatch):
    db = Database(tmp_path / "test.db")
    manager = PortalMemoryManager(db)
    portal = manager.portals.register(["Cidade"], "https://portal.example")
    manager.repo.save_flow(portal.id, "xml", ["exportar"])
    ctx = SimpleNamespace(db=db, settings=object())
    monkeypatch.setattr(cli, "bootstrap", lambda: ctx)
    learn = Mock(return_value=tmp_path / "receipt.json")
    monkeypatch.setattr(guided_learning_session, "run_guided_learning", learn)
    assert cli.main(["learn", "--cidade", "Cidade", "--url", portal.url]) == 0
    learn.assert_called_once_with(db, ctx.settings, "Cidade", portal.url)
    with sqlite3.connect(db.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM portal_flows").fetchone()[0] == 1


def test_unknown_maintenance_command_cannot_delete_memory():
    ctx = SimpleNamespace(db=Mock())
    with pytest.raises(ValueError):
        cli.cmd_maint(ctx, SimpleNamespace(cmd="typo"))
    ctx.db.execute.assert_not_called()


def test_manifest_failure_cannot_report_company_as_completed(tmp_path, monkeypatch):
    wf = CompanyWorkflow.__new__(CompanyWorkflow)
    paths = SimpleNamespace(xml_dir=tmp_path, livro_dir=tmp_path)
    wf.layout = SimpleNamespace(paths=lambda *args: paths)
    wf.row = SimpleNamespace(cidade="Cidade", razao_social="Empresa")
    wf.period = SimpleNamespace(competence=SimpleNamespace(label="10/2026"), start_br="01/10/2026", end_br="31/10/2026")
    wf.previous = SimpleNamespace(step=lambda stage: StepStatus.OK)
    wf.steps = {}
    wf.cnpj = "Empresa"
    wf.reporter = Mock()
    wf._guided_saved_xml_paths = []
    wf._guided_saved_book_paths = []
    wf._start_guided_download_capture = Mock()
    wf._attempt = Mock()
    wf._login = Mock()
    wf._log = Mock()
    wf._should_use_manual_guided_flow = lambda: False
    monkeypatch.setattr(OutputLayout, "write_manifest", Mock(side_effect=OSError("disk full")))
    result = wf._execute()
    assert result.status is RunStatus.CONCLUIDO_COM_PENDENCIA
    assert result.steps[Stage.ORGANIZACAO.value] == StepStatus.ERRO.value
    assert "Organização" in result.message


def test_learning_receipt_checks_current_routes_without_counting_previous_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXFISCAL_HOME", str(tmp_path / "home"))
    db = Database(tmp_path / "test.db")
    manager = PortalMemoryManager(db)
    portal = manager.portals.register(["Cidade"], "https://portal.example")
    old_route = "guided_actions_v1:portal.example/old"
    manager.repo.save_flow(portal.id, old_route, ['{"old": true}'])
    event = {"url": "https://portal.example/new", "order": 1,
             "selector": {"type": "id", "value": "exportar"}}
    recorder = SimpleNamespace(start=Mock(), drain=lambda: [event],
                               flow_name=GuidedActionRecorder.flow_name,
                               serialize=GuidedActionRecorder.serialize)
    monkeypatch.setattr(guided_learning_session, "GuidedActionRecorder", Mock(return_value=recorder))
    page = SimpleNamespace(goto=Mock(return_value=SimpleNamespace(status=200)))
    browser = SimpleNamespace(new_page=Mock(return_value=(Mock(), page)), stop=Mock())
    monkeypatch.setattr(guided_learning_session, "BrowserManager", lambda _settings: SimpleNamespace(start=lambda: browser))
    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    try:
        path = guided_learning_session.run_guided_learning(db, Settings(), "Cidade", portal.url)
        receipt = json.loads(path.read_text())
        assert receipt["persistence_check"] == "PASS"
        assert receipt["events_reloaded_from_database"] == 1
        assert receipt["end_to_end_workflow_test"] == "NOT_RUN"
        assert manager.repo.load_flows(portal.id)[old_route] == ['{"old": true}']
    finally:
        db.close()
