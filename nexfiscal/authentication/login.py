"""Login nos portais usando o motor adaptativo + CAPTCHA manual."""
from __future__ import annotations

import logging
import re
import time
from urllib.parse import urlsplit

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
        self._manual_guided_login = False
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

    def _has_known_login_flow(self) -> bool:
        """Só automatiza a entrada quando os seletores essenciais do login foram aprendidos."""
        profile = self.engine.profile
        if profile is None:
            return False
        return all(
            profile.has(spec.stage.value, spec.key)
            for spec in (pc.USER_FIELD, pc.PASSWORD_FIELD, pc.LOGIN_BUTTON)
        )

    # ------------------------------------------------------------- fluxo ----
    def login(self, url: str, user: str, password: str, company_label: str, city: str) -> str:
        """Executa o login. Retorna a URL inicial pós-login."""
        self._label, self._city, self._captcha_seen = company_label, city, False
        self.reporter.stage(Stage.LOGIN, StepStatus.EM_ANDAMENTO, "Preparando portal")
        known_portal = self._has_known_login_flow()
        self._manual_guided_login = self.settings.guided_learning and not known_portal
        if self.settings.guided_learning and not known_portal:
            message = f"[STATUS: APRENDIZADO GUIADO ATIVO PARA: {url}] Abra o portal manualmente no navegador; nenhuma navegação automática será executada."
            self.reporter.assist(message)
            log.warning(message)
            expected_host = (urlsplit(url).hostname or "").lower()
            deadline = time.monotonic() + self.settings.assist_timeout_seconds
            while time.monotonic() < deadline:
                if self.control:
                    self.control.checkpoint()
                current_host = (urlsplit(self.page.url).hostname or "").lower()
                if expected_host and current_host == expected_host:
                    break
                self.page.wait_for_timeout(400)
            else:
                raise PortalTimeoutError("O operador não abriu manualmente o portal correto durante o aprendizado guiado.", stage="login")
            # Detectar e sinalizar CAPTCHA assim que o operador abrir o portal,
            # antes de solicitar qualquer interação com os campos de login.
            self._wait_captcha()
        else:
            self.pm.goto(url)
        user_field = self.engine.find(pc.USER_FIELD, timeout=4, required=False)
        if user_field is None:
            entry = self.engine.find(pc.LOGIN_ENTRY, timeout=3, required=False)
            if entry is not None and self._manual_guided_login:
                self.reporter.assist(
                    f"[STATUS: APRENDIZADO GUIADO ATIVO PARA: {url}] "
                    "Clique manualmente no link de acesso do portal."
                )
                self._wait_for_password_field()
            elif entry is not None:
                self.pm.click(entry, "link de acesso")
            elif self._manual_guided_login and not self._password_visible():
                self.reporter.assist(
                    f"[STATUS: APRENDIZADO GUIADO ATIVO PARA: {url}] "
                    "Navegue manualmente até a tela de login."
                )
                self._wait_for_password_field()
        self._remember_signature()
        if not (self.settings.guided_learning and not known_portal):
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
                if self._manual_guided_login:
                    self._request_manual_submit()
                else:
                    self._submit()
            outcome = self._wait_outcome(
                login_url,
                wait=self.settings.assist_timeout_seconds if self._manual_guided_login else 20.0,
            )
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

    def _wait_for_password_field(self) -> None:
        """Aguarda o operador chegar manualmente à tela de login sem clicar/navegar por ele."""
        deadline = time.monotonic() + self.settings.assist_timeout_seconds
        while time.monotonic() < deadline:
            if self.control:
                self.control.checkpoint()
            if self._password_visible():
                return
            self.page.wait_for_timeout(400)
        raise PortalTimeoutError(
            "O operador não abriu manualmente a tela de login durante o aprendizado guiado.",
            stage="login",
        )

    def _request_manual_submit(self) -> None:
        """Aprende o seletor do botão, mas deixa o envio do formulário para o operador."""
        try:
            self.engine.find(pc.LOGIN_BUTTON, timeout=6)
        except ElementNotFoundError:
            log.warning("Botão de login não pôde ser aprendido antes do envio manual.")
        self.reporter.assist(
            f"[STATUS: APRENDIZADO GUIADO ATIVO PARA: {self.page.url}] "
            "Os campos foram preenchidos. Clique manualmente em Entrar no navegador; "
            "o NexFiscal não enviará o formulário neste primeiro aprendizado."
        )

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
