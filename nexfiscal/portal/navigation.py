"""Navegação por menus e preenchimento do período, com memória de fluxo."""
from __future__ import annotations

import logging

from ..browser.page_manager import PageManager
from ..config.portal_config import ElementSpec
from ..dates import Period
from ..exceptions import ElementNotFoundError
from .selector_engine import SelectorEngine

log = logging.getLogger("nexfiscal.nav")


class PortalNavigator:
    def __init__(self, engine: SelectorEngine, pm: PageManager) -> None:
        self.engine, self.pm = engine, pm

    def open_section(self, flow_name: str, steps: list[ElementSpec], ready: list[ElementSpec],
                     first_timeout: float = 8.0) -> list[str]:
        """Clica nos itens de menu (opcionais) até que os campos de ``ready`` apareçam.

        O fluxo percorrido é memorizado e reutilizado nas próximas execuções.
        """
        profile = self.engine.profile
        known = profile.flows.get(flow_name, []) if profile else []
        chosen = [s for s in steps if s.key in known] or steps
        clicked: list[str] = []
        for i, spec in enumerate(chosen):
            el = self.engine.find(spec, timeout=first_timeout if i == 0 else 3.0, required=False)
            if el is None:
                continue
            self.pm.click(el, spec.label)
            clicked.append(spec.key)
            if any(self.engine.find(r, timeout=1.5, required=False) for r in ready):
                break
        if profile and clicked:
            profile.set_flow(flow_name, clicked)
        return clicked

    def fill_period(self, start_spec: ElementSpec, end_spec: ElementSpec, comp_spec: ElementSpec,
                    period: Period) -> None:
        start = self.engine.find(start_spec, timeout=4, required=False)
        end = self.engine.find(end_spec, timeout=2, required=False) if start else None
        if start and end:
            self.pm.fill_date(start, period.start)
            self.pm.fill_date(end, period.end)
            log.info("Período informado: %s a %s", period.start_br, period.end_br)
            return
        comp = self.engine.find(comp_spec, timeout=3, required=False)
        if comp is None:
            raise ElementNotFoundError(
                f"Campos de período não encontrados ({start_spec.label}). O portal pode ter sido alterado.",
                stage=start_spec.stage.value)
        self.pm.fill_date(comp, period.start, month_only=True)
        log.info("Competência informada: %s", period.competence.label)
