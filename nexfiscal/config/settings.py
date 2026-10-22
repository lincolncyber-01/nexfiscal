"""Configurações persistentes do NexFiscal (JSON na pasta de dados)."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from ..security import secure_path


def data_dir() -> Path:
    """Pasta de dados (banco, chave, logs, temporários). Sobrescreva com NEXFISCAL_HOME."""
    base = Path(os.environ.get("NEXFISCAL_HOME") or Path.home() / ".nexfiscal")
    base.mkdir(parents=True, exist_ok=True)
    secure_path(base, directory=True)
    return base


class Settings(BaseModel):
    output_root: str = str(Path.home() / "AutomacaoFiscal")
    competence: str | None = None          # "MM/AAAA"; None = mês anterior
    start_date: str | None = None          # "DD/MM/AAAA" (opcional)
    end_date: str | None = None
    delay_between_ops: float = Field(0.6, ge=0, le=30)      # segundos entre ações
    timeout_seconds: int = Field(30, ge=2, le=600)          # espera por elementos/páginas
    download_timeout_seconds: int = Field(60, ge=5, le=900)
    captcha_timeout_seconds: int = Field(600, ge=5, le=7200)
    max_retries: int = Field(3, ge=1, le=10)
    existing_files: Literal["skip_valid", "overwrite"] = "skip_valid"
    browser: Literal["chromium", "firefox", "webkit"] = "chromium"
    browser_channel: str | None = None     # "chrome" / "msedge" para usar o navegador instalado
    headless: bool = False                 # CAPTCHA exige navegador visível
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    save_error_screenshots: bool = False   # capturas (com campos mascarados) em erros
    guided_learning: bool = True           # não descobre automaticamente elementos novos de um portal/etapa
    assisted_recognition: bool = False     # pede um clique do usuário quando não reconhece um elemento
    assist_timeout_seconds: int = Field(180, ge=5, le=3600)
    enable_ocr: bool = False               # fallback experimental (requer pytesseract)
    max_result_pages: int = Field(50, ge=1, le=500)
    database_path: str | None = None

    @property
    def db_file(self) -> Path:
        return Path(self.database_path) if self.database_path else data_dir() / "nexfiscal.db"

    @property
    def tmp_dir(self) -> Path:
        p = data_dir() / "tmp"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def log_dir(self) -> Path:
        p = data_dir() / "logs"
        p.mkdir(parents=True, exist_ok=True)
        return p


class SettingsStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or data_dir() / "settings.json"

    def load(self) -> Settings:
        if self.path.exists():
            try:
                return Settings.model_validate(json.loads(self.path.read_text("utf-8")))
            except (ValueError, OSError):
                pass
        return Settings()

    def save(self, settings: Settings) -> None:
        self.path.write_text(settings.model_dump_json(indent=2), "utf-8")
