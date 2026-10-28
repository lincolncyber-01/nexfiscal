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
        self.dest_dir.mkdir(parents=True, exist_ok=True)
        target = self.dest_dir / f"{self.stem}.{ext}"
        # Preserve the previous deliverable until the new file has been atomically persisted.
        # If persistence fails, the old valid file remains available for the operator.
        move_atomic(captured.path, target)
        for old in self.dest_dir.glob(f"{self.stem}.*"):
            if old == target:
                continue
            try:
                old.unlink(missing_ok=True)
            except OSError:
                log.warning("Livro Fiscal novo foi salvo, mas não foi possível remover arquivo antigo: %s",
                            old, exc_info=True)
        log.info("Livro Fiscal salvo e confirmado: %s | bytes=%d", target, target.stat().st_size,
                 extra={"event": "fiscal_book_saved", "destination": str(target),
                        "bytes_written": target.stat().st_size, "format": ext})
        return target
