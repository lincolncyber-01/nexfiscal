"""Reset seguro do estado operacional do NexFiscal."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .config.settings import Settings
from .logs.logger import setup_logging

if TYPE_CHECKING:
    from .app import AppContext


# Explicit allowlist: this operation intentionally never touches file-system
# outputs, downloaded tax documents, exported reports, the encryption key or logs.
_RESET_TABLES = (
    "portal_memory",
    "portal_flows",
    "portal_notes",
    "portal_changes",
    "portal_cities",
    "portals",
    "companies",
    "company_runs",
    "executions",
)


def reset_program(ctx: AppContext) -> None:
    """Clear application records and restore defaults as one guarded operation.

    The current database location is retained so the live SQLite connection and
    persisted configuration continue to refer to the same database after reset.
    Settings are saved inside the database transaction; if clearing fails, the
    transaction rolls back and the previous settings file is restored.
    """
    previous_settings = ctx.settings
    defaults = Settings(database_path=previous_settings.database_path)
    settings_saved = False

    try:
        with ctx.db.transaction():
            for table in _RESET_TABLES:
                ctx.db.execute(f"DELETE FROM {table}")
            ctx.store.save(defaults)
            settings_saved = True
    except Exception:
        if settings_saved:
            try:
                ctx.store.save(previous_settings)
            except Exception:
                logging.getLogger("nexfiscal.reset").exception(
                    "Falha ao restaurar o arquivo de configurações após erro no reset."
                )
        raise

    ctx.settings = defaults
    try:
        setup_logging(defaults.log_dir, defaults.log_level)
    except Exception:
        # Logging setup must not turn a completed reset into a reported failure.
        logging.getLogger("nexfiscal.reset").exception(
            "Reset concluído, mas não foi possível reconfigurar os logs."
        )
