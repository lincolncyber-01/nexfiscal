"""Tests for strict guided-login entry decisions."""
from types import SimpleNamespace
from unittest.mock import Mock

from nexfiscal.authentication.login import LoginHandler
from nexfiscal.config import portal_config as pc
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
