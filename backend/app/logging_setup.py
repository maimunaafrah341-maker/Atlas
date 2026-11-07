"""Structured JSON logging with redaction of secrets and personal data.

Redaction is a safety net, not a licence to log sensitive values: code should still avoid
passing secrets or customer details to the logger.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

REDACTED = "[REDACTED]"

_SENSITIVE_KEY = re.compile(
    r"(secret|token|password|passwd|authorization|api[_-]?key|apikey|signature|cookie|"
    r"database_url|dsn|email|phone|address)",
    re.IGNORECASE,
)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
# user:password@ in connection strings and URLs
_URL_CREDENTIALS = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@")

_STANDARD_ATTRS = set(vars(logging.makeLogRecord({}))) | {"message", "asctime", "color_message"}


def redact_text(text: str) -> str:
    text = _URL_CREDENTIALS.sub(rf"\1{REDACTED}@", text)
    text = _BEARER.sub(f"Bearer {REDACTED}", text)
    return _EMAIL.sub(REDACTED, text)


def redact_value(key: str, value: Any) -> Any:
    if _SENSITIVE_KEY.search(key):
        return REDACTED
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {str(k): redact_value(str(k), v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact_value(key, item) for item in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_text(record.getMessage()),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                entry[key] = redact_value(key, value)
        if record.exc_info:
            entry["exception"] = redact_text(self.formatException(record.exc_info))
        return json.dumps(entry, default=str, ensure_ascii=False)


class StripQueryString(logging.Filter):
    """Drop query strings from access-log arguments: they can carry search text or tokens.

    uvicorn logs ``(client, method, path_with_query, http_version, status)``. Any string
    argument that looks like a request path keeps only its path; everything after ``?`` goes.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                arg.split("?", 1)[0] if isinstance(arg, str) and arg.startswith("/") else arg for arg in record.args
            )
        return True


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers[:] = []
        logger.propagate = True

    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(existing, StripQueryString) for existing in access.filters):
        access.addFilter(StripQueryString())
    # Respect uvicorn's access-log switch. uvicorn configures logging before it loads the app:
    # with access logging on, uvicorn.access has uvicorn's own handler, which is swapped for the
    # JSON handler here; with --no-access-log (production, scripts/start.sh) uvicorn removes that
    # handler and turns propagation off, and it must stay off.
    if access.handlers:
        access.handlers[:] = []
        access.propagate = True
