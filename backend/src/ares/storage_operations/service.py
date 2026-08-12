"""Shared application service for Storage & Partition Management."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Literal

from pydantic import BaseModel, ConfigDict

from ares.audit import AuditLedger, AuditLedgerError
from ares.capabilities import CapabilityManager
from ares.events import AresEvent, EventBus, EventSeverity
from ares.protection import ProtectionCheckpoint
from ares.storage_operations.engine import (
    DeclarativeStorageOperationRequest,
    StorageOperationEngine,
    StorageOperationEngineError,
)
from ares.storage_operations.models import (
    StorageLayout,
    StorageOperationExecutionInput,
    StorageOperationPlan,
    StorageOperationRecord,
    StorageTransactionStatus,
    StorageVerification,
)
from ares.storage_operations.store import StorageOperationStore
from ares.workflows import ExecutionStatus


class StorageOperationServiceError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class StorageOperationPlanRequest(DeclarativeStorageOperationRequest):
    pass


class StorageOperationActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_authorization: Literal[True] = True


class StorageOperationAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: str
    status: StorageTransactionStatus
    authorization_challenge_id: str | None = None


class StorageOperationService:
    def __init__(
        self,
        *,
        engine: StorageOperationEngine,
        store: StorageOperationStore,
        capabilities: CapabilityManager,
        event_bus: EventBus,
        audit: AuditLedger,
    ) -> None:
        self.engine = engine
        self.store = store
        self.capabilities = capabilities
        self.event_bus = event_bus
        self.audit = audit
        self._authorization_tasks: dict[str, asyncio.Task[None]] = {}
        self._execution_tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()

    async def layout(self, target_disk: str) -> StorageLayout:
        try:
            return await self.engine.inspect(target_disk)
        except StorageOperationEngineError as exc:
            raise StorageOperationServiceError(exc.code) from exc

    async def plan(
        self,
        request: StorageOperationPlanRequest,
        *,
        session_id: str,
        created_by: str,
    ) -> StorageOperationPlan:
        try:
            plan = await self.engine.plan(request, session_id=session_id, created_by=created_by)
        except StorageOperationEngineError as exc:
            raise StorageOperationServiceError(exc.code) from exc
        await self._event(
            "storage.plan.created",
            plan.operation_id,
            session_id,
            {
                "plan_id": plan.id,
                "capability": plan.capability,
                "operation": plan.operation.value,
                "target_fingerprint": plan.target_disk.fingerprint_sha256,
                "original_layout": plan.original_layout.partition_table.fingerprint_sha256,
                "proposed_layout": plan.proposed_layout.partition_table.fingerprint_sha256,
                "risk": plan.risk,
                "executable": plan.executable,
                "write_gate": plan.write_gate.allowed,
            },
            EventSeverity.WARNING,
        )
        await self._event(
            "storage.impact.analyzed",
            plan.operation_id,
            session_id,
            {
                "data_impact": plan.data_impact.level.value,
                "boot_impact": plan.boot_impact.level.value,
                "data_loss_possible": plan.data_loss_possible,
                "affected_resources": list(plan.affected_resources),
            },
            EventSeverity.WARNING,
        )
        with suppress(AuditLedgerError):
            await self.audit.append(
                event_type="storage.plan.created",
                source="storage.operation-service",
                correlation_id=plan.operation_id,
                session_id=session_id,
                payload={
                    "plan_id": plan.id,
                    "plan_fingerprint": plan.fingerprint_sha256,
                    "target_fingerprint": plan.target_disk.fingerprint_sha256,
                    "original_layout": plan.original_layout.partition_table.fingerprint_sha256,
                    "proposed_layout": plan.proposed_layout.partition_table.fingerprint_sha256,
                    "operation": plan.operation.value,
                    "risk": plan.risk,
                    "write_gate": plan.write_gate.allowed,
                },
            )
        return plan

    async def validate(self, operation_id: str, *, session_id: str) -> StorageOperationPlan:
        try:
            plan = await self.engine.validate(operation_id, session_id=session_id)
        except StorageOperationEngineError as exc:
            raise StorageOperationServiceError(exc.code) from exc
        await self._event(
            "storage.preflight.completed",
            operation_id,
            session_id,
            {
                "plan_id": plan.id,
                "dry_run_valid": bool(plan.dry_run and plan.dry_run.valid),
                "checkpoint_id": (
                    plan.protection_checkpoint.id if plan.protection_checkpoint else None
                ),
                "executable": plan.executable,
            },
            EventSeverity.WARNING,
        )
        if plan.protection_checkpoint is not None:
            await self._event(
                "storage.checkpoint.created",
                operation_id,
                session_id,
                {
                    "checkpoint_id": plan.protection_checkpoint.id,
                    "provider": plan.protection_checkpoint.provider_capability_id,
                    "verification_id": plan.protection_checkpoint.verification_id,
                },
                EventSeverity.WARNING,
            )
        return plan

    async def authorize(
        self,
        operation_id: str,
        request: StorageOperationActionRequest,
        *,
        session_id: str,
    ) -> StorageOperationAccepted:
        del request
        record = await self._record(operation_id, session_id=session_id)
        if record.transaction.status is StorageTransactionStatus.AUTHORIZED:
            return _accepted(record)
        if record.transaction.status is not StorageTransactionStatus.PROTECTED:
            raise StorageOperationServiceError("STORAGE_OPERATION_NOT_AUTHORIZABLE")
        async with self._lock:
            existing = self._authorization_tasks.get(operation_id)
            if existing is None or existing.done():
                task = asyncio.create_task(
                    self._authorize_task(operation_id, session_id=session_id),
                    name=f"storage-authorize-{operation_id}",
                )
                self._authorization_tasks[operation_id] = task
        await asyncio.sleep(0)
        refreshed = await self._record(operation_id, session_id=session_id)
        return _accepted(refreshed)

    async def execute(
        self,
        operation_id: str,
        *,
        session_id: str,
        created_by: str,
    ) -> StorageOperationAccepted:
        record = await self._record(operation_id, session_id=session_id)
        if record.transaction.status is not StorageTransactionStatus.AUTHORIZED:
            raise StorageOperationServiceError("STORAGE_OPERATION_NOT_AUTHORIZED")
        plan = record.plan
        checkpoint = plan.protection_checkpoint
        if checkpoint is None:
            raise StorageOperationServiceError("STORAGE_PROTECTION_CHECKPOINT_INVALID")
        payload = StorageOperationExecutionInput(
            operation_id=operation_id,
            plan_id=plan.id,
            session_id=session_id,
            protected_resource_id=plan.protected_resource_id,
            protected_resource_fingerprint_sha256=plan.target_disk.fingerprint_sha256,
        )
        async with self._lock:
            existing = self._execution_tasks.get(operation_id)
            if existing is not None and not existing.done():
                raise StorageOperationServiceError("STORAGE_OPERATION_ALREADY_RUNNING")
            task = asyncio.create_task(
                self._execute_task(
                    plan.capability,
                    payload,
                    checkpoint,
                    operation_id=operation_id,
                    created_by=created_by,
                ),
                name=f"storage-operation-{operation_id}",
            )
            self._execution_tasks[operation_id] = task
        return _accepted(record)

    async def get(self, operation_id: str) -> StorageOperationRecord | None:
        return await self.store.get_record(operation_id)

    async def verification(self, operation_id: str) -> StorageVerification | None:
        return await self.store.get_verification(operation_id)

    async def reconcile_unknown(self, operation_id: str, *, session_id: str) -> StorageVerification:
        try:
            verification = await self.engine.reconcile_unknown(operation_id, session_id=session_id)
        except StorageOperationEngineError as exc:
            raise StorageOperationServiceError(exc.code) from exc
        await self._event(
            "storage.verification.completed",
            operation_id,
            session_id,
            {
                "verification_id": verification.id,
                "status": verification.status.value,
                "reconciliation": True,
            },
            EventSeverity.WARNING,
        )
        return verification

    async def shutdown(self) -> None:
        async with self._lock:
            tasks = tuple(self._authorization_tasks.values()) + tuple(
                self._execution_tasks.values()
            )
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _authorize_task(self, operation_id: str, *, session_id: str) -> None:
        async def challenge(challenge_id: str) -> None:
            record = await self.store.get_record(operation_id)
            if record is not None:
                transaction = record.transaction.model_copy(
                    update={"authorization_challenge_id": challenge_id}
                )
                await self.store.put_transaction(transaction)
            await self._event(
                "storage.authorization.requested",
                operation_id,
                session_id,
                {"challenge_id": challenge_id},
                EventSeverity.WARNING,
            )

        try:
            await self.engine.request_authorization(
                operation_id, session_id=session_id, on_challenge=challenge
            )
        except asyncio.CancelledError:
            await self.engine.mark_aborted(operation_id, "STORAGE_AUTHORIZATION_INTERRUPTED")
            raise
        except StorageOperationEngineError as exc:
            await self.engine.mark_aborted(operation_id, exc.code)
        finally:
            async with self._lock:
                self._authorization_tasks.pop(operation_id, None)

    async def _execute_task(
        self,
        capability_id: str,
        payload: StorageOperationExecutionInput,
        checkpoint: ProtectionCheckpoint,
        *,
        operation_id: str,
        created_by: str,
    ) -> None:
        del created_by
        try:
            execution = await self.capabilities.execute(
                capability_id,
                payload.model_dump(mode="json"),
                execution_id=operation_id,
                protection_checkpoint=checkpoint,
            )
            if execution.status is not ExecutionStatus.SUCCEEDED:
                record = await self.store.get_record(operation_id)
                if record is not None and record.transaction.status not in {
                    StorageTransactionStatus.UNKNOWN,
                    StorageTransactionStatus.COMMITTED,
                }:
                    await self.engine.mark_failed(
                        operation_id, execution.error_code or "STORAGE_OPERATION_FAILED"
                    )
        except asyncio.CancelledError:
            # Once execution may have crossed the write boundary, cancellation is uncertainty.
            record = await self.store.get_record(operation_id)
            if record is not None and record.transaction.status in {
                StorageTransactionStatus.EXECUTING,
                StorageTransactionStatus.VERIFYING,
            }:
                await self.engine.mark_unknown(operation_id, "STORAGE_EXECUTION_INTERRUPTED")
            raise
        except Exception:
            record = await self.store.get_record(operation_id)
            if record is not None and record.transaction.status in {
                StorageTransactionStatus.EXECUTING,
                StorageTransactionStatus.VERIFYING,
            }:
                await self.engine.mark_unknown(operation_id, "STORAGE_EXECUTION_OBSERVABILITY_LOST")
            elif (
                record is not None
                and record.transaction.status is not StorageTransactionStatus.COMMITTED
            ):
                await self.engine.mark_failed(operation_id, "STORAGE_OPERATION_FAILED")
        finally:
            async with self._lock:
                self._execution_tasks.pop(operation_id, None)

    async def _record(self, operation_id: str, *, session_id: str) -> StorageOperationRecord:
        record = await self.store.get_record(operation_id)
        if record is None:
            raise StorageOperationServiceError("STORAGE_OPERATION_NOT_FOUND")
        if record.transaction.session_id != session_id:
            raise StorageOperationServiceError("STORAGE_OPERATION_SESSION_MISMATCH")
        return record

    async def _event(
        self,
        name: str,
        correlation_id: str,
        session_id: str,
        payload: dict[str, object],
        severity: EventSeverity = EventSeverity.INFO,
    ) -> None:
        await self.event_bus.publish(
            AresEvent(
                event_type=name,
                source="storage.operation-service",
                correlation_id=correlation_id,
                session_id=session_id,
                severity=severity,
                payload=payload,
            )
        )


def _accepted(record: StorageOperationRecord) -> StorageOperationAccepted:
    return StorageOperationAccepted(
        operation_id=record.plan.operation_id,
        status=record.transaction.status,
        authorization_challenge_id=record.transaction.authorization_challenge_id,
    )
