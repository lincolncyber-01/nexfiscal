"""Eventos de progresso, controle de execução (pausar/cancelar) e emissor de eventos."""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .database.models import Stage, StepStatus
from .exceptions import CancelledError

log = logging.getLogger("nexfiscal.events")


class EventKind(str, Enum):
    CITY_START = "city_start"
    CITY_END = "city_end"
    COMPANY_START = "company_start"
    COMPANY_END = "company_end"
    STAGE = "stage"
    CAPTCHA = "captcha"
    ASSIST = "assist"
    PROGRESS = "progress"
    BATCH_END = "batch_end"


@dataclass
class ProgressEvent:
    kind: EventKind
    city: str = ""
    company: str = ""
    competence: str = ""
    stage: Stage | None = None
    status: str = ""
    message: str = ""
    current: int = 0
    total: int = 0
    data: dict[str, Any] = field(default_factory=dict)


class EventBus:
    def __init__(self) -> None:
        self._subs: list[Callable[[ProgressEvent], None]] = []

    def subscribe(self, callback: Callable[[ProgressEvent], None]) -> None:
        self._subs.append(callback)

    def emit(self, event: ProgressEvent) -> None:
        for cb in list(self._subs):
            try:
                cb(event)
            except Exception:  # um assinante com defeito nunca derruba o processamento
                log.exception("Falha em assinante de evento")


class RunControl:
    """Pausar / retomar / cancelar, verificado entre ações e durante a espera de CAPTCHA."""

    def __init__(self) -> None:
        self._paused = threading.Event()
        self._cancelled = threading.Event()

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    def cancel(self) -> None:
        self._cancelled.set()
        self._paused.clear()

    @property
    def is_paused(self) -> bool:
        return self._paused.is_set()

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled.is_set()

    def checkpoint(self) -> None:
        while self._paused.is_set() and not self._cancelled.is_set():
            time.sleep(0.2)
        if self._cancelled.is_set():
            raise CancelledError("Processamento cancelado pelo usuário.")


class CompanyReporter:
    """Atalho para emitir eventos de uma empresa específica."""

    def __init__(self, bus: EventBus, city: str, company: str, competence: str) -> None:
        self.bus, self.city, self.company, self.competence = bus, city, company, competence

    def _emit(self, kind: EventKind, **kw: Any) -> None:
        self.bus.emit(ProgressEvent(kind, self.city, self.company, self.competence, **kw))

    def stage(self, stage: Stage, status: StepStatus, message: str = "") -> None:
        self._emit(EventKind.STAGE, stage=stage, status=status.value, message=message)

    def captcha(self, message: str) -> None:
        self._emit(EventKind.CAPTCHA, stage=Stage.CAPTCHA, status="AGUARDANDO_CAPTCHA", message=message)

    def assist(self, message: str) -> None:
        self._emit(EventKind.ASSIST, message=message)

    def progress(self, current: int, total: int, message: str = "") -> None:
        self._emit(EventKind.PROGRESS, stage=Stage.XML, current=current, total=total, message=message)
