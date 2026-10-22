"""Motor adaptativo de reconhecimento de elementos.

Ordem de busca para cada elemento lógico (``ElementSpec``):

 0. estratégias guardadas na memória do portal;
 1. ``id``  2. ``name``  3. ``aria-label``  4. ``placeholder``  5. ``label``
 6. texto visível  7. atributos HTML (title/alt/value)  8. relação com o texto vizinho
 9. XPath (texto contido)  10. CSS (class/href/onclick/src)  11. análise da estrutura DOM
 12. OCR (opcional, desativado por padrão).

Quando uma estratégia nova é descoberta, ela é salva na memória e, se havia outra diferente,
a alteração do portal é registrada. A lógica de ranking é Python puro (testável sem navegador).
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Frame, Locator, Page

from ..browser.page_manager import safe_screenshot_bytes
from ..config.portal_config import ElementSpec
from ..exceptions import ElementNotFoundError
from ..utils import normalize_text, xpath_literal
from .portal_memory import PortalProfile, describe_strategy

log = logging.getLogger("nexfiscal.selector")

# ---------------------------------------------------------------- snapshot ---
SNAPSHOT_JS = r"""
() => {
  const SEL = 'input,button,a,select,textarea,label,img,[role],[onclick],[tabindex]';
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const esc = s => String(s).replace(/\\/g, '\\\\').replace(/"/g, '\\"');
  const CTRL = /^(INPUT|SELECT|TEXTAREA|BUTTON)$/;
  function cssPath(el) {
    if (el.id && document.querySelectorAll('[id="' + esc(el.id) + '"]').length === 1)
      return '[id="' + esc(el.id) + '"]';
    const parts = []; let n = el;
    while (n && n.nodeType === 1 && n !== document.documentElement) {
      let i = 1, s = n;
      while ((s = s.previousElementSibling)) if (s.tagName === n.tagName) i++;
      parts.unshift(n.tagName.toLowerCase() + ':nth-of-type(' + i + ')');
      n = n.parentElement;
    }
    return parts.join(' > ');
  }
  function xPath(el) {
    const parts = []; let n = el;
    while (n && n.nodeType === 1) {
      let i = 1, s = n;
      while ((s = s.previousElementSibling)) if (s.tagName === n.tagName) i++;
      parts.unshift(n.tagName.toLowerCase() + '[' + i + ']');
      n = n.parentElement;
    }
    return '/' + parts.join('/');
  }
  function nearText(el) {
    let t = '', n = el.previousSibling, guard = 0;
    while (n && guard++ < 8) {
      if (n.nodeType === 3) t = n.textContent + ' ' + t;
      else if (n.nodeType === 1) {
        if (CTRL.test(n.tagName)) break;
        if (n.tagName === 'BR') { if (norm(t)) break; }
        else if (!/^(SCRIPT|STYLE)$/.test(n.tagName)) t = n.textContent + ' ' + t;
      }
      if (norm(t)) break;
      n = n.previousSibling;
    }
    t = norm(t);
    if (!t) {
      const td = el.closest('td,th');
      if (td && td.previousElementSibling) t = norm(td.previousElementSibling.textContent);
    }
    if (!t && el.parentElement) {
      const c = el.parentElement.cloneNode(true);
      c.querySelectorAll('input,select,textarea,button,script,style').forEach(x => x.remove());
      t = norm(c.textContent);
    }
    return t.slice(0, 120);
  }
  function labelText(el) {
    let t = '';
    try { if (el.labels && el.labels.length) t = Array.from(el.labels).map(l => l.textContent).join(' '); } catch (e) {}
    const lb = el.getAttribute('aria-labelledby');
    if (!t && lb) t = lb.split(/\s+/).map(i => { const e = document.getElementById(i); return e ? e.textContent : ''; }).join(' ');
    return norm(t).slice(0, 120);
  }
  const forms = Array.from(document.forms);
  const out = [];
  const els = Array.from(document.querySelectorAll(SEL)).slice(0, 1500);
  els.forEach((el, order) => {
    const tag = el.tagName.toLowerCase();
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    const type = (el.getAttribute('type') || (tag === 'input' ? 'text' : '')).toLowerCase();
    let text = (tag === 'input' && ['submit', 'button', 'image', 'reset'].includes(type))
      ? (el.value || el.getAttribute('alt') || '') : (el.textContent || '');
    out.push({
      tag, type, order,
      id: el.id || '', name: el.getAttribute('name') || '',
      aria_label: el.getAttribute('aria-label') || '',
      placeholder: el.getAttribute('placeholder') || '',
      title: el.getAttribute('title') || '', alt: el.getAttribute('alt') || '',
      value: tag === 'input' && ['submit', 'button'].includes(type) ? (el.value || '') : '',
      text: norm(text).slice(0, 120), label_text: labelText(el), near_text: nearText(el),
      classes: typeof el.className === 'string' ? el.className : '',
      role: el.getAttribute('role') || '', href: el.getAttribute('href') || '',
      onclick: (el.getAttribute('onclick') || '').slice(0, 80), src: el.getAttribute('src') || '',
      maxlength: el.getAttribute('maxlength') || '',
      visible: r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none',
      disabled: !!el.disabled, readonly: !!el.readOnly,
      form: el.form ? forms.indexOf(el.form) : -1, picked: el.hasAttribute('data-nexfiscal-pick'),
      css: cssPath(el), xpath: xPath(el),
    });
  });
  return out;
}
"""

ASSIST_JS = r"""
(a) => {
  if (window.__nfPick) return;
  window.__nfPick = 'waiting';
  const sel = a.kind === 'clickable'
    ? 'a,button,input[type=submit],input[type=button],input[type=image],[role=button],[role=link],[role=menuitem],[role=tab],[onclick],img'
    : (a.kind === 'select' ? 'select' : 'input,textarea');
  const onClick = ev => {
    const t = ev.target && ev.target.closest ? ev.target.closest(sel) : null;
    if (!t || (t.id === 'nexfiscal-skip')) return;
    ev.preventDefault(); ev.stopPropagation();
    document.querySelectorAll('[data-nexfiscal-pick]').forEach(e => e.removeAttribute('data-nexfiscal-pick'));
    t.setAttribute('data-nexfiscal-pick', '1');
    window.__nfPick = 'done';
    window.__nfCleanup(false);
  };
  let bar = null;
  if (window.top === window) {
    bar = document.createElement('div');
    bar.id = 'nexfiscal-banner';
    bar.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;background:#0b57d0;color:#fff;' +
      'padding:10px 14px;font:600 14px sans-serif;display:flex;gap:12px;align-items:center';
    bar.textContent = a.text;
    const skip = document.createElement('button');
    skip.id = 'nexfiscal-skip'; skip.textContent = 'Pular';
    skip.onclick = e => { e.preventDefault(); e.stopPropagation(); window.__nfPick = 'skip'; window.__nfCleanup(false); };
    bar.appendChild(skip);
    document.body.appendChild(bar);
  }
  window.__nfCleanup = (reset) => {
    document.removeEventListener('click', onClick, true);
    if (bar) bar.remove();
    if (reset) window.__nfPick = null;
  };
  document.addEventListener('click', onClick, true);
}
"""

# ------------------------------------------------------------- ranking (puro) ---
LEVELS = ("id", "name", "aria-label", "placeholder", "label", "text", "attr",
          "relation", "xpath", "css", "dom")
_NON_TEXT_INPUT = {"hidden", "password", "submit", "button", "image", "checkbox", "radio", "file", "reset"}
_DATE_HINT = re.compile(r"(data|date|calend|mask|dt|periodo|compet)", re.I)


@dataclass
class Match:
    info: dict
    idx: int            # posição da palavra-chave (menor = mais específica)
    quality: int        # 3 igual, 2 inicia/palavra inteira, 1 contém
    value: str = ""     # valor do atributo que casou
    attr: str = ""


@dataclass
class Discovery:
    level: str
    matches: list[Match] = field(default_factory=list)


def kw_quality(text: str, kw: str) -> int:
    """0 = sem correspondência; 3 = igual; 2 = palavra inteira/início; 1 = contém."""
    t, k = normalize_text(text), normalize_text(kw)
    if not t or not k:
        return 0
    if t == k:
        return 3
    if len(k) < 4 or not k.isalnum() and len(k) < 2:
        return 2 if re.search(rf"(?<![a-z0-9]){re.escape(k)}(?![a-z0-9])", t) else 0
    if t.startswith(k + " ") or t.startswith(k):
        return 2
    if k in t or k.replace(" ", "") in t.replace(" ", ""):
        return 1
    return 0


def kind_ok(kind: str, info: dict) -> bool:
    tag, typ = info["tag"], info["type"]
    if info.get("disabled"):
        return False
    if kind == "input":
        return tag in ("input", "textarea") and typ not in _NON_TEXT_INPUT and not info.get("readonly")
    if kind == "password":
        return tag == "input" and typ == "password"
    if kind == "select":
        return tag == "select"
    if kind == "clickable":
        if tag in ("button", "a"):
            return True
        if tag == "input":
            return typ in ("submit", "button", "image")
        if tag == "img":
            return bool(info["alt"] or info["title"])
        return bool(info["role"] in ("button", "link", "menuitem", "tab") or info["onclick"])
    return True


def _excluded(spec: ElementSpec, info: dict) -> bool:
    if not spec.exclude:
        return False
    fields = [info["id"], info["name"], info["placeholder"], info["label_text"], info["aria_label"]]
    if spec.kind == "clickable":
        fields += [info["text"], info["title"], info["value"]]
    return any(kw_quality(f, ex) for f in fields if f for ex in spec.exclude)


def _match_attrs(spec: ElementSpec, cands: list[dict], attrs: tuple[str, ...], min_quality: int,
                 max_quality: int = 3) -> list[Match]:
    out: list[Match] = []
    for info in cands:
        best: Match | None = None
        for idx, kw in enumerate(spec.keywords):
            for a in attrs:
                q = kw_quality(info.get(a, ""), kw)
                if min_quality <= q <= max_quality:
                    cand = Match(info, idx, q, info[a], a)
                    if best is None or (cand.idx, -cand.quality) < (best.idx, -best.quality):
                        best = cand
            if best is not None and best.idx == idx:
                break  # palavra-chave mais específica já casou
        if best:
            out.append(best)
    return out


def _is_dateish(info: dict) -> bool:
    if info["type"] in ("date", "month", "datetime-local"):
        return True
    blob = " ".join([info["placeholder"], info["classes"], info["id"], info["name"]])
    if re.search(r"(dd/mm|mm/aaaa|mm/yyyy|aaaa|yyyy)", blob, re.I):
        return True
    return info["maxlength"] in ("10", "7") and bool(_DATE_HINT.search(blob))


def _dom_heuristics(spec: ElementSpec, cands: list[dict], all_infos: list[dict]) -> list[Match]:
    role = spec.role
    vis = [c for c in cands if c["visible"]]
    pw = [i for i in all_infos if i["tag"] == "input" and i["type"] == "password" and i["visible"]]
    if role == "password":
        return [Match(c, 0, 1) for c in vis[:1]]
    if role == "user" and pw:
        before = [c for c in vis if c["order"] < pw[0]["order"]]
        if before:
            return [Match(max(before, key=lambda c: c["order"]), 0, 1)]
    if role == "user":
        return [Match(c, 0, 1) for c in vis[:1]]
    if role == "submit":
        ref = pw[0] if pw else None
        subs = [c for c in vis if c["type"] in ("submit", "image") or c["tag"] == "button"]
        if ref is not None:
            same_form = [c for c in subs if c["form"] == ref["form"] and c["order"] > ref["order"]]
            after = same_form or [c for c in subs if c["order"] > ref["order"]]
            if after:
                return [Match(min(after, key=lambda c: c["order"]), 0, 1)]
        return [Match(c, 0, 1) for c in subs[:1]]
    if role in ("date_start", "date_end"):
        dates = [c for c in vis if _is_dateish(c)]
        pos = 0 if role == "date_start" else 1
        if len(dates) > pos:
            return [Match(dates[pos], 0, 1)]
        return []
    if role == "competence":
        comp = [c for c in vis if c["maxlength"] == "7" or re.search(r"mm/(aaaa|yyyy)", c["placeholder"], re.I)
                or c["type"] == "month"]
        return [Match(c, 0, 1) for c in comp[:1]]
    # genérico: união de atributos
    return _match_attrs(spec, cands, ("id", "name", "classes", "text", "near_text", "href", "title"), 1)


def discover(spec: ElementSpec, infos: list[dict]) -> Discovery | None:
    """Devolve o primeiro nível (na ordem de prioridade) com candidatos, já ordenados."""
    cands = [i for i in infos if kind_ok(spec.kind, i) and not _excluded(spec, i)]
    if not spec.allow_hidden:
        cands = [i for i in cands if i["visible"]] or []
    if not cands:
        return None
    is_input = spec.kind in ("input", "password", "select")
    clickable = spec.kind == "clickable"
    tiers = {
        "id": lambda: _match_attrs(spec, cands, ("id",), 1),
        "name": lambda: _match_attrs(spec, cands, ("name",), 1),
        "aria-label": lambda: _match_attrs(spec, cands, ("aria_label",), 1),
        "placeholder": lambda: _match_attrs(spec, cands, ("placeholder",), 1),
        "label": lambda: _match_attrs(spec, cands, ("label_text",), 1),
        "text": lambda: _match_attrs(spec, cands, ("text",), 2) if clickable else [],
        "attr": lambda: _match_attrs(spec, cands, ("title", "alt", "value"), 1),
        "relation": lambda: _match_attrs(spec, cands, ("near_text",), 1) if is_input else [],
        "xpath": lambda: _match_attrs(spec, cands, ("text",), 1, 1) if clickable else [],
        "css": lambda: _match_attrs(spec, cands, ("classes", "href", "onclick", "src"), 1),
        "dom": lambda: _dom_heuristics(spec, cands, infos),
    }
    anchor = next((i["order"] for i in infos if i["type"] == "password" and i["visible"]), None)
    for level in LEVELS:
        matches = tiers[level]()
        if not matches:
            continue
        if spec.role in ("user", "submit") and anchor is not None:
            matches.sort(key=lambda m: (not m.info["visible"], abs(m.info["order"] - anchor), m.idx))
        else:
            matches.sort(key=lambda m: (not m.info["visible"], m.idx, -m.quality, m.info["order"]))
        return Discovery(level, matches)
    return None


# ---------------------------------------------------------- estratégias ---
def _css_esc(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"')


_UPPER, _LOWER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"


def strategy_selector(s: dict[str, Any]) -> str:
    """Estratégia -> string de seletor do Playwright (exceto ``label``, tratada em resolve)."""
    t, v, tag = s["type"], s.get("value", ""), s.get("tag") or "*"
    if t == "id":
        return f'css=[id="{_css_esc(v)}"]'
    if t in ("name", "aria-label", "placeholder"):
        return f'css={tag}[{t}="{_css_esc(v)}"]'
    if t == "attr":
        op = "*=" if s.get("op") == "contains" else "="
        flag = " i" if s.get("op") == "contains" else ""
        return f'css={tag}[{s["attr"]}{op}"{_css_esc(v)}"{flag}]'
    if t == "text":
        if tag == "input":
            op = "*=" if s.get("op") == "contains" else "="
            return f'css=input[value{op}"{_css_esc(v)}"]'
        if s.get("op") == "contains":
            return (f"xpath=//{tag}[contains(translate(normalize-space(.), '{_UPPER}', '{_LOWER}'), "
                    f"{xpath_literal(v.lower())})]")
        return f"xpath=//{tag}[normalize-space(.)={xpath_literal(v)}]"
    if t == "relation":
        near = (s.get("near") or "")[-25:]
        return (f"xpath=//{tag}[not(@type='hidden')][preceding::text()[normalize-space()]"
                f"[not(parent::script or parent::style)][1][contains(normalize-space(.), {xpath_literal(near)})]]")
    if t == "xpath":
        return f"xpath={v}"
    return f"css={v}"


def resolve(frame: Frame, s: dict[str, Any]) -> Locator:
    if s["type"] == "label":
        return frame.get_by_label(s["value"], exact=True)
    return frame.locator(strategy_selector(s))


def derive_strategies(info: dict, level: str) -> list[dict]:
    """Estratégias candidatas (da mais semântica à mais frágil) para um elemento único."""
    tag = info["tag"]
    out: list[dict] = []
    if info["id"] and not re.search(r"\d{3,}", info["id"]):
        out.append({"type": "id", "value": info["id"], "tag": tag})
    if info["name"]:
        out.append({"type": "name", "value": info["name"], "tag": tag})
    if info["aria_label"]:
        out.append({"type": "aria-label", "value": info["aria_label"], "tag": tag})
    if info["placeholder"]:
        out.append({"type": "placeholder", "value": info["placeholder"], "tag": tag})
    if info["label_text"]:
        out.append({"type": "label", "value": info["label_text"], "tag": tag})
    if info["text"] and len(info["text"]) <= 60 and tag != "input":
        out.append({"type": "text", "value": info["text"], "tag": tag, "op": "equals"})
    if tag == "input" and info["value"]:
        out.append({"type": "text", "value": info["value"], "tag": tag, "op": "equals"})
    for a in ("title", "alt"):
        if info[a]:
            out.append({"type": "attr", "attr": a, "value": info[a], "tag": tag, "op": "equals"})
    if info["near_text"] and tag in ("input", "select", "textarea"):
        out.append({"type": "relation", "near": info["near_text"], "tag": tag})
    # o tipo que casou primeiro (ex.: name) vem à frente
    out.sort(key=lambda s: 0 if s["type"] == level else 1)
    out.append({"type": "xpath", "value": info["xpath"]})
    out.append({"type": "css", "value": info["css"]})
    return out


def derive_group_strategies(m: Match, kw: str) -> list[dict]:
    """Estratégias *genéricas* (casam vários elementos) para listas como os links de XML."""
    tag, out = m.info["tag"], []
    for a in ("title", "alt", "aria_label"):
        if m.info[a]:
            out.append({"type": "attr", "attr": a.replace("_", "-"), "value": m.info[a], "tag": tag, "op": "equals"})
    if m.info["text"] and tag != "input":
        out.append({"type": "text", "value": m.info["text"], "tag": tag, "op": "equals"})
        out.append({"type": "text", "value": normalize_text(kw), "tag": tag, "op": "contains"})
    if tag == "input" and m.info["value"]:
        out.append({"type": "text", "value": m.info["value"], "tag": tag, "op": "equals"})
    for a in ("href", "onclick", "src", "classes", "id", "name"):
        if m.info[a]:
            out.append({"type": "attr", "attr": "class" if a == "classes" else a,
                        "value": normalize_text(kw).replace(" ", ""), "tag": tag, "op": "contains"})
    return out


# ------------------------------------------------------------------ motor ---
@dataclass
class FoundElement:
    locator: Any                 # Locator (ou OcrTarget)
    strategy: dict
    source: str                  # "memória" | "descoberta" | "ocr"
    level: str = ""


@dataclass
class FoundGroup:
    locators: list[Any]
    strategy: dict
    source: str

    def __len__(self) -> int:
        return len(self.locators)


class OcrTarget:
    """Alvo por coordenadas (fallback OCR). Mesma interface mínima de um Locator."""

    def __init__(self, page: Page, x: float, y: float) -> None:
        self.page, self.x, self.y = page, x, y

    def click(self, **_: Any) -> None:
        self.page.mouse.click(self.x, self.y)

    def fill(self, value: str, **_: Any) -> None:
        self.page.mouse.click(self.x, self.y)
        self.page.keyboard.type(value)

    def is_visible(self, **_: Any) -> bool:
        return True

    def count(self) -> int:
        return 1

    def nth(self, _i: int) -> "OcrTarget":
        return self

    def input_value(self, **_: Any) -> str:
        return ""


class SelectorEngine:
    def __init__(self, page: Page, profile: PortalProfile | None = None, *, timeout: float = 30,
                 poll: float = 0.7, enable_ocr: bool = False, assisted: bool = False,
                 assist_timeout: float = 180, on_assist: Callable[[ElementSpec, str], None] | None = None,
                 control: Any = None, guided_learning: bool = True) -> None:
        self.page, self.profile = page, profile
        self.timeout, self.poll, self.enable_ocr = timeout, poll, enable_ocr
        self.assisted, self.assist_timeout = assisted, assist_timeout
        self.guided_learning = guided_learning
        self.on_assist, self.control = on_assist, control

    # ------------------------------------------------------------ DOM ----
    def _frames(self) -> list[Frame]:
        frames = [self.page.main_frame] + [f for f in self.page.frames if f is not self.page.main_frame]
        return [f for f in frames if not f.is_detached()]

    def snapshot(self) -> list[dict]:
        infos: list[dict] = []
        for fi, frame in enumerate(self._frames()):
            try:
                for info in frame.evaluate(SNAPSHOT_JS):
                    info["frame"] = fi
                    infos.append(info)
            except PlaywrightError:
                continue  # navegação em andamento / frame fechado
        return infos

    def signature(self) -> str:
        """Assinatura estrutural da página (campos e botões), p/ detectar mudança de interface."""
        parts = sorted(
            f'{i["tag"]}|{i["type"]}|{i["id"]}|{i["name"]}' for i in self.snapshot()
            if i["tag"] in ("input", "select", "textarea", "button") and i["type"] != "hidden")
        return hashlib.sha1("\n".join(parts).encode()).hexdigest()[:16]

    # ------------------------------------------------------- memória ----
    def _usable(self, loc: Locator, spec: ElementSpec) -> list[Locator]:
        try:
            n = min(loc.count(), 300)
            items = [loc.nth(i) for i in range(n)]
            if spec.allow_hidden:
                return items
            return [it for it in items if it.is_visible()]
        except PlaywrightError:
            return []

    def _try_memory(self, spec: ElementSpec, multi: bool) -> tuple[list[Locator], dict | None, list[dict]]:
        if not self.profile:
            return [], None, []
        failed: list[dict] = []
        for strat in self.profile.strategies(spec.stage.value, spec.key):
            for frame in self._frames():
                try:
                    items = self._usable(resolve(frame, strat), spec)
                except PlaywrightError:
                    items = []
                if items:
                    return (items if multi else items[:1]), strat, failed
            failed.append(strat)
        return [], None, failed

    # ----------------------------------------------------- descoberta ----
    def _frame_of(self, info: dict) -> Frame:
        frames = self._frames()
        return frames[min(info.get("frame", 0), len(frames) - 1)]

    def _materialize(self, info: dict, level: str) -> FoundElement | None:
        """Escolhe a melhor estratégia (única na página) para um elemento do snapshot."""
        frame = self._frame_of(info)
        for strat in derive_strategies(info, level):
            try:
                loc = resolve(frame, strat)
                if loc.count() == 1:
                    return FoundElement(loc, strat, "descoberta", level)
            except PlaywrightError:
                continue
        strat = {"type": "css", "value": info["css"]}  # nenhuma única: caminho CSS desta captura
        try:
            return FoundElement(frame.locator(f"css={info['css']}"), strat, "descoberta", level)
        except PlaywrightError:
            return None

    def _discover_single(self, spec: ElementSpec) -> tuple[FoundElement, dict] | None:
        found = discover(spec, self.snapshot())
        if not found:
            return None
        for m in found.matches[:3]:
            el = self._materialize(m.info, found.level)
            if el:
                return el, m.info
        return None

    # ---------------------------------------------------- modo assistido ----
    def _assist(self, spec: ElementSpec) -> FoundElement | None:
        """Pede ao usuário que clique no elemento (aprendizado assistido)."""
        if not (self.assisted or self.guided_learning):
            return None
        mode = "APRENDIZADO GUIADO" if self.guided_learning else "RECONHECIMENTO ASSISTIDO"
        msg = f"[STATUS: {mode} ATIVO PARA: {self.page.url}] NexFiscal precisa da sua orientação: selecione '{spec.label}' na página (ou em 'Pular')."
        log.warning(msg)
        kind = "input" if spec.kind == "password" else spec.kind
        frames = self._frames()
        for f in frames:
            try:
                f.evaluate(ASSIST_JS, {"kind": kind, "text": msg})
            except PlaywrightError:
                continue
        if self.on_assist:
            self.on_assist(spec, msg)
        state = None
        deadline = time.monotonic() + self.assist_timeout
        try:
            while time.monotonic() < deadline and state not in ("done", "skip"):
                if self.control:
                    self.control.checkpoint()
                for f in frames:
                    try:
                        state = f.evaluate("() => window.__nfPick || null") or state
                    except PlaywrightError:
                        continue
                    if state in ("done", "skip"):
                        break
                else:
                    self.page.wait_for_timeout(400)
            picked = next((i for i in self.snapshot() if i.get("picked")), None) if state == "done" else None
        finally:
            for f in frames:
                try:
                    f.evaluate("() => { if (window.__nfCleanup) window.__nfCleanup(true); window.__nfPick = null; }")
                except PlaywrightError:
                    continue
        if not picked:
            return None
        el = self._materialize(picked, "assistido")
        if el:
            el.source = "assistido"
            self._learn(spec, el.strategy, [])
            try:  # remove a marca do elemento
                el.locator.evaluate("e => e.removeAttribute('data-nexfiscal-pick')")
            except PlaywrightError:
                pass
        return el

    def _discover_group(self, spec: ElementSpec) -> FoundGroup | None:
        found = discover(spec, self.snapshot())
        if not found:
            return None
        first = found.matches[0]
        want = len([m for m in found.matches if m.info["visible"] or spec.allow_hidden])
        best: tuple[int, FoundGroup] | None = None
        frame = self._frame_of(first.info)
        for strat in derive_group_strategies(first, spec.keywords[first.idx]):
            try:
                items = self._usable(resolve(frame, strat), spec)
            except PlaywrightError:
                continue
            if items and (best is None or len(items) > best[0]):
                best = (len(items), FoundGroup(items, strat, "descoberta"))
            if items and len(items) >= want:
                break
        return best[1] if best else None

    # ----------------------------------------------------------- OCR ----
    def _ocr(self, spec: ElementSpec) -> FoundElement | None:  # pragma: no cover - requer tesseract
        if not self.enable_ocr:
            return None
        try:
            import io

            import pytesseract
            from PIL import Image
        except ImportError:
            log.warning("OCR habilitado, mas pytesseract/Pillow não estão instalados.")
            return None
        try:
            img = Image.open(io.BytesIO(safe_screenshot_bytes(self.page)))
            data = pytesseract.image_to_data(img, lang="por", output_type=pytesseract.Output.DICT)
        except Exception as exc:
            log.warning("Falha no OCR: %s", exc)
            return None
        for kw in spec.keywords:
            for i, word in enumerate(data["text"]):
                if word and normalize_text(word) == normalize_text(kw).split(" ")[0]:
                    x = data["left"][i] + data["width"][i] / 2
                    y = data["top"][i] + data["height"][i] / 2
                    return FoundElement(OcrTarget(self.page, x, y), {"type": "ocr", "value": kw}, "ocr", "ocr")
        return None

    # ------------------------------------------------------ API pública ----
    def _learn(self, spec: ElementSpec, strat: dict, memory_failed: list[dict]) -> None:
        if not self.profile:
            return
        for bad in memory_failed:
            self.profile.record_failure(spec.stage.value, spec.key, bad)
        change = self.profile.learn(spec.stage.value, spec.key, strat)
        if change:
            log.warning("PORTAL ALTERADO — %s | elemento: %s | antes: %s | agora: %s | memória atualizada.",
                        self.profile.url, spec.label, change.old, change.new)
        else:
            log.info("Elemento reconhecido e memorizado: %s (%s)", spec.label, describe_strategy(strat))

    def find(self, spec: ElementSpec, timeout: float | None = None, required: bool = True) -> FoundElement | None:
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while True:
            items, strat, failed = self._try_memory(spec, multi=False)
            if items and strat:
                self.profile.record_success(spec.stage.value, spec.key, strat)  # type: ignore[union-attr]
                return FoundElement(items[0], strat, "memória")
            # No modo guiado, não inferimos seletores novos nem executamos OCR.
            # Só reutilizamos conhecimento persistido ou pedimos que o operador
            # identifique explicitamente o elemento.
            result = None if self.guided_learning else self._discover_single(spec)
            if result:
                el, _info = result
                self._learn(spec, el.strategy, failed)
                return el
            if time.monotonic() >= deadline:
                break
            self.page.wait_for_timeout(int(self.poll * 1000))
        ocr = None if self.guided_learning else self._ocr(spec)
        if ocr:
            return ocr
        if required:
            assisted = self._assist(spec)
            if assisted:
                return assisted
            raise ElementNotFoundError(
                f"Elemento não encontrado: {spec.label}. O portal pode ter sido alterado.",
                stage=spec.stage.value)
        return None

    def find_all(self, spec: ElementSpec, timeout: float | None = None, required: bool = True) -> FoundGroup | None:
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while True:
            items, strat, failed = self._try_memory(spec, multi=True)
            if items and strat:
                self.profile.record_success(spec.stage.value, spec.key, strat)  # type: ignore[union-attr]
                return FoundGroup(items, strat, "memória")
            # Grupos (por exemplo, lista de XMLs) exigem roteiro aprendido no
            # modo guiado; se a memória falhar, encerramos sem inferir alvos.
            group = None if self.guided_learning else self._discover_group(spec)
            if group:
                self._learn(spec, group.strategy, failed)
                return group
            if time.monotonic() >= deadline:
                break
            self.page.wait_for_timeout(int(self.poll * 1000))
        if required:
            raise ElementNotFoundError(f"Elemento não encontrado: {spec.label}.", stage=spec.stage.value)
        return None

    def exists(self, spec: ElementSpec, timeout: float = 1.5) -> bool:
        return self.find(spec, timeout=timeout, required=False) is not None
