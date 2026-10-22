"""Leitura da planilha .xlsx de empresas."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from ..database.models import CompanyRow
from ..exceptions import SpreadsheetError
from ..utils import normalize_text

# nome canônico -> variações aceitas (normalizadas)
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "cnpj_usuario": ("cnpj/usuario", "cnpj usuario", "cnpj/cpf", "cnpj", "usuario", "login"),
    "razao_social": ("razao social", "razao social da empresa", "empresa", "nome empresarial", "nome da empresa"),
    "senha": ("senha", "password", "pass"),
    "cidade": ("cidade", "municipio"),
}
COLUMN_TITLES = {
    "cnpj_usuario": "CNPJ/USUÁRIO", "razao_social": "RAZÃO SOCIAL",
    "senha": "SENHA", "cidade": "CIDADE",
}


@dataclass
class SpreadsheetData:
    path: Path
    rows: list[CompanyRow] = field(default_factory=list)
    missing_columns: list[str] = field(default_factory=list)


def _clean_cell(value: object) -> str:
    text = "" if value is None else str(value).strip()
    if text.lower() in ("nan", "none", "nat"):
        return ""
    if re.fullmatch(r"\d+\.0", text):
        text = text[:-2]
    return text


def _map_columns(columns: list[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    normalized = {c: normalize_text(c).replace("_", " ") for c in columns}
    for canonical, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            for col, norm in normalized.items():
                if norm == alias.replace("_", " ") and col not in found.values():
                    found[canonical] = col
                    break
            if canonical in found:
                break
    return found


def read_spreadsheet(path: str | Path, sheet: str | int = 0) -> SpreadsheetData:
    path = Path(path)
    if not path.exists() or not path.is_file():
        raise SpreadsheetError(f"Planilha não encontrada: {path}")
    if path.suffix.lower() not in (".xlsx", ".xlsm"):
        raise SpreadsheetError("Formato não suportado. Use um arquivo .xlsx.")
    try:
        df = pd.read_excel(path, sheet_name=sheet, dtype=str, keep_default_na=False, engine="openpyxl")
    except Exception as exc:
        raise SpreadsheetError(f"Não foi possível ler a planilha: {exc}") from exc

    mapping = _map_columns([str(c) for c in df.columns])
    data = SpreadsheetData(path=path)
    data.missing_columns = [COLUMN_TITLES[c] for c in COLUMN_ALIASES if c not in mapping]
    if data.missing_columns:
        return data

    for idx, rec in enumerate(df.to_dict("records")):
        user = _clean_cell(rec.get(mapping["cnpj_usuario"]))
        legal_name = _clean_cell(rec.get(mapping["razao_social"]))
        pwd = _clean_cell(rec.get(mapping["senha"]))
        city = _clean_cell(rec.get(mapping["cidade"]))
        if not (user or legal_name or pwd or city):
            continue
        data.rows.append(CompanyRow(
            row_number=idx + 2, cnpj_usuario=user, razao_social=legal_name, senha=pwd, cidade=city
        ))
    return data
