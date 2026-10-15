"""Vocabulário de reconhecimento dos portais.

Todos os termos são comparados *normalizados* (minúsculas, sem acentos).
Não há URLs nem seletores fixos aqui: o conhecimento de cada portal é aprendido
e guardado na memória persistente (SQLite).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PStage(str, Enum):
    """Estágios do portal usados como chave na memória."""

    LOGIN = "login"
    NFSE = "nfse"
    LIVRO = "livro_fiscal"


@dataclass(frozen=True)
class ElementSpec:
    stage: PStage
    key: str                         # nome lógico (ex.: "campo_usuario")
    kind: str                        # input | password | clickable | select
    keywords: tuple[str, ...]        # em ordem de especificidade
    exclude: tuple[str, ...] = ()
    role: str | None = None          # dica para a heurística DOM final
    allow_hidden: bool = False       # itens de menu podem estar ocultos
    description: str = ""

    @property
    def label(self) -> str:
        return self.description or self.key


# ------------------------------------------------------------------ LOGIN ---
USER_FIELD = ElementSpec(
    PStage.LOGIN, "campo_usuario", "input",
    ("cnpj do contribuinte", "cnpj/cpf", "cpf/cnpj", "cnpj", "codigo do usuario",
     "inscricao", "usuario", "login"),
    exclude=("senha", "password", "captcha", "seguranca", "lembrar"),
    role="user", description="campo CNPJ/usuário",
)
PASSWORD_FIELD = ElementSpec(
    PStage.LOGIN, "campo_senha", "password", ("senha", "password"),
    role="password", description="campo senha",
)
LOGIN_BUTTON = ElementSpec(
    PStage.LOGIN, "botao_login", "clickable",
    ("entrar", "acessar", "login", "logar", "autenticar", "confirmar", "ok"),
    exclude=("esqueci", "cadastr", "recuperar", "voltar", "limpar", "novo"),
    role="submit", description="botão de login",
)
LOGIN_ENTRY = ElementSpec(
    PStage.LOGIN, "link_acesso", "clickable",
    ("acesso do contribuinte", "area do contribuinte", "acesso restrito",
     "acesso ao sistema", "prestador", "contribuinte", "entrar", "login"),
    exclude=("esqueci", "cadastr", "recuperar"), allow_hidden=True,
    description="link para a tela de login",
)

# ------------------------------------------------------------------- NFS-e --
NFSE_MENU = ElementSpec(
    PStage.NFSE, "menu_nfse", "clickable",
    ("nfs-e", "nfse", "nota fiscal de servico", "notas fiscais", "nota fiscal"),
    exclude=("emitir", "emissao de nota", "cancelar", "substituir"),
    allow_hidden=True, description="menu NFS-e",
)
NFSE_CONSULT = ElementSpec(
    PStage.NFSE, "menu_consulta", "clickable",
    ("consulta nfs-e", "consultar nfs-e", "consulta de nfs-e", "consultar notas",
     "consulta de notas", "notas emitidas", "notas de servico", "consultar", "consulta"),
    exclude=("livro", "guia", "dam", "cadastro"),
    allow_hidden=True, description="consulta de notas",
)
NFSE_DATE_START = ElementSpec(
    PStage.NFSE, "data_inicio", "input",
    ("data inicial", "periodo inicial", "data inicio", "competencia inicial",
     "emissao inicial", "dt inicial", "inicio"),
    role="date_start", description="data inicial",
)
NFSE_DATE_END = ElementSpec(
    PStage.NFSE, "data_fim", "input",
    ("data final", "periodo final", "data fim", "competencia final",
     "emissao final", "dt final", "data termino", "fim", "ate"),
    role="date_end", description="data final",
)
NFSE_COMPETENCE = ElementSpec(
    PStage.NFSE, "campo_competencia", "input",
    ("competencia", "mes de referencia", "mes/ano", "periodo"),
    role="competence", description="campo de competência (MM/AAAA)",
)
NFSE_SEARCH = ElementSpec(
    PStage.NFSE, "botao_pesquisar", "clickable",
    ("pesquisar", "consultar", "buscar", "filtrar", "localizar", "listar"),
    exclude=("limpar", "novo", "nova consulta"), description="botão pesquisar",
)
NFSE_XML = ElementSpec(
    PStage.NFSE, "download_xml", "clickable",
    ("download xml", "baixar xml", "exportar xml", "xml abrasf", "exportar nfs-e",
     "baixar nfs-e", "xml"),
    exclude=("pdf", "danfse", "imprimir", "visualizar"),
    allow_hidden=True, description="download do XML",
)
NFSE_NEXT = ElementSpec(
    PStage.NFSE, "proxima_pagina", "clickable",
    ("proxima", "proximo", "next", "seguinte", "»", ">"),
    exclude=("ultima", "last"), description="próxima página de resultados",
)

# ------------------------------------------------------------ LIVRO FISCAL --
LIVRO_MENU = ElementSpec(
    PStage.LIVRO, "menu_livro", "clickable",
    ("livro fiscal de servicos", "livro de prestacao de servicos", "livro de servicos",
     "livro fiscal", "livro iss", "registro de servicos", "livro"),
    allow_hidden=True, description="menu Livro Fiscal",
)
LIVRO_SUBMENU = ElementSpec(
    PStage.LIVRO, "submenu_livro", "clickable",
    ("gerar livro", "consultar livro", "emitir livro", "gerar", "consultar"),
    exclude=("guia", "dam"), allow_hidden=True, description="opção dentro do Livro Fiscal",
)
LIVRO_DATE_START = ElementSpec(
    PStage.LIVRO, "data_inicio", "input",
    ("data inicial", "periodo inicial", "data inicio", "competencia inicial", "inicio"),
    role="date_start", description="data inicial do livro",
)
LIVRO_DATE_END = ElementSpec(
    PStage.LIVRO, "data_fim", "input",
    ("data final", "periodo final", "data fim", "competencia final", "fim", "ate"),
    role="date_end", description="data final do livro",
)
LIVRO_COMPETENCE = ElementSpec(
    PStage.LIVRO, "campo_competencia", "input",
    ("competencia", "mes de referencia", "mes/ano", "periodo"),
    role="competence", description="competência do livro (MM/AAAA)",
)
LIVRO_GENERATE = ElementSpec(
    PStage.LIVRO, "botao_gerar", "clickable",
    ("gerar", "pesquisar", "consultar", "visualizar", "emitir", "buscar"),
    exclude=("limpar", "voltar"), description="botão gerar livro",
)
LIVRO_DOWNLOAD = ElementSpec(
    PStage.LIVRO, "botao_download", "clickable",
    ("baixar", "download", "exportar", "salvar", "pdf", "excel", "xlsx", "xls", "csv", "imprimir"),
    exclude=("voltar", "limpar"), allow_hidden=True, description="download do livro",
)

# Textos usados em detecções de página (normalizados)
LOGIN_ERROR_PATTERNS = (
    "senha invalida", "senha incorreta", "usuario ou senha", "login invalido",
    "credenciais invalidas", "dados de acesso invalidos", "cnpj ou senha",
    "usuario nao encontrado", "acesso negado", "usuario e/ou senha",
)
CAPTCHA_ERROR_PATTERNS = (
    "captcha invalido", "captcha incorreto", "codigo de seguranca invalido",
    "codigo de verificacao invalido", "captcha expirado",
)
LOGOUT_KEYWORDS = ("sair", "logout", "encerrar sessao", "desconectar", "sair do sistema")
EMPTY_RESULT_PATTERNS = (
    "nenhum registro", "nenhuma nota", "nao foram encontrad", "nao ha registro",
    "sem resultados", "nenhum resultado", "nao existem", "nenhum dado",
)
SESSION_EXPIRED_PATTERNS = (
    "sessao expirada", "sessao expirou", "faca login novamente", "tempo de sessao",
)
UNAVAILABLE_PATTERNS = ("nao ha livro", "livro indisponivel", "nao disponivel", "sem movimento")
