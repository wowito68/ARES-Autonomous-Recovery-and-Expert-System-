"""Shared API/CLI orchestration for Boot Recovery."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from ares.actions.boot import BootRepairExecutionInput
from ares.audit import AuditLedger, AuditLedgerError
from ares.boot.engine import BootRecoveryEngine, BootRecoveryEngineError
from ares.boot.models import (
    BootDiagnoseInput,
    BootDiagnosticResult,
    BootRepairPlan,
    BootRepairPlanInput,
    BootRepairRecord,
    BootRepairStatus,
    BootVerification,
)
from ares.capabilities import CapabilityManager
from ares.events import AresEvent, EventBus, EventSeverity
from ares.workflows import ExecutionStatus


class BootRecoveryServiceError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class BootDiagnoseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    target_disk: str | None = Field(default=None, max_length=4096)
    root_path: str | None = Field(default=None, max_length=4096)


class BootRepairPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    diagnostic_id: str = Field(min_length=8, max_length=128)
    target_os_id: str | None = Field(default=None, max_length=256)


class BootRepairRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    plan_id: str = Field(min_length=8, max_length=128)
    request_authorization: bool


class BootRepairAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    repair: BootRepairRecord
    authorization_instruction: str


class BootRecoveryService:
    def __init__(
        self,
        *,
        engine: BootRecoveryEngine,
        capabilities: CapabilityManager,
        event_bus: EventBus,
        audit: AuditLedger,
    ) -> None:
        self.engine = engine
        self.capabilities = capabilities
        self.event_bus = event_bus
        self.audit = audit
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()

    async def diagnose(
        self, request: BootDiagnoseRequest, *, session_id: str
    ) -> BootDiagnosticResult:
        execution = await self.capabilities.execute(
            "boot.diagnose",
            BootDiagnoseInput(
                target_disk=request.target_disk, root_path=request.root_path
            ).model_dump(mode="json"),
        )
        if execution.status is not ExecutionStatus.SUCCEEDED or execution.result is None:
            raise BootRecoveryServiceError(execution.error_code or "BOOT_DIAGNOSIS_FAILED")
        try:
            return BootDiagnosticResult.model_validate(execution.result)
        except ValueError as exc:
            raise BootRecoveryServiceError("BOOT_DIAGNOSTIC_RESULT_INVALID") from exc

    async def plan(self, request: BootRepairPlanRequest, *, session_id: str) -> BootRepairPlan:
        try:
            plan = await self.engine.plan(
                BootRepairPlanInput(
                    diagnostic_id=request.diagnostic_id, target_os_id=request.target_os_id
                ),
                session_id=session_id,
            )
        except BootRecoveryEngineError as exc:
            raise BootRecoveryServiceError(exc.code) from exc
        await self.event_bus.publish(
            AresEvent(
                event_type="boot.repair.planned",
                source="boot.service",
                correlation_id=plan.repair_id,
                session_id=session_id,
                severity=EventSeverity.WARNING,
                payload={
                    "repair_id": plan.repair_id,
                    "plan_id": plan.id,
                    "target_os": plan.target_os.id,
                    "target_disk_fingerprint": plan.target_disk.fingerprint_sha256,
                    "operation_count": len(plan.operations),
                    "risk": plan.risk,
                    "executable": plan.executable,
                },
            )
        )
        with suppress(AuditLedgerError):
            await self.audit.append(
                event_type="boot.repair.planned",
                source="ares-api",
                correlation_id=plan.repair_id,
                session_id=session_id,
                payload={
                    "plan_id": plan.id,
                    "plan_fingerprint": plan.fingerprint_sha256,
                    "target_os": plan.target_os.id,
                    "target_disk_fingerprint": plan.target_disk.fingerprint_sha256,
                    "esp": plan.target_esp.resource_id if plan.target_esp else None,
                    "risk": plan.risk,
                },
            )
        return plan

    async def start(
        self,
        request: BootRepairRequest,
        *,
        session_id: str,
        created_by: str,
    ) -> BootRepairAccepted:
        del created_by
        if request.request_authorization is not True:
            raise BootRecoveryServiceError("BOOT_EXPLICIT_AUTHORIZATION_REQUEST_REQUIRED")
        try:
            protected = await self.engine.protect(request.plan_id, session_id=session_id)
        except BootRecoveryEngineError as exc:
            raise BootRecoveryServiceError(exc.code) from exc
        checkpoint = protected.protection_checkpoint
        if checkpoint is None:
            raise BootRecoveryServiceError("BOOT_PROTECTION_CHECKPOINT_INVALID")
        await self.event_bus.publish(
            AresEvent(
                event_type="boot.protection-checkpoint.created",
                source="boot.service",
                correlation_id=protected.repair_id,
                session_id=session_id,
                severity=EventSeverity.WARNING,
                payload={
                    "repair_id": protected.repair_id,
                    "checkpoint_id": checkpoint.id,
                    "evidence_sha256": checkpoint.evidence_sha256,
                },
            )
        )
        workflow_id = uuid4().hex
        task = asyncio.create_task(
            self._authorize_and_run(protected.repair_id, protected.id, session_id, workflow_id),
            name=f"boot-repair-{protected.repair_id}",
        )
        async with self._lock:
            self._tasks[protected.repair_id] = task
        record = await self.engine.store.get_record(protected.repair_id)
        if record is None:
            raise BootRecoveryServiceError("BOOT_REPAIR_NOT_FOUND")
        return BootRepairAccepted(
            repair=record,
            authorization_instruction=(
                "ARES created a boot-state ProtectionCheckpoint. Review the exact repair plan "
                "and approve the independent local consent challenge; this API request does not "
                "grant authorization."
            ),
        )

    async def get(self, repair_id: str) -> BootRepairRecord | None:
        return await self.engine.store.get_record(repair_id)

    async def verification(self, repair_id: str) -> BootVerification | None:
        return await self.engine.store.get_verification(repair_id)

    async def cancel(self, repair_id: str, *, session_id: str) -> BootRepairRecord:
        record = await self.engine.store.get_record(repair_id)
        if record is None:
            raise BootRecoveryServiceError("BOOT_REPAIR_NOT_FOUND")
        if record.plan.session_id != session_id:
            raise BootRecoveryServiceError("BOOT_REPAIR_SESSION_MISMATCH")
        if record.execution.status in {
            BootRepairStatus.COMPLETED,
            BootRepairStatus.REPAIR_FAILED,
            BootRepairStatus.ABORTED,
            BootRepairStatus.UNKNOWN,
        }:
            return record
        async with self._lock:
            task = self._tasks.get(repair_id)
        if task is None or task.done():
            raise BootRecoveryServiceError("BOOT_REPAIR_CANCELLATION_UNAVAILABLE")
        task.cancel()
        await self.engine.mark_aborted(repair_id, code="BOOT_REPAIR_CANCELLED")
        await self.event_bus.publish(
            AresEvent(
                event_type="boot.repair.aborted",
                source="boot.service",
                correlation_id=repair_id,
                session_id=session_id,
                severity=EventSeverity.WARNING,
                payload={"repair_id": repair_id},
            )
        )
        return (await self.engine.store.get_record(repair_id)) or record

    async def reconcile(self, repair_id: str, *, session_id: str) -> BootVerification:
        try:
            return await self.engine.reconcile_unknown(repair_id, session_id=session_id)
        except BootRecoveryEngineError as exc:
            raise BootRecoveryServiceError(exc.code) from exc

    async def shutdown(self) -> None:
        async with self._lock:
            tasks = tuple(self._tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _authorize_and_run(
        self,
        repair_id: str,
        plan_id: str,
        session_id: str,
        workflow_id: str,
    ) -> None:
        try:

            async def challenge(challenge_id: str) -> None:
                await self.event_bus.publish(
                    AresEvent(
                        event_type="boot.repair-authorization.requested",
                        source="boot.service",
                        correlation_id=repair_id,
                        session_id=session_id,
                        severity=EventSeverity.WARNING,
                        payload={"repair_id": repair_id, "challenge_id": challenge_id},
                    )
                )

            await self.engine.request_authorization(
                plan_id, session_id=session_id, on_challenge=challenge
            )
            record = await self.engine.store.get_record(repair_id)
            if record is None or record.plan.protection_checkpoint is None:
                raise BootRecoveryServiceError("BOOT_REPAIR_NOT_FOUND")
            execution = await self.capabilities.execute(
                "boot.repair.grub",
                BootRepairExecutionInput(repair_id=repair_id, session_id=session_id).model_dump(
                    mode="json"
                ),
                execution_id=workflow_id,
                protection_checkpoint=record.plan.protection_checkpoint,
            )
            if execution.status is not ExecutionStatus.SUCCEEDED:
                await self.engine.mark_failed_or_unknown(
                    repair_id, execution.error_code or "BOOT_REPAIR_FAILED"
                )
        except asyncio.CancelledError:
            await self.engine.mark_aborted(repair_id, code="BOOT_REPAIR_CANCELLED")
        except (BootRecoveryEngineError, BootRecoveryServiceError) as exc:
            await self.engine.mark_failed_or_unknown(repair_id, exc.code)
        except Exception:
            await self.engine.mark_failed_or_unknown(repair_id, "BOOT_REPAIR_FAILED")
        finally:
            async with self._lock:
                self._tasks.pop(repair_id, None)
