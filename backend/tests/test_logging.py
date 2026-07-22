"""Structured logging must retain context without leaking exception details."""

from __future__ import annotations

import json
import logging
import sys

import pytest

from ares.core.logging import JsonFormatter, TextFormatter


def _exception_record(secret: str) -> logging.LogRecord:
    try:
        raise RuntimeError(secret)
    except RuntimeError:
        return logging.LogRecord(
            name="ares.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="Safe fixed message",
            args=(),
            exc_info=sys.exc_info(),
        )


@pytest.mark.parametrize("formatter", [JsonFormatter(), TextFormatter()])
def test_formatters_do_not_render_exception_message(formatter: logging.Formatter) -> None:
    sensitive_marker = "SENSITIVE_TOKEN_MARKER"

    output = formatter.format(_exception_record(sensitive_marker))

    assert sensitive_marker not in output
    assert "RuntimeError" in output


def test_json_formatter_preserves_request_context() -> None:
    record = logging.LogRecord(
        name="ares.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="HTTP request completed",
        args=(),
        exc_info=None,
    )
    record.request_id = "request-1234"
    record.method = "GET"
    record.path = "/api/v1/health/live"
    record.status_code = 200
    record.duration_ms = 1.25

    payload = json.loads(JsonFormatter().format(record))

    assert payload["request_id"] == "request-1234"
    assert payload["method"] == "GET"
    assert payload["status_code"] == 200
