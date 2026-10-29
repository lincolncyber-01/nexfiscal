from __future__ import annotations

import csv
from types import SimpleNamespace

import pytest

from nexfiscal.config import settings as settings_module
from nexfiscal.config.settings import Settings, SettingsStore
from nexfiscal.database.models import RunStatus
from nexfiscal.database.repositories import normalize_url
from nexfiscal.exceptions import ConfigurationError
from nexfiscal.portal.portal_registry import _url_for_log
from nexfiscal.reports.export import _safe_csv_cell, to_csv
from nexfiscal.utils import sanitize_name


@pytest.mark.parametrize("url", [
    "https://example.com:abc/login",
    "https://example.com:70000/login",
    "https://:443/login",
    "ftp://example.com/login",
    "https://example.com:99999/",
    "https://user:password@example.com/login",
    "https://example.com/\nInjected-Log: yes",
])
def test_normalize_url_rejects_invalid_port_authority_scheme_or_credentials(url):
    with pytest.raises(ConfigurationError):
        normalize_url(url)


def test_normalize_url_adds_https_and_preserves_valid_portal_route_and_query():
    assert normalize_url("  portal.example.gov.br/iss?tenant=abc  ") == (
        "https://portal.example.gov.br/iss?tenant=abc"
    )
    assert normalize_url("https://portal.example.gov.br:8443/iss/") == (
        "https://portal.example.gov.br:8443/iss/"
    )


def test_portal_url_logging_drops_query_and_fragment():
    assert _url_for_log("https://portal.example/iss?token=secret#session") == (
        "https://portal.example/iss"
    )


@pytest.mark.parametrize("name, expected", [
    ("CON.xml", "_CON.xml"),
    ("con.txt", "_con.txt"),
    ("LPT1.pdf", "_LPT1.pdf"),
    ("NUL", "_NUL"),
    ("not-CON.xml", "not-CON.xml"),
])
def test_sanitize_name_handles_windows_reserved_device_names_with_extensions(name, expected):
    assert sanitize_name(name) == expected


def test_settings_save_is_atomic_and_keeps_previous_file_on_replace_failure(tmp_path, monkeypatch):
    target = tmp_path / "nested" / "settings.json"
    store = SettingsStore(target)
    store.save(Settings(output_root=str(tmp_path / "original")))
    original = target.read_text("utf-8")

    def fail_replace(_src, _dst):
        raise PermissionError("simulated transient lock")

    monkeypatch.setattr(settings_module.os, "replace", fail_replace)
    with pytest.raises(PermissionError):
        store.save(Settings(output_root=str(tmp_path / "replacement")))

    assert target.read_text("utf-8") == original
    assert store.load().output_root == str(tmp_path / "original")
    assert not list(target.parent.glob(".settings.json.*.tmp"))


def test_csv_cells_neutralize_formula_injection_but_preserve_numeric_cells():
    assert _safe_csv_cell('=HYPERLINK("https://evil.example","open")').startswith("'=")
    assert _safe_csv_cell("  @SUM(1,2)").startswith("'")
    assert _safe_csv_cell("\t=1+1").startswith("'")
    assert _safe_csv_cell("Empresa Normal") == "Empresa Normal"
    assert _safe_csv_cell(-123) == -123


def test_csv_export_quotes_untrusted_formula_fields(tmp_path):
    row = SimpleNamespace(
        cidade="Cidade",
        cnpj_usuario='=HYPERLINK("https://example.invalid","CNPJ")',
        competencia="09/2026",
        status=RunStatus.CONCLUIDO,
        xml_found=1,
        xml_downloaded=1,
        xml_existing=0,
        xml_failed=0,
        livro_ok=True,
        message="\t=cmd|' /C calc'!A0",
    )
    report = SimpleNamespace(runs=[row])
    path = to_csv(report, tmp_path / "report.csv")
    with path.open(encoding="utf-8-sig", newline="") as stream:
        data = list(csv.reader(stream, delimiter=";"))
    assert data[1][1].startswith("'=")
    assert data[1][-1].startswith("'\t=")
