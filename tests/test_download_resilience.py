"""Regression coverage for resilient downloads and the post-CAPTCHA safety gate."""
from __future__ import annotations

import io
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.sync_api import Error as PlaywrightError

from nexfiscal.authentication.login import LoginHandler
from nexfiscal.browser.download_manager import DownloadManager
from nexfiscal.exceptions import FilePermissionError, LoginError
from nexfiscal.portal.workflow_engine import CompanyWorkflow
from nexfiscal.storage.file_manager import move_atomic


VALID_XML = b"<Nfse><InfNfse><Numero>123</Numero></InfNfse></Nfse>"


class ReporterStub:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def assist(self, message: str) -> None:
        self.messages.append(message)


class ExecutionStub:
    def __init__(self) -> None:
        self.records: list[tuple] = []

    def log(self, *args) -> None:
        self.records.append(args)


class DownloadStub:
    def __init__(self, filename: str, payload: bytes, fail_first: bool = False) -> None:
        self.suggested_filename = filename
        self.payload = payload
        self.fail_first = fail_first
        self.calls = 0

    def failure(self) -> None:
        return None

    def save_as(self, target: str) -> None:
        self.calls += 1
        if self.fail_first and self.calls == 1:
            raise PlaywrightError("transient file copy failure")
        Path(target).write_bytes(self.payload)


class EventEmitterStub:
    def __init__(self) -> None:
        self.listeners: dict[str, list] = {}

    def on(self, event: str, callback) -> None:
        self.listeners.setdefault(event, []).append(callback)

    def remove_listener(self, event: str, callback) -> None:
        self.listeners[event] = [item for item in self.listeners.get(event, []) if item != callback]

    def emit(self, event: str, value) -> None:
        for callback in list(self.listeners.get(event, [])):
            callback(value)


class PageStub(EventEmitterStub):
    def __init__(self) -> None:
        super().__init__()
        self.context = EventEmitterStub()
        self.context.pages = [self]
        self.url = "https://portal.example/consulta"

    def wait_for_timeout(self, _ms: int) -> None:
        return None


def workflow_stub(tmp_path: Path) -> CompanyWorkflow:
    workflow = CompanyWorkflow.__new__(CompanyWorkflow)
    workflow.settings = SimpleNamespace(tmp_dir=tmp_path / "tmp", guided_learning=True)
    workflow._guided_download_dest = tmp_path / "Cidade" / "Empresa"
    workflow._guided_download_context = None
    workflow._guided_download_pages = {}
    workflow._guided_attach_page = None
    workflow._guided_download_events = 0
    workflow._guided_download_saved = 0
    workflow._guided_download_failures = 0
    workflow.dm = DownloadManager(workflow.settings.tmp_dir)
    workflow.reporter = ReporterStub()
    workflow.execs = ExecutionStub()
    workflow.run_id = "test-run"
    workflow.cnpj = "empresa-teste"
    workflow.row = SimpleNamespace(cidade="Cidade")
    workflow.period = SimpleNamespace(competence=SimpleNamespace(label="2026-10"))
    return workflow


def test_manual_guided_download_preserves_portal_filename_and_verifies_bytes(tmp_path):
    workflow = workflow_stub(tmp_path)
    download = DownloadStub("nota-original.xml", VALID_XML)

    workflow._capture_guided_download(download)

    saved = workflow._guided_download_dest / "nota-original.xml"
    assert saved.is_file()
    assert saved.read_bytes() == VALID_XML
    assert workflow._guided_download_events == 1
    assert workflow._guided_download_saved == 1
    assert workflow._guided_download_failures == 0
    assert any("salvo e verificado" in str(record) for record in workflow.execs.records)


def test_manual_guided_zip_download_extracts_xml_and_keeps_inner_filename(tmp_path):
    workflow = workflow_stub(tmp_path)
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w") as archive:
        archive.writestr("export/nota-original.xml", VALID_XML)
    download = DownloadStub("exportacao.zip", zip_buffer.getvalue())

    workflow._capture_guided_download(download)

    saved = workflow._guided_download_dest / "nota-original.xml"
    assert saved.is_file()
    assert saved.read_bytes() == VALID_XML
    assert workflow._guided_download_failures == 0


