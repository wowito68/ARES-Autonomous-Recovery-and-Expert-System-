from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from ares.filesystems.executor import (
    FilesystemExecutorError,
    LocalTestFilesystemExecutor,
    UnixBrokerFilesystemExecutor,
)
from ares.filesystems.integrity import repair_plan_fingerprint, repair_plan_integrity_valid
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
from ares.filesystems.service import FilesystemRepairService, FilesystemServiceError
from ares.filesystems.store import FilesystemRepairStore
from ares.protection import ProtectionCheckpoint, ProtectionCheckpointStatus
from ares.tools.filesystem import FilesystemToolError, FilesystemToolSuite, MountSafetyChecker
from ares.tools.storage import ProcessResult, ToolAvailability


class ImageRunner:
    def inspect(self, tool: str) -> ToolAvailability:
        return ToolAvailability(tool=tool, available=True, reason=None)

    async def run(
        self,
        tool: str,
        args: tuple[str, ...],
        *,
        timeout_seconds: float,
    ) -> ProcessResult:
        del args, timeout_seconds
        stdout = "TYPE=ext4\nUUID=failure-test-uuid\n" if tool == "blkid" else ""
        return ProcessResult(
            tool=tool,
            exit_code=0,
            stdout=stdout,
            stderr="",
            duration_ms=1.0,
        )


async def _plan(tmp_path: Path) -> tuple[FilesystemToolSuite, FilesystemRepairPlan]:
    image = tmp_path / "failure.img"
    image.write_bytes(b"ARES failure fixture" * 512)
    tools = FilesystemToolSuite(
        runner=ImageRunner(),
        mount_checker=MountSafetyChecker(scan_processes=False),
        allow_regular_file_targets=True,
    )
    identity = await tools.identify(str(image))
    resource_id = f"filesystem:{identity.fingerprint_sha256}"
    checkpoint = ProtectionCheckpoint(
        status=ProtectionCheckpointStatus.READY,
        protected_resources=(resource_id,),
        resource_fingerprints={resource_id: identity.fingerprint_sha256},
        provider_capability_id="backup.create",
        backup_id="failure-backup-12345678",
        verification_id="failure-verification-12345678",
        session_id="failure-session-123",
        evidence_sha256="f" * 64,
    )
    draft = FilesystemRepairPlan(
        session_id="failure-session-123",
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
                description="Repair only synthetic failure fixture",
                mutates_target=True,
            ),
        ),
        verification_steps=("structured post-check",),
        rollback_strategy="No metadata rollback in test fixture.",
        executable=True,
        fingerprint_sha256="0" * 64,
    )
    return tools, draft.model_copy(update={"fingerprint_sha256": repair_plan_fingerprint(draft)})


async def test_disappeared_target_is_distinct_from_identity_change(tmp_path: Path) -> None:
    tools, plan = await _plan(tmp_path)
    Path(plan.target.canonical_path).unlink()

    with pytest.raises(FilesystemToolError) as caught:
        await tools.revalidate(plan.target)

    assert caught.value.code == "FILESYSTEM_DEVICE_DISAPPEARED"


def test_plan_integrity_detects_any_semantic_change(tmp_path: Path) -> None:
    plan = asyncio.run(_plan(tmp_path))[1]
    assert repair_plan_integrity_valid(plan) is True

    tampered = plan.model_copy(update={"limitations": ("changed-after-approval",)})
    assert repair_plan_integrity_valid(tampered) is False


async def test_local_authorization_expiry_is_rejected(tmp_path: Path) -> None:
    tools, plan = await _plan(tmp_path)
    executor = LocalTestFilesystemExecutor(tools)

    async def challenge(_: str) -> None:
        return None

    grant = await executor.request_authorization(plan, on_challenge=challenge)
    expired = grant.model_copy(update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)})

    with pytest.raises(FilesystemExecutorError) as caught:
        await executor.execute(plan, expired, on_stage=_ignore_stage)

    assert caught.value.code == "FILESYSTEM_AUTHORIZATION_INVALID"


async def test_unix_executor_fails_closed_when_broker_socket_is_missing(tmp_path: Path) -> None:
    executor = UnixBrokerFilesystemExecutor(tmp_path / "missing.sock")

    with pytest.raises(FilesystemExecutorError) as caught:
        await executor.inspect("/dev/test")

    assert caught.value.code == "FILESYSTEM_BROKER_UNAVAILABLE"


async def test_repair_store_marks_interrupted_mutation_aborted(tmp_path: Path) -> None:
    _, plan = await _plan(tmp_path)
    store = FilesystemRepairStore(tmp_path / "repairs")
    store.prepare()
    record = FilesystemRepairRecord(
        id=plan.repair_id,
        plan=plan,
        execution=RepairExecution(
            id=plan.repair_id,
            plan_id=plan.id,
            session_id=plan.session_id,
            status=RepairExecutionStatus.RUNNING,
            started_at=datetime.now(UTC),
            checkpoint_id=cast(ProtectionCheckpoint, plan.protection_checkpoint).id,
        ),
    )
    await store.put_repair(record)

    reloaded = FilesystemRepairStore(tmp_path / "repairs")
    reloaded.prepare()
    recovered = await reloaded.get_repair(plan.repair_id)

    assert recovered is not None
    assert recovered.execution.status is RepairExecutionStatus.ABORTED
    assert recovered.execution.error_code == "FILESYSTEM_REPAIR_INTERRUPTED"
    assert recovered.verification is None


async def test_service_refuses_cancellation_after_destructive_phase_started(
    tmp_path: Path,
) -> None:
    _, plan = await _plan(tmp_path)
    store = FilesystemRepairStore(tmp_path / "store")
    store.prepare()
    record = FilesystemRepairRecord(
        id=plan.repair_id,
        plan=plan,
        execution=RepairExecution(
            id=plan.repair_id,
            plan_id=plan.id,
            session_id=plan.session_id,
            status=RepairExecutionStatus.RUNNING,
            started_at=datetime.now(UTC),
        ),
    )
    await store.put_repair(record)
    service = cast(FilesystemRepairService, object.__new__(FilesystemRepairService))
    service.store = store

    with pytest.raises(FilesystemServiceError) as caught:
        await service.cancel(plan.repair_id, session_id=plan.session_id)

    assert caught.value.code == "FILESYSTEM_CANCELLATION_UNSAFE"


async def _ignore_stage(name: str, payload: dict[str, object]) -> None:
    del name, payload
