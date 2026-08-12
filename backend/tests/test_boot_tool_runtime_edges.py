from __future__ import annotations

import shutil
from pathlib import Path
from typing import ClassVar

import pytest

from ares.boot.models import (
    BootConfiguration,
    BootloaderKind,
    BootVerificationStatus,
    DistributionFamily,
    RepairEnvironment,
)
from ares.tools import boot as boot_tools
from ares.tools.boot import (
    BootEntryTool,
    BootRepairToolSuite,
    BootToolError,
    EFIVariablesTool,
    FstabAnalysisTool,
    GrubAdapter,
    RepairEnvironmentTool,
    SafeBootProcessRunner,
    hash_file,
    parse_efibootmgr,
)
from tests.test_boot_tools import _esp, _plan, _Result, _root


class _ConfigurableRunner:
    available_tools: ClassVar[set[str]] = {
        "efibootmgr",
        "mount",
        "umount",
        "grub-install",
        "chroot",
    }

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.fail_tool: str | None = None
        self.efi_stdout = ""

    def available(self, tool: str) -> bool:
        return tool in self.available_tools

    async def run(
        self,
        tool: str,
        args: tuple[str, ...],
        *,
        timeout_seconds: float,
        stdin_text: str | None = None,
    ) -> _Result:
        del timeout_seconds, stdin_text
        self.calls.append((tool, args))
        if self.fail_tool == tool:
            return _Result(exit_code=1, stderr="fixture failure")
        if tool == "efibootmgr" and args == ("-v",):
            return _Result(stdout=self.efi_stdout)
        return _Result()


async def test_safe_boot_runner_rejects_commands_missing_tools_and_large_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = SafeBootProcessRunner()
    monkeypatch.setattr(shutil, "which", lambda tool: None)
    assert runner.available("grub-install") is False
    assert runner.available("python") is False

    with pytest.raises(BootToolError, match="BOOT_TOOL_NOT_ALLOWED"):
        await runner.run("python", (), timeout_seconds=1)
    with pytest.raises(BootToolError, match="BOOT_TOOL_UNAVAILABLE_GRUB_INSTALL"):
        await runner.run("grub-install", (), timeout_seconds=1)

    monkeypatch.setattr(shutil, "which", lambda tool: f"/usr/bin/{tool}")
    with pytest.raises(BootToolError, match="BOOT_TOOL_INPUT_TOO_LARGE"):
        await runner.run(
            "chroot",
            ("/fixture",),
            timeout_seconds=1,
            stdin_text="x" * (boot_tools._MAX_INPUT + 1),
        )


async def test_efi_variables_and_entry_tools_fail_closed() -> None:
    runner = _ConfigurableRunner()
    runner.available_tools.remove("efibootmgr")
    entries, evidence = await EFIVariablesTool(runner).inspect()
    assert entries == ()
    assert "unavailable" in evidence[0].observation
    with pytest.raises(BootToolError, match="EFIBOOTMGR_UNAVAILABLE"):
        await BootEntryTool(runner).create_grub_entry(
            disk_path="/dev/fixture",
            partition_number=1,
            loader_path="\\EFI\\debian\\grubx64.efi",
        )

    runner.available_tools.add("efibootmgr")
    runner.fail_tool = "efibootmgr"
    entries, evidence = await EFIVariablesTool(runner).inspect()
    assert entries == ()
    assert "could not be read" in evidence[0].observation
    with pytest.raises(BootToolError, match="EFI_BOOT_ENTRY_UPDATE_FAILED"):
        await BootEntryTool(runner).create_grub_entry(
            disk_path="/dev/fixture",
            partition_number=1,
            loader_path="\\EFI\\debian\\grubx64.efi",
        )


