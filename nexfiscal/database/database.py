"""Conexão SQLite (WAL) thread-safe e esquema do banco."""
from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..security import secure_path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    usuario_key TEXT NOT NULL,
    cnpj_usuario TEXT NOT NULL,
    cidade TEXT NOT NULL,
    cidade_norm TEXT NOT NULL,
    senha_enc TEXT,
    status TEXT NOT NULL DEFAULT 'PENDENTE',
    ultima_execucao TEXT,
    UNIQUE (usuario_key, cidade_norm)
);

CREATE TABLE IF NOT EXISTS portals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nome TEXT,
    url TEXT NOT NULL UNIQUE,
    perfil TEXT,
    ultimo_reconhecimento TEXT,
    assinatura TEXT
);

CREATE TABLE IF NOT EXISTS portal_cities (
    cidade_norm TEXT PRIMARY KEY,
    cidade TEXT NOT NULL,
    portal_id INTEGER NOT NULL REFERENCES portals(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS portal_memory (
    portal_id INTEGER NOT NULL REFERENCES portals(id) ON DELETE CASCADE,
    stage TEXT NOT NULL,
    key TEXT NOT NULL,
    strategies_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (portal_id, stage, key)
);

CREATE TABLE IF NOT EXISTS portal_flows (
    portal_id INTEGER NOT NULL REFERENCES portals(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    steps_json TEXT NOT NULL,
    PRIMARY KEY (portal_id, name)
);

CREATE TABLE IF NOT EXISTS portal_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    portal_id INTEGER NOT NULL REFERENCES portals(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS portal_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    portal_id INTEGER NOT NULL REFERENCES portals(id) ON DELETE CASCADE,
    stage TEXT NOT NULL,
    key TEXT NOT NULL,
    old_desc TEXT,
    new_desc TEXT,
    at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS company_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    competencia TEXT NOT NULL,
    usuario_key TEXT NOT NULL,
    cnpj_usuario TEXT NOT NULL,
    cidade TEXT NOT NULL,
    cidade_norm TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDENTE',
    etapa TEXT,
    steps_json TEXT NOT NULL DEFAULT '{}',
    xml_found INTEGER NOT NULL DEFAULT 0,
    xml_downloaded INTEGER NOT NULL DEFAULT 0,
    xml_existing INTEGER NOT NULL DEFAULT 0,
    xml_failed INTEGER NOT NULL DEFAULT 0,
    livro_ok INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    message TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE (competencia, usuario_key, cidade_norm)
);

CREATE TABLE IF NOT EXISTS executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    empresa TEXT NOT NULL,
    cidade TEXT NOT NULL,
    competencia TEXT NOT NULL,
    etapa TEXT NOT NULL,
    status TEXT NOT NULL,
    data_hora TEXT NOT NULL,
    mensagem TEXT,
    arquivo TEXT,
    erro TEXT
);
CREATE INDEX IF NOT EXISTS idx_exec_empresa ON executions (empresa, competencia);
"""


class Database:
    """Wrapper simples: uma conexão compartilhada protegida por RLock."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._init_schema()
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(self.path) + suffix)
            if candidate.exists():
                secure_path(candidate)

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        with self._lock:
            self._conn.executemany(sql, [tuple(r) for r in rows])

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchone()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self._conn.close()
