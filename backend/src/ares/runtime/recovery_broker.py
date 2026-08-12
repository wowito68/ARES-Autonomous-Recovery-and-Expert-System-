"""Root-only semantic broker for independently authorized recovery child operations."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from ares.audit import AuditLedger
from ares.protection import ProtectionCheckpointStatus
from ares.recovery.checkpoints import RecoveryCheckpointError, RecoveryCheckpointProvider
from ares.recovery.executor import RecoveryAuthorizationGrant, RecoveryExecutorError
from ares.recovery.integrity import recovery_operation_fingerprint
from ares.recovery.models import ConfigurationDiff, RecoveryOperation, RecoveryStrategyKind
from ares.tools.recovery import (
    AptPackageManagerAdapter,
    ConfigurationRecoveryTool,
    InitramfsRecoveryTool,
    RecoveryProcessRunner,
    RecoveryToolError,
)

BrokerSend = Callable[[dict[str, Any]], Awaitable[None]]


class RecoveryConsentClient(Protocol):
    async def request_recovery(
        self,
        operation: RecoveryOperation,
        session_id: str,
        operation_fingerprint_sha256: str,
    ) -> dict[str, Any]: ...

    async def wait(
        self, challenge_id: str, timeout_seconds: float = 600.0
    ) -> dict[str, Any]: ...


class RecoveryBroker:
    def __init__(
        self,
        *,
        runner: RecoveryProcessRunner,
        audit: AuditLedger,
        consent: RecoveryConsentClient,
        checkpoint_root: Path,
        allowed_client_uids: frozenset[int] = frozenset({971, 1000}),
    ) -> None:
        self.packages = AptPackageManagerAdapter(runner)
        self.configuration = ConfigurationRecoveryTool()
        self.initramfs = InitramfsRecoveryTool(runner)
        self.checkpoints = RecoveryCheckpointProvider(checkpoint_root)
        self.audit = audit
        self.consent = consent
        self.allowed_client_uids = allowed_client_uids
        self._grants: dict[str, RecoveryAuthorizationGrant] = {}
        self._lock = asyncio.Lock()

    async def dispatch(
        self,
        request: dict[str, Any],
        peer_uid: int,
        send: BrokerSend,
    ) -> dict[str, Any]:
        if peer_uid not in self.allowed_client_uids:
            raise PermissionError("recovery broker client not authorized")
        action = request.get("action")
        if action == "recovery.protect":
            return await self._protect(request)
        if action == "recovery.authorize":
            return await self._authorize(request, send)
        if action == "recovery.execute":
            return await self._execute(request)
        if action == "recovery.rollback":
            return await self._rollback(request)
        raise RecoveryExecutorError("RECOVERY_BROKER_ACTION_REJECTED")

    async def _protect(self, request: dict[str, Any]) -> dict[str, Any]:
        operation = RecoveryOperation.model_validate(request.get("operation"))
        root = _root(request.get("root"))
        session_id = _session(request.get("session_id"))
        try:
            checkpoint = await asyncio.to_thread(
                self.checkpoints.create,
                operation,
                root=root,
                session_id=session_id,
            )
        except RecoveryCheckpointError as exc:
            raise RecoveryExecutorError(exc.code) from exc
        await self.audit.append(
            event_type="recovery.protection.created",
            source="ares-tool-broker",
            correlation_id=operation.operation_id,
            session_id=session_id,
            payload={
                "operation_id": operation.operation_id,
                "capability_id": operation.capability_id,
                "checkpoint_id": checkpoint.id,
                "resource_count": len(checkpoint.protected_resources),
            },
        )
        return {"checkpoint": checkpoint.model_dump(mode="json")}

    async def _authorize(
        self, request: dict[str, Any], send: BrokerSend
    ) -> dict[str, Any]:
        operation = RecoveryOperation.model_validate(request.get("operation"))
        session_id = _session(request.get("session_id"))
        checkpoint = operation.protection_checkpoint
        if (
            checkpoint is None
            or checkpoint.status is not ProtectionCheckpointStatus.READY
            or checkpoint.provider_id != operation.operation_id
        ):
            raise RecoveryExecutorError("RECOVERY_PROTECTION_REQUIRED")
        fingerprint = recovery_operation_fingerprint(operation)
        await self.audit.append(
            event_type="recovery.authorization.intent",
            source="ares-tool-broker",
            correlation_id=operation.operation_id,
            session_id=session_id,
            payload={
                "operation_id": operation.operation_id,
                "operation_fingerprint": fingerprint,
                "capability_id": operation.capability_id,
                "risk": operation.risk.value,
                "checkpoint_id": checkpoint.id,
            },
        )
        challenge = await self.consent.request_recovery(
            operation, session_id, fingerprint
        )
        challenge_id = challenge.get("challenge_id")
        if not isinstance(challenge_id, str):
            raise RecoveryExecutorError("RECOVERY_AUTHORIZATION_FAILED")
        await send({"type": "authorization_requested", "challenge_id": challenge_id})
        decision = await self.consent.wait(challenge_id)
        if decision.get("decision") != "approved":
            raise RecoveryExecutorError("RECOVERY_AUTHORIZATION_DENIED")
        operator_uid = decision.get("operator_uid")
        if not isinstance(operator_uid, int):
            raise RecoveryExecutorError("RECOVERY_AUTHORIZATION_FAILED")
        grant = RecoveryAuthorizationGrant(
            operation_id=operation.operation_id,
            operation_fingerprint_sha256=fingerprint,
            session_id=session_id,
            challenge_id=challenge_id,
            checkpoint_id=checkpoint.id,
            operator_uid=operator_uid,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        async with self._lock:
            self._grants[grant.id] = grant
        await self.audit.append(
            event_type="recovery.authorization.granted",
            source="ares-tool-broker",
            correlation_id=operation.operation_id,
            session_id=session_id,
            payload={
                "operation_id": operation.operation_id,
                "operation_fingerprint": fingerprint,
                "challenge_id": challenge_id,
                "checkpoint_id": checkpoint.id,
                "one_use": True,
            },
        )
        return {"grant": grant.model_dump(mode="json")}

    async def _execute(self, request: dict[str, Any]) -> dict[str, Any]:
        operation = RecoveryOperation.model_validate(request.get("operation"))
        grant = RecoveryAuthorizationGrant.model_validate(request.get("grant"))
        root = _root(request.get("root"))
        async with self._lock:
            stored = self._grants.pop(grant.id, None)
        checkpoint = operation.protection_checkpoint
        fingerprint = recovery_operation_fingerprint(operation)
        if (
            stored != grant
            or checkpoint is None
            or checkpoint.id != grant.checkpoint_id
            or grant.operation_id != operation.operation_id
            or grant.operation_fingerprint_sha256 != fingerprint
            or grant.expires_at <= datetime.now(UTC)
        ):
            raise RecoveryExecutorError("RECOVERY_AUTHORIZATION_INVALID")
        await self.audit.append(
            event_type="recovery.operation.intent",
            source="ares-tool-broker",
            correlation_id=operation.operation_id,
            session_id=grant.session_id,
            payload={
                "operation_id": operation.operation_id,
                "operation_fingerprint": fingerprint,
                "capability_id": operation.capability_id,
                "checkpoint_id": checkpoint.id,
            },
        )
        try:
            changes = await self._execute_semantic(operation, root)
        except RecoveryToolError as exc:
            await self.audit.append(
                event_type="recovery.operation.failed",
                source="ares-tool-broker",
                correlation_id=operation.operation_id,
                session_id=grant.session_id,
                payload={"operation_id": operation.operation_id, "error_code": exc.code},
            )
            raise RecoveryExecutorError(exc.code) from exc
        await self.audit.append(
            event_type="recovery.operation.completed",
            source="ares-tool-broker",
            correlation_id=operation.operation_id,
            session_id=grant.session_id,
            payload={"operation_id": operation.operation_id, "change_count": len(changes)},
        )
        return {"changes": list(changes)}

    async def _rollback(self, request: dict[str, Any]) -> dict[str, Any]:
        operation = RecoveryOperation.model_validate(request.get("operation"))
        root = _root(request.get("root"))
        if operation.strategy is not RecoveryStrategyKind.CONFIGURATION:
            raise RecoveryExecutorError("RECOVERY_ROLLBACK_UNSUPPORTED")
        change = ConfigurationDiff.model_validate(operation.payload.get("configuration_diff"))
        try:
            await asyncio.to_thread(self.configuration.rollback, root, change)
        except RecoveryToolError as exc:
            raise RecoveryExecutorError(exc.code) from exc
        await self.audit.append(
            event_type="recovery.operation.rolled-back",
            source="ares-tool-broker",
            correlation_id=operation.operation_id,
            session_id=(
                operation.protection_checkpoint.session_id
                if operation.protection_checkpoint is not None
                else "recovery"
            ),
            payload={"operation_id": operation.operation_id, "path": change.path},
        )
        return {"rolled_back": True}

    async def _execute_semantic(
        self, operation: RecoveryOperation, root: Path
    ) -> tuple[str, ...]:
        if operation.strategy is RecoveryStrategyKind.CONFIGURATION:
            change = ConfigurationDiff.model_validate(operation.payload.get("configuration_diff"))
            backup, digest = await asyncio.to_thread(self.configuration.apply, root, change)
            return (f"configuration backup: {backup}", f"configuration sha256: {digest}")
        if operation.strategy is RecoveryStrategyKind.PACKAGE:
            return await self.packages.repair(
                root, str(operation.payload.get("package_operation", ""))
            )
        if operation.strategy is RecoveryStrategyKind.INITRAMFS:
            return await self.initramfs.rebuild(
                root, str(operation.payload.get("kernel_version", ""))
            )
        raise RecoveryToolError("RECOVERY_OPERATION_UNSUPPORTED")


def _root(value: Any) -> Path:
    if not isinstance(value, str):
        raise RecoveryExecutorError("RECOVERY_TARGET_ROOT_INVALID")
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise RecoveryExecutorError("RECOVERY_TARGET_ROOT_INVALID")
    return path.resolve()


def _session(value: Any) -> str:
    if not isinstance(value, str) or len(value) < 8:
        raise RecoveryExecutorError("RECOVERY_SESSION_INVALID")
    return value
