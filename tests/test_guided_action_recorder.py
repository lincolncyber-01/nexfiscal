"""Tests for privacy-safe guided action recording."""
import json

from nexfiscal.portal.guided_action_recorder import GuidedActionRecorder, _INSTALL_RECORDER


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



def test_flow_name_is_scoped_to_web_route_and_ignores_query_fragment():
    name = GuidedActionRecorder.flow_name(
        "https://portal.example/financeiro?token=secret#download"
    )

    assert name == "guided_actions_v1:portal.example/financeiro"
    assert "secret" not in name
    assert GuidedActionRecorder.flow_name("https://portal.example:8443/login") == (
        "guided_actions_v1:portal.example:8443/login"
    )


def test_recorder_receives_events_in_python_queue_across_navigation():
    recorder = GuidedActionRecorder(page=object())
    event = {
        "order": 1, "event": "click",
        "url": "https://portal.example/login?session=secret",
        "selector": {"type": "id", "value": "entrar"},
        "tag": "button", "control_type": "submit", "label": "Entrar",
        "checked": None,
    }

    recorder._receive({}, event)
    recorder._receive({}, dict(event, order=1, url="https://portal.example/home"))

    captured = recorder.drain()

    assert [item["url"] for item in captured] == [
        "https://portal.example/login", "https://portal.example/home"
    ]
    assert recorder.drain() == []


def test_recorded_url_does_not_retain_url_credentials():
    event = {
        "order": 1, "event": "click",
        "url": "https://user:password@portal.example/login?token=secret",
        "selector": {"type": "id", "value": "go"},
        "tag": "button", "control_type": "button", "label": "Continuar",
    }

    result = GuidedActionRecorder._validate(event)

    assert result["url"] == "https://portal.example/login"
    assert "password" not in result["url"]
    assert "secret" not in result["url"]



def test_browser_recorder_never_reads_dynamic_text_or_input_values():
    assert "el.innerText" not in _INSTALL_RECORDER
    assert "el.value" not in _INSTALL_RECORDER
