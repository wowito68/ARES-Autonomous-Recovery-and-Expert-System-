from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

import ares.tools.backup as backup_tools_module
from ares.backup.models import (
    Backup,
    BackupExecution,
    BackupPolicy,
    BackupProgress,
    BackupStatus,
    BackupVerificationStatus,
)
from ares.tools.backup import BackupFilesystemTools, BackupToolError


def _device_id(path: Path) -> str:
    device = os.stat(path).st_dev
    return f"{os.major(device)}:{os.minor(device)}"


def _tools(
    tmp_path: Path, *, same_filesystem: bool = False
) -> tuple[BackupFilesystemTools, Path, Path]:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    source_device = _device_id(source)
    destination_device = source_device if same_filesystem else "99:99"
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        f"36 25 {source_device} / {source} rw,relatime - ext4 /dev/sda1 rw\n"
        f"37 25 {destination_device} / {destination} rw,relatime - ext4 /dev/sdb1 rw\n",
        encoding="utf-8",
    )
    return BackupFilesystemTools(mountinfo), source, destination


def _backup(plan) -> Backup:
    progress = BackupProgress(
        files_completed=plan.included_file_count,
        files_total=plan.included_file_count,
        bytes_completed=plan.source.estimated_size_bytes,
        bytes_total=plan.source.estimated_size_bytes,
        percent=100,
        speed_bytes_per_second=None,
        eta_seconds=0,
    )
    return Backup(
        id=plan.backup_id,
        created_at=datetime.now(UTC),
        source=plan.source,
        destination=plan.destination,
        size=plan.source.estimated_size_bytes,
        file_count=plan.included_file_count,
        status=BackupStatus.VERIFYING,
        metadata={},
        created_by="test-user",
        session_id="session-test-123",
        plan_id=plan.id,
        execution=BackupExecution(
            backup_id=plan.backup_id,
            status=BackupStatus.VERIFYING,
            progress=progress,
        ),
    )


def test_plan_supports_hidden_unicode_empty_and_long_names(tmp_path: Path) -> None:
    tools, source, destination = _tools(tmp_path)
    (source / ".hidden").write_bytes(b"")
    (source / "documento-ñ.txt").write_text("contenido", encoding="utf-8")
    long_name = "a" * 180 + ".txt"
    (source / long_name).write_text("long", encoding="utf-8")
    folder = source / "folder"
    folder.mkdir()
    (folder / "nested.txt").write_text("nested", encoding="utf-8")

    plan = tools.build_plan(str(source), str(destination), BackupPolicy())

    assert plan.source.estimated_file_count == 4
    assert plan.source.estimated_directory_count == 1
    assert plan.included_file_count == 4
    assert plan.destination.device_id == "99:99"
    assert plan.destination.available_bytes >= plan.required_bytes
    assert plan.risk == "medium"
    assert plan.authorization_required is True
    assert plan.fingerprint_sha256
    tools.revalidate_plan(plan)


def test_plan_excludes_symlinks_and_policy_names(tmp_path: Path) -> None:
    tools, source, destination = _tools(tmp_path)
    (source / "keep.txt").write_text("keep", encoding="utf-8")
    ignored = source / "lost+found"
    ignored.mkdir()
    (ignored / "skip.txt").write_text("skip", encoding="utf-8")
    (source / "linked").symlink_to(source / "keep.txt")

    plan = tools.build_plan(str(source), str(destination), BackupPolicy())

    exclusions = {(item.relative_path, item.reason) for item in plan.exclusions}
    assert ("lost+found", "policy_excluded_name") in exclusions
    assert ("linked", "symlink_not_followed") in exclusions
    assert plan.included_file_count == 1


@pytest.mark.parametrize("missing", ["source", "destination"])
def test_plan_rejects_missing_paths(tmp_path: Path, missing: str) -> None:
    tools, source, destination = _tools(tmp_path)
    target = source if missing == "source" else destination
    target.rmdir()

    with pytest.raises(BackupToolError, match="BACKUP_(SOURCE|DESTINATION)_INVALID"):
        tools.build_plan(str(source), str(destination), BackupPolicy())


