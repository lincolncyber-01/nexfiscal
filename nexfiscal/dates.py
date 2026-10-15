"""Competência e período de consulta."""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime

from .exceptions import ConfigurationError


@dataclass(frozen=True, order=True)
class Competence:
    year: int
    month: int

    def __post_init__(self) -> None:
        if not 1 <= self.month <= 12:
            raise ConfigurationError(f"Mês inválido: {self.month}")
        if not 1900 <= self.year <= 2200:
            raise ConfigurationError(f"Ano inválido: {self.year}")

    @property
    def start(self) -> date:
        return date(self.year, self.month, 1)

    @property
    def end(self) -> date:
        return date(self.year, self.month, calendar.monthrange(self.year, self.month)[1])

    @property
    def label(self) -> str:
        """'09/2026' (usado em banco, relatórios e telas)."""
        return f"{self.month:02d}/{self.year}"

    @property
    def folder(self) -> str:
        """'09-2026' (usado em nomes de pasta)."""
        return f"{self.month:02d}-{self.year}"

    @classmethod
    def previous(cls, today: date | None = None) -> "Competence":
        today = today or date.today()
        if today.month == 1:
            return cls(today.year - 1, 12)
        return cls(today.year, today.month - 1)

    @classmethod
    def parse(cls, text: str) -> "Competence":
        t = (text or "").strip()
        m = re.fullmatch(r"(\d{1,2})\s*[/\-.]\s*(\d{4})", t) or re.fullmatch(r"(\d{2})(\d{4})", t)
        if m:
            return cls(int(m.group(2)), int(m.group(1)))
        m = re.fullmatch(r"(\d{4})\s*[/\-.]\s*(\d{1,2})", t)
        if m:
            return cls(int(m.group(1)), int(m.group(2)))
        raise ConfigurationError(f"Competência inválida: '{text}'. Use MM/AAAA (ex.: 09/2026).")


def parse_br_date(text: str) -> date:
    try:
        return datetime.strptime(text.strip(), "%d/%m/%Y").date()
    except ValueError as exc:
        raise ConfigurationError(f"Data inválida: '{text}'. Use DD/MM/AAAA.") from exc


def fmt_br(d: date) -> str:
    return d.strftime("%d/%m/%Y")


@dataclass(frozen=True)
class Period:
    """Período efetivamente consultado nos portais."""

    competence: Competence
    start: date
    end: date

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise ConfigurationError("Data inicial maior que a data final.")

    @property
    def start_br(self) -> str:
        return fmt_br(self.start)

    @property
    def end_br(self) -> str:
        return fmt_br(self.end)

    @classmethod
    def for_competence(cls, comp: Competence) -> "Period":
        return cls(comp, comp.start, comp.end)

    @classmethod
    def resolve(
        cls,
        competence: str | None = None,
        start: str | None = None,
        end: str | None = None,
        today: date | None = None,
    ) -> "Period":
        """Competência padrão = mês anterior; datas manuais sobrescrevem o intervalo."""
        comp = Competence.parse(competence) if competence else None
        d_start = parse_br_date(start) if start else None
        d_end = parse_br_date(end) if end else None
        if comp is None:
            comp = Competence(d_start.year, d_start.month) if d_start else Competence.previous(today)
        return cls(comp, d_start or comp.start, d_end or comp.end)
