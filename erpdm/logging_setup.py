"""Structured logging with optional file sink and an in-process error monitor hook."""
from __future__ import annotations

import json
import logging
import sys
from collections import deque
from datetime import datetime, timezone

from erpdm.config import get_settings

_CONFIGURED = False
# Recent errors kept in memory so the Settings page can surface them (lightweight error monitor).
RECENT_ERRORS: deque[dict] = deque(maxlen=200)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class ErrorCollector(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)

    def emit(self, record: logging.LogRecord) -> None:
        RECENT_ERRORS.appendleft({
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "logger": record.name,
            "message": record.getMessage(),
        })


def configure_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    settings = get_settings()
    root = logging.getLogger()
    root.setLevel(settings.log_level.upper())
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    if settings.log_file:
        fh = logging.FileHandler(settings.log_file)
        fh.setFormatter(JsonFormatter())
        root.addHandler(fh)
    root.addHandler(ErrorCollector())
    for noisy in ("httpx", "urllib3", "watchdog", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    configure_logging()
    return logging.getLogger(name)
