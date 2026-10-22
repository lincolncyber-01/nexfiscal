"""Regressions for strict guided learning (no automatic selector discovery)."""
from types import SimpleNamespace
from unittest.mock import Mock

from nexfiscal.portal.selector_engine import SelectorEngine


def _engine():
    engine = SelectorEngine.__new__(SelectorEngine)
    engine.profile = None
    engine.guided_learning = True
    engine.timeout = 0
    engine.poll = 0
    engine.enable_ocr = True
    engine.assisted = False
    engine.assist_timeout = 1
    engine.on_assist = None
    engine.control = None
    engine.page = Mock()
    engine.page.url = "https://example.invalid"
    engine.page.wait_for_timeout = Mock()
    engine._try_memory = Mock(return_value=([], None, []))
    engine._discover_single = Mock(side_effect=AssertionError("automatic discovery must be disabled"))
    engine._discover_group = Mock(side_effect=AssertionError("automatic group discovery must be disabled"))
    engine._ocr = Mock(side_effect=AssertionError("OCR must be disabled in guided mode"))
    engine._assist = Mock(return_value=None)
    return engine


def test_find_uses_operator_guidance_instead_of_automatic_discovery():
    engine = _engine()
    spec = SimpleNamespace(stage=SimpleNamespace(value="login"), key="user", label="usuário", kind="input")

    assert engine.find(spec, timeout=0, required=False) is None
    engine._discover_single.assert_not_called()
    engine._ocr.assert_not_called()


def test_find_all_fails_closed_when_learned_group_selector_is_missing():
    engine = _engine()
    spec = SimpleNamespace(stage=SimpleNamespace(value="xml"), key="xml_list", label="lista de XMLs")

    assert engine.find_all(spec, timeout=0, required=False) is None
    engine._discover_group.assert_not_called()
