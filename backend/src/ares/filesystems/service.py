"""Shared service for filesystem inspection, planning and repair lifecycle."""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import suppress
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ares.audit import AuditLedger, AuditLedgerError
from ares.capabilities import CapabilityManager
from ares.events import AresEvent, EventBus, EventSeverity
from ares.filesystems.executor import FilesystemExecutor, FilesystemExecutorError
from ares.filesystems.models import (
    FilesystemHealth,
    FilesystemInspection,
    FilesystemRepairInput,
    FilesystemRepairPlan,
    FilesystemRepairRecord,
    RepairAction,
    RepairExecution,
    RepairExecutionStatus,
    RepairVerification,
)
from ares.filesystems.store import FilesystemRepairStore
from ares.protection import (
    ProtectionCheckpointError,
    ProtectionCheckpointService,
    ProtectionCheckpointStatus,
)
from ares.workflows import ExecutionStatus


class FilesystemServiceError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class FilesystemInspectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    device: str = Field(min_length=1, max_length=4096)


class FilesystemRepairPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    device: str = Field(min_length=1, max_length=4096)
    backup_id: str | None = Field(default=None, min_length=8, max_length=128)


class FilesystemRepairStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_id: str = Field(min_length=8, max_length=128)
    request_authorization: Literal[True]


