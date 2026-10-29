from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexfiscal.app import AppContext
from nexfiscal.config.settings import Settings, SettingsStore
from nexfiscal.database.database import Database
from nexfiscal.program_reset import reset_program


TABLES = (
    "companies", "portals", "portal_cities", "portal_memory", "portal_flows",
    "portal_notes", "portal_changes", "company_runs", "executions",
)


def _make_context(tmp_path: Path) -> AppContext:
    db_path = tmp_path / "nexfiscal.db"
    settings_path = tmp_path / "settings.json"
    settings = Settings(
        output_root=str(tmp_path / "custom-output"),
        competence="10/2026",
        timeout_seconds=120,
        database_path=str(db_path),
    )
    store = SettingsStore(settings_path)
    store.save(settings)
    db = Database(db_path)
    return AppContext(settings=settings, store=store, db=db, cipher=None)


def _seed_database(db: Database) -> None:
    with db.transaction():
        db.execute(
            "INSERT INTO companies(usuario_key, cnpj_usuario, razao_social, cidade, cidade_norm, senha_enc) "
            "VALUES ('12345678901','12345678901','Empresa Teste','Cidade Teste','cidade teste','encrypted')"
        )
        db.execute("INSERT INTO portals(url, nome) VALUES ('https://portal.example','Portal de teste')")
        pid = db.query_one("SELECT id FROM portals")["id"]
        db.execute(
            "INSERT INTO portal_cities(cidade_norm, cidade, portal_id) VALUES ('cidade teste','Cidade Teste',?)",
            (pid,),
        )
        db.execute(
            "INSERT INTO portal_memory(portal_id, stage, key, strategies_json, updated_at) "
            "VALUES (?, 'login', 'botao', '[]', '2026-10-01T00:00:00')",
            (pid,),
        )
        db.execute(
            "INSERT INTO portal_flows(portal_id, name, steps_json) VALUES (?, 'fluxo', '[]')", (pid,)
        )
        db.execute(
            "INSERT INTO portal_notes(portal_id, text, created_at) VALUES (?, 'nota', '2026-10-01')", (pid,)
        )
        db.execute(
            "INSERT INTO portal_changes(portal_id, stage, key, old_desc, new_desc, at) "
            "VALUES (?, 'login', 'botao', 'antigo', 'novo', '2026-10-01')",
            (pid,),
        )
        db.execute(
            "INSERT INTO company_runs(competencia, usuario_key, cnpj_usuario, cidade, cidade_norm, updated_at) "
            "VALUES ('10/2026','12345678901','12345678901','Cidade Teste','cidade teste','2026-10-01')"
        )
        db.execute(
            "INSERT INTO executions(empresa, cidade, competencia, etapa, status, data_hora) "
            "VALUES ('Empresa Teste','Cidade Teste','10/2026','xml','OK','2026-10-01')"
        )


def test_reset_clears_local_state_restores_defaults_and_preserves_fiscal_files(tmp_path):
    ctx = _make_context(tmp_path)
    _seed_database(ctx.db)
    fiscal_root = Path(ctx.settings.output_root)
    fiscal_root.mkdir()
    xml = fiscal_root / "nota.xml"
    livro = fiscal_root / "Livro Fiscal.pdf"
    report = fiscal_root / "relatorio.xlsx"
    for file in (xml, livro, report):
        file.write_bytes(b"preserve")

    try:
        reset_program(ctx)

        for table in TABLES:
            assert ctx.db.query_one(f"SELECT COUNT(*) AS n FROM {table}")["n"] == 0
        assert ctx.settings == Settings(database_path=str(ctx.db.path))
        stored = Settings.model_validate(json.loads(ctx.store.path.read_text("utf-8")))
        assert stored == ctx.settings
        assert all(file.read_bytes() == b"preserve" for file in (xml, livro, report))
    finally:
        ctx.db.close()


def test_reset_database_rollback_when_settings_cannot_be_saved(tmp_path, monkeypatch):
    ctx = _make_context(tmp_path)
    _seed_database(ctx.db)
    before = ctx.store.path.read_text("utf-8")
    original_save = ctx.store.save

    def fail_save(_settings):
        raise OSError("simulated settings disk failure")

    monkeypatch.setattr(ctx.store, "save", fail_save)
    try:
        with pytest.raises(OSError, match="simulated"):
            reset_program(ctx)

        assert ctx.db.query_one("SELECT COUNT(*) AS n FROM companies")["n"] == 1
        assert ctx.db.query_one("SELECT COUNT(*) AS n FROM portals")["n"] == 1
        assert ctx.store.path.read_text("utf-8") == before
        assert ctx.settings.output_root == str(tmp_path / "custom-output")
    finally:
        monkeypatch.setattr(ctx.store, "save", original_save)
        ctx.db.close()
