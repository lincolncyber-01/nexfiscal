"""Login nos portais usando o motor adaptativo + CAPTCHA manual."""
from __future__ import annotations

import logging
import re
import time

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from ..browser.page_manager import PageManager
from ..config import portal_config as pc
from ..config.settings import Settings
from ..database.models import Stage, StepStatus
from ..events import CompanyReporter
from ..exceptions import ElementNotFoundError, LoginError, PortalTimeoutError
from ..portal.portal_recognizer import PortalRecognizer
from ..portal.selector_engine import SelectorEngine, kw_quality
from .captcha import ManualCaptchaGate

log = logging.getLogger("nexfiscal.login")


class LoginHandler:
    def __init__(self, page: Page, engine: SelectorEngine, pm: PageManager, settings: Settings,
                 reporter: CompanyReporter, control: object | None = None) -> None:
        self.page, self.engine, self.pm, self.settings = page, engine, pm, settings
        self.reporter, self.control = reporter, control
        self.gate = ManualCaptchaGate(page, settings.captcha_timeout_seconds, settings.headless,
                                      control, notify=reporter.captcha)
        self._captcha_seen = False
        self._label, self._city = "", ""
        self.recognizer = PortalRecognizer(engine.profile)

    # ------------------------------------------------------------ estado ----
    def _password_visible(self) -> bool:
        for frame in self.page.frames:
            try:
                if frame.locator("input[type=password]:visible").count():
                    return True
            except PlaywrightError:
                continue
        return False

    def _has_logout(self) -> bool:
        for info in self.engine.snapshot():
            if info["tag"] in ("a", "button", "input", "span") and info["visible"]:
                if any(kw_quality(info["text"] or info["title"], kw) >= 2 for kw in pc.LOGOUT_KEYWORDS):
                    return True
        return False

    def is_logged_in(self, login_url: str | None = None) -> bool:
        if self._password_visible():
            return False
        return self._has_logout() or (login_url is not None and self.page.url != login_url)

    def looks_logged_out(self) -> bool:
        return self._password_visible()

    # ------------------------------------------------------------- fluxo ----
    def login(self, url: str, user: str, password: str, company_label: str, city: str) -> str:
        """Executa o login. Retorna a URL inicial pós-login."""
        self._label, self._city, self._captcha_seen = company_label, city, False
        self.reporter.stage(Stage.LOGIN, StepStatus.EM_ANDAMENTO, "Abrindo portal")
        self.pm.goto(url)
        user_field = self.engine.find(pc.USER_FIELD, timeout=4, required=False)
        if user_field is None:  # alguns portais exigem clicar em "Acesso do contribuinte"
            entry = self.engine.find(pc.LOGIN_ENTRY, timeout=3, required=False)
            if entry is not None:
                self.pm.click(entry, "link de acesso")
        self._remember_signature()
        self.recognizer.probe_login(self.engine, self.gate.detector)

        numeric_user = bool(re.fullmatch(r"[\d.\-/\s]+", user.strip()))
        login_url = self.page.url
        for attempt in range(1, 4):
            self.pm.checkpoint()
            self._fill_credentials(user, password, numeric_user)
            self._wait_captcha()
            if self.is_logged_in(login_url):  # o usuário já enviou o formulário
                break
            if self._password_visible():
                if self._captcha_seen:
                    # REGRA CRÍTICA DE SEGURANÇA: depois que o CAPTCHA aparece e é
                    # resolvido, a automação NUNCA clica no botão de acesso (nem por
                    # clique, nem por Enter). Só um humano pode concluir o login.
                    self._wait_human_submit(login_url)
                else:
                    self._submit()
            outcome = self._wait_outcome(login_url)
            if outcome == "ok":
                break
            if outcome == "captcha":
                log.info("O portal pediu o CAPTCHA novamente (tentativa %d/3).", attempt)
                continue
            raise LoginError("Login recusado pelo portal: usuário ou senha incorretos.", stage="login")
        else:
            raise LoginError("Não foi possível concluir o login após 3 tentativas.", stage="login")

        self.reporter.stage(Stage.CAPTCHA, StepStatus.OK if self._captcha_seen else StepStatus.PULADO,
                            "Resolvido manualmente" if self._captcha_seen else "Sem CAPTCHA")
        self.reporter.stage(Stage.LOGIN, StepStatus.OK, "Login realizado")
        self.pm.settle()
        return self.page.url

    def _wait_captcha(self) -> None:
        if self.gate.wait_if_present(self._label, self._city):
            self._captcha_seen = True
            self.reporter.stage(Stage.CAPTCHA, StepStatus.OK, "Resolvido manualmente")

    def _remember_signature(self) -> None:
        profile = self.engine.profile
        if not profile:
            return
        sig = self.engine.signature()
        if profile.signature and profile.signature != sig:
            log.warning("Assinatura da interface de login mudou (%s -> %s). Reavaliando seletores.",
                        profile.signature, sig)
        if profile.signature != sig:
            profile.signature = sig
            profile.dirty = True

    def _fill_credentials(self, user: str, password: str, numeric_user: bool) -> None:
        uf = self.engine.find(pc.USER_FIELD)
        self.pm.fill(uf, user, digits_only_compare=numeric_user)
        pf = self.engine.find(pc.PASSWORD_FIELD)
        self.pm.fill(pf, password, secret=True)

    def _submit(self) -> None:
        try:
            btn = self.engine.find(pc.LOGIN_BUTTON, timeout=6)
            self.pm.click(btn, "botão de login")
        except ElementNotFoundError:
            self.page.keyboard.press("Enter")  # último recurso: Enter no campo de senha
            self.pm.settle()

    def _wait_human_submit(self, login_url: str) -> None:
        """Barreira humana obrigatória pós-CAPTCHA.

        Depois que um CAPTCHA foi exibido e resolvido nesta tentativa de login, esta
        automação NÃO chama ``_submit()`` em hipótese alguma — nem clique no botão,
        nem Enter de último recurso. A automação apenas aguarda, passivamente, uma
        mudança de estado na página (login concluído, ou erro/novo CAPTCHA relatado
        pelo próprio portal) que só pode ocorrer depois que um humano clicar em
        'Acessar' manualmente no navegador.
        """
        msg = (f"CAPTCHA resolvido — Empresa: {self._label} | Cidade: {self._city} | "
               "Clique em 'Acessar' manualmente no navegador para continuar. "
               "[AGUARDANDO CLIQUE HUMANO...]")
        log.info("CAPTCHA resolvido (empresa=%s, cidade=%s). A automação NÃO vai clicar "
                 "em 'Acessar' — aguardando ação manual humana.", self._label, self._city)
        self.reporter.assist(msg)
        try:
            self.page.bring_to_front()
        except PlaywrightError:
            pass
        deadline = time.monotonic() + self.settings.captcha_timeout_seconds
        while time.monotonic() < deadline:
            self.pm.checkpoint()
            if self.is_logged_in(login_url):
                return
            if self.pm.contains_any(pc.LOGIN_ERROR_PATTERNS) or self.pm.contains_any(pc.CAPTCHA_ERROR_PATTERNS):
                return  # deixa o _wait_outcome() tratar o erro / um novo CAPTCHA
            self.page.wait_for_timeout(500)
        raise PortalTimeoutError(
            f"CAPTCHA resolvido, mas ninguém clicou em 'Acessar' em até "
            f"{self.settings.captcha_timeout_seconds}s. A automação não clica nesse botão "
            "por regra de segurança — é necessária ação manual.", stage="login")

    def _wait_outcome(self, login_url: str, wait: float = 20.0) -> str:
        """'ok' | 'captcha' | 'invalid'."""
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            self.pm.checkpoint()
            if self.pm.contains_any(pc.CAPTCHA_ERROR_PATTERNS):
                return "captcha"
            if self.pm.contains_any(pc.LOGIN_ERROR_PATTERNS):
                return "invalid"
            if self.is_logged_in(login_url):
                return "ok"
            if self.gate.detector.detect() and not self.gate.detector.is_solved():
                self._wait_captcha()  # desafio que só aparece após clicar em "Entrar"
                continue
            self.page.wait_for_timeout(500)
        if self._password_visible():
            raise PortalTimeoutError("O portal não concluiu o login no tempo esperado.", stage="login")
        return "ok"
