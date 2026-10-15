"""Consulta de NFS-e por período."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from ..browser.page_manager import PageManager
from ..config import portal_config as pc
from ..dates import Period
from ..exceptions import ElementNotFoundError, SessionExpiredError
from ..portal.navigation import PortalNavigator
from ..portal.selector_engine import SelectorEngine

log = logging.getLogger("nexfiscal.nfse")


@dataclass
class SearchOutcome:
    empty: bool          # consulta executada, sem notas no período


class NfseConsult:
    def __init__(self, engine: SelectorEngine, pm: PageManager, timeout: float) -> None:
        self.engine, self.pm, self.timeout = engine, pm, timeout
        self.nav = PortalNavigator(engine, pm)

    def search(self, period: Period) -> SearchOutcome:
        self.nav.open_section("nfse", [pc.NFSE_MENU, pc.NFSE_CONSULT],
                              ready=[pc.NFSE_DATE_START, pc.NFSE_COMPETENCE])
        self.nav.fill_period(pc.NFSE_DATE_START, pc.NFSE_DATE_END, pc.NFSE_COMPETENCE, period)
        button = self.engine.find(pc.NFSE_SEARCH, timeout=8)
        self.pm.click(button, "pesquisar NFS-e")
        return self._wait_results()

    def _wait_results(self) -> SearchOutcome:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            self.pm.checkpoint()
            if self.pm.contains_any(pc.SESSION_EXPIRED_PATTERNS):
                raise SessionExpiredError("A sessão do portal expirou durante a consulta.")
            if self.pm.contains_any(pc.EMPTY_RESULT_PATTERNS):
                log.info("Nenhuma NFS-e encontrada no período.")
                return SearchOutcome(empty=True)
            if self.engine.find_all(pc.NFSE_XML, timeout=0.5, required=False):
                return SearchOutcome(empty=False)
            self.page_wait()
        rows = 0
        for frame in self.pm.page.frames:
            try:
                rows += frame.locator("table tbody tr:visible").count()
            except Exception:
                continue
        if rows > 0:
            raise ElementNotFoundError(
                "A consulta retornou resultados, mas o link de download do XML não foi reconhecido. "
                "O portal pode ter sido alterado.", stage="nfse")
        log.warning("Consulta sem resultados identificáveis; considerando período sem notas.")
        return SearchOutcome(empty=True)

    def page_wait(self) -> None:
        self.pm.page.wait_for_timeout(700)
