from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from ares.filesystems.executor import LocalTestFilesystemExecutor
from ares.filesystems.integrity import repair_plan_fingerprint
from ares.filesystems.models import (
    FilesystemHealth,
    FilesystemRepairPlan,
    FilesystemType,
    MountSafetyReport,
    RepairAction,
)
from ares.protection import ProtectionCheckpoint, ProtectionCheckpointStatus
from ares.tools.filesystem import FilesystemToolSuite, MountSafetyChecker
from ares.tools.storage import SafeProcessRunner

_REQUIRED_FIXTURE_TOOLS = ("mkfs.ext4", "debugfs", "e2fsck", "blkid")


def test_real_ext4_image_is_detected_repaired_and_verified(tmp_path: Path) -> None:
    missing = [tool for tool in _REQUIRED_FIXTURE_TOOLS if shutil.which(tool) is None]
    if missing:
        pytest.skip("isolated ext4 integration tools unavailable: " + ",".join(missing))

    image = tmp_path / "corrupted-ext4.img"
    subprocess.run(("truncate", "-s", "32M", str(image)), check=True)
    subprocess.run(("mkfs.ext4", "-F", "-q", str(image)), check=True)
    subprocess.run(
        ("debugfs", "-w", "-R", "set_super_value free_blocks_count 1", str(image)),
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    async def scenario() -> None:
        tools = FilesystemToolSuite(
            runner=SafeProcessRunner(),
            mount_checker=MountSafetyChecker(scan_processes=False),
            allow_regular_file_targets=True,
        )
        executor = LocalTestFilesystemExecutor(tools)
        inspection = await executor.inspect(str(image))
        assert inspection.filesystem is FilesystemType.EXT4
        assert inspection.health is FilesystemHealth.INCONSISTENT
        resource_id = f"filesystem:{inspection.identity.fingerprint_sha256}"
        checkpoint = ProtectionCheckpoint(
            status=ProtectionCheckpointStatus.READY,
            protected_resources=(resource_id,),
            resource_fingerprints={resource_id: inspection.identity.fingerprint_sha256},
            provider_capability_id="backup.create",
            backup_id="real-image-backup-12345678",
            verification_id="real-image-verification-12345678",
            session_id="real-image-session-123",
            evidence_sha256="e" * 64,
            limitations=("synthetic test checkpoint for isolated image fixture",),
        )
        draft = FilesystemRepairPlan(
            session_id="real-image-session-123",
            target=inspection.identity,
            filesystem=FilesystemType.EXT4,
            mount=MountSafetyReport(
                mounted=False,
                busy=False,
                swap=False,
                safe_to_unmount=False,
                safe_to_remount=False,
            ),
            detected_problems=inspection.check.problems if inspection.check else (),
            evidence=inspection.check.evidence if inspection.check else (),
            required_tools=inspection.required_tools,
            required_permissions=("block-device.readwrite",),
            protection_checkpoint=checkpoint,
            repair_actions=(
                RepairAction(
                    id="filesystem.repair",
                    description="Repair only the isolated ext4 image fixture.",
                    mutates_target=True,
                ),
            ),
            verification_steps=("Run e2fsck -f -n after repair",),
            rollback_strategy="Test image can be discarded; no physical device is involved.",
            executable=True,
            fingerprint_sha256="0" * 64,
        )
        plan = draft.model_copy(update={"fingerprint_sha256": repair_plan_fingerprint(draft)})
        challenges: list[str] = []

        async def on_challenge(challenge_id: str) -> None:
            challenges.append(challenge_id)

        grant = await executor.request_authorization(plan, on_challenge=on_challenge)
        stages: list[str] = []

        async def on_stage(name: str, payload: dict[str, object]) -> None:
            del payload
            stages.append(name)

        outcome = await executor.execute(plan, grant, on_stage=on_stage)

        assert challenges
        assert outcome.before.health is FilesystemHealth.INCONSISTENT
        assert outcome.repair_tool == "e2fsck"
        assert outcome.repair_exit_code in {0, 1, 2}
        assert outcome.after.health is FilesystemHealth.HEALTHY
        assert "filesystem.repair-command.started" in stages
        assert "filesystem.verification.completed" in stages

    asyncio.run(scenario())
