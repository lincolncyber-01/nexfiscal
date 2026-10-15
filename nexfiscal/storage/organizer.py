"""Estrutura de pastas: ROOT/Cidade/CNPJ/MM-AAAA/{XML,Livro_Fiscal}."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..dates import Competence
from ..utils import city_folder_name, cnpj_folder_name
from .file_manager import ensure_dir


@dataclass(frozen=True)
class CompanyPaths:
    competence_dir: Path
    xml_dir: Path
    livro_dir: Path


class OutputLayout:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser()

    def paths(self, city: str, cnpj_usuario: str, comp: Competence, create: bool = True) -> CompanyPaths:
        base = self.root / city_folder_name(city) / cnpj_folder_name(cnpj_usuario) / comp.folder
        paths = CompanyPaths(base, base / "XML", base / "Livro_Fiscal")
        if create:
            ensure_dir(paths.xml_dir)
            ensure_dir(paths.livro_dir)
        return paths

    @staticmethod
    def write_manifest(paths: CompanyPaths, data: dict) -> Path:
        """Resumo do processamento (nunca contém senha)."""
        payload = {"gerado_em": datetime.now().isoformat(timespec="seconds"), **data}
        target = paths.competence_dir / "_resumo.json"
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
        return target
