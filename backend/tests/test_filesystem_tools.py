from __future__ import annotations

import os
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

import pytest

from ares.filesystems.models import (
    DeviceIdentity,
    FilesystemHealth,
    FilesystemRepairPlan,
    FilesystemType,
    MountSafetyReport,
    RepairAction,
)
from ares.tools.filesystem import FilesystemToolError, FilesystemToolSuite, MountSafetyChecker
from ares.tools.storage import ProcessResult, ToolAvailability


class FakeRunner:
    def __init__(self) -> None:
        self.available: dict[str, bool] = defaultdict(lambda: True)
        self.results: dict[str, deque[ProcessResult | BaseException]] = defaultdict(deque)
        self.calls: list[tuple[str, tuple[str, ...], float]] = []

    def inspect(self, tool: str) -> ToolAvailability:
        available = self.available[tool]
        return ToolAvailability(
            tool=tool,
            available=available,
            reason=None if available else "tool_not_installed",
        )

    async def run(
        self,
        tool: str,
        args: tuple[str, ...],
        *,
        timeout_seconds: float,
    ) -> ProcessResult:
        self.calls.append((tool, args, timeout_seconds))
        if not self.available[tool]:
            raise FileNotFoundError(tool)
        if self.results[tool]:
            value = self.results[tool].popleft()
            if isinstance(value, BaseException):
                raise value
            return value
        if tool == "blkid":
            return _result(
                "blkid",
                0,
                "TYPE=ext4\nUUID=11111111-2222-3333-4444-555555555555\n",
            )
        return _result(tool, 0)


def _result(tool: str, exit_code: int, stdout: str = "") -> ProcessResult:
    return ProcessResult(
        tool=tool,
        exit_code=exit_code,
        stdout=stdout,
        stderr="",
        duration_ms=10.0,
    )


def _image(tmp_path: Path) -> Path:
    image = tmp_path / "filesystem.img"
    image.write_bytes(b"ARES filesystem image fixture" * 512)
    return image


def _suite(runner: FakeRunner, *, mount_checker: MountSafetyChecker | None = None) -> FilesystemToolSuite:
    return FilesystemToolSuite(
        runner=runner,
        mount_checker=mount_checker or MountSafetyChecker(scan_processes=False),
        allow_regular_file_targets=True,
    )


def _plan(identity: DeviceIdentity, mount: MountSafetyReport) -> FilesystemRepairPlan:
    return FilesystemRepairPlan(
        session_id="filesystem-test-session",
        target=identity,
        filesystem=FilesystemType.EXT4,
        mount=mount,
        detected_problems=("filesystem_errors_detected",),
        required_tools=(),
        required_permissions=("block-device.readwrite",),
        repair_actions=(
            RepairAction(
                id="filesystem.repair",
                description="Repair isolated test image",
                mutates_target=True,
            ),
        ),
        verification_steps=("Run read-only check",),
        rollback_strategy="Test fixture has no rollback.",
        executable=True,
        fingerprint_sha256="a" * 64,
    )


async def test_regular_image_identity_is_stable_and_detects_filesystem(tmp_path: Path) -> None:
    runner = FakeRunner()
    image = _image(tmp_path)
    suite = _suite(runner)

    inspection = await suite.inspect(str(image))

    assert inspection.identity.block_device is False
    assert inspection.identity.major_minor.startswith("file:")
    assert inspection.identity.filesystem_uuid == "11111111-2222-3333-4444-555555555555"
    assert inspection.filesystem is FilesystemType.EXT4
    assert inspection.health is FilesystemHealth.HEALTHY
    assert inspection.supported is True
    assert inspection.repair_supported is True


async def test_identity_change_is_detected_before_write(tmp_path: Path) -> None:
    runner = FakeRunner()
    image = _image(tmp_path)
    suite = _suite(runner)
    original = await suite.identify(str(image))

    replacement = tmp_path / "replacement.img"
    replacement.write_bytes(image.read_bytes() + b"changed")
    replacement.replace(image)

    with pytest.raises(FilesystemToolError, match="FILESYSTEM_DEVICE_IDENTITY_CHANGED"):
        await suite.revalidate(original)


async def test_missing_target_and_path_traversal_are_rejected(tmp_path: Path) -> None:
    runner = FakeRunner()
    test_suite = _suite(runner)

    with pytest.raises(FilesystemToolError, match="FILESYSTEM_TARGET_NOT_FOUND"):
        await test_suite.identify(str(tmp_path / "missing.img"))

    production = FilesystemToolSuite(runner=runner, allow_regular_file_targets=False)
    with pytest.raises(FilesystemToolError, match="FILESYSTEM_TARGET_INVALID"):
        await production.identify("/dev/../tmp/not-a-device")
    with pytest.raises(FilesystemToolError, match="FILESYSTEM_TARGET_INVALID"):
        await production.identify("relative-device")


