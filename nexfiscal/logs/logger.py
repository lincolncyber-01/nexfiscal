"""Logging com redação de segredos: senhas nunca chegam a arquivo, console ou UI."""
from __future__ import annotations

import logging
import json
import re
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit
from collections.abc import Callable
from logging.handlers import RotatingFileHandler
from pathlib import Path

_PATTERNS = [
    re.compile(r'''(?i)(["']?(?:senha|password|passwd|pwd|secret|token|authorization)["']?\s*[=:]\s*)(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;&]+)'''),
]
_URL = re.compile(r'''https?://[^\s<>"']+''')
_CONTEXT: ContextVar[dict] = ContextVar("nexfiscal_log_context", default={})
_STANDARD_FIELDS = frozenset(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


def _safe_url_match(match: re.Match) -> str:
    try:
        parsed = urlsplit(match.group())
        host = parsed.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        if parsed.port:
            host += f":{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    except ValueError:
        return "[URL inválida]"


@contextmanager
def log_context(**values):
    """Keep run/company correlation isolated between threads and nested operations."""
    token = _CONTEXT.set({**_CONTEXT.get(), **values})
    try:
        yield
    finally:
        _CONTEXT.reset(token)


class SecretRegistry:
    """Guarda valores secretos em memória para mascará-los em qualquer mensagem."""

    def __init__(self) -> None:
        self._secrets: set[str] = set()
        self._lock = threading.Lock()

    def register(self, secret: str | None) -> None:
        if secret and len(secret) >= 3:
            with self._lock:
                self._secrets.add(secret)

    def clear(self) -> None:
        with self._lock:
            self._secrets.clear()

    def redact(self, text: str) -> str:
        with self._lock:
            secrets = sorted(self._secrets, key=len, reverse=True)
        for s in secrets:
            text = text.replace(s, "********")
        for pat in _PATTERNS:
            text = pat.sub(lambda m: f"{m.group(1)}********", text)
        return _URL.sub(_safe_url_match, text)


SECRETS = SecretRegistry()


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        record.msg = SECRETS.redact(message)
        record.args = ()
        if record.exc_info:  # traceback também é filtrado
            record.exc_text = SECRETS.redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


class CallbackHandler(logging.Handler):
    """Envia linhas de log já formatadas para a interface."""

    def __init__(self, callback: Callable[[str, int], None]) -> None:
        super().__init__()
        self.callback = callback

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.callback(self.format(record), record.levelno)
        except Exception:  # pragma: no cover
            self.handleError(record)


def _safe_value(value, depth: int = 0):
    if depth > 5:
        return "[limite de profundidade]"
    if isinstance(value, dict):
        return {
            str(key): "********" if re.search(r"senha|password|secret|token|authorization", str(key), re.I)
            else _safe_value(item, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_safe_value(item, depth + 1) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return SECRETS.redact(str(value))


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname, "logger": record.name,
            "message": record.getMessage(),
            **_CONTEXT.get(),
            **{key: value for key, value in record.__dict__.items() if key not in _STANDARD_FIELDS},
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        elif record.exc_text:
            payload["exception"] = record.exc_text
        return json.dumps(_safe_value(payload), ensure_ascii=False, default=str)


_FMT = logging.Formatter("[%(asctime)s] %(message)s", "%H:%M:%S")
_FILE_FMT = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")


def setup_logging(log_dir: Path, level: str = "INFO") -> logging.Logger:
    root = logging.getLogger("nexfiscal")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.propagate = False
    log_dir.mkdir(parents=True, exist_ok=True)
    file_h = RotatingFileHandler(log_dir / "nexfiscal.log", maxBytes=2_000_000,
                                 backupCount=5, encoding="utf-8")
    file_h.setFormatter(_FILE_FMT)
    try:
        json_h = RotatingFileHandler(log_dir / "nexfiscal.jsonl", maxBytes=2_000_000,
                                    backupCount=5, encoding="utf-8")
    except OSError:
        file_h.close()
        raise
    json_h.setFormatter(JsonFormatter())
    console = logging.StreamHandler()
    console.setFormatter(_FMT)
    for h in list(root.handlers):
        if getattr(h, "_nexfiscal_base", False):
            root.removeHandler(h)
            h.close()
    for h in (file_h, json_h, console):
        h._nexfiscal_base = True  # type: ignore[attr-defined]
        h.addFilter(RedactingFilter())
        root.addHandler(h)
    return root


def add_ui_handler(callback: Callable[[str, int], None]) -> logging.Handler:
    handler = CallbackHandler(callback)
    handler.setFormatter(_FMT)
    handler.addFilter(RedactingFilter())
    logging.getLogger("nexfiscal").addHandler(handler)
    return handler


def remove_handler(handler: logging.Handler) -> None:
    logging.getLogger("nexfiscal").removeHandler(handler)
    handler.close()