def test_manual_guided_invalid_download_is_reported_not_silent(tmp_path):
    workflow = workflow_stub(tmp_path)
    download = DownloadStub("erro.xml", b"<!doctype html><html><body>Erro do portal</body></html>")

    workflow._capture_guided_download(download)

    assert not list(workflow._guided_download_dest.glob("*.xml"))
    assert workflow._guided_download_failures == 1
    assert any("FALHA NO DOWNLOAD XML" in message for message in workflow.reporter.messages)
    assert any("ERRO" in str(record) for record in workflow.execs.records)


def test_download_capture_keeps_event_when_click_raises_after_starting_download(tmp_path):
    page = PageStub()
    manager = DownloadManager(tmp_path / "downloads", timeout_seconds=1)
    download = DownloadStub("nota.xml", VALID_XML, fail_first=True)

    def trigger() -> None:
        page.emit("download", download)
        raise PlaywrightError("target page closed after download was triggered")

    captured = manager.capture(page, trigger)

    assert captured.path.is_file()
    assert captured.path.read_bytes() == VALID_XML
    assert download.calls == 2
    assert page.listeners["download"] == []
    assert page.context.listeners["page"] == []


def test_atomic_file_move_retries_transient_windows_lock_and_verifies_content(tmp_path, monkeypatch):
    import nexfiscal.storage.file_manager as fm

    source = tmp_path / "source.xml"
    target = tmp_path / "dest" / "target.xml"
    source.write_bytes(VALID_XML)
    real_replace = fm.os.replace
    calls = 0

    def flaky_replace(src, dst):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PermissionError("temporarily locked by another process")
        return real_replace(src, dst)

    monkeypatch.setattr(fm.os, "replace", flaky_replace)
    monkeypatch.setattr(fm.time, "sleep", lambda _seconds: None)

    result = move_atomic(source, target)

    assert result == target
    assert target.read_bytes() == VALID_XML
    assert not source.exists()
    assert not list(target.parent.glob("*.part"))
    assert calls == 2


def test_atomic_file_move_keeps_source_if_destination_cannot_be_written(tmp_path, monkeypatch):
    import nexfiscal.storage.file_manager as fm

    source = tmp_path / "source.xml"
    target = tmp_path / "dest" / "target.xml"
    source.write_bytes(VALID_XML)

    def locked_replace(_src, _dst):
        raise PermissionError("locked by another process")

    monkeypatch.setattr(fm.os, "replace", locked_replace)
    monkeypatch.setattr(fm.time, "sleep", lambda _seconds: None)

    with pytest.raises(FilePermissionError):
        move_atomic(source, target)

    assert source.read_bytes() == VALID_XML
    assert not target.exists()
    assert not list(target.parent.glob("*.part")) if target.parent.exists() else True


def test_login_submit_is_forbidden_after_captcha_has_been_seen():
    handler = LoginHandler.__new__(LoginHandler)
    handler._captcha_seen = True

    with pytest.raises(LoginError, match="clique manualmente"):
        handler._submit()


def test_inline_file_download_retries_transient_playwright_network_error(tmp_path, monkeypatch):
    page = PageStub()

    class Response:
        ok = True
        status = 200
        headers = {'content-disposition': 'attachment; filename="nota-inline.xml"'}

        @staticmethod
        def body() -> bytes:
            return VALID_XML

    class RequestStub:
        calls = 0

        def get(self, _url: str, timeout: int):
            self.calls += 1
            assert timeout > 0
            if self.calls == 1:
                raise PlaywrightError("temporary connection reset")
            return Response()

    page.context.request = RequestStub()
    manager = DownloadManager(tmp_path / "inline", timeout_seconds=1)
    monkeypatch.setattr("nexfiscal.browser.download_manager.time.sleep", lambda _seconds: None)

    captured = manager._from_url(page, "https://portal.example/files/nota-inline.xml")

    assert page.context.request.calls == 2
    assert captured.suggested_name == "nota-inline.xml"
    assert captured.path.read_bytes() == VALID_XML
