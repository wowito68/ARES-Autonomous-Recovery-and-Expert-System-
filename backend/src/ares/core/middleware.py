"""HTTP request correlation and access logging middleware."""

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

_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
request_id_context: ContextVar[str | None] = ContextVar("request_id", default=None)
access_logger = logging.getLogger("ares.access")


class RequestContextMiddleware:
    """Attach a safe request ID to responses and emit one access event."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        candidate = headers.get("x-request-id", "")
        request_id = candidate if _REQUEST_ID_PATTERN.fullmatch(candidate) else uuid4().hex
        state = scope.setdefault("state", {})
        mutable_state = _as_mutable_mapping(state)
        mutable_state["request_id"] = request_id
        token = request_id_context.set(request_id)
        status_code = 500
        started = time.perf_counter()

        async def send_with_context(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                response_headers = MutableHeaders(scope=message)
                response_headers["X-Request-ID"] = request_id
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
                    "method": scope["method"],
                    "path": scope["path"],
                    "status_code": status_code,
                    "duration_ms": duration_ms,
                },
            )
            request_id_context.reset(token)


def _as_mutable_mapping(value: Any) -> MutableMapping[str, Any]:
    """Narrow Starlette's state value for strict type checking."""

    if not isinstance(value, MutableMapping):
        raise TypeError("ASGI scope state must be a mutable mapping")
    return value
