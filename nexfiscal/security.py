"""Criptografia de credenciais e proteção de arquivos locais.

Chave Fernet, por ordem de prioridade:
1. variável de ambiente ``NEXFISCAL_KEY``;
2. cofre do sistema operacional (pacote opcional ``keyring``);
3. arquivo ``secret.key`` na pasta de dados (permissão 600 em POSIX).
"""
from __future__ import annotations

import contextlib
import logging
import os
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .exceptions import ConfigurationError

log = logging.getLogger("nexfiscal.security")
_KEYRING_SERVICE = "NexFiscal"
_KEYRING_USER = "credential-key"


def secure_path(path: Path, directory: bool = False) -> None:
    """Restringe o acesso ao dono (no-op no Windows)."""
    with contextlib.suppress(OSError, NotImplementedError):
        os.chmod(path, stat.S_IRWXU if directory else stat.S_IRUSR | stat.S_IWUSR)


def _load_or_create_key(data_dir: Path) -> bytes:
    env = os.environ.get("NEXFISCAL_KEY")
    if env:
        return env.encode()
    try:  # cofre do SO, se disponível
        import keyring  # type: ignore

        stored = keyring.get_password(_KEYRING_SERVICE, _KEYRING_USER)
        if stored:
            return stored.encode()
        key = Fernet.generate_key()
        keyring.set_password(_KEYRING_SERVICE, _KEYRING_USER, key.decode())
        return key
    except Exception:  # keyring ausente ou sem backend
        pass
    key_file = data_dir / "secret.key"
    if key_file.exists():
        return key_file.read_bytes().strip()
    data_dir.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    key_file.write_bytes(key)
    secure_path(key_file)
    return key


class CredentialCipher:
    """Criptografa/decriptografa senhas armazenadas no SQLite."""

    def __init__(self, data_dir: Path, key: bytes | None = None) -> None:
        try:
            self._fernet = Fernet(key or _load_or_create_key(data_dir))
        except (ValueError, TypeError) as exc:
            raise ConfigurationError("Chave de criptografia inválida (NEXFISCAL_KEY).") from exc

    def encrypt(self, plain: str) -> str:
        return self._fernet.encrypt(plain.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise ConfigurationError(
                "Não foi possível decifrar a senha armazenada (chave diferente?). "
                "Reimporte a planilha."
            ) from exc
