"""Execução do lote em thread separada (a GUI nunca trava; o navegador roda nesta thread)."""
from __future__ import annotations

import logging

from PySide6.QtCore import QThread, Signal

from ..app import AppContext
from ..controller import BatchRunner
from ..database.models import CompanyRow
from ..dates import Period
from ..events import EventBus, RunControl
from ..logs.logger import add_ui_handler, remove_handler


class BatchWorker(QThread):
    event = Signal(object)
    log_line = Signal(str, int)
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(self, ctx: AppContext, rows: list[CompanyRow], period: Period, cities: set[str] | None,
                 force: bool, control: RunControl) -> None:
        super().__init__()
        self.ctx, self.rows, self.period = ctx, rows, period
        self.cities, self.force, self.control = cities, force, control

    def run(self) -> None:
        bus = EventBus()
        bus.subscribe(self.event.emit)
        handler = add_ui_handler(lambda msg, lvl: self.log_line.emit(msg, lvl))
        try:
            runner = BatchRunner(self.ctx.settings, self.ctx.db, self.ctx.cipher, bus, self.control)
            self.finished_ok.emit(runner.run(self.rows, self.period, cities=self.cities, force=self.force))
        except Exception as exc:  # erro de configuração (ex.: navegador não instalado)
            logging.getLogger("nexfiscal").exception("Falha no lote")
            self.failed.emit(str(exc))
        finally:
            remove_handler(handler)
