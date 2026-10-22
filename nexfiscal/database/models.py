"""Modelos de domínio (enums, linha da planilha e registros do banco)."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from pydantic import BaseModel, Field, SecretStr

from ..utils import format_cnpj, normalize_city, only_digits, user_key


class RunStatus(str, Enum):
    PENDENTE = "PENDENTE"
    EM_EXECUCAO = "EM_EXECUCAO"
    AGUARDANDO_CAPTCHA = "AGUARDANDO_CAPTCHA"
    CONCLUIDO = "CONCLUIDO"
    CONCLUIDO_COM_PENDENCIA = "CONCLUIDO_COM_PENDENCIA"
    ERRO = "ERRO"

    @property
    def is_final_ok(self) -> bool:
        return self is RunStatus.CONCLUIDO


class Stage(str, Enum):
    LOGIN = "login"
    CAPTCHA = "captcha"
    NFSE = "nfse"
    XML = "xml"
    LIVRO = "livro_fiscal"
    ORGANIZACAO = "organizacao"

    @property
    def title(self) -> str:
        return {
            "login": "Login", "captcha": "CAPTCHA", "nfse": "NFS-e", "xml": "XML",
            "livro_fiscal": "Livro Fiscal", "organizacao": "Organização",
        }[self.value]


class StepStatus(str, Enum):
    PENDENTE = "PENDENTE"
    EM_ANDAMENTO = "EM_ANDAMENTO"
    OK = "OK"
    PULADO = "PULADO"
    PENDENCIA = "PENDENCIA"
    ERRO = "ERRO"


class CompanyRow(BaseModel):
    """Linha da planilha; razão social é obrigatória para organizar os arquivos."""

    row_number: int = 0
    cnpj_usuario: str = ""
    razao_social: str = ""
    senha: SecretStr = Field(default=SecretStr(""), repr=False)
    cidade: str = ""

    @property
    def usuario_key(self) -> str:
        return user_key(self.cnpj_usuario)

    @property
    def cidade_norm(self) -> str:
        return normalize_city(self.cidade)

    @property
    def cnpj_digits(self) -> str:
        return only_digits(self.cnpj_usuario)

    @property
    def display(self) -> str:
        return format_cnpj(self.cnpj_usuario)

    @property
    def password(self) -> str:
        return self.senha.get_secret_value()


@dataclass
class Portal:
    id: int
    url: str
    nome: str | None = None
    perfil: str | None = None
    ultimo_reconhecimento: str | None = None
    assinatura: str | None = None
    cidades: list[str] = field(default_factory=list)


@dataclass
class CompanyRun:
    id: int
    competencia: str
    usuario_key: str
    cnpj_usuario: str
    cidade: str
    cidade_norm: str
    status: RunStatus
    etapa: str | None
    steps: dict[str, str]
    xml_found: int
    xml_downloaded: int
    xml_existing: int
    xml_failed: int
    livro_ok: bool
    attempts: int
    message: str | None
    updated_at: str

    def step(self, stage: Stage) -> StepStatus:
        return StepStatus(self.steps.get(stage.value, StepStatus.PENDENTE.value))