def test_bootloader_and_fstab_edge_evidence(tmp_path: Path) -> None:
    runner = _ConfigurableRunner()
    tools = BootRepairToolSuite(runner=runner, test_mode=True, runtime_root=tmp_path / "run")

    root = tmp_path / "unknown-root"
    (root / "etc").mkdir(parents=True)
    (root / "boot").mkdir()
    loader, config, target_os, evidence = tools.bootloader.inspect(root, None)
    assert loader.kind is BootloaderKind.UNKNOWN
    assert target_os.family is DistributionFamily.UNKNOWN
    assert evidence[1].confidence == 0.5
    updated, fstab_evidence = FstabAnalysisTool().inspect(config, None)
    assert updated == config
    assert "not found" in fstab_evidence[0].observation

    unreadable = BootConfiguration(root_path=str(root), fstab_path=str(root / "etc"))
    _, read_evidence = FstabAnalysisTool().inspect(unreadable, None)
    assert "could not be read" in read_evidence[0].observation

    fedora = tmp_path / "fedora-root"
    (fedora / "etc").mkdir(parents=True)
    (fedora / "boot").mkdir()
    (fedora / "etc/os-release").write_text(
        "ID=fedora\nNAME=Fedora\nVERSION_ID=43\n",
        encoding="utf-8",
    )
    _, _, fedora_os, _ = tools.bootloader.inspect(fedora, None)
    assert fedora_os.family is DistributionFamily.FEDORA

    other = tmp_path / "other-root"
    (other / "etc").mkdir(parents=True)
    (other / "boot").mkdir()
    (other / "etc/os-release").write_text("ID=arch\nNAME=Arch\n", encoding="utf-8")
    _, _, other_os, _ = tools.bootloader.inspect(other, None)
    assert other_os.family is DistributionFamily.OTHER


def test_parse_efi_and_file_safety_helpers(tmp_path: Path) -> None:
    parsed = parse_efibootmgr(
        "Boot0002 debian HD(1,GPT,x,0x1,0x2)\n"
        "Boot0003* rescue File(\\EFI\\rescue\\loader.efi)\n"
        "BootX003* invalid\n"
        "short\n"
    )
    assert len(parsed) == 2
    assert parsed[0].active is False
    assert parsed[0].loader_path is None
    assert parsed[1].active is True

    data = tmp_path / "data"
    data.write_bytes(b"abc")
    assert len(hash_file(data)) == 64
    with pytest.raises(BootToolError, match="BOOT_CHECKPOINT_FILE_TOO_LARGE"):
        hash_file(data, limit=2)
    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(BootToolError, match="BOOT_CHECKPOINT_FILE_INVALID"):
        hash_file(directory)
    symlink = tmp_path / "link"
    symlink.symlink_to(data)
    with pytest.raises(BootToolError, match="BOOT_CHECKPOINT_FILE_INVALID"):
        hash_file(symlink)
    with pytest.raises(BootToolError, match="BOOT_ROOT_PATH_INVALID"):
        boot_tools._safe_root(Path("relative"))
    with pytest.raises(BootToolError, match="BOOT_ROOT_PATH_UNAVAILABLE"):
        boot_tools._safe_root(tmp_path / "missing")


async def test_repair_environment_real_mode_mount_and_cleanup_paths(tmp_path: Path) -> None:
    root = _root(tmp_path)
    plan = _plan(root)
    runner = _ConfigurableRunner()
    environment_tool = RepairEnvironmentTool(
        runner,
        test_mode=False,
        runtime_root=tmp_path / "runtime",
    )
    environment = await environment_tool.prepare(plan)
    assert len(environment.bind_mounts) == 4
    assert [tool for tool, _ in runner.calls].count("mount") == 4
    cleaned = await environment_tool.cleanup(environment)
    assert cleaned.cleaned is True
    assert [tool for tool, _ in runner.calls].count("umount") == 4

    runner = _ConfigurableRunner()
    runner.fail_tool = "mount"
    failing_tool = RepairEnvironmentTool(
        runner,
        test_mode=False,
        runtime_root=tmp_path / "runtime-fail",
    )
    with pytest.raises(BootToolError, match="BOOT_REPAIR_ENVIRONMENT_MOUNT_FAILED"):
        await failing_tool.prepare(plan)

    runner = _ConfigurableRunner()
    runner.available_tools.remove("umount")
    with pytest.raises(BootToolError, match="UMOUNT_UNAVAILABLE"):
        await RepairEnvironmentTool(runner).cleanup_paths(("/fixture",))
    runner.available_tools.add("umount")

    runner = _ConfigurableRunner()
    runner.fail_tool = "umount"
    with pytest.raises(BootToolError, match="BOOT_REPAIR_ENVIRONMENT_CLEANUP_FAILED"):
        await RepairEnvironmentTool(runner).cleanup_paths(("/fixture",))


