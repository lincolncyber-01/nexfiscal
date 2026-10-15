"""Ciclo de vida do Playwright: navegador visível por padrão, um contexto por empresa."""
from __future__ import annotations

import logging
from types import TracebackType

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright

from ..config.settings import Settings
from ..exceptions import ConfigurationError

log = logging.getLogger("nexfiscal.browser")


class BrowserManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._pw: Playwright | None = None
        self._browser: Browser | None = None

    def start(self) -> "BrowserManager":
        self._pw = sync_playwright().start()
        launcher = getattr(self._pw, self.settings.browser)
        kwargs: dict = {"headless": self.settings.headless}
        if self.settings.browser_channel and self.settings.browser == "chromium":
            kwargs["channel"] = self.settings.browser_channel
        try:
            self._browser = launcher.launch(**kwargs)
        except Exception as exc:
            self.stop()
            raise ConfigurationError(
                f"Não foi possível abrir o navegador '{self.settings.browser}': {exc}. "
                f"Execute: python -m playwright install {self.settings.browser}") from exc
        log.info("Navegador %s iniciado (%s).", self.settings.browser,
                 "oculto" if self.settings.headless else "visível")
        return self

    @property
    def is_connected(self) -> bool:
        return bool(self._browser and self._browser.is_connected())

    def new_context(self) -> BrowserContext:
        """Contexto novo = sessão limpa (cookies isolados entre empresas)."""
        if not self.is_connected:
            self.stop()
            self.start()
        assert self._browser is not None
        ctx = self._browser.new_context(accept_downloads=True, locale="pt-BR", viewport={"width": 1366, "height": 850})
        ctx.set_default_timeout(self.settings.timeout_seconds * 1000)
        ctx.set_default_navigation_timeout(self.settings.timeout_seconds * 1000)
        return ctx

    def new_page(self) -> tuple[BrowserContext, Page]:
        ctx = self.new_context()
        return ctx, ctx.new_page()

    def stop(self) -> None:
        for closer in (lambda: self._browser and self._browser.close(), lambda: self._pw and self._pw.stop()):
            try:
                closer()
            except Exception:  # pragma: no cover
                pass
        self._browser = self._pw = None

    def __enter__(self) -> "BrowserManager":
        return self.start()

    def __exit__(self, et: type[BaseException] | None, ev: BaseException | None, tb: TracebackType | None) -> None:
        self.stop()
