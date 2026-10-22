"""Funções utilitárias de texto, CNPJ e nomes de arquivo."""
from __future__ import annotations

import re
import unicodedata

_INVALID_FS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )


def normalize_text(text: str | None) -> str:
    """Minúsculas, sem acentos, espaços colapsados. Camel case é separado."""
    if not text:
        return ""
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(text))
    text = strip_accents(text).lower()
    text = re.sub(r"[_\-\s]+", " ", text)
    return text.strip()


def normalize_city(city: str | None) -> str:
    if not city:
        return ""
    return re.sub(r"\s+", " ", strip_accents(str(city)).lower()).strip()


def only_digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def is_valid_cnpj(value: str | None) -> bool:
    """Valida CNPJ numérico (14 dígitos + dígitos verificadores)."""
    digits = only_digits(value)
    if len(digits) != 14 or len(set(digits)) == 1:
        return False

    def dv(base: str) -> str:
        weights = [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
        w = weights[-len(base):]
        total = sum(int(d) * p for d, p in zip(base, w))
        rest = total % 11
        return "0" if rest < 2 else str(11 - rest)

    d1 = dv(digits[:12])
    d2 = dv(digits[:12] + d1)
    return digits[12:] == d1 + d2


def format_cnpj(value: str | None) -> str:
    digits = only_digits(value)
    if len(digits) == 14:
        return f"{digits[:2]}.{digits[2:5]}.{digits[5:8]}/{digits[8:12]}-{digits[12:]}"
    return (value or "").strip()


def user_key(value: str | None) -> str:
    """Chave canônica de usuário: só dígitos se for numérico, senão minúsculas."""
    raw = (value or "").strip()
    digits = only_digits(raw)
    if digits and re.fullmatch(r"[\d.\-/\s]+", raw):
        return digits
    return raw.lower()


def sanitize_name(name: str, replacement: str = "-") -> str:
    """Remove caracteres inválidos em nomes de arquivo/pasta (Windows/Linux/macOS)."""
    cleaned = _INVALID_FS_CHARS.sub(replacement, name or "").strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    reserved = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                *(f"lpt{i}" for i in range(1, 10)}
                }
    if cleaned.lower() in reserved:
        cleaned = f"_{cleaned}"
    return cleaned or "_"


def city_folder_name(city: str) -> str:
    """'Vitória da Conquista' -> 'Vitoria_da_Conquista'."""
    base = re.sub(r"\s+", "_", strip_accents(city).strip())
    return sanitize_name(base, "-")


def company_folder_name(razao_social: str) -> str:
    """Preserva a razão social legível, removendo apenas caracteres inválidos no sistema de arquivos."""
    return sanitize_name(re.sub(r"\s+", " ", (razao_social or "").strip()), "-")


def cnpj_folder_name(cnpj_usuario: str) -> str:
    """'12.345.678/0001-01' -> '12.345.678-0001-01' (compatibilidade legada)."""
    return sanitize_name(format_cnpj(cnpj_usuario).replace("/", "-"), "-")


def xpath_literal(value: str) -> str:
    """Escapa texto para uso como literal XPath."""
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    parts = value.split("'")
    return "concat(" + ", \"'\", ".join(f"'{p}'" for p in parts) + ")"
