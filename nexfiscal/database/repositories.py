"""Repositórios SQLite: empresas, portais, memória, execuções."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from urllib.parse import urlparse

from ..exceptions import ConfigurationError
from ..security import CredentialCipher
from ..utils import normalize_city
from .database import Database
from .models import CompanyRow, CompanyRun, Portal, RunStatus


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# --------------------------------------------------------------- empresas ---
class CompanyRepository:
    def __init__(self, db: Database, cipher: CredentialCipher) -> None:
        self.db, self.cipher = db, cipher

    def upsert_rows(self, rows: list[CompanyRow]) -> None:
        with self.db.transaction():
            for r in rows:
                self.db.execute(
                    """INSERT INTO companies (usuario_key, cnpj_usuario, cidade, cidade_norm, senha_enc)
                       VALUES (?,?,?,?,?)
                       ON CONFLICT(usuario_key, cidade_norm) DO UPDATE SET
                         cnpj_usuario=excluded.cnpj_usuario, cidade=excluded.cidade,
                         senha_enc=excluded.senha_enc""",
                    (r.usuario_key, r.cnpj_usuario, r.cidade, r.cidade_norm,
                     self.cipher.encrypt(r.password)),
                )

    def load_rows(self, cities_norm: set[str] | None = None) -> list[CompanyRow]:
        """Reconstrói linhas (com senha decifrada) a partir do banco — usado na retomada."""
        out: list[CompanyRow] = []
        for i, row in enumerate(self.db.query("SELECT * FROM companies ORDER BY id"), start=1):
            if not row["senha_enc"]:
                continue
            if cities_norm and row["cidade_norm"] not in cities_norm:
                continue
            out.append(CompanyRow(row_number=i, cnpj_usuario=row["cnpj_usuario"],
                                  senha=self.cipher.decrypt(row["senha_enc"]), cidade=row["cidade"]))
        return out

    def set_status(self, usuario_key: str, cidade_norm: str, status: RunStatus) -> None:
        self.db.execute(
            "UPDATE companies SET status=?, ultima_execucao=? WHERE usuario_key=? AND cidade_norm=?",
            (status.value, now_iso(), usuario_key, cidade_norm),
        )

    def count(self) -> int:
        return self.db.query_one("SELECT COUNT(*) c FROM companies")["c"]

    def clear_credentials(self) -> int:
        """Remove todas as senhas armazenadas (mantém o histórico de execuções)."""
        cur = self.db.execute("UPDATE companies SET senha_enc=NULL WHERE senha_enc IS NOT NULL")
        return cur.rowcount

    def delete_all(self) -> None:
        self.db.execute("DELETE FROM companies")


# ----------------------------------------------------------------- portais ---
def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise ConfigurationError("URL do portal vazia.")
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ConfigurationError(f"URL inválida: {url}")
    return url.rstrip("/") if not parsed.path.strip("/") and not parsed.query else url


class PortalRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def _build(self, row: sqlite3.Row) -> Portal:
        cities = [r["cidade"] for r in self.db.query(
            "SELECT cidade FROM portal_cities WHERE portal_id=? ORDER BY cidade", (row["id"],))]
        return Portal(row["id"], row["url"], row["nome"], row["perfil"],
                      row["ultimo_reconhecimento"], row["assinatura"], cities)

    def register(self, cities: list[str], url: str, nome: str | None = None) -> Portal:
        url = normalize_url(url)
        with self.db.transaction():
            self.db.execute("INSERT OR IGNORE INTO portals (url, nome) VALUES (?,?)", (url, nome))
            row = self.db.query_one("SELECT * FROM portals WHERE url=?", (url,))
            for c in cities:
                self.db.execute(
                    """INSERT INTO portal_cities (cidade_norm, cidade, portal_id) VALUES (?,?,?)
                       ON CONFLICT(cidade_norm) DO UPDATE SET portal_id=excluded.portal_id,
                       cidade=excluded.cidade""",
                    (normalize_city(c), c.strip(), row["id"]),
                )
        return self._build(row)

    def get_by_city(self, city: str) -> Portal | None:
        row = self.db.query_one(
            """SELECT p.* FROM portals p JOIN portal_cities c ON c.portal_id=p.id
               WHERE c.cidade_norm=?""", (normalize_city(city),))
        return self._build(row) if row else None

    def get(self, portal_id: int) -> Portal | None:
        row = self.db.query_one("SELECT * FROM portals WHERE id=?", (portal_id,))
        return self._build(row) if row else None

    def list_all(self) -> list[Portal]:
        return [self._build(r) for r in self.db.query("SELECT * FROM portals ORDER BY url")]

    def known_cities(self) -> set[str]:
        return {r["cidade_norm"] for r in self.db.query("SELECT cidade_norm FROM portal_cities")}

    def remove_city(self, city: str) -> None:
        self.db.execute("DELETE FROM portal_cities WHERE cidade_norm=?", (normalize_city(city),))

    def delete(self, portal_id: int) -> None:
        self.db.execute("DELETE FROM portals WHERE id=?", (portal_id,))

    def update_recognition(self, portal_id: int, signature: str | None, perfil: str | None) -> None:
        self.db.execute(
            "UPDATE portals SET assinatura=COALESCE(?, assinatura), perfil=COALESCE(?, perfil), "
            "ultimo_reconhecimento=? WHERE id=?", (signature, perfil, now_iso(), portal_id))


# ----------------------------------------------------------------- memória ---
class MemoryRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def load_selectors(self, portal_id: int) -> dict[tuple[str, str], list[dict]]:
        out: dict[tuple[str, str], list[dict]] = {}
        for r in self.db.query("SELECT stage, key, strategies_json FROM portal_memory WHERE portal_id=?",
                               (portal_id,)):
            out[(r["stage"], r["key"])] = json.loads(r["strategies_json"])
        return out

    def save_selector(self, portal_id: int, stage: str, key: str, strategies: list[dict]) -> None:
        self.db.execute(
            """INSERT INTO portal_memory (portal_id, stage, key, strategies_json, updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(portal_id, stage, key) DO UPDATE SET
                 strategies_json=excluded.strategies_json, updated_at=excluded.updated_at""",
            (portal_id, stage, key, json.dumps(strategies, ensure_ascii=False), now_iso()))

    def load_flows(self, portal_id: int) -> dict[str, list[str]]:
        return {r["name"]: json.loads(r["steps_json"]) for r in self.db.query(
            "SELECT name, steps_json FROM portal_flows WHERE portal_id=?", (portal_id,))}

    def save_flow(self, portal_id: int, name: str, steps: list[str]) -> None:
        self.db.execute(
            """INSERT INTO portal_flows (portal_id, name, steps_json) VALUES (?,?,?)
               ON CONFLICT(portal_id, name) DO UPDATE SET steps_json=excluded.steps_json""",
            (portal_id, name, json.dumps(steps)))

    def load_notes(self, portal_id: int) -> list[str]:
        return [r["text"] for r in self.db.query(
            "SELECT text FROM portal_notes WHERE portal_id=? ORDER BY id", (portal_id,))]

    def add_note(self, portal_id: int, text: str) -> None:
        self.db.execute("INSERT INTO portal_notes (portal_id, text, created_at) VALUES (?,?,?)",
                        (portal_id, text, now_iso()))

    def add_change(self, portal_id: int, stage: str, key: str, old: str, new: str) -> None:
        self.db.execute(
            "INSERT INTO portal_changes (portal_id, stage, key, old_desc, new_desc, at) VALUES (?,?,?,?,?,?)",
            (portal_id, stage, key, old, new, now_iso()))

    def list_changes(self, portal_id: int | None = None, limit: int = 200) -> list[sqlite3.Row]:
        if portal_id is None:
            return self.db.query("SELECT * FROM portal_changes ORDER BY id DESC LIMIT ?", (limit,))
        return self.db.query("SELECT * FROM portal_changes WHERE portal_id=? ORDER BY id DESC LIMIT ?",
                             (portal_id, limit))

    def clear(self, portal_id: int | None = None) -> None:
        """Apaga o conhecimento aprendido (seletores, fluxos, notas, histórico)."""
        with self.db.transaction():
            for table in ("portal_memory", "portal_flows", "portal_notes", "portal_changes"):
                if portal_id is None:
                    self.db.execute(f"DELETE FROM {table}")
                else:
                    self.db.execute(f"DELETE FROM {table} WHERE portal_id=?", (portal_id,))
            if portal_id is None:
                self.db.execute("UPDATE portals SET assinatura=NULL, perfil=NULL, ultimo_reconhecimento=NULL")
            else:
                self.db.execute(
                    "UPDATE portals SET assinatura=NULL, perfil=NULL, ultimo_reconhecimento=NULL WHERE id=?",
                    (portal_id,))


# ---------------------------------------------------------------- execuções ---
class ExecutionRepository:
    """Histórico (executions) + estado atual por empresa/competência (company_runs)."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # histórico detalhado
    def log(self, run_id: str, empresa: str, cidade: str, competencia: str, etapa: str,
            status: str, mensagem: str = "", arquivo: str = "", erro: str = "") -> None:
        self.db.execute(
            """INSERT INTO executions (run_id, empresa, cidade, competencia, etapa, status,
                                       data_hora, mensagem, arquivo, erro)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (run_id, empresa, cidade, competencia, etapa, status, now_iso(), mensagem, arquivo, erro))

    # estado por empresa
    def _to_run(self, r: sqlite3.Row) -> CompanyRun:
        return CompanyRun(
            r["id"], r["competencia"], r["usuario_key"], r["cnpj_usuario"], r["cidade"],
            r["cidade_norm"], RunStatus(r["status"]), r["etapa"], json.loads(r["steps_json"]),
            r["xml_found"], r["xml_downloaded"], r["xml_existing"], r["xml_failed"],
            bool(r["livro_ok"]), r["attempts"], r["message"], r["updated_at"])

    def get_or_create(self, competencia: str, row: CompanyRow) -> CompanyRun:
        self.db.execute(
            """INSERT OR IGNORE INTO company_runs
               (competencia, usuario_key, cnpj_usuario, cidade, cidade_norm, updated_at)
               VALUES (?,?,?,?,?,?)""",
            (competencia, row.usuario_key, row.cnpj_usuario, row.cidade, row.cidade_norm, now_iso()))
        return self._to_run(self.db.query_one(
            "SELECT * FROM company_runs WHERE competencia=? AND usuario_key=? AND cidade_norm=?",
            (competencia, row.usuario_key, row.cidade_norm)))

    def update(self, run: CompanyRun) -> None:
        self.db.execute(
            """UPDATE company_runs SET status=?, etapa=?, steps_json=?, xml_found=?, xml_downloaded=?,
               xml_existing=?, xml_failed=?, livro_ok=?, attempts=?, message=?, updated_at=? WHERE id=?""",
            (run.status.value, run.etapa, json.dumps(run.steps), run.xml_found, run.xml_downloaded,
             run.xml_existing, run.xml_failed, int(run.livro_ok), run.attempts, run.message,
             now_iso(), run.id))

    def list_for(self, competencia: str, keys: list[tuple[str, str]] | None = None) -> list[CompanyRun]:
        rows = self.db.query("SELECT * FROM company_runs WHERE competencia=? ORDER BY cidade_norm, id",
                             (competencia,))
        runs = [self._to_run(r) for r in rows]
        if keys is not None:
            wanted = set(keys)
            runs = [r for r in runs if (r.usuario_key, r.cidade_norm) in wanted]
        return runs

    def reset_interrupted(self) -> int:
        """Execuções que ficaram EM_EXECUCAO/AGUARDANDO_CAPTCHA (programa fechado) voltam a PENDENTE."""
        cur = self.db.execute(
            "UPDATE company_runs SET status='PENDENTE' WHERE status IN ('EM_EXECUCAO','AGUARDANDO_CAPTCHA')")
        return cur.rowcount
