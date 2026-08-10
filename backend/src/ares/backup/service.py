"""Application service for planning, executing, listing and verifying backups."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime
from typing import Annotated
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ares.audit.ledger import AuditLedger, AuditLedgerError
from ares.backup.models import (
    Backup,
    BackupCreateResult,
    BackupExecution,
    BackupListResult,
    BackupManifest,
    BackupPlan,
    BackupPolicy,
    BackupProgress,
    BackupStatus,
    BackupVerification,
    BackupVerifyResult,
)
from ares.backup.store import BackupStore
from ares.capabilities import CapabilityManager
from ares.events import AresEvent, EventBus, EventSeverity
from ares.tools.backup import BackupFilesystemTools, BackupToolError
from ares.workflows import ExecutionStatus, WorkflowEngine


class BackupServiceError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class BackupPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: Annotated[str, Field(min_length=1, max_length=4096)]
    destination: Annotated[str, Field(min_length=1, max_length=4096)]
    excluded_names: tuple[str, ...] = ()

    @field_validator("excluded_names")
    @classmethod
    def validate_excluded_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > 64:
            raise ValueError("too many exclusions")
        for name in value:
            if not name or len(name) > 255 or name in {".", ".."} or "/" in name or "\x00" in name:
                raise ValueError("invalid exclusion name")
        return value


class BackupCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_id: str = Field(min_length=8, max_length=128)
    request_authorization: bool


class BackupAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    backup: Backup
    authorization_instruction: str


class BackupService:
    """Shared API/CLI orchestration; endpoints never perform filesystem writes."""

    def __init__(
        self,
        capabilities: CapabilityManager,
        workflows: WorkflowEngine,
        store: BackupStore,
        tools: BackupFilesystemTools,
        event_bus: EventBus,
        audit: AuditLedger,
    ) -> None:
        self.capabilities = capabilities
        self.workflows = workflows
        self.store = store
        self.tools = tools
        self.event_bus = event_bus
        self.audit = audit
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._task_lock = asyncio.Lock()

    async def plan(self, request: BackupPlanRequest, *, session_id: str) -> BackupPlan:
        base_policy = BackupPolicy()
        policy = base_policy.model_copy(
            update={
                "excluded_names": tuple(
                    dict.fromkeys((*base_policy.excluded_names, *request.excluded_names))
                )
            }
        )
        try:
            plan = await asyncio.to_thread(
                self.tools.build_plan,
                request.source,
                request.destination,
                policy,
            )
        except BackupToolError as exc:
            raise BackupServiceError(exc.code) from exc
        await self.store.put_plan(plan)
        await self.event_bus.publish(
            AresEvent(
                event_type="backup.planned",
                source="backup.service",
                correlation_id=plan.backup_id,
                session_id=session_id,
                payload={
                    "backup_id": plan.backup_id,
                    "plan_id": plan.id,
                    "source_device": plan.source.device_id,
                    "destination_device": plan.destination.device_id,
                    "estimated_bytes": plan.source.estimated_size_bytes,
                    "required_bytes": plan.required_bytes,
                    "available_bytes": plan.destination.available_bytes,
                    "included_file_count": plan.included_file_count,
                    "excluded_count": len(plan.exclusions),
                    "risk": plan.risk,
                    "authorization_required": True,
                },
            )
        )
        with suppress(AuditLedgerError):
            await self.audit.append(
                event_type="backup.planned",
                source="ares-api",
                correlation_id=plan.backup_id,
                session_id=session_id,
                payload={
                    "plan_id": plan.id,
                    "plan_fingerprint": plan.fingerprint_sha256,
                    "source_device": plan.source.device_id,
                    "destination_device": plan.destination.device_id,
                    "estimated_bytes": plan.source.estimated_size_bytes,
                    "required_bytes": plan.required_bytes,
                    "available_bytes": plan.destination.available_bytes,
                },
            )
        return plan

    async def create(
        self,
        request: BackupCreateRequest,
        *,
        session_id: str,
        created_by: str,
    ) -> BackupAccepted:
        if request.request_authorization is not True:
            raise BackupServiceError("BACKUP_EXPLICIT_AUTHORIZATION_REQUEST_REQUIRED")
        plan = await self.store.get_plan(request.plan_id)
        if plan is None:
            raise BackupServiceError("BACKUP_PLAN_NOT_FOUND")
        if await self.store.get_backup(plan.backup_id) is not None:
            raise BackupServiceError("BACKUP_ALREADY_REQUESTED")
        progress = BackupProgress(
            files_completed=0,
            files_total=plan.included_file_count,
            bytes_completed=0,
            bytes_total=plan.source.estimated_size_bytes,
            percent=0.0,
            speed_bytes_per_second=None,
            eta_seconds=None,
        )
        workflow_id = uuid4().hex
        execution = BackupExecution(
            backup_id=plan.backup_id,
            workflow_execution_id=workflow_id,
            status=BackupStatus.PLANNED,
            progress=progress,
        )
        backup = Backup(
            id=plan.backup_id,
            created_at=datetime.now(UTC),
            source=plan.source,
            destination=plan.destination,
            size=0,
            file_count=0,
            status=BackupStatus.PLANNED,
            metadata={
                "plan_fingerprint_sha256": plan.fingerprint_sha256,
                "policy": plan.policy.model_dump(mode="json"),
                "exclusion_count": len(plan.exclusions),
            },
            created_by=created_by,
            session_id=session_id,
            plan_id=plan.id,
            execution=execution,
        )
        await self.store.put_backup(backup)
        task = asyncio.create_task(
            self._run_create(
                backup.id,
                plan.id,
                session_id,
                created_by,
                workflow_id,
            ),
            name=f"backup-create-{backup.id}",
        )
        async with self._task_lock:
            self._tasks[backup.id] = task
        return BackupAccepted(
            backup=backup,
            authorization_instruction=(
                "ARES will request an independent local consent challenge. Inspect the exact "
                "plan and approve the challenge from the trusted local consent CLI; the web "
                "request itself does not grant permission."
            ),
        )

    async def list(self, *, limit: int = 100) -> tuple[Backup, ...]:
        execution = await self.capabilities.execute("backup.list", {"limit": limit})
        if execution.status is not ExecutionStatus.SUCCEEDED or execution.result is None:
            raise BackupServiceError(execution.error_code or "BACKUP_LIST_FAILED")
        try:
            return BackupListResult.model_validate(execution.result).backups
        except ValueError as exc:
            raise BackupServiceError("BACKUP_LIST_RESULT_INVALID") from exc

    async def get(self, backup_id: str) -> Backup | None:
        return await self.store.get_backup(backup_id)

    async def manifest(self, backup_id: str) -> BackupManifest | None:
        return await self.store.get_manifest(backup_id)

    async def verification(self, backup_id: str) -> BackupVerification | None:
        return await self.store.get_verification(backup_id)

    async def verify(self, backup_id: str, *, session_id: str) -> BackupVerifyResult:
        execution = await self.capabilities.execute(
            "backup.verify",
            {"backup_id": backup_id, "session_id": session_id},
        )
        if execution.status is not ExecutionStatus.SUCCEEDED or execution.result is None:
            raise BackupServiceError(execution.error_code or "BACKUP_VERIFY_FAILED")
        try:
            return BackupVerifyResult.model_validate(execution.result)
        except ValueError as exc:
            raise BackupServiceError("BACKUP_VERIFY_RESULT_INVALID") from exc

    async def cancel(self, backup_id: str, *, session_id: str) -> Backup:
        backup = await self.store.get_backup(backup_id)
        if backup is None:
            raise BackupServiceError("BACKUP_NOT_FOUND")
        if backup.status in {
            BackupStatus.COMPLETED,
            BackupStatus.FAILED,
            BackupStatus.CANCELLED,
            BackupStatus.CORRUPTED,
        }:
            return backup
        workflow_id = backup.execution.workflow_execution_id
        cancelled = await self.workflows.cancel(workflow_id) if workflow_id is not None else False
        if not cancelled:
            async with self._task_lock:
                task = self._tasks.get(backup_id)
            if task is not None and not task.done():
                task.cancel()
                cancelled = True
        if not cancelled:
            raise BackupServiceError("BACKUP_CANCELLATION_UNAVAILABLE")
        await self.event_bus.publish(
            AresEvent(
                event_type="backup.cancellation.requested",
                source="backup.service",
                correlation_id=workflow_id or backup.id,
                session_id=session_id,
                severity=EventSeverity.WARNING,
                payload={"backup_id": backup.id},
            )
        )
        return (await self.store.get_backup(backup_id)) or backup

    async def shutdown(self) -> None:
        async with self._task_lock:
            tasks = tuple(self._tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_create(
        self,
        backup_id: str,
        plan_id: str,
        session_id: str,
        created_by: str,
        workflow_id: str,
    ) -> None:
        try:
            execution = await self.capabilities.execute(
                "backup.create",
                {
                    "plan_id": plan_id,
                    "session_id": session_id,
                    "created_by": created_by,
                },
                execution_id=workflow_id,
            )
            if execution.status is ExecutionStatus.SUCCEEDED and execution.result is not None:
                BackupCreateResult.model_validate(execution.result)
            else:
                await self._mark_terminal_failure(
                    backup_id,
                    execution.error_code
                    or (
                        "BACKUP_CANCELLED"
                        if execution.status is ExecutionStatus.CANCELLED
                        else "BACKUP_CREATE_FAILED"
                    ),
                    cancelled=execution.status is ExecutionStatus.CANCELLED,
                )
        except asyncio.CancelledError:
            await self._mark_terminal_failure(
                backup_id,
                "BACKUP_CANCELLED",
                cancelled=True,
            )
        except Exception:
            await self._mark_terminal_failure(backup_id, "BACKUP_CREATE_FAILED")
        finally:
            async with self._task_lock:
                self._tasks.pop(backup_id, None)

    async def _mark_terminal_failure(
        self,
        backup_id: str,
        code: str,
        *,
        cancelled: bool = False,
    ) -> None:
        backup = await self.store.get_backup(backup_id)
        if backup is None or backup.status in {
            BackupStatus.COMPLETED,
            BackupStatus.CORRUPTED,
        }:
            return
        status = BackupStatus.CANCELLED if cancelled else BackupStatus.FAILED
        execution = backup.execution.model_copy(
            update={
                "status": status,
                "finished_at": datetime.now(UTC),
                "error_code": code,
            }
        )
        await self.store.put_backup(
            backup.model_copy(update={"status": status, "execution": execution})
        )
