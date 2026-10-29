import json
import logging
import zipfile
from types import SimpleNamespace

import pytest
from openpyxl import load_workbook

from nexfiscal.browser.download_manager import DownloadManager
from nexfiscal.database.models import RunStatus
from nexfiscal.exceptions import InvalidFileError
from nexfiscal.exceptions import NexFiscalError
from nexfiscal.controller import BatchRunner
from nexfiscal.logs.logger import JsonFormatter, SECRETS, log_context, setup_logging
from nexfiscal.reports import export as report_export
from nexfiscal.storage import archive
from nexfiscal.storage.file_manager import validate_deliverable


@pytest.mark.parametrize("limit,value", [
    ("MAX_ZIP_ENTRIES", 1), ("MAX_ZIP_MEMBER_BYTES", 2), ("MAX_ZIP_TOTAL_BYTES", 5),
    ("MAX_ZIP_RATIO", 1),
])
def test_zip_limits_apply_before_any_file_is_extracted(tmp_path, monkeypatch, limit, value):
    bundle = tmp_path / "bundle.zip"
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("first.xml", b"<Nfse>" + b" " * 1000 + b"</Nfse>")
        zf.writestr("second.xml", b"<Nfse/>")
    monkeypatch.setattr(archive, limit, value)
    with pytest.raises(InvalidFileError):
        DownloadManager.extract_zip(bundle)
    assert not (tmp_path / "bundle_unzipped").exists()
    with pytest.raises(InvalidFileError):
        validate_deliverable(bundle, ("zip",))


def test_archive_path_traversal_cannot_escape_extraction_directory(tmp_path):
    bundle = tmp_path / "bundle.zip"
    with zipfile.ZipFile(bundle, "w") as zf:
        zf.writestr("../../outside.xml", b"<Nfse/>")
        zf.writestr("..\\..\\windows.xml", b"<Nfse/>")
    extracted = DownloadManager.extract_zip(bundle)
    assert {p.name for p in extracted} == {"outside.xml", "windows.xml"}
    assert all(p.parent == tmp_path / "bundle_unzipped" for p in extracted)
    assert not (tmp_path / "outside.xml").exists()


def _report():
    row = SimpleNamespace(cidade="=1+1", cnpj_usuario="Empresa", competencia="10/2026",
                          status=RunStatus.CONCLUIDO, xml_found=1, xml_downloaded=1,
                          xml_existing=0, xml_failed=0, livro_ok=True, message="=HYPERLINK(\"evil\")")
    return SimpleNamespace(runs=[row], cities=[SimpleNamespace(
        city="=1+1", empresas=1, concluidas=1, pendencias=0, erros=0)], to_text=lambda: "=SUM(1,2)")


def test_xlsx_untrusted_text_is_never_executable_formula(tmp_path):
    path = report_export.export(_report(), tmp_path / "report.xlsx")
    wb = load_workbook(path)
    try:
        assert wb["Empresas"]["A2"].value == "=1+1"
        for sheet in wb:
            assert all(cell.data_type != "f" for row in sheet for cell in row)
        assert wb["Empresas"]["E2"].data_type == "n"
    finally:
        wb.close()


def test_report_serialization_failure_preserves_previous_file(tmp_path, monkeypatch):
    path = tmp_path / "report.xlsx"
    path.write_bytes(b"previous report")

    def fail(_rep, temp):
        temp.write_bytes(b"partial")
        raise OSError("disk full")

    monkeypatch.setattr(report_export, "to_xlsx", fail)
    with pytest.raises(OSError):
        report_export.export(_report(), path)
    assert path.read_bytes() == b"previous report"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["report.xlsx"]


def test_json_logs_preserve_context_and_redact_nested_secrets_and_traceback(tmp_path):
    logger = setup_logging(tmp_path)
    SECRETS.register("private-value")
    try:
        with log_context(run_id="test-run", company="Empresa"):
            try:
                raise OSError("private-value https://portal.example/xml?session=unregistered#secret")
            except OSError:
                logger.exception("Falha", extra={"event": "download_failed", "attempt": 2,
                                                "details": {"password": "not-registered", "value": "private-value"}})
        payload = json.loads((tmp_path / "nexfiscal.jsonl").read_text())
        assert payload["run_id"] == "test-run"
        assert payload["event"] == "download_failed"
        assert payload["attempt"] == 2
        assert payload["details"]["password"] == "********"
        for filename in ("nexfiscal.jsonl", "nexfiscal.log"):
            text = (tmp_path / filename).read_text()
            assert "private-value" not in text
            assert "session=" not in text
        record = logging.makeLogRecord({"msg": "after context", "levelname": "INFO"})
        assert "run_id" not in json.loads(JsonFormatter().format(record))
    finally:
        SECRETS.clear()
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()


def test_logging_reconfiguration_closes_previous_file_handlers(tmp_path):
    logger = setup_logging(tmp_path)
    previous = [h for h in logger.handlers if isinstance(h, logging.FileHandler)]
    setup_logging(tmp_path)
    assert all(h.stream is None for h in previous)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


def test_batch_error_is_sanitized_before_secret_registry_is_cleared(tmp_path):
    logger = setup_logging(tmp_path)
    runner = BatchRunner.__new__(BatchRunner)

    def fail(*_args, **_kwargs):
        SECRETS.register("private-batch-secret")
        raise RuntimeError("connection failed: private-batch-secret")

    runner._run = fail
    try:
        with pytest.raises(NexFiscalError) as caught:
            runner.run([], SimpleNamespace(competence=SimpleNamespace(label="10/2026")))
        assert "private-batch-secret" not in str(caught.value)
        assert "private-batch-secret" not in (tmp_path / "nexfiscal.log").read_text()
        assert SECRETS.redact("private-batch-secret") == "private-batch-secret"
    finally:
        SECRETS.clear()
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
