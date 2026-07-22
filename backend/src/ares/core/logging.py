"""Local structured logging without external telemetry."""

from __future__ import annotations

import json
import logging
import logging.config
from datetime import UTC, datetime
from typing import Any

from ares.config import LogFormat, Settings

_EXTRA_FIELDS = (
    "event",
    "request_id",
    "method",
    "path",
    "status_code",
    "duration_ms",
    "error_code",
    "exception_type",
)


def _safe_payload(record: logging.LogRecord) -> dict[str, Any]:
    """Build a bounded payload without serializing exception messages or traces."""

    payload: dict[str, Any] = {
        "timestamp": datetime.now(UTC).isoformat(),
        "level": record.levelname,
        "logger": record.name,
        "message": record.getMessage(),
    }
    for field in _EXTRA_FIELDS:
        value = getattr(record, field, None)
        if value is not None:
            payload[field] = value
    if record.exc_info and record.exc_info[0] is not None:
        payload["exception_type"] = record.exc_info[0].__name__
    return payload


class JsonFormatter(logging.Formatter):
    """Encode curated, non-secret log fields as one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(_safe_payload(record), ensure_ascii=False, separators=(",", ":"))


class TextFormatter(logging.Formatter):
    """Render the same curated fields in a compact developer-friendly form."""

    def format(self, record: logging.LogRecord) -> str:
        payload = _safe_payload(record)
        leading = (
            f"{payload.pop('timestamp')} {payload.pop('level')} "
            f"{payload.pop('logger')} {payload.pop('message')}"
        )
        context = " ".join(
            f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in payload.items()
        )
        return f"{leading} {context}" if context else leading


def configure_logging(settings: Settings) -> None:
    """Configure application loggers from validated settings."""

    formatter: dict[str, Any]
    if settings.log_format is LogFormat.JSON:
        formatter = {"()": "ares.core.logging.JsonFormatter"}
    else:
        formatter = {"()": "ares.core.logging.TextFormatter"}

    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {"default": formatter},
            "handlers": {
                "default": {
                    "class": "logging.StreamHandler",
                    "formatter": "default",
                    "stream": "ext://sys.stderr",
                }
            },
            "root": {
                "handlers": ["default"],
                "level": settings.log_level.upper(),
            },
            "loggers": {
                "uvicorn.access": {"handlers": [], "propagate": False},
            },
        }
    )
