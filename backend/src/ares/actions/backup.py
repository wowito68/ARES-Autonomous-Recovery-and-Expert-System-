"""Private workflow actions for backup.create, backup.verify and backup.list."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from typing import Any

from ares.actions.base import ActionContext, ActionError
from ares.backup.executor import BackupExecutor, BackupExecutorError
from ares.backup.models import (
    AuthorizationGrant,
    Backup,
    BackupCreateInput,
    BackupEntry,
    BackupGraphSummary,
    BackupListInput,
    BackupListResult,
    BackupManifest,
    BackupPlan,
    BackupProgress,
    BackupStatus,
    BackupVerification,
    BackupVerificationStatus,
    BackupVerifyInput,
)
from ares.backup.store import BackupStore
from ares.events import AresEvent, EventSeverity
from ares.knowledge import GraphEdge, GraphKind, GraphNode
from ares.tools.backup import BackupFilesystemTools, BackupToolError


class ValidateBackupPlanAction:
    id = "backup.validate-plan"
    idempotent = True

    def __init__(self, store: BackupStore, tools: BackupFilesystemTools) -> None:
        self.store = store
        self.tools = tools

    async def run(self, inputs: dict[str, Any], context: ActionContext) -> dict[str, Any]:
        request = BackupCreateInput.model_validate(inputs)
        plan = await self.store.get_plan(request.plan_id)
        if plan is None:
            raise ActionError("BACKUP_PLAN_NOT_FOUND")
        try:
            await asyncio.to_thread(self.tools.revalidate_plan, plan)
        except BackupToolError as exc:
            raise ActionError(exc.code) from exc
        backup = await self.store.get_backup(plan.backup_id)
        if backup is None:
            raise ActionError("BACKUP_RECORD_NOT_FOUND")
        backup = _with_status(backup, BackupStatus.VALIDATING)
        await self.store.put_backup(backup)
        await _event(
            context,
            "backup.validation.completed",
            {"backup_id": backup.id, "plan_id": plan.id, "result": "ok"},
        )
        return {
            "plan": plan.model_dump(mode="json"),
            "request": request.model_dump(mode="json"),
        }

    async def compensate(self, output: dict[str, Any], context: ActionContext) -> None:
        del output, context


class RequestBackupAuthorizationAction:
    id = "backup.request-authorization"
    idempotent = False

    def __init__(self, store: BackupStore, executor: BackupExecutor) -> None:
        self.store = store
        self.executor = executor

    async def run(self, inputs: dict[str, Any], context: ActionContext) -> dict[str, Any]:
        plan = BackupPlan.model_validate(inputs["plan"])
        request = BackupCreateInput.model_validate(inputs["request"])

        async def challenge(challenge_id: str) -> None:
            backup = await self.store.get_backup(plan.backup_id)
            if backup is not None:
                execution = backup.execution.model_copy(
                    update={"authorization_challenge_id": challenge_id}
                )
                await self.store.put_backup(backup.model_copy(update={"execution": execution}))
            await _event(
                context,
                "backup.authorization.requested",
                {
                    "backup_id": plan.backup_id,
                    "plan_id": plan.id,
                    "challenge_id": challenge_id,
                    "estimated_bytes": plan.source.estimated_size_bytes,
                    "included_file_count": plan.included_file_count,
                    "excluded_count": len(plan.exclusions),
                    "risk": "medium",
                },
                EventSeverity.WARNING,
                session_id=request.session_id,
            )

        try:
            grant = await self.executor.request_authorization(
                plan, session_id=request.session_id, on_challenge=challenge
            )
        except BackupExecutorError as exc:
            raise ActionError(exc.code) from exc
        return {
            "plan": plan.model_dump(mode="json"),
            "request": request.model_dump(mode="json"),
            "grant": grant.model_dump(mode="json"),
        }

    async def compensate(self, output: dict[str, Any], context: ActionContext) -> None:
        del output, context


class CreateBackupFilesAction:
    id = "backup.create-files"
    idempotent = False

    def __init__(self, store: BackupStore, executor: BackupExecutor) -> None:
        self.store = store
        self.executor = executor

    async def run(self, inputs: dict[str, Any], context: ActionContext) -> dict[str, Any]:
        plan = BackupPlan.model_validate(inputs["plan"])
        request = BackupCreateInput.model_validate(inputs["request"])
        grant = AuthorizationGrant.model_validate(inputs["grant"])
        backup = await self.store.get_backup(plan.backup_id)
        if backup is None:
            raise ActionError("BACKUP_RECORD_NOT_FOUND")
        started = datetime.now(UTC)
        execution = backup.execution.model_copy(
            update={
                "status": BackupStatus.RUNNING,
                "started_at": started,
                "workflow_execution_id": context.execution_id,
            }
        )
        backup = backup.model_copy(update={"status": BackupStatus.RUNNING, "execution": execution})
        await self.store.put_backup(backup)
        await _event(
            context,
            "backup.started",
            {"backup_id": backup.id, "plan_id": plan.id},
            EventSeverity.WARNING,
            session_id=request.session_id,
        )

        async def progress(value: BackupProgress) -> None:
            current = await self.store.get_backup(plan.backup_id)
            if current is not None:
                current_execution = current.execution.model_copy(update={"progress": value})
                await self.store.put_backup(
                    current.model_copy(update={"execution": current_execution})
                )
            await _event(
                context,
                "backup.progress",
                {
                    "backup_id": plan.backup_id,
                    "files_completed": value.files_completed,
                    "files_total": value.files_total,
                    "bytes_completed": value.bytes_completed,
                    "bytes_total": value.bytes_total,
                    "percent": round(value.percent, 3),
                    "speed_bytes_per_second": value.speed_bytes_per_second,
                    "eta_seconds": value.eta_seconds,
                },
                session_id=request.session_id,
            )

        async def entry(value: BackupEntry) -> None:
            await _event(
                context,
                "backup.entry.created",
                {
                    "backup_id": plan.backup_id,
                    "entry_token": hashlib.sha256(value.relative_path.encode("utf-8")).hexdigest()[
                        :24
                    ],
                    "entry_type": value.entry_type.value,
                    "size_bytes": value.size_bytes,
                },
                session_id=request.session_id,
            )

        try:
            manifest = await self.executor.create(plan, grant, on_progress=progress, on_entry=entry)
        except asyncio.CancelledError:
            await self._mark_cancelled(plan.backup_id)
            await _event(
                context,
                "backup.cancelled",
                {"backup_id": plan.backup_id},
                EventSeverity.WARNING,
                session_id=request.session_id,
            )
            raise
        except BackupExecutorError as exc:
            await self._mark_failed(plan.backup_id, exc.code)
            await _event(
                context,
                "backup.failed",
                {"backup_id": plan.backup_id, "error_code": exc.code},
                EventSeverity.ERROR,
                session_id=request.session_id,
            )
            raise ActionError(exc.code) from exc
        return {
            "manifest": manifest.model_dump(mode="json"),
            "request": request.model_dump(mode="json"),
        }

    async def compensate(self, output: dict[str, Any], context: ActionContext) -> None:
        del output, context

    async def _mark_cancelled(self, backup_id: str) -> None:
        backup = await self.store.get_backup(backup_id)
        if backup is None:
            return
        execution = backup.execution.model_copy(
            update={
                "status": BackupStatus.CANCELLED,
                "finished_at": datetime.now(UTC),
                "error_code": "BACKUP_CANCELLED",
            }
        )
        await self.store.put_backup(
            backup.model_copy(update={"status": BackupStatus.CANCELLED, "execution": execution})
        )

    async def _mark_failed(self, backup_id: str, code: str) -> None:
        backup = await self.store.get_backup(backup_id)
        if backup is None:
            return
        execution = backup.execution.model_copy(
            update={
                "status": BackupStatus.FAILED,
                "finished_at": datetime.now(UTC),
                "error_code": code,
            }
        )
        await self.store.put_backup(
            backup.model_copy(update={"status": BackupStatus.FAILED, "execution": execution})
        )


class PersistBackupManifestAction:
    id = "backup.persist-manifest"
    idempotent = True

    def __init__(self, store: BackupStore) -> None:
        self.store = store

    async def run(self, inputs: dict[str, Any], context: ActionContext) -> dict[str, Any]:
        del context
        manifest = BackupManifest.model_validate(inputs["manifest"])
        request = BackupCreateInput.model_validate(inputs["request"])
        await self.store.put_manifest(manifest)
        backup = await self.store.get_backup(manifest.backup_id)
        if backup is None:
            raise ActionError("BACKUP_RECORD_NOT_FOUND")
        backup = backup.model_copy(
            update={
                "size": manifest.total_size_bytes,
                "file_count": manifest.file_count,
                "checksum": manifest.manifest_checksum_sha256,
                "status": BackupStatus.VERIFYING,
                "verification_status": BackupVerificationStatus.RUNNING,
                "execution": backup.execution.model_copy(update={"status": BackupStatus.VERIFYING}),
            }
        )
        await self.store.put_backup(backup)
        return {
            "backup": backup.model_dump(mode="json"),
            "manifest": manifest.model_dump(mode="json"),
            "request": request.model_dump(mode="json"),
        }

    async def compensate(self, output: dict[str, Any], context: ActionContext) -> None:
        del output, context


class VerifyBackupAction:
    id = "backup.verify-integrity"
    idempotent = True

    def __init__(self, store: BackupStore, executor: BackupExecutor) -> None:
        self.store = store
        self.executor = executor

    async def run(self, inputs: dict[str, Any], context: ActionContext) -> dict[str, Any]:
        backup = Backup.model_validate(inputs["backup"])
        manifest = BackupManifest.model_validate(inputs["manifest"])
        session_id = backup.session_id
        await _event(
            context,
            "backup.verification.started",
            {"backup_id": backup.id, "manifest_checksum": manifest.manifest_checksum_sha256},
            session_id=session_id,
        )
        try:
            verification = await self.executor.verify(backup, manifest)
        except BackupExecutorError as exc:
            raise ActionError(exc.code) from exc
        await self.store.put_verification(verification)
        if verification.status is BackupVerificationStatus.VERIFIED:
            status = BackupStatus.COMPLETED
            severity = EventSeverity.INFO
            event_name = "backup.verified"
        elif verification.status is BackupVerificationStatus.CORRUPTED:
            status = BackupStatus.CORRUPTED
            severity = EventSeverity.ERROR
            event_name = "backup.failed"
        else:
            status = BackupStatus.FAILED
            severity = EventSeverity.ERROR
            event_name = "backup.failed"
        execution = backup.execution.model_copy(
            update={
                "status": status,
                "finished_at": datetime.now(UTC),
                "error_code": None if status is BackupStatus.COMPLETED else "BACKUP_VERIFY_FAILED",
            }
        )
        backup = backup.model_copy(
            update={
                "status": status,
                "verification_status": verification.status,
                "execution": execution,
            }
        )
        await self.store.put_backup(backup)
        await _event(
            context,
            event_name,
            {
                "backup_id": backup.id,
                "verification_id": verification.id,
                "status": verification.status.value,
                "mismatch_count": len(verification.checksum_mismatches),
                "missing_count": len(verification.missing_entries),
            },
            severity,
            session_id=session_id,
        )
        if status is BackupStatus.COMPLETED:
            await _event(
                context,
                "backup.completed",
                {
                    "backup_id": backup.id,
                    "size_bytes": backup.size,
                    "file_count": backup.file_count,
                    "verified": True,
                },
                session_id=session_id,
            )
        return {
            "backup": backup.model_dump(mode="json"),
            "manifest": manifest.model_dump(mode="json"),
            "verification": verification.model_dump(mode="json"),
        }

    async def compensate(self, output: dict[str, Any], context: ActionContext) -> None:
        del output, context


class ProjectBackupGraphAction:
    id = "knowledge.project-backup"
    idempotent = True

    async def run(self, inputs: dict[str, Any], context: ActionContext) -> dict[str, Any]:
        backup = Backup.model_validate(inputs["backup"])
        manifest = BackupManifest.model_validate(inputs["manifest"])
        verification = BackupVerification.model_validate(inputs["verification"])
        source_id = f"backup-source:{_id(backup.source.path)}"
        destination_id = f"backup-destination:{_id(backup.destination.device_id + backup.destination.mount_point)}"
        backup_id = f"backup:{backup.id}"
        verification_id = f"backup-verification:{verification.id}"
        nodes = [
            GraphNode(
                id=source_id,
                kind=GraphKind.BACKUP_SOURCE,
                attributes={
                    "path": backup.source.path,
                    "device_id": backup.source.device_id,
                    "filesystem_type": backup.source.filesystem_type,
                },
            ),
            GraphNode(
                id=destination_id,
                kind=GraphKind.BACKUP_DESTINATION,
                attributes={
                    "mount_point": backup.destination.mount_point,
                    "device_id": backup.destination.device_id,
                    "filesystem_type": backup.destination.filesystem_type,
                    "kind": backup.destination.kind.value,
                },
            ),
            GraphNode(
                id=backup_id,
                kind=GraphKind.BACKUP,
                attributes={
                    "backup_id": backup.id,
                    "created_at": backup.created_at.isoformat(),
                    "status": backup.status.value,
                    "size": backup.size,
                    "file_count": backup.file_count,
                    "verification_status": backup.verification_status.value,
                    "manifest_checksum": backup.checksum,
                    "projected_entry_count": min(len(manifest.entries), 1024),
                    "entry_projection_truncated": len(manifest.entries) > 1024,
                },
            ),
            GraphNode(
                id=verification_id,
                kind=GraphKind.BACKUP_VERIFICATION,
                attributes={
                    "status": verification.status.value,
                    "verified_file_count": verification.verified_file_count,
                    "verified_size_bytes": verification.verified_size_bytes,
                },
            ),
        ]
        edges = [
            GraphEdge(source=source_id, relation="backed_up_to", target=backup_id),
            GraphEdge(source=backup_id, relation="stored_on", target=destination_id),
            GraphEdge(source=backup_id, relation="verified_by", target=verification_id),
            GraphEdge(source=backup_id, relation="protects", target=source_id),
        ]
        for entry in manifest.entries[:1024]:
            entry_id = f"backup-entry:{_id(backup.id + ':' + entry.relative_path)}"
            nodes.append(
                GraphNode(
                    id=entry_id,
                    kind=GraphKind.BACKUP_ENTRY,
                    attributes={
                        "path_token": _id(entry.relative_path),
                        "type": entry.entry_type.value,
                        "size_bytes": entry.size_bytes,
                        "checksum": entry.checksum_sha256,
                    },
                )
            )
            edges.append(GraphEdge(source=backup_id, relation="contains", target=entry_id))
        snapshot = await context.graph.apply(tuple(nodes), tuple(edges))
        await _event(
            context,
            "knowledge.graph.updated",
            {
                "backup_id": backup.id,
                "revision": snapshot.revision,
                "node_count": len(snapshot.nodes),
                "edge_count": len(snapshot.edges),
            },
            session_id=backup.session_id,
        )
        return {
            **inputs,
            "knowledge_graph": BackupGraphSummary(
                revision=snapshot.revision,
                node_count=len(snapshot.nodes),
                edge_count=len(snapshot.edges),
            ).model_dump(mode="json"),
        }

    async def compensate(self, output: dict[str, Any], context: ActionContext) -> None:
        del output, context


class LoadBackupForVerificationAction:
    id = "backup.load-verification-target"
    idempotent = True

    def __init__(self, store: BackupStore) -> None:
        self.store = store

    async def run(self, inputs: dict[str, Any], context: ActionContext) -> dict[str, Any]:
        del context
        request = BackupVerifyInput.model_validate(inputs)
        backup = await self.store.get_backup(request.backup_id)
        manifest = await self.store.get_manifest(request.backup_id)
        if backup is None:
            raise ActionError("BACKUP_NOT_FOUND")
        if manifest is None:
            raise ActionError("BACKUP_MANIFEST_NOT_FOUND")
        return {
            "backup": backup.model_dump(mode="json"),
            "manifest": manifest.model_dump(mode="json"),
        }

    async def compensate(self, output: dict[str, Any], context: ActionContext) -> None:
        del output, context


class ListBackupsAction:
    id = "backup.list-records"
    idempotent = True

    def __init__(self, store: BackupStore) -> None:
        self.store = store

    async def run(self, inputs: dict[str, Any], context: ActionContext) -> dict[str, Any]:
        del context
        request = BackupListInput.model_validate(inputs)
        result = BackupListResult(backups=await self.store.list_backups(request.limit))
        return result.model_dump(mode="json")

    async def compensate(self, output: dict[str, Any], context: ActionContext) -> None:
        del output, context


def _with_status(backup: Backup, status: BackupStatus) -> Backup:
    execution = backup.execution.model_copy(update={"status": status})
    return backup.model_copy(update={"status": status, "execution": execution})


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
            source="backup.workflow",
            correlation_id=context.execution_id,
            session_id=session_id or context.execution_id,
            severity=severity,
            payload=payload,
        )
    )


def _id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
