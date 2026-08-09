"""HTTP request correlation, stable local session and access logging middleware."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import MutableMapping
from contextvars import ContextVar
from typing import Any
from uuid import uuid4

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
request_id_context: ContextVar[str | None] = ContextVar("request_id", default=None)
session_id_context: ContextVar[str | None] = ContextVar("session_id", default=None)
access_logger = logging.getLogger("ares.access")


class RequestContextMiddleware:
    """Attach independent request correlation and caller-provided stable session IDs."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        request_candidate = headers.get("x-request-id", "")
        request_id = (
            request_candidate if _IDENTIFIER_PATTERN.fullmatch(request_candidate) else uuid4().hex
        )
        session_candidate = headers.get("x-ares-session-id", "")
        session_id = (
            session_candidate if _IDENTIFIER_PATTERN.fullmatch(session_candidate) else request_id
        )
        state = scope.setdefault("state", {})
        mutable_state = _as_mutable_mapping(state)
        mutable_state["request_id"] = request_id
        mutable_state["session_id"] = session_id
        request_token = request_id_context.set(request_id)
        session_token = session_id_context.set(session_id)
        status_code = 500
        started = time.perf_counter()

        async def send_with_context(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                response_headers = MutableHeaders(scope=message)
                response_headers["X-Request-ID"] = request_id
                response_headers["X-ARES-Session-ID"] = session_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_context)
        finally:
            duration_ms = round((time.perf_counter() - started) * 1_000, 3)
            access_logger.info(
                "HTTP request completed",
                extra={
                    "event": "http.request.completed",
                    "request_id": request_id,
                    "session_id": session_id,
                    "method": scope["method"],
                    "path": scope["path"],
                    "status_code": status_code,
                    "duration_ms": duration_ms,
                },
            )
            session_id_context.reset(session_token)
            request_id_context.reset(request_token)


def _as_mutable_mapping(value: Any) -> MutableMapping[str, Any]:
    """Narrow Starlette's state value for strict type checking."""

    if not isinstance(value, MutableMapping):
        raise TypeError("ASGI scope state must be a mutable mapping")
    return value
