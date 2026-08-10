"""Privileged broker policy for exact filesystem repair plans."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from ares.audit import AuditLedger, AuditLedgerError
from ares.filesystems.adapters import adapter_for
from ares.filesystems.integrity import repair_plan_integrity_valid
from ares.filesystems.models import FilesystemAuthorizationGrant, FilesystemRepairPlan
from ares.protection import ProtectionCheckpointStatus, ProtectionCheckpointStore
from ares.tools.filesystem import FilesystemToolError, FilesystemToolSuite


class FilesystemConsentClient(Protocol):
    async def request_filesystem(self, plan: FilesystemRepairPlan) -> dict[str, Any]: ...

    async def wait(self, challenge_id: str, timeout_seconds: float = 600.0) -> dict[str, Any]: ...


class FilesystemBroker:
    """Own all block-device writes, one-use grants and durable mutation audit."""

    def __init__(
        self,
        tools: FilesystemToolSuite,
        audit: AuditLedger,
        consent: FilesystemConsentClient,
        checkpoints: ProtectionCheckpointStore,
        *,
        allowed_client_uids: frozenset[int] = frozenset({971, 1000}),
        emergency_journal: Path = Path("/var/lib/ares/broker/filesystem-reconciliation.jsonl"),
    ) -> None:
        self.tools = tools
        self.audit = audit
        self.consent = consent
        self.checkpoints = checkpoints
        self.allowed_client_uids = allowed_client_uids
        self.emergency_journal = emergency_journal
        self._grants: dict[str, FilesystemAuthorizationGrant] = {}
        self._lock = asyncio.Lock()

    async def dispatch(
        self,
        request: dict[str, Any],
        peer_uid: int,
        send,
    ) -> dict[str, Any]:
        if peer_uid not in self.allowed_client_uids:
            raise PermissionError("broker client not authorized")
        action = request.get("action")
        if action == "filesystem.inspect":
            return await self._inspect(request)
        if action == "filesystem.authorize":
            return await self._authorize(request, send)
        if action == "filesystem.execute":
            return await self._execute(request, send)
        raise FilesystemToolError("FILESYSTEM_BROKER_ACTION_REJECTED")

    async def _inspect(self, request: dict[str, Any]) -> dict[str, Any]:
        device = request.get("device")
        if not isinstance(device, str):
            raise FilesystemToolError("FILESYSTEM_TARGET_INVALID")
        inspection = await self.tools.inspect(device)
        await self.audit.append(
            event_type="filesystem.inspection.completed",
            source="ares-tool-broker",
            correlation_id=inspection.id,
            session_id=inspection.id,
            payload={
                "target_fingerprint": inspection.identity.fingerprint_sha256,
                "filesystem": (inspection.filesystem.value if inspection.filesystem else None),
                "health": inspection.health.value,
                "mounted": inspection.mount.mounted,
            },
        )
        return inspection.model_dump(mode="json")

    async def _authorize(self, request: dict[str, Any], send) -> dict[str, Any]:
        plan = FilesystemRepairPlan.model_validate(request.get("plan"))
        await self._validate_plan(plan)
        await self.tools.revalidate(plan.target)
        current_mount = await asyncio.to_thread(self.tools.mount_checker.inspect, plan.target)
        if current_mount != plan.mount:
            raise FilesystemToolError("FILESYSTEM_MOUNT_STATE_CHANGED")
        checkpoint = plan.protection_checkpoint
        assert checkpoint is not None
        await self.audit.append(
            event_type="repair.authorization.intent",
            source="ares-tool-broker",
            correlation_id=plan.repair_id,
            session_id=plan.session_id,
            payload={
                "plan_id": plan.id,
                "plan_fingerprint": plan.fingerprint_sha256,
                "target": plan.target.canonical_path,
                "target_fingerprint": plan.target.fingerprint_sha256,
                "major_minor": plan.target.major_minor,
                "filesystem_uuid": plan.target.filesystem_uuid,
                "partuuid": plan.target.partuuid,
                "filesystem": plan.filesystem.value,
                "checkpoint_id": checkpoint.id,
                "risk": "high",
            },
        )
        try:
            challenge = await self.consent.request_filesystem(plan)
            challenge_id = challenge.get("challenge_id")
            if not isinstance(challenge_id, str):
                raise FilesystemToolError("FILESYSTEM_AUTHORIZATION_FAILED")
            await send({"type": "authorization_requested", "challenge_id": challenge_id})
            decision = await self.consent.wait(challenge_id)
        except FilesystemToolError:
            raise
        except RuntimeError as exc:
            raise FilesystemToolError("FILESYSTEM_AUTHORIZATION_FAILED") from exc
        if decision.get("decision") != "approved":
            raise FilesystemToolError("FILESYSTEM_AUTHORIZATION_DENIED")
        operator_uid = decision.get("operator_uid")
        if not isinstance(operator_uid, int) or operator_uid < 0:
            raise FilesystemToolError("FILESYSTEM_AUTHORIZATION_INVALID")
        await self._validate_plan(plan)
        await self.tools.revalidate(plan.target)
        current_mount = await asyncio.to_thread(self.tools.mount_checker.inspect, plan.target)
        if current_mount != plan.mount:
            raise FilesystemToolError("FILESYSTEM_MOUNT_STATE_CHANGED")
        grant = FilesystemAuthorizationGrant(
            id=uuid4().hex,
            challenge_id=challenge_id,
            plan_id=plan.id,
            repair_id=plan.repair_id,
            session_id=plan.session_id,
            target_fingerprint_sha256=plan.target.fingerprint_sha256,
            plan_fingerprint_sha256=plan.fingerprint_sha256,
            operator_uid=operator_uid,
            expires_at=min(plan.expires_at, datetime.now(UTC) + timedelta(minutes=5)),
        )
        async with self._lock:
            self._grants[grant.id] = grant
        await self.audit.append(
            event_type="repair.authorization.granted",
            source="ares-tool-broker",
            correlation_id=plan.repair_id,
            session_id=plan.session_id,
            payload={
                "grant_id_hash": _token(grant.id),
                "challenge_id": challenge_id,
                "operator_uid": operator_uid,
                "plan_id": plan.id,
                "plan_fingerprint": plan.fingerprint_sha256,
                "target_fingerprint": plan.target.fingerprint_sha256,
                "one_use": True,
            },
        )
        return grant.model_dump(mode="json")

    async def _execute(self, request: dict[str, Any], send) -> dict[str, Any]:
        plan = FilesystemRepairPlan.model_validate(request.get("plan"))
        grant = FilesystemAuthorizationGrant.model_validate(request.get("grant"))
        async with self._lock:
            stored = self._grants.pop(grant.id, None)
        if not self._grant_matches(plan, grant, stored):
            raise FilesystemToolError("FILESYSTEM_AUTHORIZATION_INVALID")
        await self._validate_plan(plan)
        await self.tools.revalidate(plan.target)
        current_mount = await asyncio.to_thread(self.tools.mount_checker.inspect, plan.target)
        if current_mount != plan.mount:
            raise FilesystemToolError("FILESYSTEM_MOUNT_STATE_CHANGED")
        checkpoint = plan.protection_checkpoint
        assert checkpoint is not None
        adapter = adapter_for(plan.filesystem)
        repair_tool, repair_args = adapter.repair_invocation(
            plan.target.canonical_path,
            image=not plan.target.block_device,
        )
        await self.audit.append(
            event_type="repair.execution.intent",
            source="ares-tool-broker",
            correlation_id=plan.repair_id,
            session_id=plan.session_id,
            payload={
                "plan_id": plan.id,
                "plan_fingerprint": plan.fingerprint_sha256,
                "target": plan.target.canonical_path,
                "target_fingerprint": plan.target.fingerprint_sha256,
                "major_minor": plan.target.major_minor,
                "filesystem_uuid": plan.target.filesystem_uuid,
                "partuuid": plan.target.partuuid,
                "filesystem": plan.filesystem.value,
                "checkpoint_id": checkpoint.id,
                "operator_uid": grant.operator_uid,
                "tool": repair_tool,
                "safe_arguments": list(repair_args),
            },
        )

        async def stage(name: str, payload: dict[str, Any]) -> None:
            sanitized = _sanitize_stage(payload)
            await send({"type": "stage", "name": name, "payload": sanitized})
            if name in {
                "filesystem.unmounted",
                "filesystem.repair-command.started",
                "filesystem.repair-command.completed",
                "filesystem.verification.started",
                "filesystem.verification.completed",
                "filesystem.remounted",
            }:
                await self.audit.append(
                    event_type=name,
                    source="ares-tool-broker",
                    correlation_id=plan.repair_id,
                    session_id=plan.session_id,
                    payload={
                        "target_fingerprint": plan.target.fingerprint_sha256,
                        **sanitized,
                    },
                )

        try:
            outcome = await self.tools.execute_repair(plan, stage)
        except BaseException as exc:
            await self._audit_failure(plan, exc)
            raise
        try:
            await self.audit.append(
                event_type="repair.execution.completed",
                source="ares-tool-broker",
                correlation_id=plan.repair_id,
                session_id=plan.session_id,
                payload={
                    "target_fingerprint": plan.target.fingerprint_sha256,
                    "filesystem": plan.filesystem.value,
                    "tool": outcome.repair_tool,
                    "repair_exit_code": outcome.repair_exit_code,
                    "after_health": outcome.after.health.value,
                    "remounted": outcome.remounted,
                },
            )
        except AuditLedgerError as exc:
            await asyncio.to_thread(
                self._emergency,
                {
                    "event": "repair.execution.completed",
                    "repair_id": plan.repair_id,
                    "target_fingerprint": plan.target.fingerprint_sha256,
                    "after_health": outcome.after.health.value,
                    "reconciliation_required": True,
                },
            )
            raise FilesystemToolError("FILESYSTEM_RECONCILIATION_REQUIRED") from exc
        return outcome.model_dump(mode="json")

    @staticmethod
    def _grant_matches(
        plan: FilesystemRepairPlan,
        grant: FilesystemAuthorizationGrant,
        stored: FilesystemAuthorizationGrant | None,
    ) -> bool:
        return bool(
            stored is not None
            and stored == grant
            and grant.expires_at > datetime.now(UTC)
            and grant.plan_id == plan.id
            and grant.repair_id == plan.repair_id
            and grant.session_id == plan.session_id
            and grant.target_fingerprint_sha256 == plan.target.fingerprint_sha256
            and grant.plan_fingerprint_sha256 == plan.fingerprint_sha256
        )

    async def _validate_plan(self, plan: FilesystemRepairPlan) -> None:
        checkpoint = plan.protection_checkpoint
        if (
            not repair_plan_integrity_valid(plan)
            or not plan.executable
            or checkpoint is None
            or checkpoint.status is not ProtectionCheckpointStatus.READY
            or checkpoint.session_id != plan.session_id
            or plan.protected_resource_id not in checkpoint.protected_resources
            or checkpoint.resource_fingerprints.get(plan.protected_resource_id)
            != plan.target.fingerprint_sha256
            or checkpoint.verification_id is None
            or plan.expires_at <= datetime.now(UTC)
        ):
            raise FilesystemToolError("FILESYSTEM_PROTECTION_CHECKPOINT_INVALID")
        durable = await self.checkpoints.get(checkpoint.id)
        if durable is None or durable != checkpoint:
            raise FilesystemToolError("FILESYSTEM_PROTECTION_CHECKPOINT_INVALID")

    async def _audit_failure(self, plan: FilesystemRepairPlan, exc: BaseException) -> None:
        code = _safe_code(exc)
        try:
            await self.audit.append(
                event_type="repair.execution.failed",
                source="ares-tool-broker",
                correlation_id=plan.repair_id,
                session_id=plan.session_id,
                payload={
                    "target_fingerprint": plan.target.fingerprint_sha256,
                    "filesystem": plan.filesystem.value,
                    "error_code": code,
                },
            )
        except AuditLedgerError:
            await asyncio.to_thread(
                self._emergency,
                {
                    "event": "repair.execution.failed",
                    "repair_id": plan.repair_id,
                    "target_fingerprint": plan.target.fingerprint_sha256,
                    "error_code": code,
                },
            )

    def _emergency(self, record: dict[str, Any]) -> None:
        self.emergency_journal.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        encoded = (
            json.dumps(
                {"timestamp": datetime.now(UTC).isoformat(), **record},
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        descriptor = os.open(
            self.emergency_journal,
            os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_CLOEXEC,
            0o600,
        )
        try:
            os.write(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _sanitize_stage(payload: dict[str, Any]) -> dict[str, Any]:
    allowed: dict[str, Any] = {}
    for key in ("tool", "exit_code", "health", "success"):
        value = payload.get(key)
        if isinstance(value, (str, int, bool)) or value is None:
            allowed[key] = value
    return allowed


def _token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _safe_code(exc: BaseException) -> str:
    if isinstance(exc, FilesystemToolError):
        return exc.code
    if isinstance(exc, AuditLedgerError):
        return "AUDIT_LEDGER_UNAVAILABLE"
    return "FILESYSTEM_BROKER_FAILED"
