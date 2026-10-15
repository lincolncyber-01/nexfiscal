"""Armazenamento e validação do arquivo do Livro Fiscal."""
from __future__ import annotations

import logging
from pathlib import Path

from ..browser.download_manager import CapturedFile
from ..dates import Competence
from ..exceptions import InvalidFileError
from ..storage.file_manager import move_atomic, validate_deliverable
from ..utils import cnpj_folder_name

log = logging.getLogger("nexfiscal.livro")
ALLOWED = ("pdf", "xml", "xls", "xlsx", "csv", "zip")


class LivroFileStore:
    def __init__(self, dest_dir: Path, cnpj_usuario: str, comp: Competence, policy: str = "skip_valid") -> None:
        self.dest_dir, self.policy = dest_dir, policy
        self.stem = f"Livro_Fiscal_{cnpj_folder_name(cnpj_usuario)}_{comp.folder}"

    def find_existing(self) -> Path | None:
        """Livro já baixado e íntegro (evita novo download)."""
        if self.policy == "overwrite":
            return None
        for f in sorted(self.dest_dir.glob(f"{self.stem}.*")):
            try:
                validate_deliverable(f, ALLOWED)
                return f
            except InvalidFileError:
                log.warning("Livro Fiscal existente está incompleto/corrompido: %s (será baixado de novo)", f.name)
        return None

    def store(self, captured: CapturedFile) -> Path:
        ext = validate_deliverable(captured.path, ALLOWED)  # levanta InvalidFileError
        for old in self.dest_dir.glob(f"{self.stem}.*"):
            old.unlink(missing_ok=True)
        target = self.dest_dir / f"{self.stem}.{ext}"
        move_atomic(captured.path, target)
        log.info("Livro Fiscal salvo: %s", target.name)
        return target
