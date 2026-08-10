from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

from ares.events import EventBus, MemoryEventSink
from ares.filesystems.integrity import repair_plan_fingerprint
from ares.filesystems.models import (
    DeviceIdentity,
    FilesystemRepairPlan,
    FilesystemRepairRecord,
    FilesystemType,
    MountSafetyReport,
    RepairAction,
    RepairExecution,
    RepairExecutionStatus,
)
from ares.filesystems.service import FilesystemRepairService
from ares.filesystems.store import FilesystemRepairStore
from ares.protection import ProtectionCheckpoint, ProtectionCheckpointStatus


async def test_service_cancels_only_before_mutation(tmp_path: Path) -> None:
    store = FilesystemRepairStore(tmp_path / "repairs")
    store.prepare()
    identity = DeviceIdentity(
        requested_path=str(tmp_path / "image.img"),
        canonical_path=str(tmp_path / "image.img"),
        major_minor="file:1:1",
        size_bytes=4096,
        block_device=False,
        fingerprint_sha256="1" * 64,
    )
    resource_id = f"filesystem:{identity.fingerprint_sha256}"
    checkpoint = ProtectionCheckpoint(
        status=ProtectionCheckpointStatus.READY,
        protected_resources=(resource_id,),
        resource_fingerprints={resource_id: identity.fingerprint_sha256},
        provider_capability_id="backup.create",
        backup_id="cancel-backup-12345678",
        verification_id="cancel-verification-12345678",
        session_id="cancel-session-123",
        evidence_sha256="2" * 64,
    )
    draft = FilesystemRepairPlan(
        session_id="cancel-session-123",
        target=identity,
        filesystem=FilesystemType.EXT4,
        mount=MountSafetyReport(
            mounted=False,
            busy=False,
            swap=False,
            safe_to_unmount=False,
            safe_to_remount=False,
        ),
        detected_problems=("filesystem_errors_detected",),
        protection_checkpoint=checkpoint,
        required_permissions=("block-device.readwrite",),
        repair_actions=(
            RepairAction(
                id="filesystem.repair",
                description="Synthetic cancellation fixture",
                mutates_target=True,
            ),
        ),
        verification_steps=("post-check",),
        rollback_strategy="No mutation has started, so cancellation is safe.",
        executable=True,
        fingerprint_sha256="0" * 64,
    )
    plan = draft.model_copy(
        update={"fingerprint_sha256": repair_plan_fingerprint(draft)}
    )
    record = FilesystemRepairRecord(
        id=plan.repair_id,
        plan=plan,
        execution=RepairExecution(
            id=plan.repair_id,
            plan_id=plan.id,
            session_id=plan.session_id,
            status=RepairExecutionStatus.AUTHORIZING,
            checkpoint_id=checkpoint.id,
        ),
    )
    await store.put_repair(record)

    async def pending_authorization() -> None:
        await asyncio.sleep(3600)

    task = asyncio.create_task(pending_authorization())
    service = cast(FilesystemRepairService, object.__new__(FilesystemRepairService))
    service.store = store
    service.event_bus = EventBus(MemoryEventSink())
    service._task_lock = asyncio.Lock()
    service._tasks = {plan.repair_id: task}

    cancelled = await service.cancel(plan.repair_id, session_id=plan.session_id)

    assert cancelled.execution.status is RepairExecutionStatus.CANCELLED
    assert cancelled.execution.error_code == "FILESYSTEM_REPAIR_CANCELLED"
    await asyncio.gather(task, return_exceptions=True)
