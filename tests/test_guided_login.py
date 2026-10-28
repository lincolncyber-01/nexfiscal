"""Tests for strict guided-login entry decisions."""
from types import SimpleNamespace
from unittest.mock import Mock

from nexfiscal.authentication.captcha import ManualCaptchaGate
from nexfiscal.authentication.login import LoginHandler
from nexfiscal.config import portal_config as pc
from nexfiscal.database.models import Stage, StepStatus
from nexfiscal.events import RunControl
from nexfiscal.portal.portal_memory import PortalProfile


def _handler(profile):
    handler = LoginHandler.__new__(LoginHandler)
    handler.engine = SimpleNamespace(profile=profile)
    return handler


def test_portal_with_only_partial_selectors_stays_in_manual_guided_entry():
    profile = PortalProfile(portal_id=1, url="https://portal.example")
    profile.selectors[(pc.USER_FIELD.stage.value, pc.USER_FIELD.key)] = [
        {"type": "id", "value": "usuario"}
    ]

    assert not _handler(profile)._has_known_login_flow()


def test_login_navigation_is_automatic_only_after_essential_selectors_are_learned():
    profile = PortalProfile(portal_id=1, url="https://portal.example")
    for spec in (pc.USER_FIELD, pc.PASSWORD_FIELD, pc.LOGIN_BUTTON):
        profile.selectors[(spec.stage.value, spec.key)] = [
            {"type": "id", "value": spec.key}
        ]

    assert _handler(profile)._has_known_login_flow()


def test_portal_without_profile_never_counts_as_known_login_flow():
    assert not _handler(None)._has_known_login_flow()



def test_unknown_portal_login_submission_is_left_to_operator():
    handler = LoginHandler.__new__(LoginHandler)
    handler.engine = SimpleNamespace(find=Mock())
    handler.page = SimpleNamespace(url="https://portal.example/login")
    handler.reporter = Mock()
    handler.pm = Mock()

    handler._request_manual_submit()

    handler.engine.find.assert_called_once_with(pc.LOGIN_BUTTON, timeout=6)
    handler.pm.click.assert_not_called()
    assert "Clique manualmente" in handler.reporter.assist.call_args.args[0]



def test_manual_captcha_confirmation_unblocks_gate_without_automating_challenge():
    control = RunControl()

    class PageStub:
        def bring_to_front(self):
            return None

        def wait_for_timeout(self, _ms):
            # Simulate the operator clicking the explicit confirmation in the GUI.
            control.confirm_captcha_resolved()

    gate = ManualCaptchaGate(
        PageStub(), timeout_seconds=2, headless=False, control=control,
    )
    gate.detector = SimpleNamespace(detect=lambda: "image", is_solved=lambda: False)

    assert gate.wait_if_present("Empresa", "Cidade") is True


def test_guided_login_can_resume_after_a_verified_manual_fiscal_download():
    handler = LoginHandler.__new__(LoginHandler)
    handler._manual_guided_login = True
    handler._captcha_seen = True
    handler.guided_progress = lambda: True
    handler.page = SimpleNamespace(url="https://portal.example/fiscal")
    handler.reporter = Mock()

    assert handler._finish_guided_login_if_artifacts() is True
    assert handler.reporter.stage.call_args_list == [
        ((Stage.CAPTCHA, StepStatus.OK, "Resolução manual confirmada pela ação fiscal"), {}),
        ((Stage.LOGIN, StepStatus.OK, "Ação fiscal manual confirmada"), {}),
    ]