def test_plan_rejects_same_filesystem(tmp_path: Path) -> None:
    tools, source, destination = _tools(tmp_path, same_filesystem=True)
    (source / "file.txt").write_text("data", encoding="utf-8")

    with pytest.raises(BackupToolError, match="BACKUP_SAME_FILESYSTEM"):
        tools.build_plan(str(source), str(destination), BackupPolicy())


def test_plan_rejects_same_physical_device(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, source, destination = _tools(tmp_path)
    (source / "file.txt").write_text("data", encoding="utf-8")
    monkeypatch.setattr(backup_tools_module, "_physical_device_id", lambda _: "disk:fixture")

    with pytest.raises(BackupToolError, match="BACKUP_SAME_DEVICE"):
        tools.build_plan(str(source), str(destination), BackupPolicy())


def test_plan_rejects_destination_inside_source(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = source / "backup"
    source.mkdir()
    destination.mkdir()
    device = _device_id(source)
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        f"36 25 {device} / {source} rw - ext4 /dev/sda1 rw\n"
        f"37 25 99:99 / {destination} rw - ext4 /dev/sdb1 rw\n",
        encoding="utf-8",
    )
    tools = BackupFilesystemTools(mountinfo)

    with pytest.raises(BackupToolError, match="BACKUP_DESTINATION_INSIDE_SOURCE"):
        tools.build_plan(str(source), str(destination), BackupPolicy())


def test_plan_rejects_insufficient_space(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, source, destination = _tools(tmp_path)
    (source / "file.bin").write_bytes(b"x" * 1024)
    monkeypatch.setattr(backup_tools_module, "_available_bytes", lambda _: 1)

    with pytest.raises(BackupToolError, match="BACKUP_INSUFFICIENT_SPACE"):
        tools.build_plan(str(source), str(destination), BackupPolicy())


def test_plan_rejects_unwritable_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools, source, destination = _tools(tmp_path)
    (source / "file.txt").write_text("data", encoding="utf-8")
    original = backup_tools_module.os.access

    def access(path: os.PathLike[str] | str, mode: int) -> bool:
        return False if Path(path) == destination else original(path, mode)

    monkeypatch.setattr(backup_tools_module.os, "access", access)
    with pytest.raises(BackupToolError, match="BACKUP_DESTINATION_NOT_WRITABLE"):
        tools.build_plan(str(source), str(destination), BackupPolicy())


def test_plan_rejects_symlink_component(tmp_path: Path) -> None:
    tools, source, destination = _tools(tmp_path)
    real = source / "real"
    real.mkdir()
    (real / "file.txt").write_text("data", encoding="utf-8")
    link = source / "alias"
    link.symlink_to(real, target_is_directory=True)

    with pytest.raises(BackupToolError, match="BACKUP_SOURCE_INVALID"):
        tools.build_plan(str(link), str(destination), BackupPolicy())


async def test_create_manifest_progress_and_verify_large_file(tmp_path: Path) -> None:
    tools, source, destination = _tools(tmp_path)
    payload = b"x" * (2 * 1024 * 1024 + 17)
    (source / "large.bin").write_bytes(payload)
    (source / "empty.txt").write_bytes(b"")
    progress: list[BackupProgress] = []
    entries = []
    plan = tools.build_plan(str(source), str(destination), BackupPolicy())

    async def on_progress(value: BackupProgress) -> None:
        progress.append(value)

    async def on_entry(value) -> None:
        entries.append(value)

    manifest = await tools.create_backup(plan, on_progress, on_entry)
    verification = await tools.verify_backup(_backup(plan), manifest)

    assert manifest.file_count == 2
    assert manifest.total_size_bytes == len(payload)
    assert len(entries) == 2
    assert progress[-1].percent == 100
    assert any(0 < item.bytes_completed < len(payload) for item in progress)
    assert verification.status is BackupVerificationStatus.VERIFIED
    assert verification.verified_file_count == 2
    assert (Path(plan.destination.backup_path) / ".ares-manifest.json").is_file()


async def test_verify_detects_data_corruption_and_unexpected_entries(tmp_path: Path) -> None:
    tools, source, destination = _tools(tmp_path)
    (source / "file.txt").write_text("original", encoding="utf-8")
    plan = tools.build_plan(str(source), str(destination), BackupPolicy())

    async def noop(_) -> None:
        return None

    manifest = await tools.create_backup(plan, noop, noop)
    backup = _backup(plan)
    target = Path(plan.destination.backup_path)
    (target / "file.txt").write_text("corrupt!", encoding="utf-8")
    (target / "unexpected.txt").write_text("unexpected", encoding="utf-8")

    verification = await tools.verify_backup(backup, manifest)

    assert verification.status is BackupVerificationStatus.CORRUPTED
    assert verification.checksum_mismatches
    assert verification.unexpected_entries


async def test_verify_detects_destination_manifest_corruption(tmp_path: Path) -> None:
    tools, source, destination = _tools(tmp_path)
    (source / "file.txt").write_text("data", encoding="utf-8")
    plan = tools.build_plan(str(source), str(destination), BackupPolicy())

    async def noop(_) -> None:
        return None

    manifest = await tools.create_backup(plan, noop, noop)
    (Path(plan.destination.backup_path) / ".ares-manifest.json").write_text("{}", encoding="utf-8")

    verification = await tools.verify_backup(_backup(plan), manifest)

    assert verification.status is BackupVerificationStatus.CORRUPTED
    assert verification.manifest_valid is False


async def test_verify_reports_source_changed_after_copy(tmp_path: Path) -> None:
    tools, source, destination = _tools(tmp_path)
    file_path = source / "file.txt"
    file_path.write_text("before", encoding="utf-8")
    plan = tools.build_plan(str(source), str(destination), BackupPolicy())

    async def noop(_) -> None:
        return None

    manifest = await tools.create_backup(plan, noop, noop)
    file_path.write_text("after", encoding="utf-8")

    verification = await tools.verify_backup(_backup(plan), manifest)

    assert verification.status is BackupVerificationStatus.FAILED
    assert verification.source_changed_entries


async def test_cancellation_removes_partial_backup(tmp_path: Path) -> None:
    tools, source, destination = _tools(tmp_path)
    (source / "large.bin").write_bytes(b"x" * (3 * 1024 * 1024))
    plan = tools.build_plan(str(source), str(destination), BackupPolicy())
    reached_chunk = asyncio.Event()
    release = asyncio.Event()

    async def on_progress(value: BackupProgress) -> None:
        if 0 < value.bytes_completed < value.bytes_total:
            reached_chunk.set()
            await release.wait()

    async def noop(_) -> None:
        return None

    task = asyncio.create_task(tools.create_backup(plan, on_progress, noop))
    await asyncio.wait_for(reached_chunk.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    ares_root = destination / "ARES"
    assert not Path(plan.destination.backup_path).exists()
    assert not (ares_root / f".partial-{plan.backup_id}").exists()


def test_revalidate_detects_source_change_and_existing_destination(tmp_path: Path) -> None:
    tools, source, destination = _tools(tmp_path)
    file_path = source / "file.txt"
    file_path.write_text("before", encoding="utf-8")
    plan = tools.build_plan(str(source), str(destination), BackupPolicy())
    file_path.write_text("after-change", encoding="utf-8")

    with pytest.raises(BackupToolError, match="BACKUP_SOURCE_CHANGED"):
        tools.revalidate_plan(plan)

    file_path.write_text("before", encoding="utf-8")
    plan = tools.build_plan(str(source), str(destination), BackupPolicy())
    Path(plan.destination.backup_path).mkdir(parents=True)
    with pytest.raises(BackupToolError, match="BACKUP_DESTINATION_EXISTS"):
        tools.revalidate_plan(plan)
