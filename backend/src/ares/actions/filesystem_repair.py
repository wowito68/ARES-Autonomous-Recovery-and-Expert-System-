"""Private workflow actions for filesystem.repair."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from typing import Any

from ares.actions.base import ActionContext, ActionError
from ares.events import AresEvent, EventSeverity
from ares.filesystems.executor import FilesystemExecutor, FilesystemExecutorError
from ares.filesystems.models import (
    FilesystemAuthorizationGrant,
    FilesystemHealth,
    FilesystemRepairInput,
    FilesystemRepairOutcome,
    FilesystemRepairPlan,
    FilesystemRepairRecord,
    FilesystemRepairResult,
    FilesystemType,
    RepairExecutionStatus,
    RepairVerification,
    RepairVerificationStatus,
)
from ares.filesystems.store import FilesystemRepairStore
from ares.knowledge import GraphEdge, GraphKind, GraphNode
from ares.protection import ProtectionCheckpointStatus


class ValidateFilesystemRepairAction:
    id = "filesystem.validate-repair-plan"
    idempotent = True

    def __init__(self, store: FilesystemRepairStore, executor: FilesystemExecutor) -> None:
        self.store = store
        self.executor = executor

    async def run(self, inputs: dict[str, Any], context: ActionContext) -> dict[str, Any]:
        request = FilesystemRepairInput.model_validate(inputs)
        plan = await self.store.get_plan(request.plan_id)
        if plan is None:
            raise ActionError("FILESYSTEM_REPAIR_PLAN_NOT_FOUND")
        checkpoint = plan.protection_checkpoint
        if (
            not plan.executable
            or checkpoint is None
            or checkpoint.status is not ProtectionCheckpointStatus.READY
            or checkpoint.id == ""
        ):
            raise ActionError("FILESYSTEM_PROTECTION_CHECKPOINT_REQUIRED")
        if plan.expires_at <= datetime.now(UTC):
            raise ActionError("FILESYSTEM_REPAIR_PLAN_EXPIRED")
        if request.session_id != plan.session_id:
            raise ActionError("FILESYSTEM_REPAIR_SESSION_MISMATCH")
        if request.protected_resource_id != plan.protected_resource_id:
            raise ActionError("FILESYSTEM_PROTECTION_RESOURCE_MISMATCH")
        if (
            request.protected_resource_fingerprint_sha256
            != plan.target.fingerprint_sha256
        ):
            raise ActionError("FILESYSTEM_PROTECTION_RESOURCE_MISMATCH")
        try:
            inspection = await self.executor.inspect(plan.target.requested_path)
        except FilesystemExecutorError as exc:
            raise ActionError(exc.code) from exc
        if inspection.identity.fingerprint_sha256 != plan.target.fingerprint_sha256:
            raise ActionError("FILESYSTEM_DEVICE_IDENTITY_CHANGED")
        if inspection.mount != plan.mount:
            raise ActionError("FILESYSTEM_MOUNT_STATE_CHANGED")
        record = await self.store.get_repair(plan.repair_id)
        if record is None:
            raise ActionError("FILESYSTEM_REPAIR_RECORD_NOT_FOUND")
        await _event(
            context,
            "repair.preflight.completed",
            {
                "repair_id": plan.repair_id,
                "plan_id": plan.id,
                "filesystem": plan.filesystem.value,
                "target_fingerprint": plan.target.fingerprint_sha256,
                "checkpoint_id": checkpoint.id,
            },
            session_id=request.session_id,
        )
        return {
            "request": request.model_dump(mode="json"),
            "plan": plan.model_dump(mode="json"),
        }

    async def compensate(self, output: dict[str, Any], context: ActionContext) -> None:
        del output, context


class RequestFilesystemRepairAuthorizationAction:
    id = "filesystem.request-repair-authorization"
    idempotent = False

    def __init__(self, store: FilesystemRepairStore, executor: FilesystemExecutor) -> None:
        self.store = store
        self.executor = executor

    async def run(self, inputs: dict[str, Any], context: ActionContext) -> dict[str, Any]:
        request = FilesystemRepairInput.model_validate(inputs["request"])
        plan = FilesystemRepairPlan.model_validate(inputs["plan"])
        record = await self.store.get_repair(plan.repair_id)
        if record is None:
            raise ActionError("FILESYSTEM_REPAIR_RECORD_NOT_FOUND")
        execution = record.execution.model_copy(
            update={"status": RepairExecutionStatus.AUTHORIZING}
        )
        await self.store.put_repair(record.model_copy(update={"execution": execution}))

        async def challenge(challenge_id: str) -> None:
            current = await self.store.get_repair(plan.repair_id)
            if current is not None:
                updated = current.execution.model_copy(
                    update={"authorization_challenge_id": challenge_id}
                )
                await self.store.put_repair(current.model_copy(update={"execution": updated}))
            await _event(
                context,
                "repair.authorization.requested",
                {
                    "repair_id": plan.repair_id,
                    "plan_id": plan.id,
                    "challenge_id": challenge_id,
                    "filesystem": plan.filesystem.value,
                    "target_fingerprint": plan.target.fingerprint_sha256,
                    "risk": "high",
                },
                EventSeverity.WARNING,
                session_id=request.session_id,
            )

        try:
            grant = await self.executor.request_authorization(plan, on_challenge=challenge)
        except FilesystemExecutorError as exc:
            raise ActionError(exc.code) from exc
        return {
            "request": request.model_dump(mode="json"),
            "plan": plan.model_dump(mode="json"),
            "grant": grant.model_dump(mode="json"),
        }

    async def compensate(self, output: dict[str, Any], context: ActionContext) -> None:
        del output, context


class ExecuteFilesystemRepairAction:
    id = "filesystem.execute-repair"
    idempotent = False

    def __init__(self, store: FilesystemRepairStore, executor: FilesystemExecutor) -> None:
        self.store = store
        self.executor = executor

    async def run(self, inputs: dict[str, Any], context: ActionContext) -> dict[str, Any]:
        request = FilesystemRepairInput.model_validate(inputs["request"])
        plan = FilesystemRepairPlan.model_validate(inputs["plan"])
        grant = FilesystemAuthorizationGrant.model_validate(inputs["grant"])
        record = await self.store.get_repair(plan.repair_id)
        if record is None:
            raise ActionError("FILESYSTEM_REPAIR_RECORD_NOT_FOUND")
        execution = record.execution.model_copy(
            update={
                "status": RepairExecutionStatus.RUNNING,
                "started_at": datetime.now(UTC),
                "checkpoint_id": plan.protection_checkpoint.id
                if plan.protection_checkpoint is not None
                else None,
            }
        )
        await self.store.put_repair(record.model_copy(update={"execution": execution}))
        await _event(
            context,
            "repair.started",
            {
                "repair_id": plan.repair_id,
                "plan_id": plan.id,
                "target_fingerprint": plan.target.fingerprint_sha256,
            },
            EventSeverity.WARNING,
            session_id=request.session_id,
        )

        async def stage(name: str, payload: dict[str, object]) -> None:
            severity = (
                EventSeverity.WARNING
                if name in {"filesystem.unmount.started", "filesystem.repair-command.started"}
                else EventSeverity.INFO
            )
            await _event(
                context,
                name,
                {"repair_id": plan.repair_id, **payload},
                severity,
                session_id=request.session_id,
            )

        try:
            outcome = await self.executor.execute(plan, grant, on_stage=stage)
        except asyncio.CancelledError:
            await self._terminal(plan.repair_id, RepairExecutionStatus.CANCELLED, "FILESYSTEM_REPAIR_CANCELLED")
            await _event(
                context,
                "repair.cancelled",
                {"repair_id": plan.repair_id},
                EventSeverity.WARNING,
                session_id=request.session_id,
            )
            raise
        except FilesystemExecutorError as exc:
            await self._terminal(plan.repair_id, RepairExecutionStatus.FAILED, exc.code)
            await _event(
                context,
                "repair.failed",
                {"repair_id": plan.repair_id, "error_code": exc.code},
                EventSeverity.ERROR,
                session_id=request.session_id,
            )
            raise ActionError(exc.code) from exc
        return {
            "request": request.model_dump(mode="json"),
            "plan": plan.model_dump(mode="json"),
            "outcome": outcome.model_dump(mode="json"),
        }

    async def compensate(self, output: dict[str, Any], context: ActionContext) -> None:
        del output, context

    async def _terminal(
        self, repair_id: str, status: RepairExecutionStatus, code: str
    ) -> None:
        record = await self.store.get_repair(repair_id)
        if record is None:
            return
        execution = record.execution.model_copy(
            update={
                "status": status,
                "finished_at": datetime.now(UTC),
                "error_code": code,
            }
        )
        await self.store.put_repair(record.model_copy(update={"execution": execution}))


class VerifyFilesystemRepairAction:
    id = "filesystem.verify-repair"
    idempotent = True

    def __init__(self, store: FilesystemRepairStore) -> None:
        self.store = store

    async def run(self, inputs: dict[str, Any], context: ActionContext) -> dict[str, Any]:
        request = FilesystemRepairInput.model_validate(inputs["request"])
        plan = FilesystemRepairPlan.model_validate(inputs["plan"])
        outcome = FilesystemRepairOutcome.model_validate(inputs["outcome"])
        record = await self.store.get_repair(plan.repair_id)
        if record is None:
            raise ActionError("FILESYSTEM_REPAIR_RECORD_NOT_FOUND")
        verification_status, message = _verification_status(plan, outcome)
        verification = RepairVerification(
            repair_id=plan.repair_id,
            status=verification_status,
            before=outcome.before,
            after=outcome.after,
            target_identity_verified=True,
            checkpoint_verified=plan.protection_checkpoint is not None,
            remounted=outcome.remounted,
            evidence=outcome.repair_evidence,
            limitations=outcome.limitations,
            message=message,
        )
        if verification_status is RepairVerificationStatus.SUCCESS:
            execution_status = RepairExecutionStatus.COMPLETED
            error_code = None
            event_name = "repair.completed"
            severity = EventSeverity.INFO
        elif verification_status in {
            RepairVerificationStatus.PARTIAL,
            RepairVerificationStatus.UNKNOWN,
        }:
            execution_status = RepairExecutionStatus.PARTIAL
            error_code = "FILESYSTEM_REPAIR_PARTIAL"
            event_name = "repair.failed"
            severity = EventSeverity.WARNING
        else:
            execution_status = RepairExecutionStatus.FAILED
            error_code = "FILESYSTEM_VERIFICATION_FAILED"
            event_name = "repair.failed"
            severity = EventSeverity.ERROR
        execution = record.execution.model_copy(
            update={
                "status": execution_status,
                "finished_at": datetime.now(UTC),
                "tool": outcome.repair_tool,
                "error_code": error_code,
            }
        )
        record = record.model_copy(
            update={"execution": execution, "verification": verification}
        )
        await self.store.put_repair(record)
        await _event(
            context,
            event_name,
            {
                "repair_id": plan.repair_id,
                "verification_id": verification.id,
                "verification_status": verification.status.value,
                "before": outcome.before.health.value,
                "after": outcome.after.health.value,
                "remounted": outcome.remounted,
            },
            severity,
            session_id=request.session_id,
        )
        return {
            "repair": record.model_dump(mode="json"),
            "verification": verification.model_dump(mode="json"),
        }

    async def compensate(self, output: dict[str, Any], context: ActionContext) -> None:
        del output, context


class ProjectFilesystemRepairGraphAction:
    id = "knowledge.project-filesystem-repair"
    idempotent = True

    async def run(self, inputs: dict[str, Any], context: ActionContext) -> dict[str, Any]:
        record = FilesystemRepairRecord.model_validate(inputs["repair"])
        verification = RepairVerification.model_validate(inputs["verification"])
        plan = record.plan
        filesystem_id = f"filesystem:{plan.target.fingerprint_sha256[:24]}"
        before_id = f"filesystem-status:{record.id}:before"
        repair_id = f"repair-execution:{record.id}"
        verification_id = f"repair-verification:{verification.id}"
        after_id = f"filesystem-status:{record.id}:after"
        checkpoint_id = (
            f"protection-checkpoint:{plan.protection_checkpoint.id}"
            if plan.protection_checkpoint is not None
            else None
        )
        nodes = [
            GraphNode(
                id=filesystem_id,
                kind=GraphKind.FILESYSTEM,
                attributes={
                    "filesystem": plan.filesystem.value,
                    "device_fingerprint": plan.target.fingerprint_sha256,
                    "filesystem_uuid": plan.target.filesystem_uuid,
                },
            ),
            GraphNode(
                id=before_id,
                kind=GraphKind.FILESYSTEM_STATUS,
                attributes={"status": verification.before.health.value if verification.before else "UNKNOWN"},
            ),
            GraphNode(
                id=repair_id,
                kind=GraphKind.REPAIR_EXECUTION,
                attributes={
                    "repair_id": record.id,
                    "status": record.execution.status.value,
                    "tool": record.execution.tool,
                },
            ),
            GraphNode(
                id=verification_id,
                kind=GraphKind.REPAIR_VERIFICATION,
                attributes={"status": verification.status.value},
            ),
            GraphNode(
                id=after_id,
                kind=GraphKind.FILESYSTEM_STATUS,
                attributes={"status": verification.after.health.value if verification.after else "UNKNOWN"},
            ),
        ]
        edges = [
            GraphEdge(source=filesystem_id, relation="had_status", target=before_id),
            GraphEdge(source=filesystem_id, relation="repaired_by", target=repair_id),
            GraphEdge(source=repair_id, relation="verified_by", target=verification_id),
            GraphEdge(source=filesystem_id, relation="current_status", target=after_id),
        ]
        if checkpoint_id is not None and plan.protection_checkpoint is not None:
            nodes.append(
                GraphNode(
                    id=checkpoint_id,
                    kind=GraphKind.PROTECTION_CHECKPOINT,
                    attributes={
                        "checkpoint_id": plan.protection_checkpoint.id,
                        "provider": plan.protection_checkpoint.provider_capability_id,
                        "verification_id": plan.protection_checkpoint.verification_id,
                    },
                )
            )
            edges.append(
                GraphEdge(source=repair_id, relation="protected_by", target=checkpoint_id)
            )
        snapshot = await context.graph.apply(tuple(nodes), tuple(edges))
        result = FilesystemRepairResult(
            repair=record,
            verification=verification,
            knowledge_graph_revision=snapshot.revision,
        )
        await _event(
            context,
            "knowledge.graph.updated",
            {"repair_id": record.id, "revision": snapshot.revision},
            session_id=record.execution.session_id,
        )
        return result.model_dump(mode="json")

    async def compensate(self, output: dict[str, Any], context: ActionContext) -> None:
        del output, context


def _verification_status(
    plan: FilesystemRepairPlan, outcome: FilesystemRepairOutcome
) -> tuple[RepairVerificationStatus, str]:
    if outcome.after.health is not FilesystemHealth.HEALTHY:
        if outcome.after.health is FilesystemHealth.UNKNOWN:
            return RepairVerificationStatus.UNKNOWN, "La verificación posterior no fue concluyente."
        return RepairVerificationStatus.FAILED, "La comprobación posterior aún detecta inconsistencias."
    if plan.filesystem is FilesystemType.NTFS and outcome.repair_tool != "none":
        return (
            RepairVerificationStatus.PARTIAL,
            "ntfsfix terminó y el check Linux no detecta errores comunes, pero Windows CHKDSK sigue siendo necesario.",
        )
    if plan.mount.mounted and outcome.remounted is not True:
        return RepairVerificationStatus.PARTIAL, "El filesystem verificó sano pero no pudo remontarse."
    return RepairVerificationStatus.SUCCESS, "La comprobación posterior demuestra consistencia según el adapter."


async def _event(
    context: ActionContext,
    name: str,
    payload: dict[str, Any],
    severity: EventSeverity = EventSeverity.INFO,
    *,
    session_id: str | None = None,
) -> None:
    await context.event_bus.publish(
        AresEvent(
            event_type=name,
            source="filesystem-repair.workflow",
            correlation_id=context.execution_id,
            session_id=session_id or context.execution_id,
            severity=severity,
            payload=payload,
        )
    )


def _token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
