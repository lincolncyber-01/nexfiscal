"""Registro estruturado de ações do operador durante aprendizado guiado.

Valores digitados nunca são capturados. O roteiro guarda apenas metadados do evento
e seletores candidatos; credenciais e dados fiscais permanecem fora da memória.
"""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit


_INSTALL_RECORDER = r"""
(() => {
  if (window.__nexfiscalRecorderInstalled) return;
  window.__nexfiscalRecorderInstalled = true;
  window.__nexfiscalRecordedActions = [];
  const norm = value => String(value || '').replace(/\\s+/g, ' ').trim().slice(0, 120);
  function selector(el) {
    if (!el || el.nodeType !== 1) return null;
    if (el.id && document.querySelectorAll('#' + CSS.escape(el.id)).length === 1)
      return {type: 'id', value: el.id};
    for (const attr of ['data-testid', 'name', 'aria-label', 'placeholder']) {
      const value = el.getAttribute(attr);
      if (value && document.querySelectorAll('[' + attr + '="' +
          CSS.escape(value).replace(/"/g, '\\"') + '"]').length === 1)
        return {type: attr, value: value};
    }
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && node !== document.documentElement) {
      let index = 1, prev = node.previousElementSibling;
      while (prev) {
        if (prev.tagName === node.tagName) index++;
        prev = prev.previousElementSibling;
      }
      parts.unshift(node.tagName.toLowerCase() + ':nth-of-type(' + index + ')');
      node = node.parentElement;
    }
    return {type: 'css', value: parts.join(' > ')};
  }
  function label(el) {
    return norm(el.getAttribute('aria-label') || el.getAttribute('placeholder') ||
      el.getAttribute('title') || el.innerText || '');
  }
  function record(event) {
    const el = event.target && event.target.closest &&
      event.target.closest('button,a,input,select,textarea,[role="button"],[onclick]');
    if (!el || el.disabled) return;
    const type = (el.type || '').toLowerCase();
    const sensitive = type === 'password' || el.autocomplete === 'current-password' ||
      /senha|password|token|secret|cpf|cnpj/i.test(
        [el.name, el.id, el.getAttribute('aria-label'), el.getAttribute('placeholder')].join(' '));
    // Nunca registrar o conteúdo de campos. Ignorar eventos de entrada em campos sensíveis.
    if (sensitive && event.type !== 'click') return;
    if (event.type === 'input' && !['date', 'month', 'checkbox', 'radio'].includes(type)) return;
    const action = {
      order: window.__nexfiscalRecordedActions.length + 1,
      event: event.type,
      url: location.href,
      selector: selector(el),
      tag: el.tagName.toLowerCase(),
      control_type: type || null,
      label: label(el),
      // Apenas estado booleano de checkbox/radio; nenhum texto ou valor digitado.
      checked: ['checkbox', 'radio'].includes(type) ? Boolean(el.checked) : null
    };
    window.__nexfiscalRecordedActions.push(action);
    if (typeof window.__nexfiscalRecordAction === 'function')
      window.__nexfiscalRecordAction(action);
  }
  document.addEventListener('click', record, true);
  document.addEventListener('change', record, true);
  document.addEventListener('input', record, true);
})();
"""


class GuidedActionRecorder:
    """Captura ações de interface como eventos JSON sem armazenar valores digitados."""

    def __init__(self, page: Any) -> None:
        self.page = page
        self._events: list[dict[str, Any]] = []
        self._binding_installed = False

    def _receive(self, _source: dict[str, Any], event: dict[str, Any]) -> None:
        """Recebe eventos diretamente do documento, inclusive antes de navegações."""
        try:
            self._events.append(self._validate(event))
        except (TypeError, ValueError, AttributeError):
            return

    def start(self) -> None:
        """Instala captura em documentos novos e no documento atualmente aberto."""
        if not self._binding_installed:
            self.page.expose_binding("__nexfiscalRecordAction", self._receive)
            self._binding_installed = True
        self.page.add_init_script(_INSTALL_RECORDER)
        self.page.evaluate(_INSTALL_RECORDER)

    def drain(self) -> list[dict[str, Any]]:
        """Retira eventos capturados, preservando a ordem observada pelo navegador."""
        events, self._events = self._events, []
        return [event for event in events if event["url"].startswith(("http://", "https://"))]

    @staticmethod
    def _validate(event: dict[str, Any]) -> dict[str, Any]:
        """Normaliza o esquema persistido; URLs sem query/fragment evitam tokens em links."""
        parsed = urlsplit(str(event.get("url", "")))
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            safe_url = ""
        else:
            host = parsed.hostname
            try:
                port = parsed.port
            except ValueError:
                port = None
            if port and not ((parsed.scheme == "http" and port == 80) or
                             (parsed.scheme == "https" and port == 443)):
                host = f"{host}:{port}"
            safe_url = f"{parsed.scheme}://{host}{parsed.path}"
        selector = event.get("selector") or {}
        return {
            "order": int(event.get("order", 0)),
            "event": str(event.get("event", "")),
            "url": safe_url,
            "selector": {
                "type": str(selector.get("type", "")),
                "value": str(selector.get("value", ""))[:300],
            },
            "tag": str(event.get("tag", "")),
            "control_type": event.get("control_type"),
            "label": str(event.get("label", ""))[:120],
            "checked": event.get("checked") if isinstance(event.get("checked"), bool) else None,
        }

    @staticmethod
    def flow_name(url: str) -> str:
        """Chave estável por rota, sem credenciais, query string ou fragmento."""
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return ""
        route = parsed.path or "/"
        return f"guided_actions_v1:{parsed.hostname.lower()}{route}"

    @staticmethod
    def serialize(events: list[dict[str, Any]]) -> list[str]:
        """Formato compatível com a tabela portal_flows atual (lista JSON de strings)."""
        return [json.dumps(event, ensure_ascii=False, sort_keys=True) for event in events]
