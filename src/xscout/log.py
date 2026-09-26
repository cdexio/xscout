"""Structured JSON logging with secret redaction (spec §10, §12)."""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import UTC, datetime

REDACTED = "<redacted>"

# Header/cookie shaped secrets, redacted even when they were never registered.
_PATTERNS = [
    re.compile(r"(?i)(auth_token\s*[=:]\s*[\"']?)([A-Za-z0-9%]+)"),
    re.compile(r"(?i)(ct0\s*[=:]\s*[\"']?)([A-Za-z0-9%]+)"),
    re.compile(r"(?i)(x-csrf-token[\"']?\s*[=:]\s*[\"']?)([A-Za-z0-9%]+)"),
    re.compile(r"(?i)(authorization[\"']?\s*[=:]\s*[\"']?Bearer\s+)([A-Za-z0-9%=._-]+)"),
]


class Redactor:
    """Replaces known secret values and secret-shaped fragments in text."""

    def __init__(self) -> None:
        self._secrets: set[str] = set()
        self._lock = threading.Lock()

    def register(self, *values: str | None) -> None:
        with self._lock:
            self._secrets.update(v for v in values if v and len(v) >= 8)

    def redact(self, text: str) -> str:
        with self._lock:
            secrets = sorted(self._secrets, key=len, reverse=True)
        for s in secrets:
            text = text.replace(s, REDACTED)
        for rx in _PATTERNS:
            text = rx.sub(lambda m: m.group(1) + REDACTED, text)
        return text


redactor = Redactor()


class RedactingJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return redactor.redact(json.dumps(payload, default=str, ensure_ascii=False))


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingJsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
