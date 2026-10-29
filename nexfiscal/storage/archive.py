"""Resource limits shared by ZIP extraction and fiscal-book validation."""
from __future__ import annotations

import zipfile

from ..exceptions import InvalidFileError

MAX_ZIP_ENTRIES = 10_000
MAX_ZIP_MEMBER_BYTES = 64 * 1024 * 1024
MAX_ZIP_TOTAL_BYTES = 512 * 1024 * 1024
MAX_ZIP_RATIO = 1_000


def validate_archive(archive: zipfile.ZipFile) -> None:
    """Reject oversized/encrypted archives before reading or extracting any member."""
    members = archive.infolist()
    if len(members) > MAX_ZIP_ENTRIES:
        raise InvalidFileError("ZIP excede o limite de 10.000 entradas.")
    total = 0
    for member in members:
        if member.is_dir():
            continue
        total += member.file_size
        if member.flag_bits & 1:
            raise InvalidFileError("ZIP criptografado não é suportado; solicite uma exportação sem senha.")
        if member.file_size > MAX_ZIP_MEMBER_BYTES or total > MAX_ZIP_TOTAL_BYTES:
            raise InvalidFileError("ZIP excede o limite de tamanho descompactado (64 MiB/arquivo, 512 MiB/ZIP).")
        if member.file_size / max(member.compress_size, 1) > MAX_ZIP_RATIO:
            raise InvalidFileError("ZIP recusado por taxa de compressão excessiva.")
