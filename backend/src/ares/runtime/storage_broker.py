"""Privileged broker policy for exact StorageOperationPlan partition-table mutations."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from ares.audit import AuditLedger, AuditLedgerError
from ares.protection import (
    ProtectionCheckpoint,
    ProtectionCheckpointStatus,
    ProtectionCheckpointStore,
)
from ares.storage_operations.integrity import (
    canonical_sha256,
    partition_geometry_signature,
    storage_plan_integrity_valid,
)
from ares.storage_operations.models import (
    StorageAuthorizationGrant,
    StorageCheckpointBundle,
    StorageOperationPlan,
)
from ares.storage_operations.policy import ProductionStorageWriteGate
from ares.storage_operations.store import StorageOperationStore
from ares.tools.partition import PartitionToolError, StoragePartitionToolSuite


class StorageConsentClient(Protocol):
    async def request_storage(self, plan: StorageOperationPlan) -> dict[str, Any]: ...

    async def wait(self, challenge_id: str, timeout_seconds: float = 600.0) -> dict[str, Any]: ...


BrokerSend = Callable[[dict[str, Any]], Awaitable[None]]


class StorageBroker:
    """Own the physical write boundary for partition-table operations."""

    def __init__(
        self,
        tools: StoragePartitionToolSuite,
        audit: AuditLedger,
        consent: StorageConsentClient,
        checkpoints: ProtectionCheckpointStore,
        operation_store: StorageOperationStore,
        *,
        write_gate: ProductionStorageWriteGate,
        allowed_client_uids: frozenset[int] = frozenset({971, 1000}),
        emergency_journal: Path = Path("/var/lib/ares/broker/storage-reconciliation.jsonl"),
    ) -> None:
        self.tools = tools
        self.audit = audit
        self.consent = consent
        self.checkpoints = checkpoints
        self.operation_store = operation_store
        self.write_gate = write_gate
        self.allowed_client_uids = allowed_client_uids
        self.emergency_journal = emergency_journal
        self._grants: dict[str, StorageAuthorizationGrant] = {}
        self._lock = asyncio.Lock()

    async def dispatch(
        self, request: dict[str, Any], peer_uid: int, send: BrokerSend
    ) -> dict[str, Any]:
        if peer_uid not in self.allowed_client_uids:
            raise PermissionError("broker client not authorized")
        action = request.get("action")
        if action == "storage.inspect":
            target = request.get("target_disk")
            if not isinstance(target, str):
                raise PartitionToolError("STORAGE_TARGET_INVALID")
            return (await self.tools.inspect(target)).model_dump(mode="json")
        if action == "storage.dry-run":
            plan = StorageOperationPlan.model_validate(request.get("plan"))
            await self._validate_preflight_plan(plan, checkpoint_required=False)
            return (await self.tools.dry_run(plan)).model_dump(mode="json")
        if action == "storage.checkpoint":
            return await self._checkpoint(request)
        if action == "storage.authorize":
            return await self._authorize(request, send)
        if action == "storage.execute":
            return await self._execute(request, send)
        if action == "storage.verify":
            plan = StorageOperationPlan.model_validate(request.get("plan"))
            await self._validate_postwrite_plan(plan)
            layout = await self.tools.verify_tool.verify(plan)
            await self.audit.append(
                event_type="storage.verification.completed",
                source="ares-tool-broker",
                correlation_id=plan.operation_id,
                session_id=plan.session_id,
                payload={
                    "target_fingerprint": plan.target_disk.fingerprint_sha256,
                    "expected_layout": plan.proposed_layout.partition_table.fingerprint_sha256,
                    "actual_layout": layout.partition_table.fingerprint_sha256,
                    "verified": True,
                },
            )
            return layout.model_dump(mode="json")
        raise PartitionToolError("STORAGE_BROKER_ACTION_REJECTED")

    async def _checkpoint(self, request: dict[str, Any]) -> dict[str, Any]:
        plan = StorageOperationPlan.model_validate(request.get("plan"))
        await self._validate_preflight_plan(plan, checkpoint_required=False)
        checkpoint_id = uuid4().hex
        artifact = await self.tools.create_checkpoint(plan, checkpoint_id)
        evidence = canonical_sha256(
            {
                "checkpoint_id": checkpoint_id,
                "operation_id": plan.operation_id,
                "target_fingerprint": plan.target_disk.fingerprint_sha256,
                "partition_table_fingerprint": artifact.partition_table_fingerprint_sha256,
                "dump_sha256": artifact.dump_sha256,
            }
        )
        checkpoint = ProtectionCheckpoint(
            id=checkpoint_id,
            status=ProtectionCheckpointStatus.READY,
            protected_resources=(plan.protected_resource_id,),
            resource_fingerprints={plan.protected_resource_id: plan.target_disk.fingerprint_sha256},
            provider_capability_id="storage.partition.inspect",
            protection_kind="snapshot",
            verification_id=f"partition-table:{artifact.dump_sha256[:24]}",
            session_id=plan.session_id,
            evidence_sha256=evidence,
            limitations=(
                "Checkpoint restores partition-table metadata only; it is not a data backup.",
                "Execution is limited to table-only create/delete where impact policy permits it.",
            ),
        )
        await self.audit.append(
            event_type="storage.checkpoint.created",
            source="ares-tool-broker",
            correlation_id=plan.operation_id,
            session_id=plan.session_id,
            payload={
                "checkpoint_id": checkpoint.id,
                "target_fingerprint": plan.target_disk.fingerprint_sha256,
                "partition_table_fingerprint": artifact.partition_table_fingerprint_sha256,
                "dump_sha256": artifact.dump_sha256,
            },
        )
        return StorageCheckpointBundle(checkpoint=checkpoint, artifact=artifact).model_dump(
            mode="json"
        )

    async def _authorize(self, request: dict[str, Any], send: BrokerSend) -> dict[str, Any]:
        plan = StorageOperationPlan.model_validate(request.get("plan"))
        await self._validate_preflight_plan(plan, checkpoint_required=True)
        checkpoint = plan.protection_checkpoint
        assert checkpoint is not None
        await self.audit.append(
            event_type="storage.authorization.intent",
            source="ares-tool-broker",
            correlation_id=plan.operation_id,
            session_id=plan.session_id,
            payload=_audit_plan(plan),
        )
        try:
            challenge = await self.consent.request_storage(plan)
            challenge_id = challenge.get("challenge_id")
            if not isinstance(challenge_id, str):
                raise PartitionToolError("STORAGE_AUTHORIZATION_FAILED")
            await send({"type": "authorization_requested", "challenge_id": challenge_id})
            decision = await self.consent.wait(challenge_id)
        except PartitionToolError:
            raise
        except RuntimeError as exc:
            raise PartitionToolError("STORAGE_AUTHORIZATION_FAILED") from exc
        if decision.get("decision") != "approved":
            raise PartitionToolError("STORAGE_AUTHORIZATION_DENIED")
        operator_uid = decision.get("operator_uid")
        if not isinstance(operator_uid, int) or operator_uid < 0:
            raise PartitionToolError("STORAGE_AUTHORIZATION_INVALID")
        await self._validate_preflight_plan(plan, checkpoint_required=True)
        grant = StorageAuthorizationGrant(
            id=uuid4().hex,
            challenge_id=challenge_id,
            operation_id=plan.operation_id,
            plan_id=plan.id,
            session_id=plan.session_id,
            target_fingerprint_sha256=plan.target_disk.fingerprint_sha256,
            plan_fingerprint_sha256=plan.fingerprint_sha256,
            operator_uid=operator_uid,
            expires_at=min(plan.expires_at, datetime.now(UTC) + timedelta(minutes=5)),
        )
        async with self._lock:
            self._grants[grant.id] = grant
        await self.audit.append(
            event_type="storage.authorization.granted",
            source="ares-tool-broker",
            correlation_id=plan.operation_id,
            session_id=plan.session_id,
            payload={
                "challenge_id": challenge_id,
                "grant_id_hash": _token(grant.id),
                "operator_uid": operator_uid,
                "plan_id": plan.id,
                "plan_fingerprint": plan.fingerprint_sha256,
                "target_fingerprint": plan.target_disk.fingerprint_sha256,
                "checkpoint_id": checkpoint.id,
                "one_use": True,
            },
        )
        return grant.model_dump(mode="json")

    async def _execute(self, request: dict[str, Any], send: BrokerSend) -> dict[str, Any]:
        plan = StorageOperationPlan.model_validate(request.get("plan"))
        grant = StorageAuthorizationGrant.model_validate(request.get("grant"))
        async with self._lock:
            stored = self._grants.pop(grant.id, None)
        if not _grant_matches(plan, grant, stored):
            raise PartitionToolError("STORAGE_AUTHORIZATION_INVALID")
        # One-use grant is consumed before the final revalidation so a failed preflight
        # cannot leave reusable write authority.
        await self._validate_preflight_plan(plan, checkpoint_required=True)
        await self.audit.append(
            event_type="storage.transaction.intent",
            source="ares-tool-broker",
            correlation_id=plan.operation_id,
            session_id=plan.session_id,
            payload={**_audit_plan(plan), "operator_uid": grant.operator_uid, "tool": "sfdisk"},
        )
        observer_lost = False

        async def stage(name: str, payload: dict[str, object]) -> None:
            nonlocal observer_lost
            sanitized = _sanitize_stage(payload)
            if not observer_lost:
                try:
                    await send({"type": "stage", "name": name, "payload": sanitized})
                except (OSError, RuntimeError):
                    observer_lost = True
            await self.audit.append(
                event_type=name,
                source="ares-tool-broker",
                correlation_id=plan.operation_id,
                session_id=plan.session_id,
                payload={
                    "target_fingerprint": plan.target_disk.fingerprint_sha256,
                    **sanitized,
                },
            )

        try:
            outcome = await self.tools.execute(plan, on_stage=stage)
        except BaseException as exc:
            await self._audit_failure(plan, exc)
            raise
        try:
            await self.audit.append(
                event_type="storage.transaction.completed",
                source="ares-tool-broker",
                correlation_id=plan.operation_id,
                session_id=plan.session_id,
                payload={
                    "target_fingerprint": plan.target_disk.fingerprint_sha256,
                    "operation": plan.operation.value,
                    "exit_code": outcome.exit_code,
                    "kernel_reread": outcome.kernel_reread,
                    "before_layout": plan.original_layout.partition_table.fingerprint_sha256,
                    "after_layout": outcome.after.partition_table.fingerprint_sha256,
                    "observer_lost": observer_lost,
                },
            )
        except AuditLedgerError as exc:
            await asyncio.to_thread(
                self._emergency,
                {
                    "event": "storage.transaction.completed",
                    "operation_id": plan.operation_id,
                    "target_fingerprint": plan.target_disk.fingerprint_sha256,
                    "after_layout": outcome.after.partition_table.fingerprint_sha256,
                    "reconciliation_required": True,
                },
            )
            raise PartitionToolError("STORAGE_RECONCILIATION_REQUIRED") from exc
        return outcome.model_dump(mode="json")

    async def _validate_preflight_plan(
        self, plan: StorageOperationPlan, *, checkpoint_required: bool
    ) -> None:
        if not storage_plan_integrity_valid(plan) or plan.expires_at <= datetime.now(UTC):
            raise PartitionToolError("STORAGE_OPERATION_PLAN_INVALID")
        if not self.write_gate.evaluate(plan.target_disk).allowed:
            raise PartitionToolError("PRODUCTION_STORAGE_WRITE_GATE_BLOCKED")
        current = await self.tools.inspect(plan.target_disk.requested_path)
        if current.disk.identity.fingerprint_sha256 != plan.target_disk.fingerprint_sha256:
            raise PartitionToolError("STORAGE_DEVICE_IDENTITY_CHANGED")
        if partition_geometry_signature(current.partition_table) != partition_geometry_signature(
            plan.original_layout.partition_table
        ):
            raise PartitionToolError("STORAGE_LAYOUT_CHANGED")
        if checkpoint_required:
            if not plan.executable or plan.dry_run is None or not plan.dry_run.valid:
                raise PartitionToolError("STORAGE_OPERATION_NOT_EXECUTABLE")
            checkpoint = plan.protection_checkpoint
            if (
                checkpoint is None
                or checkpoint.status is not ProtectionCheckpointStatus.READY
                or checkpoint.session_id != plan.session_id
                or checkpoint.verification_id is None
                or plan.protected_resource_id not in checkpoint.protected_resources
                or checkpoint.resource_fingerprints.get(plan.protected_resource_id)
                != plan.target_disk.fingerprint_sha256
            ):
                raise PartitionToolError("STORAGE_PROTECTION_CHECKPOINT_INVALID")
            durable = await self.checkpoints.get(checkpoint.id)
            artifact = await self.operation_store.get_checkpoint(checkpoint.id)
            if (
                durable != checkpoint
                or artifact is None
                or artifact.operation_id != plan.operation_id
                or artifact.partition_table_fingerprint_sha256
                != plan.original_layout.partition_table.fingerprint_sha256
            ):
                raise PartitionToolError("STORAGE_PROTECTION_CHECKPOINT_INVALID")

    async def _validate_postwrite_plan(self, plan: StorageOperationPlan) -> None:
        if not storage_plan_integrity_valid(plan) or plan.expires_at <= datetime.now(UTC):
            raise PartitionToolError("STORAGE_OPERATION_PLAN_INVALID")
        if not plan.executable or plan.dry_run is None or not plan.dry_run.valid:
            raise PartitionToolError("STORAGE_OPERATION_NOT_EXECUTABLE")
        if not self.write_gate.evaluate(plan.target_disk).allowed:
            raise PartitionToolError("PRODUCTION_STORAGE_WRITE_GATE_BLOCKED")
        current_identity = await asyncio.to_thread(
            self.tools.identity.identify,
            plan.target_disk.requested_path,
        )
        if current_identity.fingerprint_sha256 != plan.target_disk.fingerprint_sha256:
            raise PartitionToolError("STORAGE_DEVICE_IDENTITY_CHANGED")
        checkpoint = plan.protection_checkpoint
        if (
            checkpoint is None
            or checkpoint.status is not ProtectionCheckpointStatus.READY
            or checkpoint.session_id != plan.session_id
            or checkpoint.verification_id is None
            or plan.protected_resource_id not in checkpoint.protected_resources
            or checkpoint.resource_fingerprints.get(plan.protected_resource_id)
            != plan.target_disk.fingerprint_sha256
        ):
            raise PartitionToolError("STORAGE_PROTECTION_CHECKPOINT_INVALID")
        durable = await self.checkpoints.get(checkpoint.id)
        artifact = await self.operation_store.get_checkpoint(checkpoint.id)
        if (
            durable != checkpoint
            or artifact is None
            or artifact.operation_id != plan.operation_id
            or artifact.partition_table_fingerprint_sha256
            != plan.original_layout.partition_table.fingerprint_sha256
        ):
            raise PartitionToolError("STORAGE_PROTECTION_CHECKPOINT_INVALID")

    async def _audit_failure(self, plan: StorageOperationPlan, exc: BaseException) -> None:
        code = _safe_code(exc)
        try:
            await self.audit.append(
                event_type="storage.transaction.failed",
                source="ares-tool-broker",
                correlation_id=plan.operation_id,
                session_id=plan.session_id,
                payload={
                    "target_fingerprint": plan.target_disk.fingerprint_sha256,
                    "operation": plan.operation.value,
                    "error_code": code,
                },
            )
        except AuditLedgerError:
            await asyncio.to_thread(
                self._emergency,
                {
                    "event": "storage.transaction.failed",
                    "operation_id": plan.operation_id,
                    "target_fingerprint": plan.target_disk.fingerprint_sha256,
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


def _grant_matches(
    plan: StorageOperationPlan,
    grant: StorageAuthorizationGrant,
    stored: StorageAuthorizationGrant | None,
) -> bool:
    return bool(
        stored is not None
        and stored == grant
        and grant.expires_at > datetime.now(UTC)
        and grant.operation_id == plan.operation_id
        and grant.plan_id == plan.id
        and grant.session_id == plan.session_id
        and grant.target_fingerprint_sha256 == plan.target_disk.fingerprint_sha256
        and grant.plan_fingerprint_sha256 == plan.fingerprint_sha256
    )


def _audit_plan(plan: StorageOperationPlan) -> dict[str, Any]:
    checkpoint = plan.protection_checkpoint
    return {
        "plan_id": plan.id,
        "plan_fingerprint": plan.fingerprint_sha256,
        "operation": plan.operation.value,
        "target": plan.target_disk.canonical_path,
        "target_fingerprint": plan.target_disk.fingerprint_sha256,
        "device_kind": plan.target_disk.device_kind,
        "original_layout": plan.original_layout.partition_table.fingerprint_sha256,
        "proposed_layout": plan.proposed_layout.partition_table.fingerprint_sha256,
        "dry_run_script_sha256": plan.dry_run.script_sha256 if plan.dry_run else None,
        "checkpoint_id": checkpoint.id if checkpoint else None,
        "data_impact": plan.data_impact.level.value,
        "boot_impact": plan.boot_impact.level.value,
        "risk": plan.risk,
        "write_gate": plan.write_gate.allowed,
    }


def _sanitize_stage(payload: dict[str, object]) -> dict[str, object]:
    safe: dict[str, object] = {}
    for key in ("operation", "target_fingerprint", "exit_code", "kernel_reread"):
        value = payload.get(key)
        if isinstance(value, (str, int, bool)) or value is None:
            safe[key] = value
    return safe


def _token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _safe_code(exc: BaseException) -> str:
    if isinstance(exc, PartitionToolError):
        return exc.code
    if isinstance(exc, AuditLedgerError):
        return "AUDIT_LEDGER_UNAVAILABLE"
    if isinstance(exc, asyncio.CancelledError):
        return "STORAGE_BROKER_CANCELLED"
    return "STORAGE_BROKER_FAILED"