def test_mount_safety_detects_busy_process_nested_mount_and_swap(tmp_path: Path) -> None:
    mount_point = tmp_path / "mounted"
    mount_point.mkdir()
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        f"36 25 8:1 / {mount_point} rw,relatime - ext4 /dev/sda1 rw\n"
        f"37 36 8:2 / {mount_point}/nested rw - ext4 /dev/sda2 rw\n",
        encoding="utf-8",
    )
    swaps = tmp_path / "swaps"
    swaps.write_text(
        "Filename Type Size Used Priority\n/dev/sda1 partition 1024 0 -2\n",
        encoding="utf-8",
    )
    proc = tmp_path / "proc"
    process = proc / "123"
    (process / "fd").mkdir(parents=True)
    os.symlink(mount_point, process / "cwd")
    identity = DeviceIdentity(
        requested_path="/dev/sda1",
        canonical_path="/dev/sda1",
        major_minor="8:1",
        size_bytes=1024,
        block_device=True,
        fingerprint_sha256="b" * 64,
    )
    checker = MountSafetyChecker(
        mountinfo_path=mountinfo,
        swaps_path=swaps,
        proc_root=proc,
    )

    report = checker.inspect(identity)

    assert report.mounted is True
    assert report.busy is True
    assert report.swap is True
    assert report.active_processes == (123,)
    assert str(mount_point / "nested") in report.nested_mounts
    assert report.safe_to_unmount is False
    assert {"nested_mounts", "active_swap", "active_process_handles"} <= set(report.reasons)


def test_mount_safety_detects_bind_and_unrestorable_options(tmp_path: Path) -> None:
    mount_point = tmp_path / "mounted"
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        f"36 25 8:1 /subdir {mount_point} rw,nodev - ext4 /dev/sda1 rw\n",
        encoding="utf-8",
    )
    swaps = tmp_path / "swaps"
    swaps.write_text("Filename Type Size Used Priority\n", encoding="utf-8")
    proc = tmp_path / "proc"
    proc.mkdir()
    identity = DeviceIdentity(
        requested_path="/dev/sda1",
        canonical_path="/dev/sda1",
        major_minor="8:1",
        size_bytes=1024,
        block_device=True,
        fingerprint_sha256="c" * 64,
    )

    report = MountSafetyChecker(
        mountinfo_path=mountinfo,
        swaps_path=swaps,
        proc_root=proc,
    ).inspect(identity)

    assert report.mounts[0].bind_mount is True
    assert "nodev" in report.unsupported_mount_options
    assert report.safe_to_unmount is False
    assert report.safe_to_remount is False


async def test_tool_unavailable_blocks_check_but_keeps_structured_inspection(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.available["e2fsck"] = False
    image = _image(tmp_path)

    inspection = await _suite(runner).inspect(str(image))

    assert inspection.health is FilesystemHealth.UNKNOWN
    assert inspection.check is None
    assert "required_tools_unavailable:e2fsck" in inspection.limitations
    assert any(item.tool == "e2fsck" and not item.available for item in inspection.required_tools)


async def test_repair_runs_check_mutation_and_verification_in_fixed_order(tmp_path: Path) -> None:
    runner = FakeRunner()
    image = _image(tmp_path)
    runner.results["e2fsck"].extend(
        (
            _result("e2fsck", 4),
            _result("e2fsck", 1),
            _result("e2fsck", 0),
        )
    )
    suite = _suite(runner)
    identity = await suite.identify(str(image))
    mount = MountSafetyReport(
        mounted=False,
        busy=False,
        swap=False,
        safe_to_unmount=False,
        safe_to_remount=False,
    )
    events: list[str] = []

    async def on_event(name: str, payload: dict[str, Any]) -> None:
        del payload
        events.append(name)

    outcome = await suite.execute_repair(_plan(identity, mount), on_event)

    e2fsck_calls = [call for call in runner.calls if call[0] == "e2fsck"]
    assert [call[1] for call in e2fsck_calls] == [
        ("-f", "-n", str(image)),
        ("-f", "-p", str(image)),
        ("-f", "-n", str(image)),
    ]
    assert outcome.before.health is FilesystemHealth.INCONSISTENT
    assert outcome.after.health is FilesystemHealth.HEALTHY
    assert outcome.repair_exit_code == 1
    assert events == [
        "filesystem.repair-command.started",
        "filesystem.repair-command.completed",
        "filesystem.verification.started",
        "filesystem.verification.completed",
    ]


async def test_failed_repair_and_failed_verification_remain_evident(tmp_path: Path) -> None:
    runner = FakeRunner()
    image = _image(tmp_path)
    runner.results["e2fsck"].extend(
        (
            _result("e2fsck", 4),
            _result("e2fsck", 4),
            _result("e2fsck", 4),
        )
    )
    suite = _suite(runner)
    identity = await suite.identify(str(image))
    mount = MountSafetyReport(
        mounted=False,
        busy=False,
        swap=False,
        safe_to_unmount=False,
        safe_to_remount=False,
    )

    async def ignore(name: str, payload: dict[str, Any]) -> None:
        del name, payload

    outcome = await suite.execute_repair(_plan(identity, mount), ignore)

    assert outcome.after.health is FilesystemHealth.INCONSISTENT
    assert "repair_tool_reported_unresolved_errors" in outcome.limitations


async def test_check_timeout_is_structured_failure(tmp_path: Path) -> None:
    runner = FakeRunner()
    image = _image(tmp_path)
    runner.results["e2fsck"].append(TimeoutError())
    suite = _suite(runner)
    identity = await suite.identify(str(image))

    with pytest.raises(FilesystemToolError, match="FILESYSTEM_CHECK_TIMEOUT"):
        await suite.check(identity, FilesystemType.EXT4)
