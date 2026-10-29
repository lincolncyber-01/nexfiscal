"""Estrutura fixa: ROOT/Cidade/Razão Social/{XMLs e Livro Fiscal}."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..config.settings import data_dir
from ..dates import Competence
from ..utils import city_folder_name, company_folder_name
from .file_manager import ensure_dir
from .atomic import atomic_write_text


@dataclass(frozen=True)
class CompanyPaths:
    competence_dir: Path
    xml_dir: Path
    livro_dir: Path


class OutputLayout:
    """Aplica a mesma hierarquia para todos os portais; somente root é configurável."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser()

    def paths(self, city: str, razao_social: str, comp: Competence, create: bool = True) -> CompanyPaths:
        # A competência é mantida na assinatura por compatibilidade com os chamadores,
        # mas não cria subpastas: a estrutura exigida é apenas Cidade/Razão Social.
        base = self.root / city_folder_name(city) / company_folder_name(razao_social)
        paths = CompanyPaths(base, base, base)
        if create:
            ensure_dir(base)
        return paths

    @staticmethod
    def write_manifest(paths: CompanyPaths, data: dict) -> Path:
        """Salva o resumo fora da pasta fiscal para manter nela apenas os arquivos baixados."""
        payload = {"gerado_em": datetime.now().isoformat(timespec="seconds"), **data}
        folder = data_dir() / "logs" / "manifests"
        folder.mkdir(parents=True, exist_ok=True)
        city = city_folder_name(str(data.get("cidade") or "cidade"))
        company = company_folder_name(str(data.get("razao_social") or data.get("empresa") or "empresa"))
        competence = city_folder_name(str(data.get("competencia") or "sem-competencia"))
        target = folder / f"{city}--{company}--{competence}.json"
        atomic_write_text(target, json.dumps(payload, ensure_ascii=False, indent=2))
        return target
