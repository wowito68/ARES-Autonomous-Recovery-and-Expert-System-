"""Semantic privileged executor for recovery child operations."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from ares.protection import ProtectionCheckpoint
from ares.recovery.checkpoints import RecoveryCheckpointProvider
from ares.recovery.models import ConfigurationDiff, RecoveryOperation, RecoveryStrategyKind
from ares.tools.recovery import (
    AptPackageManagerAdapter,
    ConfigurationRecoveryTool,
    InitramfsRecoveryTool,
    RecoveryProcessRunner,
    RecoveryToolError,
)

_MAX_REQUEST_BYTES = 2_000_000
_MAX_RESPONSE_BYTES = 4_000_000
ChallengeObserver = Callable[[str], Awaitable[None]]


class RecoveryExecutorError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class RecoveryAuthorizationGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: uuid4().hex)
    operation_id: str
    session_id: str
    challenge_id: str
    checkpoint_id: str
    expires_at: datetime


class RecoveryMutationExecutor(Protocol):
    async def protect(
        self,
        operation: RecoveryOperation,
        *,
        root: Path,
        session_id: str,
    ) -> ProtectionCheckpoint: ...

    async def authorize(
        self,
        operation: RecoveryOperation,
        *,
        session_id: str,
        on_challenge: ChallengeObserver,
    ) -> RecoveryAuthorizationGrant: ...

    async def execute(
        self,
        operation: RecoveryOperation,
        *,
        root: Path,
        grant: RecoveryAuthorizationGrant,
    ) -> tuple[str, ...]: ...


class LocalTestRecoveryExecutor:
    """Controlled fixture executor; never selected outside Environment.TEST."""

    def __init__(self, runner: RecoveryProcessRunner, checkpoint_root: Path) -> None:
        self.packages = AptPackageManagerAdapter(runner)
        self.configuration = ConfigurationRecoveryTool()
        self.initramfs = InitramfsRecoveryTool(runner)
        self.checkpoints = RecoveryCheckpointProvider(checkpoint_root)
        self._grants: dict[str, RecoveryAuthorizationGrant] = {}
        self._lock = asyncio.Lock()

    async def protect(
        self,
        operation: RecoveryOperation,
        *,
        root: Path,
        session_id: str,
    ) -> ProtectionCheckpoint:
        return await asyncio.to_thread(
            self.checkpoints.create, operation, root=root, session_id=session_id
        )

    async def authorize(
        self,
        operation: RecoveryOperation,
        *,
        session_id: str,
        on_challenge: ChallengeObserver,
    ) -> RecoveryAuthorizationGrant:
        checkpoint = operation.protection_checkpoint
        if checkpoint is None:
            raise RecoveryExecutorError("RECOVERY_PROTECTION_REQUIRED")
        challenge_id = uuid4().hex
        await on_challenge(challenge_id)
        grant = RecoveryAuthorizationGrant(
            operation_id=operation.operation_id,
            session_id=session_id,
            challenge_id=challenge_id,
            checkpoint_id=checkpoint.id,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        async with self._lock:
            self._grants[grant.id] = grant
        return grant

    async def execute(
        self,
        operation: RecoveryOperation,
        *,
        root: Path,
        grant: RecoveryAuthorizationGrant,
    ) -> tuple[str, ...]:
        async with self._lock:
            stored = self._grants.pop(grant.id, None)
        checkpoint = operation.protection_checkpoint
        if (
            stored != grant
            or checkpoint is None
            or checkpoint.id != grant.checkpoint_id
            or grant.operation_id != operation.operation_id
            or grant.expires_at <= datetime.now(UTC)
        ):
            raise RecoveryExecutorError("RECOVERY_AUTHORIZATION_INVALID")
        try:
            if operation.strategy is RecoveryStrategyKind.CONFIGURATION:
                change = ConfigurationDiff.model_validate(operation.payload.get("configuration_diff"))
                backup, digest = await asyncio.to_thread(self.configuration.apply, root, change)
                return (f"configuration backup: {backup}", f"configuration sha256: {digest}")
            if operation.strategy is RecoveryStrategyKind.PACKAGE:
                package_operation = str(operation.payload.get("package_operation", ""))
                return await self.packages.repair(root, package_operation)
            if operation.strategy is RecoveryStrategyKind.INITRAMFS:
                kernel = str(operation.payload.get("kernel_version", ""))
                return await self.initramfs.rebuild(root, kernel)
        except RecoveryToolError as exc:
            raise RecoveryExecutorError(exc.code) from exc
        raise RecoveryExecutorError("RECOVERY_OPERATION_UNSUPPORTED")


class UnixBrokerRecoveryExecutor:
    def __init__(self, socket_path: Path, *, timeout_seconds: float = 240.0) -> None:
        self.socket_path = socket_path
        self.timeout_seconds = timeout_seconds

    async def protect(
        self,
        operation: RecoveryOperation,
        *,
        root: Path,
        session_id: str,
    ) -> ProtectionCheckpoint:
        payload = await self._request(
            {
                "action": "recovery.protect",
                "operation": operation.model_dump(mode="json"),
                "root": str(root),
                "session_id": session_id,
            }
        )
        try:
            return ProtectionCheckpoint.model_validate(payload.get("checkpoint"))
        except ValueError as exc:
            raise RecoveryExecutorError("RECOVERY_BROKER_RESPONSE_INVALID") from exc

    async def authorize(
        self,
        operation: RecoveryOperation,
        *,
        session_id: str,
        on_challenge: ChallengeObserver,
    ) -> RecoveryAuthorizationGrant:
        payload = await self._request(
            {
                "action": "recovery.authorize",
                "operation": operation.model_dump(mode="json"),
                "session_id": session_id,
            },
            on_challenge=on_challenge,
        )
        try:
            return RecoveryAuthorizationGrant.model_validate(payload.get("grant"))
        except ValueError as exc:
            raise RecoveryExecutorError("RECOVERY_BROKER_RESPONSE_INVALID") from exc

    async def execute(
        self,
        operation: RecoveryOperation,
        *,
        root: Path,
        grant: RecoveryAuthorizationGrant,
    ) -> tuple[str, ...]:
        result = await self._request(
            {
                "action": "recovery.execute",
                "operation": operation.model_dump(mode="json"),
                "root": str(root),
                "grant": grant.model_dump(mode="json"),
            }
        )
        changes = result.get("changes")
        if not isinstance(changes, list) or not all(isinstance(item, str) for item in changes):
            raise RecoveryExecutorError("RECOVERY_BROKER_RESPONSE_INVALID")
        return tuple(changes)

    async def _request(
        self,
        request: dict[str, Any],
        *,
        on_challenge: ChallengeObserver | None = None,
    ) -> dict[str, Any]:
        encoded = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > _MAX_REQUEST_BYTES:
            raise RecoveryExecutorError("RECOVERY_BROKER_REQUEST_TOO_LARGE")
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(self.socket_path)), timeout=3.0
            )
        except (OSError, TimeoutError) as exc:
            raise RecoveryExecutorError("RECOVERY_BROKER_UNAVAILABLE") from exc
        try:
            writer.write(encoded + b"\n")
            await writer.drain()
            while True:
                raw = await asyncio.wait_for(reader.readline(), timeout=self.timeout_seconds)
                if not raw:
                    raise RecoveryExecutorError("RECOVERY_BROKER_DISCONNECTED")
                if len(raw) > _MAX_RESPONSE_BYTES:
                    raise RecoveryExecutorError("RECOVERY_BROKER_RESPONSE_TOO_LARGE")
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise RecoveryExecutorError("RECOVERY_BROKER_RESPONSE_INVALID") from exc
                if not isinstance(message, dict):
                    raise RecoveryExecutorError("RECOVERY_BROKER_RESPONSE_INVALID")
                kind = message.get("type")
                if kind == "authorization_requested":
                    challenge_id = message.get("challenge_id")
                    if not isinstance(challenge_id, str) or on_challenge is None:
                        raise RecoveryExecutorError("RECOVERY_BROKER_RESPONSE_INVALID")
                    await on_challenge(challenge_id)
                    continue
                if kind == "error":
                    code = message.get("code")
                    raise RecoveryExecutorError(
                        code if isinstance(code, str) else "RECOVERY_BROKER_FAILED"
                    )
                payload = message.get("payload")
                if kind != "result" or not isinstance(payload, dict):
                    raise RecoveryExecutorError("RECOVERY_BROKER_RESPONSE_INVALID")
                return {str(key): value for key, value in payload.items()}
        except TimeoutError as exc:
            raise RecoveryExecutorError("RECOVERY_BROKER_TIMEOUT") from exc
        finally:
            writer.close()
            await writer.wait_closed()
