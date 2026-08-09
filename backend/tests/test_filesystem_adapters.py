from __future__ import annotations

import pytest

from ares.filesystems.adapters import (
    BtrfsFilesystemAdapter,
    ExtFilesystemAdapter,
    NtfsFilesystemAdapter,
    XfsFilesystemAdapter,
    adapter_for,
)
from ares.filesystems.models import FilesystemHealth, FilesystemType
from ares.tools.storage import ProcessResult


def _result(tool: str, exit_code: int) -> ProcessResult:
    return ProcessResult(
        tool=tool,
        exit_code=exit_code,
        stdout="structured test output",
        stderr="",
        duration_ms=12.5,
    )


def test_ext_adapter_uses_read_only_check_and_noninteractive_safe_repair() -> None:
    adapter = ExtFilesystemAdapter()

    assert adapter.check_invocation("/dev/test") == (
        "e2fsck",
        ("-f", "-n", "/dev/test"),
    )
    assert adapter.repair_invocation("/dev/test") == (
        "e2fsck",
        ("-f", "-p", "/dev/test"),
    )
    assert "-y" not in adapter.repair_invocation("/dev/test")[1]
    assert adapter.capabilities.requires_unmounted is True
    assert adapter.capabilities.supports_rollback is False


def test_ext_adapter_maps_fsck_exit_semantics() -> None:
    adapter = ExtFilesystemAdapter()

    healthy = adapter.parse_check(_result("e2fsck", 0), FilesystemType.EXT4)
    inconsistent = adapter.parse_check(_result("e2fsck", 4), FilesystemType.EXT4)
    operational = adapter.parse_check(_result("e2fsck", 8), FilesystemType.EXT4)

    assert healthy.health is FilesystemHealth.HEALTHY
    assert inconsistent.health is FilesystemHealth.INCONSISTENT
    assert inconsistent.problems == ("filesystem_errors_detected",)
    assert operational.health is FilesystemHealth.UNKNOWN
    assert adapter.repair_succeeded(_result("e2fsck", 1)) is True
    assert adapter.repair_partial(_result("e2fsck", 4)) is True


def test_xfs_adapter_never_uses_log_zeroing() -> None:
    adapter = XfsFilesystemAdapter()

    check_tool, check_args = adapter.check_invocation("/dev/test")
    repair_tool, repair_args = adapter.repair_invocation("/dev/test")
    image_tool, image_args = adapter.repair_invocation("/tmp/xfs.img", image=True)

    assert check_tool == repair_tool == image_tool == "xfs_repair"
    assert check_args == ("-n", "/dev/test")
    assert repair_args == ("/dev/test",)
    assert image_args == ("-f", "/tmp/xfs.img")
    assert "-L" not in check_args + repair_args + image_args
    assert (
        adapter.parse_check(_result("xfs_repair", 1), FilesystemType.XFS).health
        is FilesystemHealth.INCONSISTENT
    )
    assert (
        "xfs_dirty_log_requires_safe_replay"
        in adapter.parse_check(_result("xfs_repair", 2), FilesystemType.XFS).problems
    )


def test_btrfs_adapter_is_inspection_only() -> None:
    adapter = BtrfsFilesystemAdapter()

    assert adapter.check_invocation("/dev/test") == (
        "btrfs",
        ("check", "--readonly", "/dev/test"),
    )
    assert adapter.capabilities.repair_supported is False
    with pytest.raises(PermissionError, match="FILESYSTEM_REPAIR_UNSUPPORTED_BTRFS"):
        adapter.repair_invocation("/dev/test")


def test_ntfs_adapter_is_explicitly_limited() -> None:
    adapter = NtfsFilesystemAdapter()

    assert adapter.check_invocation("/dev/test") == ("ntfsfix", ("-n", "/dev/test"))
    assert adapter.repair_invocation("/dev/test") == ("ntfsfix", ("/dev/test",))
    assert any("CHKDSK" in limitation for limitation in adapter.capabilities.limitations)


def test_adapter_lookup_supports_only_declared_initial_filesystems() -> None:
    assert isinstance(adapter_for(FilesystemType.EXT2), ExtFilesystemAdapter)
    assert isinstance(adapter_for(FilesystemType.EXT3), ExtFilesystemAdapter)
    assert isinstance(adapter_for(FilesystemType.EXT4), ExtFilesystemAdapter)
    assert isinstance(adapter_for(FilesystemType.XFS), XfsFilesystemAdapter)
    assert isinstance(adapter_for(FilesystemType.BTRFS), BtrfsFilesystemAdapter)
    assert isinstance(adapter_for(FilesystemType.NTFS), NtfsFilesystemAdapter)
