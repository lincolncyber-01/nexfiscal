"""Tests for privacy-safe guided action recording."""
import json

from nexfiscal.portal.guided_action_recorder import GuidedActionRecorder


def test_validate_strips_query_and_fragment_and_never_keeps_input_value():
    event = {
        "order": 2, "event": "change",
        "url": "https://portal.example/login?token=secret#form",
        "selector": {"type": "name", "value": "competencia"},
        "tag": "input", "control_type": "text", "label": "Competência",
        "value": "should-not-be-captured",
    }

    result = GuidedActionRecorder._validate(event)

    assert result["url"] == "https://portal.example/login"
    assert "value" not in result
    assert result["selector"] == {"type": "name", "value": "competencia"}


def test_serialize_produces_json_strings_compatible_with_existing_flow_storage():
    events = [{
        "order": 1, "event": "click", "url": "https://portal.example/",
        "selector": {"type": "id", "value": "entrar"}, "tag": "button",
        "control_type": "submit", "label": "Entrar", "checked": None,
    }]

    serialized = GuidedActionRecorder.serialize(events)

    assert len(serialized) == 1
    assert json.loads(serialized[0]) == events[0]


def test_sensitive_field_identifiers_are_not_part_of_event_payload_values():
    event = {
        "order": 1, "event": "click", "url": "https://portal.example/",
        "selector": {"type": "id", "value": "senha"}, "tag": "input",
        "control_type": "password", "label": "Senha", "checked": None,
    }

    result = GuidedActionRecorder._validate(event)

    assert "senha" in result["selector"]["value"]
    assert "value" not in result
