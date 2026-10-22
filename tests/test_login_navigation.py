from types import SimpleNamespace
from unittest.mock import MagicMock

from nexfiscal.authentication.login import LoginHandler


def test_guided_learning_opens_configured_portal_url():
    url = "https://portal.exemplo.gov.br/login"
    handler = LoginHandler.__new__(LoginHandler)
    handler.page = MagicMock()
    handler.page.url = url
    handler.engine = MagicMock()
    handler.engine.find.return_value = None
    handler.pm = MagicMock()
    handler.settings = SimpleNamespace(guided_learning=True, assist_timeout_seconds=30)
    handler.reporter = MagicMock()
    handler.control = None
    handler._captcha_seen = False
    handler._has_known_login_flow = MagicMock(return_value=False)
    handler._wait_captcha = MagicMock()
    handler._remember_signature = MagicMock()
    handler._password_visible = MagicMock(return_value=False)
    handler._wait_for_password_field = MagicMock()
    handler._fill_credentials = MagicMock()
    handler.is_logged_in = MagicMock(return_value=True)

    result = handler.login(url, "12345678900", "senha-teste", "Empresa Teste", "Cidade Teste")

    handler.pm.goto.assert_called_once_with(url)
    assert result == url
    assert any(
        "aberto automaticamente" in call.args[0]
        for call in handler.reporter.assist.call_args_list
    )
