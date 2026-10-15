"""Acesso ao Livro Fiscal: período, geração e download."""
from __future__ import annotations

import logging
import time
from pathlib import Path

from playwright.sync_api import Page

from ..browser.download_manager import DownloadManager
from ..browser.page_manager import PageManager
from ..config import portal_config as pc
from ..config.settings import Settings
from ..dates import Period
from ..exceptions import (DownloadError, ElementNotFoundError, InvalidFileError, SessionExpiredError,
                          StageUnavailableError)
from ..portal.navigation import PortalNavigator
from ..portal.selector_engine import SelectorEngine
from .downloader import LivroFileStore

log = logging.getLogger("nexfiscal.livro")


class FiscalBookHandler:
    def __init__(self, page: Page, engine: SelectorEngine, pm: PageManager, dm: DownloadManager,
                 settings: Settings) -> None:
        self.page, self.engine, self.pm, self.dm, self.settings = page, engine, pm, dm, settings
        self.nav = PortalNavigator(engine, pm)

    def run(self, period: Period, store: LivroFileStore) -> tuple[Path, bool]:
        """Devolve (arquivo, já_existia)."""
        existing = store.find_existing()
        if existing:
            log.info("Livro Fiscal já existente: %s", existing.name)
            return existing, True

        menu = self.engine.find(pc.LIVRO_MENU, timeout=8, required=False)
        if menu is None:
            raise StageUnavailableError("Módulo Livro Fiscal não encontrado no portal para esta empresa.",
                                        stage="livro_fiscal")
        self.nav.open_section("livro", [pc.LIVRO_MENU, pc.LIVRO_SUBMENU],
                              ready=[pc.LIVRO_DATE_START, pc.LIVRO_COMPETENCE])
        self.nav.fill_period(pc.LIVRO_DATE_START, pc.LIVRO_DATE_END, pc.LIVRO_COMPETENCE, period)

        # 1) o próprio botão "Gerar" pode já devolver o arquivo
        generate = self.engine.find(pc.LIVRO_GENERATE, timeout=6, required=False)
        if generate is not None:
            try:
                cap = self.dm.capture(self.page, lambda: self.pm.click(generate, "gerar livro"), timeout=8)
                return store.store(cap), False
            except DownloadError:
                pass  # não houve download direto: o livro foi gerado na tela
            except InvalidFileError:
                raise
        self._ensure_available()

        # 2) botão/link de download após a geração
        deadline = time.monotonic() + self.settings.timeout_seconds
        button = None
        while time.monotonic() < deadline and button is None:
            self.pm.checkpoint()
            self._ensure_available()
            button = self.engine.find(pc.LIVRO_DOWNLOAD, timeout=1.5, required=False)
        if button is None:
            raise ElementNotFoundError("Botão de download do Livro Fiscal não encontrado.", stage="livro_fiscal")
        cap = self.dm.capture(self.page, lambda: self.pm.click(button, "baixar livro"))
        return store.store(cap), False

    def _ensure_available(self) -> None:
        if self.pm.contains_any(pc.SESSION_EXPIRED_PATTERNS):
            raise SessionExpiredError("A sessão do portal expirou.", stage="livro_fiscal")
        hit = self.pm.contains_any(pc.UNAVAILABLE_PATTERNS)
        if hit:
            raise StageUnavailableError("O portal informou que o Livro Fiscal não está disponível no período.",
                                        stage="livro_fiscal")
