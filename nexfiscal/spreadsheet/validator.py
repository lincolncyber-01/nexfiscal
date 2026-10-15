"""Validação da planilha antes de qualquer processamento."""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from ..database.models import CompanyRow
from ..exceptions import SpreadsheetError
from ..utils import is_valid_cnpj, normalize_city, only_digits
from .reader import SpreadsheetData, read_spreadsheet


class Level(str, Enum):
    ERROR = "ERRO"        # crítico: impede o processamento
    WARNING = "AVISO"     # informativo


@dataclass
class Issue:
    row: int              # 0 = problema geral (arquivo/colunas)
    message: str
    level: Level = Level.ERROR


@dataclass
class ValidationReport:
    rows: list[CompanyRow] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level is Level.ERROR]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.level is Level.WARNING]

    @property
    def has_critical(self) -> bool:
        return bool(self.errors)

    @property
    def valid_rows(self) -> list[CompanyRow]:
        bad = {i.row for i in self.errors}
        return [r for r in self.rows if r.row_number not in bad]

    def cities(self) -> dict[str, list[CompanyRow]]:
        """Agrupa por cidade (nome original da primeira ocorrência), preservando a ordem."""
        groups: dict[str, list[CompanyRow]] = {}
        names: dict[str, str] = {}
        for r in self.rows:
            names.setdefault(r.cidade_norm, r.cidade)
            groups.setdefault(r.cidade_norm, []).append(r)
        return {names[k]: v for k, v in groups.items()}

    def format(self) -> str:
        """Texto no formato da especificação."""
        lines: list[str] = []
        if self.errors:
            lines.append("ERROS ENCONTRADOS")
            lines.extend(self._group(self.errors))
        if self.warnings:
            if lines:
                lines.append("")
            lines.append("AVISOS")
            lines.extend(self._group(self.warnings))
        return "\n".join(lines) if lines else "Planilha válida."

    @staticmethod
    def _group(issues: list[Issue]) -> list[str]:
        by_row: dict[int, list[str]] = defaultdict(list)
        for i in issues:
            by_row[i.row].append(i.message)
        out: list[str] = []
        for row in sorted(by_row):
            out.append("")
            out.append(f"Linha {row}:" if row else "Geral:")
            out.extend(f"- {m}" for m in by_row[row])
        return out


def validate_rows(rows: list[CompanyRow], known_cities: set[str] | None = None) -> ValidationReport:
    """Valida linhas já lidas. ``known_cities`` = cidades (normalizadas) com portal cadastrado."""
    report = ValidationReport(rows=list(rows))
    seen: dict[tuple[str, str], CompanyRow] = {}

    for r in rows:
        n = r.row_number
        if not r.cnpj_usuario.strip():
            report.issues.append(Issue(n, "CNPJ/USUÁRIO vazio"))
        if not r.password:
            report.issues.append(Issue(n, "Senha vazia"))
        if not r.cidade.strip():
            report.issues.append(Issue(n, "Cidade não informada"))

        user = r.cnpj_usuario.strip()
        if user:
            digits = only_digits(user)
            numeric_like = re.fullmatch(r"[\d.\-/\s]+", user) is not None
            if numeric_like:
                if len(digits) not in (11, 14):
                    report.issues.append(Issue(n, f"CNPJ com formato inválido ({len(digits)} dígitos)"))
                elif len(digits) == 14 and not is_valid_cnpj(user):
                    report.issues.append(Issue(n, "CNPJ com dígito verificador inválido", Level.WARNING))
            else:
                report.issues.append(Issue(n, "Usuário não numérico (será usado como login literal)",
                                           Level.WARNING))

        if r.cidade.strip() and r.cnpj_usuario.strip():
            key = (r.usuario_key, r.cidade_norm)
            if key in seen:
                first = seen[key]
                if first.password == r.password:
                    report.issues.append(Issue(
                        n, f"Linha duplicada (igual à linha {first.row_number}); será processada uma vez",
                        Level.WARNING))
                else:
                    report.issues.append(Issue(
                        n, f"Empresa duplicada com senha diferente da linha {first.row_number}"))
            else:
                seen[key] = r

        if known_cities is not None and r.cidade.strip() and normalize_city(r.cidade) not in known_cities:
            report.issues.append(Issue(
                n, f"Cidade desconhecida: '{r.cidade}' (portal não cadastrado — será solicitado)",
                Level.WARNING))

    # remove duplicatas exatas da lista de processamento
    unique: dict[tuple[str, str], CompanyRow] = {}
    for r in report.rows:
        if r.cidade.strip() and r.cnpj_usuario.strip():
            unique.setdefault((r.usuario_key, r.cidade_norm), r)
        else:
            unique[(f"_invalid_{r.row_number}", "")] = r
    report.rows = list(unique.values())
    return report


def validate_spreadsheet(path: str | Path, known_cities: set[str] | None = None) -> ValidationReport:
    try:
        data: SpreadsheetData = read_spreadsheet(path)
    except SpreadsheetError as exc:
        return ValidationReport(issues=[Issue(0, exc.message)])
    if data.missing_columns:
        return ValidationReport(issues=[
            Issue(0, f"Coluna obrigatória ausente: {c}") for c in data.missing_columns])
    if not data.rows:
        return ValidationReport(issues=[Issue(0, "A planilha não contém nenhuma empresa.")])
    return validate_rows(data.rows, known_cities)