async def test_grub_production_adapter_success_and_failure_contracts(tmp_path: Path) -> None:
    root = _root(tmp_path)
    esp = _esp(tmp_path, "EFI/debian/old.efi")
    plan = _plan(root, esp)
    environment = RepairEnvironment(
        repair_id=plan.repair_id,
        root_path=str(root),
        boot_path=str(root / "boot"),
        esp_path=str(esp),
        work_root=str(tmp_path / "work"),
    )
    runner = _ConfigurableRunner()
    adapter = GrubAdapter(runner, test_mode=False)
    await adapter.install(plan, environment)
    await adapter.regenerate_config(plan, environment)
    await adapter.regenerate_initramfs(plan, environment)
    tools = [tool for tool, _ in runner.calls]
    assert "grub-install" in tools
    assert tools.count("chroot") == 2

    bios_plan = _plan(root)
    bios_environment = environment.model_copy(update={"esp_path": None})
    await adapter.install(bios_plan, bios_environment)
    grub_args = [args for tool, args in runner.calls if tool == "grub-install"][-1]
    assert "--target=i386-pc" in grub_args
    assert bios_plan.target_disk.canonical_path in grub_args

    runner.available_tools.remove("grub-install")
    with pytest.raises(BootToolError, match="GRUB_INSTALL_UNAVAILABLE"):
        await adapter.install(plan, environment)
    runner.available_tools.add("grub-install")
    runner.fail_tool = "grub-install"
    with pytest.raises(BootToolError, match="GRUB_INSTALL_FAILED"):
        await adapter.install(plan, environment)

    no_esp_environment = environment.model_copy(update={"esp_path": None})
    runner.fail_tool = None
    with pytest.raises(BootToolError, match="BOOT_ESP_PATH_UNAVAILABLE"):
        await adapter.install(plan, no_esp_environment)

    runner.available_tools.remove("chroot")
    with pytest.raises(BootToolError, match="CHROOT_UNAVAILABLE"):
        await adapter.regenerate_config(plan, environment)
    with pytest.raises(BootToolError, match="CHROOT_UNAVAILABLE"):
        await adapter.regenerate_initramfs(plan, environment)
    runner.available_tools.add("chroot")
    runner.fail_tool = "chroot"
    with pytest.raises(BootToolError, match="GRUB_CONFIGURATION_REGENERATION_FAILED"):
        await adapter.regenerate_config(plan, environment)
    with pytest.raises(BootToolError, match="INITRAMFS_REGENERATION_FAILED"):
        await adapter.regenerate_initramfs(plan, environment)


async def test_boot_verification_failed_static_chain_and_efi_entry(tmp_path: Path) -> None:
    root = _root(tmp_path, grub=False, initramfs=False)
    esp = _esp(tmp_path, "EFI/debian/grubx64.efi")
    runner = _ConfigurableRunner()
    runner.efi_stdout = "Boot0001* debian HD(1,GPT,x,0x1,0x2)File(\\EFI\\debian\\grubx64.efi)\n"
    tools = BootRepairToolSuite(runner=runner, test_mode=True, runtime_root=tmp_path / "run")
    verification = await tools.verification.verify(_plan(root, esp))
    assert verification.status is BootVerificationStatus.FAILED
    assert verification.efi_entry_verified is True
    assert verification.esp_verified is True
    assert verification.bootloader_verified is False
    assert verification.initramfs_verified is False
