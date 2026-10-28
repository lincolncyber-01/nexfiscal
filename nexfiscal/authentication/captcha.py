"""CAPTCHA: apenas DETECTA e AGUARDA a resolução manual pelo usuário.

Este módulo NÃO resolve, contorna, ignora nem tenta burlar nenhum CAPTCHA, e não captura
nem armazena imagens dele. O navegador permanece visível até que a pessoa o resolva.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from ..exceptions import CaptchaTimeoutError, ConfigurationError

log = logging.getLogger("nexfiscal.captcha")

_PROBE_JS = r"""
() => {
  const vis = el => { if (!el) return false; const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 1 && r.height > 1 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const any = sel => Array.from(document.querySelectorAll(sel)).some(vis);
  const val = sel => Array.from(document.querySelectorAll(sel)).some(e => (e.value || '').length > 20);
  const inputs = Array.from(document.querySelectorAll(
    'input[name*="captcha" i],input[id*="captcha" i],input[placeholder*="captcha" i],input[name*="codigoseguranca" i]'))
    .filter(e => vis(e) && !['hidden','checkbox'].includes(e.type));
  return {
    recaptcha: any('iframe[src*="recaptcha" i],.g-recaptcha') ,
    hcaptcha: any('iframe[src*="hcaptcha" i],.h-captcha'),
    turnstile: any('iframe[src*="turnstile" i],.cf-turnstile'),
    image: any('img[src*="captcha" i],img[id*="captcha" i],img[class*="captcha" i],canvas[id*="captcha" i]') || inputs.length > 0,
    tokenRecaptcha: val('textarea[name="g-recaptcha-response"],input[name="g-recaptcha-response"]'),
    tokenHcaptcha: val('textarea[name="h-captcha-response"],input[name="h-captcha-response"]'),
    tokenTurnstile: val('input[name="cf-turnstile-response"]'),
    inputFilled: inputs.length > 0 && inputs.every(e => (e.value || '').trim().length >= 3),
  };
}
"""


class CaptchaDetector:
    def __init__(self, page: Page) -> None:
        self.page = page

    def _probe(self) -> dict:
        merged: dict = {}
        for frame in self.page.frames:
            try:
                res = frame.evaluate(_PROBE_JS)
            except PlaywrightError:
                continue
            for k, v in res.items():
                merged[k] = merged.get(k, False) or v
        return merged

    def detect(self) -> str | None:
        p = self._probe()
        for kind in ("recaptcha", "hcaptcha", "turnstile", "image"):
            if p.get(kind):
                return kind
        return None

    def is_solved(self) -> bool:
        p = self._probe()
        kind = next((k for k in ("recaptcha", "hcaptcha", "turnstile", "image") if p.get(k)), None)
        if kind is None:
            return True  # widget sumiu (resolvido ou página liberada)
        if kind == "recaptcha":
            return bool(p.get("tokenRecaptcha"))
        if kind == "hcaptcha":
            return bool(p.get("tokenHcaptcha"))
        if kind == "turnstile":
            return bool(p.get("tokenTurnstile"))
        return bool(p.get("inputFilled"))


class ManualCaptchaGate:
    def __init__(self, page: Page, timeout_seconds: int, headless: bool,
                 control: object | None = None, notify: Callable[[str], None] | None = None) -> None:
        self.page, self.timeout, self.headless = page, timeout_seconds, headless
        self.control, self.notify = control, notify
        self.detector = CaptchaDetector(page)

    def wait_if_present(self, empresa: str, cidade: str) -> bool:
        """Se houver CAPTCHA: pausa e aguarda a resolução manual. Devolve True se houve CAPTCHA."""
        if self.control:
            clear_confirmation = getattr(self.control, "clear_captcha_confirmation", None)
            clear_progress = getattr(self.control, "clear_guided_progress", None)
            if callable(clear_confirmation):
                clear_confirmation()
            if callable(clear_progress):
                clear_progress()
        kind = self.detector.detect()
        if kind is None:
            return False
        if self.headless:
            raise ConfigurationError(
                "CAPTCHA detectado, mas o navegador está oculto. Desative o modo headless nas configurações.")
        msg = (f"CAPTCHA DETECTADO — Empresa: {empresa} | Cidade: {cidade} | "
               "Resolva o CAPTCHA no navegador. [AGUARDANDO RESOLUÇÃO...]")
        log.info("CAPTCHA detectado (%s). Aguardando resolução manual.", kind)
        if self.notify:
            self.notify(msg)
        try:
            self.page.bring_to_front()
        except PlaywrightError:
            pass
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if self.control:
                self.control.checkpoint()  # type: ignore[attr-defined]
                consume_confirmation = getattr(self.control, "consume_captcha_confirmation", None)
                if callable(consume_confirmation) and consume_confirmation():
                    log.info("Operador confirmou que resolveu o CAPTCHA manualmente.")
                    return True
                consume_progress = getattr(self.control, "consume_guided_progress", None)
                if callable(consume_progress) and consume_progress():
                    log.info("Ação guiada produziu arquivo validado; liberando a espera bloqueada do CAPTCHA.")
                    return True
            if self.detector.is_solved():
                log.info("CAPTCHA resolvido.")
                return True
            self.page.wait_for_timeout(1000)
        raise CaptchaTimeoutError(f"CAPTCHA não foi resolvido em {self.timeout}s.")
