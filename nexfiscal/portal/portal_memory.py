"""Perfil de um portal em memória: estratégias aprendidas, fluxos e observações.

É a camada independente do fluxo de automação: o motor de seletores só conversa com
esta interface. A persistência fica em ``memory.portal_memory_manager``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MAX_STRATEGIES = 4
_ID_FIELDS = ("type", "value", "tag", "attr", "op", "near")


def strategy_id(s: dict[str, Any]) -> tuple:
    return tuple(s.get(f) for f in _ID_FIELDS)


def describe_strategy(s: dict[str, Any]) -> str:
    """Descrição legível (usada em logs e no histórico de alterações)."""
    t, v, tag = s.get("type"), s.get("value", ""), s.get("tag") or "*"
    if t == "id":
        return f'{tag}#{v}'
    if t == "name":
        return f'{tag}[name="{v}"]'
    if t == "aria-label":
        return f'{tag}[aria-label="{v}"]'
    if t == "placeholder":
        return f'{tag}[placeholder="{v}"]'
    if t == "label":
        return f'label "{v}"'
    if t == "text":
        return f'{tag} com texto {"contendo " if s.get("op") == "contains" else ""}"{v}"'
    if t == "attr":
        op = "*=" if s.get("op") == "contains" else "="
        return f'{tag}[{s.get("attr")}{op}"{v}"]'
    if t == "relation":
        return f'{tag} após o texto "{s.get("near")}"'
    return str(v)  # css / xpath


@dataclass
class PortalChange:
    stage: str
    key: str
    old: str
    new: str


@dataclass
class PortalProfile:
    portal_id: int
    url: str
    cities: list[str] = field(default_factory=list)
    selectors: dict[tuple[str, str], list[dict]] = field(default_factory=dict)
    flows: dict[str, list[str]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    signature: str | None = None
    last_recognition: str | None = None
    pending_changes: list[PortalChange] = field(default_factory=list)
    pending_notes: list[str] = field(default_factory=list)
    dirty: bool = False

    # ---------------------------------------------------------- consulta ----
    def strategies(self, stage: str, key: str) -> list[dict]:
        items = self.selectors.get((stage, key), [])
        return sorted(items, key=lambda s: (-(s.get("ok", 0) - s.get("fail", 0)), -s.get("ok", 0)))

    def has(self, stage: str, key: str) -> bool:
        return bool(self.selectors.get((stage, key)))

    def is_known_stage(self, stage: str) -> bool:
        return any(st == stage for st, _ in self.selectors)

    # ------------------------------------------------------ aprendizagem ----
    def record_success(self, stage: str, key: str, strategy: dict) -> None:
        for s in self.selectors.get((stage, key), []):
            if strategy_id(s) == strategy_id(strategy):
                s["ok"] = s.get("ok", 0) + 1
                s["fail"] = 0
                self.dirty = True
                return

    def record_failure(self, stage: str, key: str, strategy: dict) -> None:
        items = self.selectors.get((stage, key), [])
        for s in items:
            if strategy_id(s) == strategy_id(strategy):
                s["fail"] = s.get("fail", 0) + 1
        self.selectors[(stage, key)] = [s for s in items if s.get("fail", 0) < 3]
        self.dirty = True

    def learn(self, stage: str, key: str, strategy: dict) -> PortalChange | None:
        """Guarda uma estratégia como a preferida. Retorna a alteração se havia conhecimento anterior
        diferente (portal alterado)."""
        items = self.selectors.get((stage, key), [])
        sid = strategy_id(strategy)
        existing = next((s for s in items if strategy_id(s) == sid), None)
        change: PortalChange | None = None
        if existing is not None:
            existing["ok"] = existing.get("ok", 0) + 1
            existing["fail"] = 0
            items = [existing] + [s for s in items if s is not existing]
        else:
            if items:
                change = PortalChange(stage, key, describe_strategy(items[0]), describe_strategy(strategy))
                self.pending_changes.append(change)
            new = dict(strategy, ok=1, fail=0)
            items = [new] + items
        self.selectors[(stage, key)] = items[:MAX_STRATEGIES]
        self.dirty = True
        return change

    def set_flow(self, name: str, steps: list[str]) -> None:
        if self.flows.get(name) != steps:
            self.flows[name] = list(steps)
            self.dirty = True

    def add_note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)
            self.pending_notes.append(text)
            self.dirty = True
