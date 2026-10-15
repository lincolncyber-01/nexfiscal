"""Operações seguras de arquivo e verificação de integridade básica."""
from __future__ import annotations

import hashlib
import os
import shutil
import zipfile
from pathlib import Path

from lxml import etree

from ..exceptions import FilePermissionError, InvalidFileError

XML_PARSER_KW = dict(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def ensure_dir(path: Path) -> Path:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".nexfiscal_write_test"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as exc:
        raise FilePermissionError(f"Sem permissão para gravar em '{path}': {exc.strerror}") from exc
    return path


def move_atomic(src: Path, dst: Path) -> Path:
    """Move para o destino final sem deixar arquivo parcial (copia p/ .part e renomeia)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_name(dst.name + ".part")
    try:
        shutil.copyfile(src, part)
        os.replace(part, dst)
    except OSError as exc:
        part.unlink(missing_ok=True)
        raise FilePermissionError(f"Não foi possível gravar '{dst}': {exc.strerror}") from exc
    finally:
        src.unlink(missing_ok=True)
    return dst


def detect_kind(head: bytes) -> str:
    """Tipo pelo conteúdo: pdf | zip | xls | xml | html | text | empty | unknown."""
    if not head:
        return "empty"
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        return "zip"
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return "xls"
    text = head[:512].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if text.startswith(b"<!doctype html") or text.startswith(b"<html") or b"<body" in text:
        return "html"
    if text.startswith(b"<?xml") or text.startswith(b"<"):
        return "xml"
    try:
        head.decode("utf-8")
        return "text"
    except UnicodeDecodeError:
        try:
            head.decode("latin-1")
            return "text"
        except UnicodeDecodeError:
            return "unknown"


def read_head(path: Path, n: int = 1024) -> bytes:
    with path.open("rb") as fh:
        return fh.read(n)


def xml_is_wellformed(data: bytes) -> tuple[bool, str]:
    try:
        etree.fromstring(data, etree.XMLParser(**XML_PARSER_KW))
        return True, ""
    except etree.XMLSyntaxError as exc:
        return False, f"XML malformado/incompleto: {exc}"


def validate_deliverable(path: Path, allowed: tuple[str, ...] = ("pdf", "xml", "xls", "xlsx", "csv")) -> str:
    """Valida o Livro Fiscal baixado. Retorna a extensão normalizada ou levanta InvalidFileError."""
    if not path.exists() or path.stat().st_size == 0:
        raise InvalidFileError("Arquivo vazio ou inexistente.")
    kind = detect_kind(read_head(path))
    if kind == "html":
        raise InvalidFileError("O arquivo baixado é uma página HTML (provável erro ou sessão expirada).")
    if kind == "pdf":
        ext = "pdf"
        tail = path.read_bytes()[-2048:]
        if b"%%EOF" not in tail:
            raise InvalidFileError("PDF incompleto (marcador de fim ausente).")
    elif kind == "xls":
        ext = "xls"
    elif kind == "zip":
        if not zipfile.is_zipfile(path):
            raise InvalidFileError("Arquivo ZIP/XLSX corrompido.")
        with zipfile.ZipFile(path) as zf:
            if zf.testzip() is not None:
                raise InvalidFileError("Arquivo ZIP/XLSX corrompido.")
            ext = "xlsx" if any(n.startswith("xl/") for n in zf.namelist()) else "zip"
    elif kind == "xml":
        ok, why = xml_is_wellformed(path.read_bytes())
        if not ok:
            raise InvalidFileError(why)
        ext = "xml"
    elif kind == "text":
        ext = "csv"
    else:
        raise InvalidFileError("Formato de arquivo não reconhecido.")
    if ext not in allowed:
        raise InvalidFileError(f"Formato '{ext}' não esperado para este arquivo.")
    return ext
