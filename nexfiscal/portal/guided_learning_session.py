"""Sessão explícita de aprendizado guiado e comprovante de persistência."""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from ..browser.browser_manager import BrowserManager
from ..config.settings import Settings
from ..database.database import Database
from ..memory.portal_memory_manager import PortalMemoryManager
from ..portal.guided_action_recorder import GuidedActionRecorder
from ..portal.portal_registry import PortalRegistry
from ..database.repositories import normalize_url
from ..exceptions import ConfigurationError
from ..storage.atomic import atomic_write_text

log = logging.getLogger("nexfiscal.learning")


def _safe_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return ""
    host = parsed.hostname
    if parsed.port and not ((parsed.scheme == "http" and parsed.port == 80) or
                            (parsed.scheme == "https" and parsed.port == 443)):
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path or "/", "", ""))


def _receipt_path(settings: Settings, city: str) -> Path:
    folder = settings.log_dir / "learning-receipts"
    folder.mkdir(parents=True, exist_ok=True)
    slug = "".join(c.lower() if c.isalnum() else "-" for c in city).strip("-") or "portal"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return folder / f"{slug[:40]}-{stamp}-{uuid.uuid4().hex[:8]}.json"


def run_guided_learning(db: Database, settings: Settings, city: str, supplied_url: str) -> Path:
    """Abre o portal informado, grava somente metadados de ações e verifica a persistência.

    O operador controla a navegação. O comando não preenche campos nem submete formulários.
    """
    if not city.strip():
        raise ConfigurationError("Informe a cidade associada ao portal.")
    url = normalize_url(supplied_url)
    parsed = urlsplit(url)
    if parsed.username or parsed.password:
        raise ConfigurationError("A URL não pode conter usuário ou senha embutidos.")

    registry = PortalRegistry(db)
    portal = registry.register([city.strip()], url)
    memory = PortalMemoryManager(db)
    profile = memory.load_profile(portal)

    browser = BrowserManager(settings).start()
    ctx = None
    try:
        ctx, page = browser.new_page()
        recorder = GuidedActionRecorder(page)
        recorder.start()
        page.goto(portal.url, wait_until="domcontentloaded")
        print("\nAPRENDIZADO GUIADO ATIVO")
        print(f"Cidade: {city.strip()}")
        print(f"Portal: {_safe_url(portal.url)}")
        print("Navegue manualmente pelo processo que deseja ensinar.")
        print("Não use este modo para executar downloads fiscais reais de teste.")
        input("Quando terminar a navegação de aprendizado, volte aqui e pressione ENTER: ")

        events = recorder.drain()
        grouped: dict[str, list[dict]] = {}
        for event in events:
            name = recorder.flow_name(event.get("url", ""))
            if name:
                grouped.setdefault(name, []).append(event)
        for name, route_events in grouped.items():
            normalized = [dict(event, order=i) for i, event in enumerate(route_events, 1)]
            profile.set_flow(name, recorder.serialize(normalized))
        memory.save_profile(profile)

        # Confere o que foi realmente persistido, relendo o SQLite em vez de confiar no objeto em RAM.
        persisted = memory.load_profile(portal)
        expected_flows = {name: recorder.serialize([
            dict(event, order=i) for i, event in enumerate(route_events, 1)
        ]) for name, route_events in grouped.items()}
        persisted_events = [
            json.loads(item)
            for route, steps in persisted.flows.items()
            if route in expected_flows
            for item in steps
        ]
        expected_hash = hashlib.sha256(json.dumps(
            {k: v for k, v in sorted(persisted.flows.items()) if k.startswith("guided_actions_v1:")},
            ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        selectors_valid = all(
            isinstance(event.get("selector"), dict)
            and bool(event["selector"].get("type"))
            and bool(event["selector"].get("value"))
            for event in persisted_events
        )
        saved_ok = (bool(persisted_events) and len(persisted_events) == len(events) and selectors_valid
                    and all(persisted.flows.get(name) == steps for name, steps in expected_flows.items()))

        # Teste prático seguro: uma sessão nova consegue abrir a URL inicial; não reproduz cliques
        # nem ações fiscais, pois podem submeter formulários ou disparar downloads reais.
        smoke_ok = False
        smoke_error = None
        try:
            check_ctx, check_page = browser.new_page()
            try:
                response = check_page.goto(portal.url, wait_until="domcontentloaded")
                smoke_ok = response is None or response.status < 400
            finally:
                check_ctx.close()
        except Exception as exc:
            smoke_error = type(exc).__name__
            log.warning("Falha no teste de abertura da URL após aprendizado.", exc_info=True)

        receipt = {
            "schema_version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "city": city.strip(),
            "portal_url": _safe_url(portal.url),
            "events_captured": len(events),
            "routes_persisted": len(grouped),
            "events_reloaded_from_database": len(persisted_events),
            "persistence_check": "PASS" if saved_ok else "FAIL",
            "stored_flow_sha256": expected_hash,
            "initial_url_smoke_test": "PASS" if smoke_ok else "FAIL",
            "initial_url_error_type": smoke_error,
            "end_to_end_workflow_test": "NOT_RUN",
            "note": (
                "O teste ponta a ponta fiscal exige roteiro de validação aprovado, credenciais "
                "de homologação e período/dados de teste. Não são reproduzidos cliques nem downloads "
                "automaticamente para evitar efeitos reais no portal."
            ),
        }
        path = _receipt_path(settings, city)
        atomic_write_text(path, json.dumps(receipt, ensure_ascii=False, indent=2))
        print("\nComprovante:", path)
        print("Persistência:", receipt["persistence_check"])
        print("Teste de abertura da URL:", receipt["initial_url_smoke_test"])
        print("Teste ponta a ponta fiscal:", receipt["end_to_end_workflow_test"])
        if not saved_ok or not smoke_ok:
            raise ConfigurationError(f"Aprendizado/validação não aprovado. Consulte o comprovante: {path}")
        return path
    finally:
        if ctx is not None:
            try:
                ctx.close()
            except Exception:
                log.warning("Falha ao fechar contexto do aprendizado.", exc_info=True)
        browser.stop()
