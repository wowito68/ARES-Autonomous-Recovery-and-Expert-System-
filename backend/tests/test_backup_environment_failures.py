from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from ares.backup.models import BackupPolicy, BackupVerificationStatus
from ares.tools.backup import BackupFilesystemTools, BackupToolError


def _fixture(
    tmp_path: Path, *, destination_fs: str = "ext4"
) -> tuple[BackupFilesystemTools, Path, Path]:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    (source / "file.txt").write_text("data", encoding="utf-8")
    device = os.stat(source).st_dev
    source_device = f"{os.major(device)}:{os.minor(device)}"
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        f"36 25 {source_device} / {source} rw - ext4 /dev/sda1 rw\n"
        f"37 25 99:99 / {destination} rw - {destination_fs} /dev/sdb1 rw\n",
        encoding="utf-8",
    )
    return BackupFilesystemTools(mountinfo), source, destination


def test_plan_rejects_incompatible_destination_filesystem(tmp_path: Path) -> None:
    tools, source, destination = _fixture(tmp_path, destination_fs="tmpfs")

    with pytest.raises(BackupToolError, match="BACKUP_DESTINATION_FILESYSTEM_INCOMPATIBLE"):
        tools.build_plan(str(source), str(destination), BackupPolicy())


def test_revalidation_detects_disconnected_destination(tmp_path: Path) -> None:
    tools, source, destination = _fixture(tmp_path)
    plan = tools.build_plan(str(source), str(destination), BackupPolicy())
    destination.rmdir()

    with pytest.raises(BackupToolError, match="BACKUP_DESTINATION_INVALID"):
        tools.revalidate_plan(plan)


async def test_backup_does_not_depend_on_external_copy_or_hash_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools, source, destination = _fixture(tmp_path)
    monkeypatch.setattr(shutil, "which", lambda _: None)
    plan = tools.build_plan(str(source), str(destination), BackupPolicy())

    async def noop(_: object) -> None:
        return None

    manifest = await tools.create_backup(plan, noop, noop)
    from tests.test_backup_tools import _backup

    verification = await tools.verify_backup(_backup(plan), manifest)
    assert manifest.file_count == 1
    assert verification.status is BackupVerificationStatus.VERIFIED
