"""Declarative Storage Operation Engine for partition-table changes."""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Literal, cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ares.protection import ProtectionCheckpointStatus, ProtectionCheckpointStore
from ares.storage_operations.executor import (
    StorageExecutorError,
    StorageOperationExecutor,
)
from ares.storage_operations.integrity import (
    layout_fingerprint,
    partition_geometry_signature,
    storage_plan_fingerprint,
    storage_plan_integrity_valid,
)
from ares.storage_operations.models import (
    BootImpactLevel,
    DataImpactAssessment,
    DataImpactLevel,
    PartitionResource,
    PartitionTable,
    PartitionTableType,
    StorageAuthorizationGrant,
    StorageLayout,
    StorageOperationOutcome,
    StorageOperationPlan,
    StorageOperationType,
    StoragePrimitiveOperation,
    StorageTransaction,
    StorageTransactionStatus,
    StorageVerification,
    StorageVerificationStatus,
    StorageVerificationStep,
)
from ares.storage_operations.policy import (
    ProductionStorageWriteGate,
    analyze_boot_impact,
    analyze_data_impact,
)
from ares.storage_operations.store import StorageOperationStore
from ares.tools.partition import (
    PartitionToolError,
    build_empty_table,
    layout_with_table,
    new_partition_resource,
    with_partitions,
)


