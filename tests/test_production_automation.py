import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from playwright.sync_api import sync_playwright

from nexfiscal.authentication.login import LoginHandler
from nexfiscal.browser.browser_manager import BrowserManager
from nexfiscal.config import portal_config as pc
from nexfiscal.config.settings import Settings
from nexfiscal.events import RunControl
from nexfiscal.exceptions import CancelledError, DownloadError
from nexfiscal.browser.download_manager import DownloadManager
from nexfiscal.nfse.xml_validator import XmlValidator
from nexfiscal.portal.portal_memory import PortalProfile
from nexfiscal.portal.selector_engine import SelectorEngine


@pytest.mark.parametrize("guided", [False, True])
def test_login_retry_never_clicks_or_presses_enter_after_captcha(guided):
    profile = PortalProfile(1, "https://portal.example")
    for spec in (pc.USER_FIELD, pc.PASSWORD_FIELD, pc.LOGIN_BUTTON):
        profile.selectors[(spec.stage.value, spec.key)] = [{"type": "id", "value": spec.key}]
    page, pm, reporter = Mock(), Mock(), Mock()
    page.url = "https://portal.example/login"
    engine = SimpleNamespace(profile=profile, find=Mock())
    handler = LoginHandler(page, engine, pm, Settings(guided_learning=guided), reporter)
    handler._remember_signature = Mock()
    handler.recognizer.probe_login = Mock()
    handler._fill_credentials = Mock()
    handler.is_logged_in = Mock(return_value=False)
    handler._password_visible = Mock(return_value=True)
    handler._wait_outcome = Mock(side_effect=["captcha", "ok"])
    calls = 0

    def resolve_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            handler._captcha_seen = True

    handler._wait_captcha = Mock(side_effect=resolve_once)
    handler._submit = Mock(side_effect=AssertionError("automatic access is forbidden"))
    handler.login(profile.url, "usuario", "senha", "Empresa", "Cidade")
    assert calls == 2
    handler._submit.assert_not_called()
    pm.click.assert_not_called()
    page.keyboard.press.assert_not_called()
    assert reporter.assist.call_count == 2
    assert all("clique manualmente" in c.args[0] for c in reporter.assist.call_args_list)


@pytest.mark.parametrize("method", ["find", "find_all"])
def test_cancelled_selector_search_stops_before_touching_page(method):
    page = Mock()
    control = RunControl()
    control.cancel()
    engine = SelectorEngine(page, control=control)
    with pytest.raises(CancelledError):
        getattr(engine, method)(pc.USER_FIELD)
    assert page.mock_calls == []


def test_cancellation_interrupts_long_retry_delay():
    control = RunControl()
    timer = threading.Timer(0.01, control.cancel)
    timer.start()
    try:
        with pytest.raises(CancelledError):
            control.wait(30)
    finally:
        timer.cancel()
        timer.join()


def test_new_page_failure_closes_already_created_context():
    manager = BrowserManager(Settings())
    context = Mock()
    context.new_page.side_effect = RuntimeError("browser disconnected")
    manager.new_context = Mock(return_value=context)
    with pytest.raises(RuntimeError, match="disconnected"):
        manager.new_page()
    context.close.assert_called_once()


def test_repeated_browser_start_reuses_connected_instance():
    manager = BrowserManager(Settings())
    manager._browser = Mock(is_connected=Mock(return_value=True))
    assert manager.start() is manager
    manager._browser.close.assert_not_called()


@pytest.mark.parametrize("status,expected_requests", [(500, 2), (401, 1)])
def test_real_playwright_http_download_retries_only_transient_failures(tmp_path, status, expected_requests):
    """Exercise Playwright's real HTTP client against an isolated local test portal."""
    requests = []
    payload = "<ConsultarNfseServicoPrestadoResposta><Mensagem>NOTAS FISCAIS NÃO LOCALIZADAS.</Mensagem></ConsultarNfseServicoPrestadoResposta>".encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            response_status = status if len(requests) == 1 else 200
            self.send_response(response_status)
            self.send_header("Content-Disposition", 'attachment; filename="consulta.xml"')
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05), daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            request = playwright.request.new_context()
            try:
                page = SimpleNamespace(context=SimpleNamespace(request=request))
                dm = DownloadManager(tmp_path, timeout_seconds=2)
                url = f"http://127.0.0.1:{server.server_port}/consulta.xml"
                if status == 401:
                    with pytest.raises(DownloadError, match="HTTP 401"):
                        dm._from_url(page, url)
                else:
                    captured = dm._from_url(page, url)
                    assert captured.path.read_bytes() == payload
                    assert captured.suggested_name == "consulta.xml"
                    assert XmlValidator().validate_file(captured.path).is_consulta_sem_notas
            finally:
                request.dispose()
        assert len(requests) == expected_requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_window_does_not_close_while_worker_is_still_running(monkeypatch):
    from nexfiscal.ui import main_window

    monkeypatch.setattr(main_window.QMessageBox, "question", Mock(return_value=main_window.QMessageBox.Yes))
    monkeypatch.setattr(main_window.QMessageBox, "information", Mock())
    worker = Mock(isRunning=Mock(return_value=True), wait=Mock(return_value=False))
    window = SimpleNamespace(worker=worker, control=Mock())
    event = Mock()
    main_window.MainWindow.closeEvent(window, event)
    window.control.cancel.assert_called_once()
    event.ignore.assert_called_once()
    event.accept.assert_not_called()
