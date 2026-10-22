from pathlib import Path

import pandas as pd

from nexfiscal.dates import Competence
from nexfiscal.spreadsheet.reader import read_spreadsheet
from nexfiscal.storage.organizer import OutputLayout
from nexfiscal.utils import company_folder_name, city_folder_name


def test_output_layout_uses_city_and_legal_name_without_extra_folders(tmp_path: Path) -> None:
    comp = Competence(2026, 9)
    paths = OutputLayout(tmp_path).paths(
        "São Paulo", "ACME Comércio e Serviços Ltda.", comp
    )

    expected = tmp_path / "São Paulo" / "ACME Comércio e Serviços Ltda"
    assert paths.competence_dir == expected
    assert paths.xml_dir == expected
    assert paths.livro_dir == expected
    assert expected.is_dir()
    assert not (expected / comp.folder).exists()


def test_folder_names_preserve_official_names_and_sanitize_invalid_chars() -> None:
    assert city_folder_name("  Vitória da Conquista  ") == "Vitória da Conquista"
    assert company_folder_name("Empresa: Comércio/Serviços Ltda.") == "Empresa- Comércio-Serviços Ltda"


def test_spreadsheet_requires_legal_name_column(tmp_path: Path) -> None:
    path = tmp_path / "sem_razao_social.xlsx"
    pd.DataFrame([{
        "CNPJ/USUÁRIO": "12.345.678/0001-01",
        "SENHA": "senha-teste",
        "CIDADE": "Salvador",
    }]).to_excel(path, index=False)

    data = read_spreadsheet(path)

    assert data.missing_columns == ["RAZÃO SOCIAL"]


def test_spreadsheet_reads_legal_name(tmp_path: Path) -> None:
    path = tmp_path / "com_razao_social.xlsx"
    pd.DataFrame([{
        "CNPJ/USUÁRIO": "12.345.678/0001-01",
        "RAZÃO SOCIAL": "ACME Comércio e Serviços Ltda.",
        "SENHA": "senha-teste",
        "CIDADE": "Salvador",
    }]).to_excel(path, index=False)

    data = read_spreadsheet(path)

    assert not data.missing_columns
    assert data.rows[0].razao_social == "ACME Comércio e Serviços Ltda."
