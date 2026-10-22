from types import SimpleNamespace

from nexfiscal.ui.main_window import build_run_request_summary


def test_run_request_summary_lists_scope_and_destination():
    rows = [
        SimpleNamespace(cidade="Salvador"),
        SimpleNamespace(cidade="Salvador"),
        SimpleNamespace(cidade="Feira de Santana"),
    ]
    period = SimpleNamespace(
        competence=SimpleNamespace(label="09/2026"),
        start_br="01/09/2026",
        end_br="30/09/2026",
    )

    summary = build_run_request_summary(
        {"Salvador"}, rows, period, r"C:\\Fiscal"
    )

    assert "Cidades (1):" in summary
    assert "Salvador" in summary
    assert "Empresas a processar: 2" in summary
    assert "09/2026 (01/09/2026 a 30/09/2026)" in summary
    assert r"C:\\Fiscal" in summary
    assert "navegador só será aberto após sua confirmação" in summary


def test_run_request_summary_counts_cities_case_insensitively():
    rows = [SimpleNamespace(cidade="SALVADOR"), SimpleNamespace(cidade="Feira de Santana")]
    period = SimpleNamespace(
        competence=SimpleNamespace(label="09/2026"),
        start_br="01/09/2026",
        end_br="30/09/2026",
    )

    summary = build_run_request_summary({"Salvador"}, rows, period, "/tmp/fiscal")

    assert "Empresas a processar: 1" in summary