class FilesystemRepairService:
    def __init__(
        self,
        *,
        capabilities: CapabilityManager,
        executor: FilesystemExecutor,
        store: FilesystemRepairStore,
        protection: ProtectionCheckpointService,
        event_bus: EventBus,
        audit: AuditLedger,
    ) -> None:
        self.capabilities = capabilities
        self.executor = executor
        self.store = store
        self.protection = protection
        self.event_bus = event_bus
        self.audit = audit
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._task_lock = asyncio.Lock()

    async def inspect(
        self, request: FilesystemInspectRequest, *, session_id: str
    ) -> FilesystemInspection:
        try:
            inspection = await self.executor.inspect(request.device)
        except FilesystemExecutorError as exc:
            raise FilesystemServiceError(exc.code) from exc
        await self.store.put_inspection(inspection)
        await self.event_bus.publish(
            AresEvent(
                event_type="filesystem.inspected",
                source="filesystem.service",
                correlation_id=inspection.id,
                session_id=session_id,
                payload={
                    "inspection_id": inspection.id,
                    "target_fingerprint": inspection.identity.fingerprint_sha256,
                    "filesystem": inspection.filesystem.value if inspection.filesystem else None,
                    "health": inspection.health.value,
                    "mounted": inspection.mount.mounted,
                    "repair_supported": inspection.repair_supported,
                },
            )
        )
        return inspection

    async def plan(
        self, request: FilesystemRepairPlanRequest, *, session_id: str
    ) -> FilesystemRepairPlan:
        inspection = await self.inspect(
            FilesystemInspectRequest(device=request.device), session_id=session_id
        )
        if inspection.filesystem is None or not inspection.supported:
            raise FilesystemServiceError("FILESYSTEM_TYPE_UNSUPPORTED")
        resource_id = f"filesystem:{inspection.identity.fingerprint_sha256}"
        limitations = list(inspection.limitations)
        checkpoint = None
        if request.backup_id is not None:
            try:
                checkpoint = await self.protection.from_backup(
                    backup_id=request.backup_id,
                    resource_id=resource_id,
                    resource_fingerprint_sha256=inspection.identity.fingerprint_sha256,
                    expected_device_id=inspection.identity.major_minor,
                    session_id=session_id,
                )
                await self.event_bus.publish(
                    AresEvent(
                        event_type="protection.checkpoint.created",
                        source="filesystem.service",
                        correlation_id=inspection.id,
                        session_id=session_id,
                        payload={
                            "checkpoint_id": checkpoint.id,
                            "provider": checkpoint.provider_capability_id,
                            "target_fingerprint": inspection.identity.fingerprint_sha256,
                        },
                    )
                )
            except ProtectionCheckpointError as exc:
                limitations.append(exc.code)
        else:
            limitations.append("verified_full_filesystem_backup_required")
        missing_tools = tuple(item.tool for item in inspection.required_tools if not item.available)
        if missing_tools:
            limitations.append("required_tools_unavailable:" + ",".join(missing_tools))
        if inspection.mount.mounted and (
            not inspection.mount.safe_to_unmount or not inspection.mount.safe_to_remount
        ):
            limitations.append("mounted_filesystem_cannot_be_safely_unmounted_and_restored")
        if not inspection.writable:
            limitations.append("target_not_writable")
        if not inspection.repair_supported:
            limitations.append("automatic_repair_not_supported_for_this_filesystem")
        if inspection.check is not None and inspection.health is FilesystemHealth.HEALTHY:
            limitations.append("repair_not_required")
        executable = (
            checkpoint is not None
            and checkpoint.status is ProtectionCheckpointStatus.READY
            and inspection.writable
            and inspection.repair_supported
            and not missing_tools
            and (
                not inspection.mount.mounted
                or (inspection.mount.safe_to_unmount and inspection.mount.safe_to_remount)
            )
            and not (inspection.check is not None and inspection.health is FilesystemHealth.HEALTHY)
        )
        problems = (
            inspection.check.problems
            if inspection.check is not None
            else ("health_check_deferred_until_safe_unmount",)
        )
        evidence = (
            inspection.check.evidence
            if inspection.check is not None
            else (
                f"target_fingerprint={inspection.identity.fingerprint_sha256}",
                f"mounted={str(inspection.mount.mounted).lower()}",
            )
        )
        actions = [
            RepairAction(
                id="filesystem.revalidate-identity",
                description=(
                    "Revalidar identidad estable del dispositivo inmediatamente antes de escribir."
                ),
                mutates_target=False,
            )
        ]
        if inspection.mount.mounted:
            actions.append(
                RepairAction(
                    id="filesystem.safe-unmount",
                    description=(
                        "Desmontar únicamente si no hay swap, bind/nested mounts ni handles "
                        "activos."
                    ),
                    mutates_target=False,
                )
            )
        actions.extend(
            (
                RepairAction(
                    id="filesystem.check-only",
                    description="Ejecutar comprobación read-only con el adapter específico.",
                    mutates_target=False,
                ),
                RepairAction(
                    id="filesystem.repair",
                    description="Ejecutar la reparación fija y allowlisted del adapter.",
                    mutates_target=True,
                ),
                RepairAction(
                    id="filesystem.verify",
                    description=(
                        "Repetir comprobación read-only y comparar evidencia antes/después."
                    ),
                    mutates_target=False,
                ),
            )
        )
        if inspection.mount.mounted:
            actions.append(
                RepairAction(
                    id="filesystem.safe-remount",
                    description=(
                        "Remontar solo tras verificación sana y con opciones restaurables."
                    ),
                    mutates_target=False,
                )
            )
        permissions = ("block-device.readwrite",)
        if inspection.mount.mounted:
            permissions += ("mount.manage",)
        draft = FilesystemRepairPlan(
            session_id=session_id,
            target=inspection.identity,
            filesystem=inspection.filesystem,
            mount=inspection.mount,
            detected_problems=problems,
            evidence=evidence,
            required_tools=inspection.required_tools,
            required_permissions=permissions,
            estimated_duration_seconds=None,
            protection_checkpoint=checkpoint,
            repair_actions=tuple(actions),
            verification_steps=(
                "Revalidar fingerprint de dispositivo.",
                "Ejecutar check read-only posterior.",
                "Comparar estado antes/después.",
                "Remontar únicamente si la verificación lo permite.",
            ),
            rollback_strategy=(
                "No existe rollback automático del metadata. El checkpoint conserva una copia "
                "verificada a nivel de archivos para recuperación manual/posterior."
            ),
            limitations=tuple(dict.fromkeys(limitations)),
            executable=executable,
            fingerprint_sha256="0" * 64,
        )
        plan = draft.model_copy(update={"fingerprint_sha256": _plan_fingerprint(draft)})
        await self.store.put_plan(plan)
        await self.event_bus.publish(
            AresEvent(
                event_type="repair.planned",
                source="filesystem.service",
                correlation_id=plan.repair_id,
                session_id=session_id,
                severity=EventSeverity.WARNING,
                payload={
                    "repair_id": plan.repair_id,
                    "plan_id": plan.id,
                    "filesystem": plan.filesystem.value,
                    "target_fingerprint": plan.target.fingerprint_sha256,
                    "checkpoint_id": checkpoint.id if checkpoint else None,
                    "executable": plan.executable,
                    "risk": "high",
                },
            )
        )
        with suppress(AuditLedgerError):
            await self.audit.append(
                event_type="repair.planned",
                source="filesystem.service",
                correlation_id=plan.repair_id,
                session_id=session_id,
                payload={
                    "plan_id": plan.id,
                    "plan_fingerprint": plan.fingerprint_sha256,
                    "target_fingerprint": plan.target.fingerprint_sha256,
                    "filesystem": plan.filesystem.value,
                    "checkpoint_id": checkpoint.id if checkpoint else None,
                    "executable": plan.executable,
                },
            )
        return plan

    async def get_plan(self, plan_id: str) -> FilesystemRepairPlan | None:
        return await self.store.get_plan(plan_id)

    async def start(
        self,
        request: FilesystemRepairStartRequest,
        *,
        session_id: str,
        created_by: str,
    ) -> FilesystemRepairRecord:
        plan = await self.store.get_plan(request.plan_id)
        if plan is None:
            raise FilesystemServiceError("FILESYSTEM_REPAIR_PLAN_NOT_FOUND")
        if not plan.executable or plan.protection_checkpoint is None:
            raise FilesystemServiceError("FILESYSTEM_PROTECTION_CHECKPOINT_REQUIRED")
        if plan.expires_at <= datetime.now(UTC):
            raise FilesystemServiceError("FILESYSTEM_REPAIR_PLAN_EXPIRED")
        if plan.session_id != session_id:
            raise FilesystemServiceError("FILESYSTEM_REPAIR_SESSION_MISMATCH")
        existing = await self.store.get_repair(plan.repair_id)
        if existing is not None:
            raise FilesystemServiceError("FILESYSTEM_REPAIR_ALREADY_REQUESTED")
        try:
            current = await self.executor.inspect(plan.target.requested_path)
        except FilesystemExecutorError as exc:
            raise FilesystemServiceError(exc.code) from exc
        if current.identity.fingerprint_sha256 != plan.target.fingerprint_sha256:
            raise FilesystemServiceError("FILESYSTEM_DEVICE_IDENTITY_CHANGED")
        execution = RepairExecution(
            id=plan.repair_id,
            plan_id=plan.id,
            session_id=session_id,
            status=RepairExecutionStatus.PLANNED,
            checkpoint_id=plan.protection_checkpoint.id,
            metadata={"created_by": created_by},
        )
        record = FilesystemRepairRecord(id=plan.repair_id, plan=plan, execution=execution)
        await self.store.put_repair(record)
        task = asyncio.create_task(
            self._run(plan, session_id=session_id, created_by=created_by),
            name=f"filesystem-repair-{plan.repair_id}",
        )
        async with self._task_lock:
            self._tasks[plan.repair_id] = task
        return record

    async def get(self, repair_id: str) -> FilesystemRepairRecord | None:
        return await self.store.get_repair(repair_id)

    async def verification(self, repair_id: str) -> RepairVerification | None:
        record = await self.store.get_repair(repair_id)
        return record.verification if record is not None else None

    async def cancel(self, repair_id: str, *, session_id: str) -> FilesystemRepairRecord:
        record = await self.store.get_repair(repair_id)
        if record is None:
            raise FilesystemServiceError("FILESYSTEM_REPAIR_NOT_FOUND")
        if record.execution.status not in {
            RepairExecutionStatus.PLANNED,
            RepairExecutionStatus.AUTHORIZING,
        }:
            if record.execution.status in {
                RepairExecutionStatus.RUNNING,
                RepairExecutionStatus.VERIFYING,
            }:
                raise FilesystemServiceError("FILESYSTEM_CANCELLATION_UNSAFE")
            return record
        async with self._task_lock:
            task = self._tasks.get(repair_id)
        if task is None or task.done():
            raise FilesystemServiceError("FILESYSTEM_CANCELLATION_UNAVAILABLE")
        task.cancel()
        execution = record.execution.model_copy(
            update={
                "status": RepairExecutionStatus.CANCELLED,
                "finished_at": datetime.now(UTC),
                "error_code": "FILESYSTEM_REPAIR_CANCELLED",
            }
        )
        record = record.model_copy(update={"execution": execution})
        await self.store.put_repair(record)
        await self.event_bus.publish(
            AresEvent(
                event_type="repair.cancelled",
                source="filesystem.service",
                correlation_id=repair_id,
                session_id=session_id,
                severity=EventSeverity.WARNING,
                payload={"repair_id": repair_id},
            )
        )
        return record

    async def shutdown(self) -> None:
        async with self._task_lock:
            tasks = tuple(self._tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(self, plan: FilesystemRepairPlan, *, session_id: str, created_by: str) -> None:
        checkpoint = plan.protection_checkpoint
        assert checkpoint is not None
        payload = FilesystemRepairInput(
            plan_id=plan.id,
            session_id=session_id,
            created_by=created_by,
            protected_resource_id=plan.protected_resource_id,
            protected_resource_fingerprint_sha256=plan.target.fingerprint_sha256,
        )
        try:
            result = await self.capabilities.execute(
                "filesystem.repair",
                payload.model_dump(mode="json"),
                execution_id=plan.repair_id,
                protection_checkpoint=checkpoint,
            )
            if result.status is not ExecutionStatus.SUCCEEDED:
                await self._mark_failed(
                    plan.repair_id, result.error_code or "FILESYSTEM_REPAIR_FAILED"
                )
        except asyncio.CancelledError:
            record = await self.store.get_repair(plan.repair_id)
            if record is not None and record.execution.status in {
                RepairExecutionStatus.PLANNED,
                RepairExecutionStatus.AUTHORIZING,
            }:
                execution = record.execution.model_copy(
                    update={
                        "status": RepairExecutionStatus.CANCELLED,
                        "finished_at": datetime.now(UTC),
                        "error_code": "FILESYSTEM_REPAIR_CANCELLED",
                    }
                )
                await self.store.put_repair(record.model_copy(update={"execution": execution}))
        except Exception:
            await self._mark_failed(plan.repair_id, "FILESYSTEM_REPAIR_FAILED")
        finally:
            async with self._task_lock:
                self._tasks.pop(plan.repair_id, None)

    async def _mark_failed(self, repair_id: str, code: str) -> None:
        record = await self.store.get_repair(repair_id)
        if record is None or record.execution.status in {
            RepairExecutionStatus.COMPLETED,
            RepairExecutionStatus.PARTIAL,
            RepairExecutionStatus.CANCELLED,
        }:
            return
        execution = record.execution.model_copy(
            update={
                "status": RepairExecutionStatus.FAILED,
                "finished_at": datetime.now(UTC),
                "error_code": code,
            }
        )
        await self.store.put_repair(record.model_copy(update={"execution": execution}))


def _plan_fingerprint(plan: FilesystemRepairPlan) -> str:
    payload = plan.model_dump(mode="json", exclude={"fingerprint_sha256"})
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
