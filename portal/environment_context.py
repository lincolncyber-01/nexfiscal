"""Reconhecimento de ambiente (portal) a partir da seleção manual de uma empresa.

O usuário escolhe manualmente UMA empresa no início da execução. A partir da cidade
dessa empresa, o portal/ambiente é identificado (:class:`PortalRegistry`) e sua memória
é carregada (:class:`PortalMemoryManager`, persistida em SQLite). O resultado — portal +
perfil de seletores aprendidos — é guardado em cache de sessão (:class:`EnvironmentContext`)
e reutilizado automaticamente por todas as etapas seguintes da mesma execução (login,
NFS-e, Livro Fiscal), sem precisar reidentificar o ambiente a cada etapa.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from ..database.models import CompanyRow, Portal
from ..exceptions import PortalNotRegisteredError
from ..memory.portal_memory_manager import PortalMemoryManager
from .portal_memory import PortalProfile
from .portal_registry import PortalRegistry

log = logging.getLogger("nexfiscal.environment")


@dataclass
class EnvironmentContext:
    """Ambiente (portal) identificado para a empresa selecionada manualmente."""

    company: CompanyRow
    portal: Portal
    profile: PortalProfile

    @property
    def portal_url(self) -> str:
        return self.portal.url

    @property
    def cidade(self) -> str:
        return self.company.cidade


class EnvironmentRecognizer:
    """Identifica o ambiente/portal da empresa escolhida manualmente e cacheia o resultado.

    O cache tem duas camadas:
    1. Sessão (``self._cache``, em memória): evita reidentificar o mesmo portal mais de
       uma vez dentro da mesma execução, mesmo que várias etapas consultem o ambiente.
    2. Persistida (banco, via :class:`PortalMemoryManager`): o que foi aprendido sobre o
       portal (seletores, fluxos) sobrevive entre execuções e é carregado automaticamente
       na próxima vez que esse portal for reconhecido.
    """

    def __init__(self, registry: PortalRegistry, memory: PortalMemoryManager) -> None:
        self.registry, self.memory = registry, memory
        self._cache: dict[int, EnvironmentContext] = {}

    def recognize(self, company: CompanyRow) -> EnvironmentContext:
        """Reconhece o ambiente da empresa selecionada manualmente pelo usuário.

        Levanta :class:`PortalNotRegisteredError` se a cidade da empresa não tiver
        portal cadastrado.
        """
        portal = self.registry.resolve(company.cidade)
        if portal is None:
            raise PortalNotRegisteredError(
                f"Portal não cadastrado para a cidade '{company.cidade}' da empresa selecionada "
                f"({company.display}). Cadastre o portal antes de continuar.")
        cached = self._cache.get(portal.id)
        if cached is not None:
            log.info("Ambiente de '%s' já identificado nesta execução (reaproveitado do cache): %s",
                     company.cidade, portal.url)
            return EnvironmentContext(company=company, portal=portal, profile=cached.profile)
        profile = self.memory.load_profile(portal)
        ctx = EnvironmentContext(company=company, portal=portal, profile=profile)
        self._cache[portal.id] = ctx
        log.info("Ambiente identificado — Empresa: %s | Cidade: %s | Portal: %s | Memória: %s",
                 company.display, company.cidade, portal.url,
                 "encontrada" if profile.selectors else "não encontrada (modo de reconhecimento)")
        return ctx

    def save(self, ctx: EnvironmentContext) -> None:
        """Persiste o que foi aprendido durante a execução para reuso em execuções futuras."""
        self.memory.save_profile(ctx.profile)
