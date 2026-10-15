"""Hierarquia de exceções do NexFiscal.

``retryable`` indica se o erro é temporário (vale a pena tentar de novo).
"""
from __future__ import annotations


class NexFiscalError(Exception):
    """Erro base. Mensagens nunca devem conter senhas."""

    retryable: bool = False

    def __init__(self, message: str, *, stage: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.stage = stage

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


class ConfigurationError(NexFiscalError):
    """Configuração inválida ou ausente."""


class SpreadsheetError(NexFiscalError):
    """Planilha inexistente, ilegível ou fora do formato."""


class PortalNotRegisteredError(NexFiscalError):
    """Cidade sem portal cadastrado."""


class CancelledError(NexFiscalError):
    """Processamento cancelado pelo usuário."""


# --- erros temporários (elegíveis a nova tentativa) -------------------------
class TemporaryError(NexFiscalError):
    retryable = True


class NetworkError(TemporaryError):
    """Internet indisponível, portal fora do ar, DNS, conexão recusada."""


class PortalTimeoutError(TemporaryError):
    """Timeout aguardando página/elemento/download."""


class SessionExpiredError(TemporaryError):
    """Sessão expirou: é preciso autenticar de novo."""


class DownloadError(TemporaryError):
    """Download não iniciado, interrompido ou incompleto."""


# --- erros permanentes -------------------------------------------------------
class LoginError(NexFiscalError):
    """Credenciais recusadas pelo portal."""


class CaptchaTimeoutError(NexFiscalError):
    """CAPTCHA não foi resolvido no tempo máximo."""


class ElementNotFoundError(NexFiscalError):
    """Elemento não localizado por nenhuma estratégia (portal alterado?)."""


class PortalChangedError(NexFiscalError):
    """Estrutura do portal mudou de forma que impede o fluxo."""


class StageUnavailableError(NexFiscalError):
    """Módulo inexistente/indisponível para a empresa (ex.: Livro Fiscal)."""


class InvalidFileError(NexFiscalError):
    """Arquivo baixado é inválido/incompleto."""


class FilePermissionError(NexFiscalError):
    """Sem permissão para gravar na pasta de saída."""
