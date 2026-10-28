"""Tests for persisting structured guided route events on the portal profile."""
import json
from unittest.mock import Mock

from nexfiscal.portal.portal_memory import PortalProfile
from nexfiscal.portal.workflow_engine import CompanyWorkflow


def test_workflow_persists_events_separately_for_each_route():
    workflow = CompanyWorkflow.__new__(CompanyWorkflow)
    workflow.profile = PortalProfile(portal_id=7, url="https://portal.example")
    workflow.action_recorder = Mock()
    workflow.action_recorder.drain.return_value = [
        {
            "order": 1, "event": "click", "url": "https://portal.example/login",
            "selector": {"type": "id", "value": "entrar"}, "tag": "button",
            "control_type": "submit", "label": "Entrar", "checked": None,
        },
        {
            "order": 2, "event": "click", "url": "https://portal.example/financeiro?session=secret",
            "selector": {"type": "id", "value": "livro"}, "tag": "a",
            "control_type": None, "label": "Livro Fiscal", "checked": None,
        },
    ]

    workflow._persist_guided_actions()

    login = workflow.profile.flows["guided_actions_v1:portal.example/login"]
    fiscal = workflow.profile.flows["guided_actions_v1:portal.example/financeiro"]
    assert json.loads(login[0])["order"] == 1
    assert json.loads(fiscal[0])["url"] == "https://portal.example/financeiro"
    assert "secret" not in fiscal[0]
    assert workflow.profile.dirty


def test_guided_mode_keeps_unlearned_xml_and_book_routes_manual():
    from types import SimpleNamespace
    from nexfiscal.config import portal_config as pc

    workflow = CompanyWorkflow.__new__(CompanyWorkflow)
    workflow.settings = SimpleNamespace(guided_learning=True)
    workflow.login_handler = SimpleNamespace(_manual_guided_login=False)
    workflow.profile = PortalProfile(portal_id=8, url="https://portal.example")

    assert workflow._should_use_manual_guided_flow()

    workflow.profile.selectors[(pc.NFSE_XML.stage.value, pc.NFSE_XML.key)] = [
        {"type": "id", "value": "xml-download"}
    ]
    workflow.profile.selectors[(pc.LIVRO_DOWNLOAD.stage.value, pc.LIVRO_DOWNLOAD.key)] = [
        {"type": "id", "value": "livro-download"}
    ]

    assert not workflow._should_use_manual_guided_flow()


def test_explicit_save_forces_persistence_of_clean_portal_profile():
    from contextlib import nullcontext
    from unittest.mock import Mock
    from types import SimpleNamespace
    from nexfiscal.memory.portal_memory_manager import PortalMemoryManager

    manager = PortalMemoryManager.__new__(PortalMemoryManager)
    manager.db = SimpleNamespace(transaction=lambda: nullcontext())
    manager.repo = SimpleNamespace(
        save_selector=Mock(), save_flow=Mock(), add_change=Mock(), add_note=Mock()
    )
    manager.portals = SimpleNamespace(update_recognition=Mock())
    profile = PortalProfile(
        portal_id=9,
        url="https://portal.example",
        flows={"guided_actions_v1:portal.example/consulta": ['{"order": 1}']},
        dirty=False,
    )

    manager.save_profile(profile, force=True)

    manager.repo.save_flow.assert_called_once_with(
        9, "guided_actions_v1:portal.example/consulta", ['{"order": 1}']
    )
    manager.portals.update_recognition.assert_called_once()
    assert not profile.dirty
