"""Regression tests for fiscal download validation."""
import pytest

from nexfiscal.exceptions import InvalidFileError
from nexfiscal.nfse.xml_validator import XmlValidator
from nexfiscal.storage.file_manager import validate_deliverable


def test_xml_validator_rejects_html_error_page():
    info = XmlValidator().validate_bytes(b"<!doctype html><html><body>sessao expirada</body></html>")

    assert not info.valid
    assert "HTML" in info.reason


def test_xml_validator_rejects_malformed_xml():
    info = XmlValidator().validate_bytes(b"<Nfse><InfNfse>")

    assert not info.valid
    assert "malformado" in info.reason.lower()


def test_xml_validator_accepts_well_formed_nfse_xml():
    info = XmlValidator().validate_bytes(
        b"<Nfse><InfNfse><Numero>123</Numero></InfNfse></Nfse>"
    )

    assert info.valid
    assert info.numero == "123"
    assert info.quantidade_notas == 1


def test_livro_validator_rejects_html_instead_of_a_deliverable(tmp_path):
    path = tmp_path / "livro.pdf"
    path.write_text("<html>erro de sessao</html>", encoding="utf-8")

    with pytest.raises(InvalidFileError, match="HTML"):
        validate_deliverable(path, ("pdf", "xml", "xls", "xlsx", "csv", "zip"))


def test_livro_validator_rejects_empty_file(tmp_path):
    path = tmp_path / "livro.pdf"
    path.write_bytes(b"")

    with pytest.raises(InvalidFileError, match="vazio"):
        validate_deliverable(path, ("pdf", "xml", "xls", "xlsx", "csv", "zip"))


def test_livro_validator_accepts_complete_pdf(tmp_path):
    path = tmp_path / "livro.pdf"
    path.write_bytes(b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF")

    assert validate_deliverable(path, ("pdf", "xml", "xls", "xlsx", "csv", "zip")) == "pdf"



def test_xml_validator_preserves_well_formed_consulta_response_without_known_empty_phrase():
    payload = b"""
    <ConsultarNfseServicoPrestadoResposta>
      <ListaMensagemRetorno>
        <MensagemRetorno><Codigo>0</Codigo><Mensagem>Consulta concluida.</Mensagem></MensagemRetorno>
      </ListaMensagemRetorno>
    </ConsultarNfseServicoPrestadoResposta>
    """

    info = XmlValidator().validate_bytes(payload)

    assert info.valid
    assert info.is_resposta_consulta
    assert not info.is_consulta_sem_notas
    assert info.quantidade_notas == 0


def test_xml_validator_extracts_invoice_inside_consulta_response():
    payload = b"""
    <ConsultarNfseServicoPrestadoResposta>
      <ListaNfse><CompNfse><Nfse><InfNfse><Numero>205241</Numero></InfNfse></Nfse></CompNfse></ListaNfse>
    </ConsultarNfseServicoPrestadoResposta>
    """

    info = XmlValidator().validate_bytes(payload)

    assert info.valid
    assert info.is_resposta_consulta
    assert info.numero == "205241"
    assert info.quantidade_notas == 1
