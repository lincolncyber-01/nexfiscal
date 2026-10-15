"""Modo de reconhecimento: aprende a tela de login de um portal desconhecido.

Portal novo  -> MODO DE RECONHECIMENTO: localiza campo de usuário, senha e botão (sem preencher nada),
                detecta CAPTCHA e grava tudo na memória. NFS-e e Livro Fiscal são aprendidos quando
                cada tela é alcançada (exigem login).
Portal conhecido -> nada é reaprendido: o motor usa a memória e só descobre o que mudou.
"""
from __future__ import annotations

import logging

from ..authentication.captcha import CaptchaDetector
from ..config import portal_config as pc
from ..exceptions import ElementNotFoundError
from .portal_memory import PortalProfile, describe_strategy
from .selector_engine import SelectorEngine

log = logging.getLogger("nexfiscal.recognizer")


class PortalRecognizer:
    def __init__(self, profile: PortalProfile | None) -> None:
        self.profile = profile

    @property
    def mode(self) -> str:
        return "conhecido" if self.profile and self.profile.is_known_stage(pc.PStage.LOGIN.value) else "reconhecimento"

    def probe_login(self, engine: SelectorEngine, captcha: CaptchaDetector) -> dict[str, str]:
        """Reconhece a tela de login. Retorna {elemento: descrição da estratégia}."""
        if self.mode != "reconhecimento":
            return {}
        log.info("MODO DE RECONHECIMENTO — portal ainda desconhecido: %s", self.profile.url if self.profile else "")
        found: dict[str, str] = {}
        for spec in (pc.USER_FIELD, pc.PASSWORD_FIELD, pc.LOGIN_BUTTON):
            try:
                el = engine.find(spec, timeout=6)
                found[spec.label] = describe_strategy(el.strategy)
            except ElementNotFoundError:
                log.warning("Reconhecimento: %s não localizado na tela inicial.", spec.label)
        kind = captcha.detect()
        if kind and self.profile:
            self.profile.add_note(f"Tela de login exibe CAPTCHA do tipo '{kind}' (resolução manual).")
        log.info("Reconhecimento do login concluído: %s", "; ".join(f"{k} → {v}" for k, v in found.items()) or "nada")
        return found
