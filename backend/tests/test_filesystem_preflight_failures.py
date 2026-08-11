from __future__ import annotations

import os
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

import pytest

from ares.filesystems.integrity import repair_plan_fingerprint
from ares.filesystems.models import (
    DeviceIdentity,
    FilesystemRepairPlan,
    FilesystemType,
    MountRecord,
    MountSafetyReport,
    RepairAction,
)
from ares.tools.filesystem import FilesystemToolError, FilesystemToolSuite
from ares.tools.storage import ProcessResult, ToolAvailability


class Runner:
    def __init__(self) -> None:
        self.results: dict[str, deque[ProcessResult | BaseException]] = defaultdict(deque)
        self.calls: list[str] = []

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
        self.calls.append(tool)
        if self.results[tool]:
            result = self.results[tool].popleft()
            if isinstance(result, BaseException):
                raise result
            return result
        if tool == "blkid":
            return _result(tool, 0, "TYPE=ext4\nUUID=preflight-uuid\n")
        return _result(tool, 0)


class FixedMountChecker:
    def __init__(self, report: MountSafetyReport) -> None:
        self.report = report

    def inspect(self, identity: DeviceIdentity) -> MountSafetyReport:
        del identity
        return self.report


def _result(tool: str, exit_code: int, stdout: str = "") -> ProcessResult:
    return ProcessResult(
        tool=tool,
        exit_code=exit_code,
        stdout=stdout,
        stderr="",
        duration_ms=1.0,
    )


async def _plan(
    tmp_path: Path,
    runner: Runner,
    mount: MountSafetyReport,
) -> tuple[FilesystemToolSuite, FilesystemRepairPlan]:
    image = tmp_path / "preflight.img"
    image.write_bytes(b"ARES preflight fixture" * 512)
    suite = FilesystemToolSuite(
        runner=runner,
        mount_checker=FixedMountChecker(mount),
        allow_regular_file_targets=True,
    )
    identity = await suite.identify(str(image))
    draft = FilesystemRepairPlan(
        session_id="preflight-session-123",
        target=identity,
        filesystem=FilesystemType.EXT4,
        mount=mount,
        detected_problems=("filesystem_errors_detected",),
        required_permissions=("block-device.readwrite",),
        repair_actions=(
            RepairAction(
                id="filesystem.repair",
                description="Synthetic preflight fixture",
                mutates_target=True,
            ),
        ),
        verification_steps=("post-check",),
        rollback_strategy="No production resource is used.",
        executable=True,
        fingerprint_sha256="0" * 64,
    )
    return suite, draft.model_copy(update={"fingerprint_sha256": repair_plan_fingerprint(draft)})


async def test_inspection_reports_target_not_writable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = Runner()
    mount = MountSafetyReport(
        mounted=False,
        busy=False,
        swap=False,
        safe_to_unmount=False,
        safe_to_remount=False,
    )
    suite, plan = await _plan(tmp_path, runner, mount)
    real_access = os.access

    def denied(path: str | bytes | os.PathLike[str] | os.PathLike[bytes], mode: int) -> bool:
        if os.fspath(path) == plan.target.canonical_path and mode == os.W_OK:
            return False
        return real_access(path, mode)

    monkeypatch.setattr("ares.tools.filesystem.os.access", denied)
    inspection = await suite.inspect(plan.target.requested_path)

    assert inspection.writable is False
    assert "target_not_writable" in inspection.limitations


async def test_unmount_failure_stops_before_repair_command(tmp_path: Path) -> None:
    runner = Runner()
    mount_point = str(tmp_path / "mounted")
    mount = MountSafetyReport(
        mounted=True,
        busy=False,
        swap=False,
        mounts=(
            MountRecord(
                mount_point=mount_point,
                root="/",
                source=str(tmp_path / "preflight.img"),
                filesystem_type="ext4",
                options=("rw",),
                bind_mount=False,
            ),
        ),
        safe_to_unmount=True,
        safe_to_remount=True,
    )
    runner.results["umount"].append(_result("umount", 1))
    suite, plan = await _plan(tmp_path, runner, mount)

    with pytest.raises(FilesystemToolError) as caught:
        await suite.execute_repair(plan, _ignore)

    assert caught.value.code == "FILESYSTEM_UNMOUNT_FAILED"
    assert "e2fsck" not in runner.calls


async def test_repair_timeout_never_reaches_successful_verification(tmp_path: Path) -> None:
    runner = Runner()
    mount = MountSafetyReport(
        mounted=False,
        busy=False,
        swap=False,
        safe_to_unmount=False,
        safe_to_remount=False,
    )
    runner.results["e2fsck"].extend((_result("e2fsck", 4), TimeoutError()))
    suite, plan = await _plan(tmp_path, runner, mount)

    with pytest.raises(FilesystemToolError) as caught:
        await suite.execute_repair(plan, _ignore)

    assert caught.value.code == "FILESYSTEM_REPAIR_TIMEOUT"
    assert runner.calls.count("e2fsck") == 2


async def _ignore(name: str, payload: dict[str, Any]) -> None:
    del name, payload