class StorageOperationEngineError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class DeclarativeStorageOperationRequest(BaseModel):
    """Desired state input; contains no executable, command, argv or Action ID."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: Literal["create", "delete", "resize", "move"]
    target_disk: str = Field(min_length=1, max_length=4096)
    partition_number: int | None = Field(default=None, ge=1)
    size_bytes: int | None = Field(default=None, ge=1_048_576)
    new_size_bytes: int | None = Field(default=None, ge=1_048_576)
    new_start_sector: int | None = Field(default=None, ge=0)
    table_type: PartitionTableType | None = None
    partition_type: str | None = Field(default=None, max_length=128)
    partition_name: str | None = Field(default=None, max_length=96)
    free_region_index: int = Field(default=0, ge=0, le=127)

    @model_validator(mode="after")
    def operation_fields(self) -> DeclarativeStorageOperationRequest:
        if self.operation == "create" and self.size_bytes is None:
            raise ValueError("create requires size_bytes")
        if self.operation in {"delete", "resize", "move"} and self.partition_number is None:
            raise ValueError(f"{self.operation} requires partition_number")
        if self.operation == "resize" and self.new_size_bytes is None:
            raise ValueError("resize requires new_size_bytes")
        if self.operation == "move" and self.new_start_sector is None:
            raise ValueError("move requires new_start_sector")
        return self


class StorageOperationEngine:
    """Own validation, impact, dry-run, protection, execution and verification policy."""

    def __init__(
        self,
        *,
        executor: StorageOperationExecutor,
        store: StorageOperationStore,
        checkpoints: ProtectionCheckpointStore,
        write_gate: ProductionStorageWriteGate,
    ) -> None:
        self.executor = executor
        self.store = store
        self.checkpoints = checkpoints
        self.write_gate = write_gate
        self._grants: dict[str, StorageAuthorizationGrant] = {}

    async def inspect(self, target_disk: str) -> StorageLayout:
        try:
            return await self.executor.inspect(target_disk)
        except StorageExecutorError as exc:
            raise StorageOperationEngineError(exc.code) from exc

    async def plan(
        self,
        request: DeclarativeStorageOperationRequest,
        *,
        session_id: str,
        created_by: str,
    ) -> StorageOperationPlan:
        original = await self.inspect(request.target_disk)
        operation = _operation_enum(request.operation)
        table = original.partition_table
        target: PartitionResource | None = None
        limitations: list[str] = ["validation_and_dry_run_required_before_authorization"]

        if operation.value == "create":
            proposed_table, target = _plan_create(request, original)
        elif operation.value == "delete":
            target = _find_partition(original.partition_table.partitions, request.partition_number)
            proposed_table = with_partitions(
                table, tuple(item for item in table.partitions if item.number != target.number)
            )
        elif operation.value == "resize":
            target = _find_partition(original.partition_table.partitions, request.partition_number)
            proposed_table = _plan_resize(request, original, target)
            limitations.append(
                "storage.partition.resize is designed but disabled in this increment"
            )
        else:
            target = _find_partition(original.partition_table.partitions, request.partition_number)
            proposed_table = _plan_move(request, original, target)
            limitations.append("storage.partition.move is designed but disabled in this increment")

        proposed = layout_with_table(original, proposed_table)
        data_impact = analyze_data_impact(operation, original, target)
        boot_impact = analyze_boot_impact(operation, original, target)
        gate = self.write_gate.evaluate(original.disk.identity)
        capability = cast(
            Literal[
                "storage.partition.create",
                "storage.partition.delete",
                "storage.partition.resize",
                "storage.partition.move",
            ],
            f"storage.partition.{request.operation}",
        )
        risk: Literal["low", "medium", "high", "critical"] = (
            "medium"
            if request.operation == "create"
            else "high"
            if request.operation in {"delete", "resize"}
            else "critical"
        )
        enabled = request.operation in {"create", "delete"}
        if not data_impact.executable:
            limitations.extend(data_impact.reasons)
        if not boot_impact.executable:
            limitations.extend(boot_impact.reasons)
        if not gate.allowed:
            limitations.append(gate.reason)
        if not enabled:
            limitations.append("operation_adapter_disabled")
        required = _required_operations(request.operation, request.partition_number)
        draft = StorageOperationPlan(
            capability=capability,
            operation=operation,
            session_id=session_id,
            target_disk=original.disk.identity,
            target_partition=target,
            original_layout=original,
            proposed_layout=proposed,
            required_operations=required,
            affected_resources=_affected_resources(original, target),
            risk=risk,
            estimated_duration_seconds=None,
            data_loss_possible=data_impact.data_loss_possible,
            data_impact=data_impact,
            boot_impact=boot_impact,
            filesystem_impact=_filesystem_impact(data_impact),
            verification_plan=_verification_plan(),
            write_gate=gate,
            executable=False,
            limitations=tuple(dict.fromkeys(limitations)),
            fingerprint_sha256="0" * 64,
        )
        plan = draft.model_copy(update={"fingerprint_sha256": storage_plan_fingerprint(draft)})
        transaction = StorageTransaction(
            id=plan.operation_id,
            operation_id=plan.operation_id,
            plan_id=plan.id,
            session_id=session_id,
            status=StorageTransactionStatus.PLANNED,
            before_layout_sha256=layout_fingerprint(original),
            metadata={"created_by": created_by},
        )
        await self.store.put_plan(plan)
        await self.store.put_transaction(transaction)
        return plan

    async def validate(self, operation_id: str, *, session_id: str) -> StorageOperationPlan:
        record = await self.store.get_record(operation_id)
        if record is None:
            raise StorageOperationEngineError("STORAGE_OPERATION_NOT_FOUND")
        if record.transaction.session_id != session_id:
            raise StorageOperationEngineError("STORAGE_OPERATION_SESSION_MISMATCH")
        if record.transaction.status is StorageTransactionStatus.UNKNOWN:
            await self.reconcile_unknown(operation_id, session_id=session_id)
            refreshed = await self.store.get_record(operation_id)
            if (
                refreshed is None
                or refreshed.transaction.status is StorageTransactionStatus.UNKNOWN
            ):
                raise StorageOperationEngineError("STORAGE_TRANSACTION_RECONCILIATION_REQUIRED")
            return refreshed.plan
        if record.transaction.status not in {
            StorageTransactionStatus.PLANNED,
            StorageTransactionStatus.VALIDATED,
            StorageTransactionStatus.PROTECTED,
        }:
            raise StorageOperationEngineError("STORAGE_OPERATION_NOT_VALIDATABLE")
        plan = record.plan
        if plan.expires_at <= datetime.now(UTC):
            raise StorageOperationEngineError("STORAGE_OPERATION_PLAN_EXPIRED")
        current = await self.inspect(plan.target_disk.requested_path)
        if current.disk.identity.fingerprint_sha256 != plan.target_disk.fingerprint_sha256:
            raise StorageOperationEngineError("STORAGE_DEVICE_IDENTITY_CHANGED")
        if partition_geometry_signature(current.partition_table) != partition_geometry_signature(
            plan.original_layout.partition_table
        ):
            raise StorageOperationEngineError("STORAGE_LAYOUT_CHANGED")
        gate = self.write_gate.evaluate(current.disk.identity)
        data_impact = analyze_data_impact(plan.operation, current, plan.target_partition)
        boot_impact = analyze_boot_impact(plan.operation, current, plan.target_partition)
        adapter_enabled = plan.operation.value in {"create", "delete"}
        candidate = (
            gate.allowed and data_impact.executable and boot_impact.executable and adapter_enabled
        )
        preflight_draft = plan.model_copy(
            update={
                "target_disk": current.disk.identity,
                "original_layout": current,
                "data_impact": data_impact,
                "boot_impact": boot_impact,
                "write_gate": gate,
                "protection_checkpoint": None,
                "dry_run": None,
                "executable": False,
                "fingerprint_sha256": "0" * 64,
            }
        )
        preflight = preflight_draft.model_copy(
            update={"fingerprint_sha256": storage_plan_fingerprint(preflight_draft)}
        )
        try:
            dry_run = await self.executor.dry_run(preflight)
        except StorageExecutorError as exc:
            raise StorageOperationEngineError(exc.code) from exc
        if not dry_run.valid:
            candidate = False
        checkpoint = None
        if candidate:
            checkpoint_plan_draft = preflight.model_copy(
                update={"dry_run": dry_run, "fingerprint_sha256": "0" * 64}
            )
            checkpoint_plan = checkpoint_plan_draft.model_copy(
                update={"fingerprint_sha256": storage_plan_fingerprint(checkpoint_plan_draft)}
            )
            try:
                bundle = await self.executor.create_checkpoint(checkpoint_plan)
            except StorageExecutorError as exc:
                raise StorageOperationEngineError(exc.code) from exc
            checkpoint = bundle.checkpoint
            if (
                checkpoint.status is not ProtectionCheckpointStatus.READY
                or checkpoint.session_id != plan.session_id
                or checkpoint.verification_id is None
                or plan.protected_resource_id not in checkpoint.protected_resources
                or checkpoint.resource_fingerprints.get(plan.protected_resource_id)
                != plan.target_disk.fingerprint_sha256
                or bundle.artifact.partition_table_fingerprint_sha256
                != plan.original_layout.partition_table.fingerprint_sha256
            ):
                raise StorageOperationEngineError("STORAGE_PROTECTION_CHECKPOINT_INVALID")
            await self.store.put_checkpoint(bundle.artifact)
            await self.checkpoints.put(checkpoint)
        limitations = [
            item
            for item in plan.limitations
            if item != "validation_and_dry_run_required_before_authorization"
        ]
        if not dry_run.valid:
            limitations.append("dry_run_rejected_proposed_layout")
        if not gate.allowed:
            limitations.append(gate.reason)
        if not data_impact.executable:
            limitations.extend(data_impact.reasons)
        if not boot_impact.executable:
            limitations.extend(boot_impact.reasons)
        if not adapter_enabled:
            limitations.append("operation_adapter_disabled")
        validated_draft = preflight.model_copy(
            update={
                "dry_run": dry_run,
                "protection_checkpoint": checkpoint,
                "executable": bool(candidate and checkpoint is not None),
                "limitations": tuple(dict.fromkeys(limitations)),
                "fingerprint_sha256": "0" * 64,
            }
        )
        validated = validated_draft.model_copy(
            update={"fingerprint_sha256": storage_plan_fingerprint(validated_draft)}
        )
        await self.store.put_plan(validated)
        transaction = record.transaction.model_copy(
            update={
                "status": (
                    StorageTransactionStatus.PROTECTED
                    if checkpoint is not None
                    else StorageTransactionStatus.VALIDATED
                ),
                "checkpoint_id": checkpoint.id if checkpoint is not None else None,
                "last_known_stage": "preflight-completed",
            }
        )
        await self.store.put_transaction(transaction)
        return validated

    async def request_authorization(
        self,
        operation_id: str,
        *,
        session_id: str,
        on_challenge: Callable[[str], Awaitable[None]],
    ) -> StorageAuthorizationGrant:
        record = await self.store.get_record(operation_id)
        if record is None:
            raise StorageOperationEngineError("STORAGE_OPERATION_NOT_FOUND")
        plan = record.plan
        checkpoint = plan.protection_checkpoint
        if (
            record.transaction.status is not StorageTransactionStatus.PROTECTED
            or not plan.executable
            or checkpoint is None
            or not storage_plan_integrity_valid(plan)
        ):
            raise StorageOperationEngineError("STORAGE_OPERATION_NOT_AUTHORIZABLE")
        if plan.session_id != session_id:
            raise StorageOperationEngineError("STORAGE_OPERATION_SESSION_MISMATCH")
        durable = await self.checkpoints.get(checkpoint.id)
        artifact = await self.store.get_checkpoint(checkpoint.id)
        if durable != checkpoint or artifact is None:
            raise StorageOperationEngineError("STORAGE_PROTECTION_CHECKPOINT_INVALID")
        try:
            grant = await self.executor.request_authorization(plan, on_challenge=on_challenge)
        except StorageExecutorError as exc:
            raise StorageOperationEngineError(exc.code) from exc
        transaction = record.transaction.model_copy(
            update={
                "status": StorageTransactionStatus.AUTHORIZED,
                "authorization_challenge_id": grant.challenge_id,
                "last_known_stage": "authorization-granted",
            }
        )
        await self.store.put_transaction(transaction)
        self._grants[operation_id] = grant
        return grant

    async def execute_authorized(
        self,
        operation_id: str,
        *,
        session_id: str,
        on_stage: Callable[[str, dict[str, object]], Awaitable[None]],
    ) -> StorageVerification:
        grant = self._grants.pop(operation_id, None)
        if grant is None:
            raise StorageOperationEngineError("STORAGE_AUTHORIZATION_GRANT_UNAVAILABLE")
        return await self.execute(operation_id, grant, session_id=session_id, on_stage=on_stage)

    async def execute(
        self,
        operation_id: str,
        grant: StorageAuthorizationGrant,
        *,
        session_id: str,
        on_stage: Callable[[str, dict[str, object]], Awaitable[None]],
    ) -> StorageVerification:
        record = await self.store.get_record(operation_id)
        if record is None:
            raise StorageOperationEngineError("STORAGE_OPERATION_NOT_FOUND")
        plan = record.plan
        if record.transaction.status is not StorageTransactionStatus.AUTHORIZED:
            raise StorageOperationEngineError("STORAGE_OPERATION_NOT_AUTHORIZED")
        if plan.session_id != session_id or grant.session_id != session_id:
            raise StorageOperationEngineError("STORAGE_OPERATION_SESSION_MISMATCH")
        if not storage_plan_integrity_valid(plan):
            raise StorageOperationEngineError("STORAGE_OPERATION_PLAN_TAMPERED")
        try:
            self.write_gate.require_write_allowed(plan.target_disk)
        except PermissionError as exc:
            raise StorageOperationEngineError("PRODUCTION_STORAGE_WRITE_GATE_BLOCKED") from exc
        await self._require_checkpoint(plan)
        transaction = record.transaction.model_copy(
            update={
                "status": StorageTransactionStatus.EXECUTING,
                "started_at": datetime.now(UTC),
                "last_known_stage": "storage-transaction-started",
                "reconciliation_required": False,
                "error_code": None,
            }
        )
        await self.store.put_transaction(transaction)

        async def stage(name: str, payload: dict[str, object]) -> None:
            current_record = await self.store.get_record(operation_id)
            if current_record is not None:
                updated = current_record.transaction.model_copy(update={"last_known_stage": name})
                await self.store.put_transaction(updated)
            await on_stage(name, payload)

        try:
            outcome = await self.executor.execute(plan, grant, on_stage=stage)
        except StorageExecutorError as exc:
            if exc.code in {
                "STORAGE_BROKER_TIMEOUT",
                "STORAGE_BROKER_DISCONNECTED",
                "STORAGE_KERNEL_REREAD_FAILED",
                "STORAGE_RECONCILIATION_REQUIRED",
            }:
                await self.mark_unknown(operation_id, exc.code)
            else:
                await self.mark_failed(operation_id, exc.code)
            raise StorageOperationEngineError(exc.code) from exc
        await self._set_status(
            operation_id,
            StorageTransactionStatus.VERIFYING,
            last_known_stage="storage-verification-started",
        )
        verification = self.verify_outcome(plan, outcome)
        await self.store.put_verification(verification)
        terminal = (
            StorageTransactionStatus.COMMITTED
            if verification.status is StorageVerificationStatus.VERIFIED
            else StorageTransactionStatus.FAILED
            if verification.status is StorageVerificationStatus.FAILED
            else StorageTransactionStatus.UNKNOWN
        )
        current_record = await self.store.get_record(operation_id)
        if current_record is None:
            raise StorageOperationEngineError("STORAGE_OPERATION_NOT_FOUND")
        updated = current_record.transaction.model_copy(
            update={
                "status": terminal,
                "finished_at": (
                    datetime.now(UTC) if terminal is not StorageTransactionStatus.UNKNOWN else None
                ),
                "after_layout_sha256": (
                    layout_fingerprint(outcome.after) if outcome.after is not None else None
                ),
                "verification_id": verification.id,
                "last_known_stage": "storage-verification-completed",
                "reconciliation_required": terminal is StorageTransactionStatus.UNKNOWN,
                "error_code": (
                    None
                    if terminal is StorageTransactionStatus.COMMITTED
                    else "STORAGE_VERIFICATION_FAILED"
                    if terminal is StorageTransactionStatus.FAILED
                    else "STORAGE_VERIFICATION_UNKNOWN"
                ),
            }
        )
        await self.store.put_transaction(updated)
        return verification

    def verify_outcome(
        self, plan: StorageOperationPlan, outcome: StorageOperationOutcome
    ) -> StorageVerification:
        identity_ok = (
            outcome.after.disk.identity.fingerprint_sha256 == plan.target_disk.fingerprint_sha256
        )
        geometry_ok = partition_geometry_signature(
            outcome.after.partition_table
        ) == partition_geometry_signature(plan.proposed_layout.partition_table)
        if identity_ok and geometry_ok:
            status = StorageVerificationStatus.VERIFIED
            message = "Device identity and proposed partition geometry were verified after write."
        else:
            status = StorageVerificationStatus.FAILED
            message = "Post-write identity or partition geometry differs from the approved plan."
        no_filesystem_change = not plan.data_impact.filesystem_change_required
        no_mount_change = not plan.data_impact.mounted
        no_boot_change = plan.boot_impact.level is BootImpactLevel.NONE
        no_os_impact = not plan.target_partition or not plan.target_partition.operating_system_ids
        return StorageVerification(
            operation_id=plan.operation_id,
            status=status,
            identity_verified=identity_ok,
            partition_table_verified=geometry_ok,
            partition_geometry_verified=geometry_ok,
            filesystem_verified=True if no_filesystem_change else None,
            mountability_verified=True if no_mount_change else None,
            operating_system_verified=True if no_os_impact else None,
            boot_dependencies_verified=True if no_boot_change else None,
            before=outcome.before,
            after=outcome.after,
            evidence=outcome.evidence,
            limitations=(
                "Filesystem verification is not applicable when no filesystem content was modified."
                if no_filesystem_change
                else "Filesystem-aware resize is not enabled.",
            ),
            message=message,
        )

    async def reconcile_unknown(self, operation_id: str, *, session_id: str) -> StorageVerification:
        record = await self.store.get_record(operation_id)
        if record is None:
            raise StorageOperationEngineError("STORAGE_OPERATION_NOT_FOUND")
        if record.transaction.status is not StorageTransactionStatus.UNKNOWN:
            if record.verification is not None:
                return record.verification
            raise StorageOperationEngineError("STORAGE_TRANSACTION_NOT_UNKNOWN")
        if record.transaction.session_id != session_id:
            raise StorageOperationEngineError("STORAGE_OPERATION_SESSION_MISMATCH")
        plan = record.plan
        try:
            current = await self.executor.inspect(plan.target_disk.requested_path)
        except StorageExecutorError as exc:
            raise StorageOperationEngineError(exc.code) from exc
        identity_ok = (
            current.disk.identity.fingerprint_sha256 == plan.target_disk.fingerprint_sha256
        )
        proposed = partition_geometry_signature(
            current.partition_table
        ) == partition_geometry_signature(plan.proposed_layout.partition_table)
        original = partition_geometry_signature(
            current.partition_table
        ) == partition_geometry_signature(plan.original_layout.partition_table)
        if identity_ok and proposed:
            verification = StorageVerification(
                operation_id=operation_id,
                status=StorageVerificationStatus.VERIFIED,
                identity_verified=True,
                partition_table_verified=True,
                partition_geometry_verified=True,
                filesystem_verified=True
                if not plan.data_impact.filesystem_change_required
                else None,
                mountability_verified=True if not plan.data_impact.mounted else None,
                operating_system_verified=(
                    True
                    if not plan.target_partition or not plan.target_partition.operating_system_ids
                    else None
                ),
                boot_dependencies_verified=(
                    True if plan.boot_impact.level is BootImpactLevel.NONE else None
                ),
                before=plan.original_layout,
                after=current,
                evidence=("reconciled_after_unknown=proposed_layout",),
                message="Reinspection proved the approved proposed layout is present.",
            )
            status = StorageTransactionStatus.COMMITTED
            error_code = None
        elif identity_ok and original:
            verification = StorageVerification(
                operation_id=operation_id,
                status=StorageVerificationStatus.PARTIAL,
                identity_verified=True,
                partition_table_verified=True,
                partition_geometry_verified=True,
                before=plan.original_layout,
                after=current,
                evidence=("reconciled_after_unknown=original_layout",),
                limitations=("No committed partition-table change was observed.",),
                message=(
                    "Reinspection found the original layout; transaction is aborted, not retried."
                ),
            )
            status = StorageTransactionStatus.ABORTED
            error_code = "STORAGE_UNKNOWN_RECONCILED_NO_CHANGE"
        else:
            verification = StorageVerification(
                operation_id=operation_id,
                status=StorageVerificationStatus.UNKNOWN,
                identity_verified=identity_ok,
                partition_table_verified=False,
                partition_geometry_verified=False,
                before=plan.original_layout,
                after=current,
                evidence=("reconciled_after_unknown=unexpected_layout",),
                limitations=(
                    "Manual inspection is required; automatic continuation is forbidden.",
                ),
                message="Current layout matches neither approved BEFORE nor approved AFTER state.",
            )
            status = StorageTransactionStatus.UNKNOWN
            error_code = "STORAGE_TRANSACTION_RECONCILIATION_REQUIRED"
        await self.store.put_verification(verification)
        updated = record.transaction.model_copy(
            update={
                "status": status,
                "finished_at": datetime.now(UTC)
                if status is not StorageTransactionStatus.UNKNOWN
                else None,
                "verification_id": verification.id,
                "after_layout_sha256": layout_fingerprint(current),
                "reconciliation_required": status is StorageTransactionStatus.UNKNOWN,
                "error_code": error_code,
                "last_known_stage": "unknown-reinspection-completed",
            }
        )
        await self.store.put_transaction(updated)
        return verification

    async def mark_unknown(self, operation_id: str, code: str) -> None:
        record = await self.store.get_record(operation_id)
        if record is None:
            return
        updated = record.transaction.model_copy(
            update={
                "status": StorageTransactionStatus.UNKNOWN,
                "finished_at": None,
                "reconciliation_required": True,
                "error_code": code,
                "last_known_stage": record.transaction.last_known_stage or "communication-lost",
            }
        )
        await self.store.put_transaction(updated)

    async def mark_failed(self, operation_id: str, code: str) -> None:
        await self._set_status(
            operation_id,
            StorageTransactionStatus.FAILED,
            error_code=code,
            last_known_stage="storage-transaction-failed",
            finished=True,
        )

    async def mark_aborted(self, operation_id: str, code: str) -> None:
        await self._set_status(
            operation_id,
            StorageTransactionStatus.ABORTED,
            error_code=code,
            last_known_stage="storage-transaction-aborted",
            finished=True,
        )

    async def _require_checkpoint(self, plan: StorageOperationPlan) -> None:
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
            raise StorageOperationEngineError("STORAGE_PROTECTION_CHECKPOINT_INVALID")
        durable = await self.checkpoints.get(checkpoint.id)
        artifact = await self.store.get_checkpoint(checkpoint.id)
        if (
            durable != checkpoint
            or artifact is None
            or artifact.operation_id != plan.operation_id
            or artifact.target_fingerprint_sha256 != plan.target_disk.fingerprint_sha256
            or artifact.partition_table_fingerprint_sha256
            != plan.original_layout.partition_table.fingerprint_sha256
        ):
            raise StorageOperationEngineError("STORAGE_PROTECTION_CHECKPOINT_INVALID")

    async def _set_status(
        self,
        operation_id: str,
        status: StorageTransactionStatus,
        *,
        error_code: str | None = None,
        last_known_stage: str | None = None,
        finished: bool = False,
    ) -> None:
        record = await self.store.get_record(operation_id)
        if record is None:
            return
        updated = record.transaction.model_copy(
            update={
                "status": status,
                "error_code": error_code,
                "last_known_stage": last_known_stage,
                "finished_at": datetime.now(UTC) if finished else record.transaction.finished_at,
            }
        )
        await self.store.put_transaction(updated)


def _operation_enum(value: str) -> StorageOperationType:
    return StorageOperationType(value)


def _plan_create(
    request: DeclarativeStorageOperationRequest, original: StorageLayout
) -> tuple[PartitionTable, PartitionResource]:
    table = original.partition_table
    if table.type is PartitionTableType.UNKNOWN:
        if request.table_type not in {PartitionTableType.GPT, PartitionTableType.MBR}:
            raise StorageOperationEngineError("STORAGE_PARTITION_TABLE_TYPE_REQUIRED")
        guid = (
            str(uuid4()).upper()
            if request.table_type is PartitionTableType.GPT
            else f"0x{uuid4().hex[:8]}"
        )
        table = build_empty_table(original.disk.identity, request.table_type, guid)
    elif request.table_type is not None and request.table_type is not table.type:
        raise StorageOperationEngineError("STORAGE_PARTITION_TABLE_TYPE_MISMATCH")
    if request.free_region_index >= len(table.free_regions):
        raise StorageOperationEngineError("STORAGE_FREE_REGION_NOT_FOUND")
    region = table.free_regions[request.free_region_index]
    sector_size = table.sector_size
    assert request.size_bytes is not None
    size_sectors = math.ceil(request.size_bytes / sector_size)
    alignment = max(1, 1_048_576 // sector_size)
    start = _align_up(region.start_sector, alignment)
    end = start + size_sectors - 1
    if end > region.end_sector:
        raise StorageOperationEngineError("STORAGE_INSUFFICIENT_FREE_SPACE")
    number = _next_partition_number(table.type, table.partitions)
    try:
        partition = new_partition_resource(
            identity=original.disk.identity,
            table_type=table.type,
            number=number,
            start_sector=start,
            size_sectors=size_sectors,
            sector_size=sector_size,
            type_code=request.partition_type,
            name=request.partition_name,
        )
    except PartitionToolError as exc:
        raise StorageOperationEngineError(exc.code) from exc
    return with_partitions(table, (*table.partitions, partition)), partition


def _plan_resize(
    request: DeclarativeStorageOperationRequest,
    original: StorageLayout,
    target: PartitionResource,
) -> PartitionTable:
    assert request.new_size_bytes is not None
    sector_size = original.partition_table.sector_size
    size_sectors = math.ceil(request.new_size_bytes / sector_size)
    if size_sectors <= 0 or size_sectors == target.size_sectors:
        raise StorageOperationEngineError("STORAGE_RESIZE_SIZE_INVALID")
    end = target.start_sector + size_sectors - 1
    if end > original.partition_table.last_usable_sector:
        raise StorageOperationEngineError("STORAGE_INSUFFICIENT_FREE_SPACE")
    for other in original.partition_table.partitions:
        if other.number == target.number:
            continue
        if _overlaps(target.start_sector, end, other.start_sector, other.end_sector):
            raise StorageOperationEngineError("STORAGE_PARTITION_CONFLICT")
    resized = target.model_copy(
        update={
            "end_sector": end,
            "size_sectors": size_sectors,
            "size_bytes": size_sectors * sector_size,
        }
    )
    partitions = tuple(
        resized if item.number == target.number else item
        for item in original.partition_table.partitions
    )
    return with_partitions(original.partition_table, partitions)


def _plan_move(
    request: DeclarativeStorageOperationRequest,
    original: StorageLayout,
    target: PartitionResource,
) -> PartitionTable:
    assert request.new_start_sector is not None
    start = request.new_start_sector
    end = start + target.size_sectors - 1
    if (
        start < original.partition_table.first_usable_sector
        or end > original.partition_table.last_usable_sector
    ):
        raise StorageOperationEngineError("STORAGE_MOVE_BOUNDARY_INVALID")
    for other in original.partition_table.partitions:
        if other.number == target.number:
            continue
        if _overlaps(start, end, other.start_sector, other.end_sector):
            raise StorageOperationEngineError("STORAGE_PARTITION_CONFLICT")
    moved = target.model_copy(update={"start_sector": start, "end_sector": end})
    partitions = tuple(
        moved if item.number == target.number else item
        for item in original.partition_table.partitions
    )
    return with_partitions(original.partition_table, partitions)


def _find_partition(
    partitions: tuple[PartitionResource, ...], number: int | None
) -> PartitionResource:
    if number is None:
        raise StorageOperationEngineError("STORAGE_PARTITION_REQUIRED")
    for partition in partitions:
        if partition.number == number:
            return partition
    raise StorageOperationEngineError("STORAGE_PARTITION_NOT_FOUND")


def _next_partition_number(
    table_type: PartitionTableType, partitions: tuple[PartitionResource, ...]
) -> int:
    used = {item.number for item in partitions}
    limit = 128 if table_type is PartitionTableType.GPT else 4
    for number in range(1, limit + 1):
        if number not in used:
            return number
    raise StorageOperationEngineError(
        "STORAGE_GPT_PARTITION_LIMIT"
        if table_type is PartitionTableType.GPT
        else "STORAGE_MBR_PRIMARY_LIMIT"
    )


def _required_operations(
    operation: str, partition_number: int | None
) -> tuple[StoragePrimitiveOperation, ...]:
    if operation in {"create", "delete"}:
        return (
            StoragePrimitiveOperation(
                id="storage.partition-table.write",
                kind="write_partition_table",
                description="Write the exact approved partition-table geometry with sfdisk.",
                partition_number=partition_number,
                mutates_target=True,
                enabled=True,
            ),
            StoragePrimitiveOperation(
                id="storage.partition-table.reread",
                kind="reread_partition_table",
                description="Ask the kernel to reread the table for block/loop devices.",
                partition_number=partition_number,
                mutates_target=False,
                enabled=True,
            ),
            StoragePrimitiveOperation(
                id="storage.partition-table.verify",
                kind="verify_partition_table",
                description="Reinspect identity and exact geometry after the write.",
                partition_number=partition_number,
                mutates_target=False,
                enabled=True,
            ),
        )
    kind: Literal["resize_partition", "move_partition"] = (
        "resize_partition" if operation == "resize" else "move_partition"
    )
    return (
        StoragePrimitiveOperation(
            id=f"storage.partition.{operation}",
            kind=kind,
            description=(
                "Architecture is modeled, but the executable adapter is disabled until "
                "filesystem-aware recovery exists."
            ),
            partition_number=partition_number,
            mutates_target=True,
            enabled=False,
        ),
    )


def _verification_plan() -> tuple[StorageVerificationStep, ...]:
    return (
        StorageVerificationStep(
            id="storage.verify-device-identity",
            description=(
                "Compare current multi-attribute device fingerprint with the approved target."
            ),
        ),
        StorageVerificationStep(
            id="storage.verify-partition-table",
            description="Run sfdisk verification and compare table type/GUID.",
        ),
        StorageVerificationStep(
            id="storage.verify-partition-geometry",
            description="Compare exact partition numbers, starts, sizes, types and PARTUUIDs.",
        ),
        StorageVerificationStep(
            id="storage.verify-filesystem-impact",
            description="Confirm no filesystem mutation was required by this executable slice.",
        ),
        StorageVerificationStep(
            id="storage.verify-boot-impact",
            description="Confirm the operation did not modify a detected boot dependency.",
        ),
    )


def _affected_resources(
    original: StorageLayout, target: PartitionResource | None
) -> tuple[str, ...]:
    values = [original.disk.id, original.disk.partition_table_id]
    if target is not None:
        values.append(target.id)
        values.extend(target.mount_point_ids)
        values.extend(target.operating_system_ids)
        values.extend(target.volume_ids)
        if target.filesystem_id is not None:
            values.append(target.filesystem_id)
    return tuple(dict.fromkeys(values))


def _filesystem_impact(data_impact: DataImpactAssessment) -> str:
    if data_impact.filesystem_change_required:
        return "filesystem_change_required_and_not_supported"
    if data_impact.level in {DataImpactLevel.CRITICAL, DataImpactLevel.UNKNOWN}:
        return "filesystem_or_data_impact_blocks_execution"
    return "no_filesystem_content_change_planned"


def _align_up(value: int, alignment: int) -> int:
    return ((value + alignment - 1) // alignment) * alignment


def _overlaps(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return a_start <= b_end and b_start <= a_end
