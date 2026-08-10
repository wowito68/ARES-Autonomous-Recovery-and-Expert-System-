"""Filesystem execution port with test-local and privileged broker adapters."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from ares.filesystems.models import (
    FilesystemAuthorizationGrant,
    FilesystemInspection,
    FilesystemRepairOutcome,
    FilesystemRepairPlan,
)
from ares.tools.filesystem import FilesystemToolError, FilesystemToolSuite

AuthorizationCallback = Callable[[str], Awaitable[None]]
StageCallback = Callable[[str, dict[str, object]], Awaitable[None]]
_MAX_REQUEST_BYTES = 2_000_000
_MAX_RESPONSE_BYTES = 8_000_000


class FilesystemExecutorError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class FilesystemExecutor(Protocol):
    async def inspect(self, device: str) -> FilesystemInspection: ...

    async def request_authorization(
        self,
        plan: FilesystemRepairPlan,
        *,
        on_challenge: AuthorizationCallback,
    ) -> FilesystemAuthorizationGrant: ...

    async def execute(
        self,
        plan: FilesystemRepairPlan,
        grant: FilesystemAuthorizationGrant,
        *,
        on_stage: StageCallback,
    ) -> FilesystemRepairOutcome: ...


class LocalTestFilesystemExecutor:
    """Test-only adapter capable of operating on isolated filesystem image files."""

    def __init__(self, tools: FilesystemToolSuite, *, authorize: bool = True) -> None:
        self.tools = tools
        self.authorize = authorize
        self._grants: dict[str, FilesystemAuthorizationGrant] = {}

    async def inspect(self, device: str) -> FilesystemInspection:
        try:
            return await self.tools.inspect(device)
        except FilesystemToolError as exc:
            raise FilesystemExecutorError(exc.code) from exc

    async def request_authorization(
        self,
        plan: FilesystemRepairPlan,
        *,
        on_challenge: AuthorizationCallback,
    ) -> FilesystemAuthorizationGrant:
        challenge_id = f"test-{uuid4().hex}"
        await on_challenge(challenge_id)
        if not self.authorize:
            raise FilesystemExecutorError("FILESYSTEM_AUTHORIZATION_DENIED")
        grant = FilesystemAuthorizationGrant(
            id=uuid4().hex,
            challenge_id=challenge_id,
            plan_id=plan.id,
            repair_id=plan.repair_id,
            session_id=plan.session_id,
            target_fingerprint_sha256=plan.target.fingerprint_sha256,
            plan_fingerprint_sha256=plan.fingerprint_sha256,
            operator_uid=1000,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        self._grants[grant.id] = grant
        return grant

    async def execute(
        self,
        plan: FilesystemRepairPlan,
        grant: FilesystemAuthorizationGrant,
        *,
        on_stage: StageCallback,
    ) -> FilesystemRepairOutcome:
        stored = self._grants.pop(grant.id, None)
        if (
            stored != grant
            or grant.plan_id != plan.id
            or grant.repair_id != plan.repair_id
            or grant.session_id != plan.session_id
            or grant.target_fingerprint_sha256 != plan.target.fingerprint_sha256
            or grant.plan_fingerprint_sha256 != plan.fingerprint_sha256
            or grant.expires_at <= datetime.now(UTC)
        ):
            raise FilesystemExecutorError("FILESYSTEM_AUTHORIZATION_INVALID")
        try:
            return await self.tools.execute_repair(plan, on_stage)
        except FilesystemToolError as exc:
            raise FilesystemExecutorError(exc.code) from exc


class UnixBrokerFilesystemExecutor:
    """Semantic client for filesystem operations owned by the root broker."""

    def __init__(self, socket_path: Path, *, timeout_seconds: float = 28_800) -> None:
        self.socket_path = socket_path
        self.timeout_seconds = timeout_seconds

    async def inspect(self, device: str) -> FilesystemInspection:
        result = await self._request(
            {"action": "filesystem.inspect", "device": device}, _ignore_message
        )
        try:
            return FilesystemInspection.model_validate(result)
        except ValueError as exc:
            raise FilesystemExecutorError("FILESYSTEM_BROKER_RESPONSE_INVALID") from exc

    async def request_authorization(
        self,
        plan: FilesystemRepairPlan,
        *,
        on_challenge: AuthorizationCallback,
    ) -> FilesystemAuthorizationGrant:
        async def handle(message: dict[str, object]) -> None:
            if message.get("type") != "authorization_requested":
                return
            challenge_id = message.get("challenge_id")
            if isinstance(challenge_id, str):
                await on_challenge(challenge_id)

        result = await self._request(
            {"action": "filesystem.authorize", "plan": plan.model_dump(mode="json")},
            handle,
        )
        try:
            return FilesystemAuthorizationGrant.model_validate(result)
        except ValueError as exc:
            raise FilesystemExecutorError("FILESYSTEM_BROKER_RESPONSE_INVALID") from exc

    async def execute(
        self,
        plan: FilesystemRepairPlan,
        grant: FilesystemAuthorizationGrant,
        *,
        on_stage: StageCallback,
    ) -> FilesystemRepairOutcome:
        async def handle(message: dict[str, object]) -> None:
            if message.get("type") != "stage":
                return
            name = message.get("name")
            payload = message.get("payload")
            if isinstance(name, str) and isinstance(payload, dict):
                await on_stage(name, payload)

        result = await self._request(
            {
                "action": "filesystem.execute",
                "plan": plan.model_dump(mode="json"),
                "grant": grant.model_dump(mode="json"),
            },
            handle,
        )
        try:
            return FilesystemRepairOutcome.model_validate(result)
        except ValueError as exc:
            raise FilesystemExecutorError("FILESYSTEM_BROKER_RESPONSE_INVALID") from exc

    async def _request(
        self,
        request: dict[str, object],
        handler: Callable[[dict[str, object]], Awaitable[None]],
    ) -> dict[str, object]:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(self.socket_path)), timeout=3.0
            )
        except (OSError, TimeoutError) as exc:
            raise FilesystemExecutorError("FILESYSTEM_BROKER_UNAVAILABLE") from exc
        try:
            encoded = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(encoded) > _MAX_REQUEST_BYTES:
                raise FilesystemExecutorError("FILESYSTEM_BROKER_REQUEST_TOO_LARGE")
            writer.write(encoded + b"\n")
            await writer.drain()
            while True:
                try:
                    line = await asyncio.wait_for(reader.readline(), timeout=self.timeout_seconds)
                except TimeoutError as exc:
                    raise FilesystemExecutorError("FILESYSTEM_BROKER_TIMEOUT") from exc
                if not line:
                    raise FilesystemExecutorError("FILESYSTEM_BROKER_DISCONNECTED")
                if len(line) > _MAX_RESPONSE_BYTES:
                    raise FilesystemExecutorError("FILESYSTEM_BROKER_RESPONSE_TOO_LARGE")
                try:
                    message = json.loads(line)
                except ValueError as exc:
                    raise FilesystemExecutorError("FILESYSTEM_BROKER_RESPONSE_INVALID") from exc
                if not isinstance(message, dict):
                    raise FilesystemExecutorError("FILESYSTEM_BROKER_RESPONSE_INVALID")
                if message.get("type") == "error":
                    code = message.get("code")
                    raise FilesystemExecutorError(
                        code if isinstance(code, str) else "FILESYSTEM_BROKER_FAILED"
                    )
                if message.get("type") == "result":
                    result = message.get("payload")
                    if not isinstance(result, dict):
                        raise FilesystemExecutorError("FILESYSTEM_BROKER_RESPONSE_INVALID")
                    return result
                await handler(message)
        finally:
            writer.close()
            await writer.wait_closed()


async def _ignore_message(message: dict[str, object]) -> None:
    del message
