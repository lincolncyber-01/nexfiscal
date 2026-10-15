"""Relatório final do lote (geral e por cidade)."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..database.models import CompanyRun, RunStatus
from ..database.repositories import ExecutionRepository


@dataclass
class CityReport:
    city: str
    empresas: int = 0
    concluidas: int = 0
    pendencias: int = 0
    erros: int = 0


@dataclass
class Report:
    competencia: str
    empresas: int = 0
    concluidas: int = 0
    pendencias: int = 0
    erros: int = 0
    xml_found: int = 0
    xml_downloaded: int = 0
    xml_existing: int = 0
    xml_failed: int = 0
    livros_ok: int = 0
    livros_falhas: int = 0
    cities: list[CityReport] = field(default_factory=list)
    runs: list[CompanyRun] = field(default_factory=list)

    def to_text(self) -> str:
        line = "=" * 45
        out = [line, "             RELATÓRIO FINAL", line, "", f"Competência: {self.competencia}", "",
               f"Empresas processadas: {self.empresas}", f"Concluídas: {self.concluidas}",
               f"Com pendência: {self.pendencias}", f"Com erro: {self.erros}", "",
               f"XMLs encontrados: {self.xml_found}", f"XMLs baixados: {self.xml_downloaded}",
               f"XMLs já existentes: {self.xml_existing}", f"Falhas XML: {self.xml_failed}", "",
               "Livros Fiscais:", f"Gerados: {self.livros_ok}", f"Falhas: {self.livros_falhas}", ""]
        for c in self.cities:
            out += [c.city.upper(), f"{c.empresas} empresas", f"{c.concluidas} concluídas"]
            if c.pendencias:
                out.append(f"{c.pendencias} pendência(s)")
            out += [f"{c.erros} erro(s)", ""]
        return "\n".join(out)


class ReportGenerator:
    def __init__(self, execs: ExecutionRepository) -> None:
        self.execs = execs

    def build(self, competencia: str, keys: list[tuple[str, str]] | None = None) -> Report:
        runs = self.execs.list_for(competencia, keys)
        rep = Report(competencia=competencia, runs=runs, empresas=len(runs))
        by_city: dict[str, CityReport] = {}
        for r in runs:
            c = by_city.setdefault(r.cidade_norm, CityReport(r.cidade))
            c.empresas += 1
            if r.status is RunStatus.CONCLUIDO:
                rep.concluidas += 1
                c.concluidas += 1
            elif r.status is RunStatus.CONCLUIDO_COM_PENDENCIA:
                rep.pendencias += 1
                c.pendencias += 1
            elif r.status is RunStatus.ERRO:
                rep.erros += 1
                c.erros += 1
            rep.xml_found += r.xml_found
            rep.xml_downloaded += r.xml_downloaded
            rep.xml_existing += r.xml_existing
            rep.xml_failed += r.xml_failed
            if r.livro_ok:
                rep.livros_ok += 1
            elif r.status in (RunStatus.ERRO, RunStatus.CONCLUIDO_COM_PENDENCIA):
                rep.livros_falhas += 1
        rep.cities = list(by_city.values())
        return rep
