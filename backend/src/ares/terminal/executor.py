"""Terminal execution boundary clients.

Production uses the privileged broker.  The API never mounts, unmounts,
unshares, chroots or spawns privileged terminals directly.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

from ares.terminal.models import (
    TerminalAuthorizationGrant,
    TerminalCleanupEvidence,
    TerminalCleanupStatus,
    TerminalPlan,
    TerminalSession,
    TerminalSessionStatus,
)

_MAX_REQUEST_BYTES = 4_000_000
_MAX_RESPONSE_BYTES = 128_000_000
MessageHandler = Callable[[dict[str, object]], Awaitable[None]]


class TerminalExecutorError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class TerminalExecutor(Protocol):
    async def authorize(self, plan: TerminalPlan, *, operator_uid: int) -> TerminalAuthorizationGrant: ...

    async def start(self, plan: TerminalPlan, grant: TerminalAuthorizationGrant) -> TerminalSession: ...

    async def status(self, session_id: str) -> TerminalSession: ...

    async def close(self, session_id: str) -> TerminalSession: ...

    async def cleanup(self, session_id: str) -> TerminalCleanupEvidence: ...


class LocalTestTerminalExecutor:
    """Test executor that models lifecycle without privileged side effects."""

    def __init__(self) -> None:
        self._grants: dict[str, TerminalAuthorizationGrant] = {}
        self._sessions: dict[str, TerminalSession] = {}

    async def authorize(
        self, plan: TerminalPlan, *, operator_uid: int
    ) -> TerminalAuthorizationGrant:
        grant = TerminalAuthorizationGrant(
            plan_id=plan.id,
            session_id=plan.session_id,
            context_kind=plan.context_kind,
            target_fingerprint=plan.target_fingerprint,
            plan_fingerprint_sha256=plan.fingerprint_sha256,
            operator_uid=operator_uid,
            expires_at=plan.expires_at,
        )
        self._grants[grant.id] = grant
        return grant

    async def start(self, plan: TerminalPlan, grant: TerminalAuthorizationGrant) -> TerminalSession:
        stored = self._grants.pop(grant.id, None)
        if stored != grant or grant.consumed:
            raise TerminalExecutorError("TERMINAL_AUTHORIZATION_INVALID")
        session = TerminalSession(
            id=plan.session_id,
            plan_id=plan.id,
            context_kind=plan.context_kind,
            status=TerminalSessionStatus.ACTIVE,
            authorized_at=plan.created_at,
            started_at=plan.created_at,
            expires_at=plan.expires_at,
            technical_details={"executor": "local-test"},
        )
        self._sessions[session.id] = session
        return session

    async def status(self, session_id: str) -> TerminalSession:
        session = self._sessions.get(session_id)
        if session is None:
            raise TerminalExecutorError("TERMINAL_SESSION_NOT_FOUND")
        return session

    async def close(self, session_id: str) -> TerminalSession:
        session = await self.status(session_id)
        closed = session.model_copy(
            update={
                "status": TerminalSessionStatus.CLOSED,
                "cleanup_status": TerminalCleanupStatus.VERIFIED,
                "closed_at": session.started_at,
            }
        )
        self._sessions[session_id] = closed
        return closed

    async def cleanup(self, session_id: str) -> TerminalCleanupEvidence:
        session = self._sessions.get(session_id)
        return TerminalCleanupEvidence(
            session_id=session_id,
            status=session.cleanup_status if session is not None else TerminalCleanupStatus.VERIFIED,
            process_active=False,
            mount_active=False,
            socket_active=False,
            cleanup_verified=True,
            evidence=("local-test-no-residue",),
        )


class UnixBrokerTerminalExecutor:
    def __init__(self, socket_path: Path, *, timeout_seconds: float = 120.0) -> None:
        self.socket_path = socket_path
        self.timeout_seconds = timeout_seconds

    async def authorize(
        self, plan: TerminalPlan, *, operator_uid: int
    ) -> TerminalAuthorizationGrant:
        result = await self._request(
            {
                "action": "terminal.authorize",
                "plan": plan.model_dump(mode="json"),
                "operator_uid": operator_uid,
            },
            _ignore_message,
        )
        try:
            return TerminalAuthorizationGrant.model_validate(result)
        except ValueError as exc:
            raise TerminalExecutorError("TERMINAL_BROKER_RESPONSE_INVALID") from exc

    async def start(self, plan: TerminalPlan, grant: TerminalAuthorizationGrant) -> TerminalSession:
        result = await self._request(
            {
                "action": "terminal.start",
                "plan": plan.model_dump(mode="json"),
                "grant": grant.model_dump(mode="json"),
            },
            _ignore_message,
        )
        try:
            return TerminalSession.model_validate(result)
        except ValueError as exc:
            raise TerminalExecutorError("TERMINAL_BROKER_RESPONSE_INVALID") from exc

    async def status(self, session_id: str) -> TerminalSession:
        result = await self._request({"action": "terminal.status", "session_id": session_id}, _ignore_message)
        try:
            return TerminalSession.model_validate(result)
        except ValueError as exc:
            raise TerminalExecutorError("TERMINAL_BROKER_RESPONSE_INVALID") from exc

    async def close(self, session_id: str) -> TerminalSession:
        result = await self._request({"action": "terminal.close", "session_id": session_id}, _ignore_message)
        try:
            return TerminalSession.model_validate(result)
        except ValueError as exc:
            raise TerminalExecutorError("TERMINAL_BROKER_RESPONSE_INVALID") from exc

    async def cleanup(self, session_id: str) -> TerminalCleanupEvidence:
        result = await self._request(
            {"action": "terminal.cleanup", "session_id": session_id}, _ignore_message
        )
        try:
            return TerminalCleanupEvidence.model_validate(result)
        except ValueError as exc:
            raise TerminalExecutorError("TERMINAL_BROKER_RESPONSE_INVALID") from exc

    async def _request(
        self,
        request: dict[str, object],
        handler: MessageHandler,
    ) -> dict[str, object]:
        forbidden = {
            "command",
            "argv",
            "shell",
            "executable",
            "script",
            "device",
            "mountpoint",
            "flags",
            "environment",
        }
        if forbidden.intersection(request):
            raise TerminalExecutorError("TERMINAL_REQUEST_FORBIDDEN_FIELD")
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(self.socket_path)), timeout=3.0
            )
        except (OSError, TimeoutError) as exc:
            raise TerminalExecutorError("TERMINAL_BROKER_UNAVAILABLE") from exc
        try:
            encoded = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(encoded) > _MAX_REQUEST_BYTES:
                raise TerminalExecutorError("TERMINAL_BROKER_REQUEST_TOO_LARGE")
            writer.write(encoded + b"\n")
            await writer.drain()
            while True:
                try:
                    line = await asyncio.wait_for(reader.readline(), timeout=self.timeout_seconds)
                except TimeoutError as exc:
                    raise TerminalExecutorError("TERMINAL_BROKER_TIMEOUT") from exc
                if not line:
                    raise TerminalExecutorError("TERMINAL_BROKER_DISCONNECTED")
                if len(line) > _MAX_RESPONSE_BYTES:
                    raise TerminalExecutorError("TERMINAL_BROKER_RESPONSE_TOO_LARGE")
                try:
                    message = json.loads(line)
                except ValueError as exc:
                    raise TerminalExecutorError("TERMINAL_BROKER_RESPONSE_INVALID") from exc
                if not isinstance(message, dict):
                    raise TerminalExecutorError("TERMINAL_BROKER_RESPONSE_INVALID")
                if message.get("type") == "error":
                    code = message.get("code")
                    raise TerminalExecutorError(
                        code if isinstance(code, str) else "TERMINAL_BROKER_FAILED"
                    )
                if message.get("type") == "result":
                    result = message.get("payload")
                    if not isinstance(result, dict):
                        raise TerminalExecutorError("TERMINAL_BROKER_RESPONSE_INVALID")
                    return result
                await handler(message)
        finally:
            writer.close()
            await writer.wait_closed()


async def _ignore_message(message: dict[str, object]) -> None:
    del message
