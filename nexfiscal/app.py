"""Contexto compartilhado (configurações, banco, criptografia) para GUI e CLI."""
from __future__ import annotations

from dataclasses import dataclass

from .config.settings import Settings, SettingsStore, data_dir
from .database.database import Database
from .logs.logger import setup_logging
from .security import CredentialCipher


@dataclass
class AppContext:
    settings: Settings
    store: SettingsStore
    db: Database
    cipher: CredentialCipher

    def save_settings(self) -> None:
        self.store.save(self.settings)


def bootstrap() -> AppContext:
    store = SettingsStore()
    settings = store.load()
    setup_logging(settings.log_dir, settings.log_level)
    cipher = CredentialCipher(data_dir())
    return AppContext(settings, store, Database(settings.db_file), cipher)
