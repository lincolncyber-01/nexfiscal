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
