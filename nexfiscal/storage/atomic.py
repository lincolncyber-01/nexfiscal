"""Publish local files only after a complete, flushed write in the same directory."""
from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger("nexfiscal.storage")


@contextmanager
def atomic_output(path: Path) -> Iterator[Path]:
    """Keep the previous destination intact if serialization or publication fails."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=path.suffix, dir=path.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        yield temporary
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            log.warning("Não foi possível remover arquivo temporário: %s", temporary, exc_info=True)


def atomic_write_text(path: Path, payload: str) -> None:
    with atomic_output(path) as temporary:
        temporary.write_text(payload, encoding="utf-8", newline="\n")
