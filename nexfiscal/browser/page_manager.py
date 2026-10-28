"""Ações de página: navegação, preenchimento (inclusive datas mascaradas), cliques, textos."""
from __future__ import annotations

import logging
import time
from datetime import date
from pathlib import Path
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from ..config.settings import Settings
from ..exceptions import NetworkError, PortalTimeoutError
from ..logs.logger import SECRETS
from ..utils import normalize_text, only_digits

log = logging.getLogger("nexfiscal.page")

_MASK_CSS = (
    "input,textarea{color:transparent!important;text-shadow:none!important;caret-color:transparent!important}"
    "input[type=password]{visibility:hidden!important}"
)
_NET_ERRORS = ("ERR_INTERNET_DISCONNECTED", "ERR_NAME_NOT_RESOLVED", "ERR_CONNECTION", "ERR_NETWORK",
               "ERR_TIMED_OUT", "ERR_ADDRESS_UNREACHABLE", "ERR_PROXY", "ERR_SSL", "ERR_EMPTY_RESPONSE")


def safe_screenshot_bytes(page: Page) -> bytes:
    """Captura com campos de formulário mascarados (credenciais nunca aparecem)."""
    handle = None
    try:
        handle = page.add_style_tag(content=_MASK_CSS)
        return page.screenshot(full_page=False)
    finally:
        if handle is not None:
            try:
                handle.evaluate("el => el.remove()")
            except PlaywrightError:
                pass


def _unwrap(target: Any) -> Any:
    """FoundElement (tem ``strategy``) -> seu locator; um Locator do Playwright passa direto."""
    return target.locator if hasattr(target, "strategy") else target


class PageManager:
    def __init__(self, page: Page, settings: Settings, control: Any = None) -> None:
        self.page, self.settings, self.control = page, settings, control

    # ----------------------------------------------------------- controle ----
    def pause(self, factor: float = 1.0) -> None:
        """Intervalo configurável entre operações (também respeita pausar/cancelar)."""
        end = time.monotonic() + self.settings.delay_between_ops * factor
        while time.monotonic() < end:
            if self.control:
                self.control.checkpoint()
            time.sleep(0.1)

    def checkpoint(self) -> None:
        if self.control:
            self.control.checkpoint()

    # ----------------------------------------------------------- navegação ----
    def goto(self, url: str) -> None:
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=self.settings.timeout_seconds * 1000)
        except PlaywrightTimeout as exc:
            raise PortalTimeoutError(f"Tempo esgotado ao abrir o portal ({url}).") from exc
        except PlaywrightError as exc:
            raise self.classify(exc) from exc
        self.settle()

    def settle(self, timeout_ms: int = 6000) -> None:
        """Aguarda a página assentar (rede ociosa); não falha se continuar ativa."""
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
            self.page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except (PlaywrightTimeout, PlaywrightError):
            pass

    @staticmethod
    def classify(exc: Exception) -> Exception:
        msg = str(exc)
        if any(code in msg for code in _NET_ERRORS):
            return NetworkError("Falha de rede ao acessar o portal (internet/portal indisponível).")
        if "Timeout" in msg:
            return PortalTimeoutError("Tempo esgotado aguardando o portal.")
        return exc

    # --------------------------------------------------------------- ações ----
    def click(self, target: Any, what: str = "") -> None:
        self.checkpoint()
        loc = _unwrap(target)
        try:
            loc.click(timeout=4000)
        except PlaywrightTimeout as exc:
            # Never replay a side-effecting click through JS after a timeout: Playwright may
            # have dispatched it already (e.g. export started just before navigation).
            log.warning("Clique expirou e não será repetido via JavaScript | ação=%s", what,
                        extra={"event": "playwright_click_timeout", "action": what})
            raise PortalTimeoutError(f"Tempo esgotado ao executar a ação: {what or 'clique'}.") from exc
        except PlaywrightError as exc:
            classified = self.classify(exc)
            log.warning("Clique Playwright falhou; evitando segundo clique via JavaScript | ação=%s | erro=%s",
                        what, exc, extra={"event": "playwright_click_failed", "action": what,
                                        "error_type": type(exc).__name__}, exc_info=True)
            if classified is exc:
                raise
            raise classified from exc
        log.debug("Clique confirmado pelo Playwright: %s", what)
        self.settle(3000)
        self.pause(0.5)

    def fill(self, target: Any, value: str, *, secret: bool = False, digits_only_compare: bool = False) -> None:
        """Preenche e confere o valor lido de volta (máscaras podem reformatar)."""
        self.checkpoint()
        loc = _unwrap(target)
        if secret:
            SECRETS.register(value)
        loc.click(timeout=4000)
        loc.fill("")
        loc.fill(value)

        def ok() -> bool:
            try:
                got = loc.input_value(timeout=1500)
            except PlaywrightError:
                return True
            if digits_only_compare:
                return only_digits(got) == only_digits(value)
            return got == value

        if not ok():  # campo com máscara/JS: digita tecla a tecla
            loc.fill("")
            loc.press_sequentially(value, delay=35)
        self.pause(0.3)

    def fill_date(self, target: Any, d: date, month_only: bool = False) -> None:
        """Escolhe o formato pelo tipo/placeholder/maxlength do campo (DD/MM/AAAA, MM/AAAA, ISO)."""
        loc = _unwrap(target)
        try:
            attrs = loc.evaluate(
                "el => ({type: el.type || '', ph: el.placeholder || '', max: el.maxLength || -1})")
        except PlaywrightError:
            attrs = {"type": "", "ph": "", "max": -1}
        ph = normalize_text(attrs["ph"])
        if attrs["type"] == "date":
            loc.fill(d.isoformat())
        elif attrs["type"] == "month":
            loc.fill(d.strftime("%Y-%m"))
        else:
            if month_only or attrs["max"] == 7 or ph.replace(" ", "") in ("mm/aaaa", "mm/yyyy"):
                text = d.strftime("%m/%Y")
            else:
                text = d.strftime("%d/%m/%Y")
            self.fill(target, text, digits_only_compare=True)
            loc.press("Tab")
        self.pause(0.3)

    # --------------------------------------------------------------- textos ----
    def body_text(self) -> str:
        """Texto visível de todos os frames, normalizado (sem acentos, minúsculo)."""
        chunks: list[str] = []
        for frame in self.page.frames:
            try:
                chunks.append(frame.evaluate("() => document.body ? document.body.innerText : ''"))
            except PlaywrightError:
                continue
        return normalize_text(" ".join(chunks))

    def contains_any(self, patterns: tuple[str, ...]) -> str | None:
        text = self.body_text()
        return next((p for p in patterns if p in text), None)

    def screenshot_on_error(self, name: str, folder: Path) -> Path | None:
        if not self.settings.save_error_screenshots:
            return None
        try:
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{name}_{int(time.time())}.png"
            path.write_bytes(safe_screenshot_bytes(self.page))
            return path
        except Exception:  # pragma: no cover
            return None
