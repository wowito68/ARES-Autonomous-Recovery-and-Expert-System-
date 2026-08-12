from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

from ares.boot.integrity import boot_plan_fingerprint
from ares.boot.models import (
    BootConfiguration,
    Bootloader,
    BootloaderKind,
    BootOperationKind,
    BootPartition,
    BootRepairOperation,
    BootRepairPlan,
    BootTargetOS,
    BootVerificationConfidence,
    BootVerificationStatus,
    DistributionFamily,
    FirmwareMode,
)
from ares.storage_operations.models import BootImpactAssessment, BootImpactLevel
from ares.tools.boot import (
    BootRepairToolSuite,
    BootToolError,
    FirmwareDetectionTool,
    GrubAdapter,
    parse_efibootmgr,
)
from tests.test_storage_partition_edges import _identity


class _Result:
    def __init__(self, exit_code: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


class _Runner:
    available_tools: ClassVar[set[str]] = {
        "efibootmgr",
        "mount",
        "umount",
        "grub-install",
        "chroot",
    }

    def __init__(self) -> None:
        self.entries = "BootCurrent: 0001\n"
        self.calls: list[tuple[str, tuple[str, ...]]] = []

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
        if tool == "efibootmgr" and args == ("-v",):
            return _Result(stdout=self.entries)
        if tool == "efibootmgr" and "--create" in args:
            self.entries += (
                "Boot0007* debian HD(1,GPT,fixture,0x800,0x1000)"
                "File(\\\\EFI\\\\debian\\\\grubx64.efi)\n"
            )
        return _Result()


def _root(tmp_path: Path, *, grub: bool = True, initramfs: bool = True) -> Path:
    root = tmp_path / "root"
    (root / "etc/default").mkdir(parents=True)
    (root / "boot/grub").mkdir(parents=True)
    (root / "etc/os-release").write_text(
        'ID=debian\nPRETTY_NAME="Debian GNU/Linux 13"\nVERSION_ID="13"\n',
        encoding="utf-8",
    )
    (root / "etc/fstab").write_text("UUID=missing / ext4 defaults 0 1\n", encoding="utf-8")
    (root / "etc/default/grub").write_text("GRUB_TIMEOUT=1\n", encoding="utf-8")
    (root / "boot/vmlinuz-6.12-test").write_bytes(b"kernel")
    if initramfs:
        (root / "boot/initrd.img-6.12-test").write_bytes(b"initramfs")
    if grub:
        (root / "boot/grub/grub.cfg").write_text("menuentry Linux {}\n", encoding="utf-8")
    return root


def _esp(tmp_path: Path, loader: str) -> Path:
    esp = tmp_path / "esp"
    target = esp / loader
    target.parent.mkdir(parents=True)
    target.write_bytes(b"efi")
    return esp


def _plan(root: Path, esp: Path | None = None) -> BootRepairPlan:
    identity = _identity()
    target_os = BootTargetOS(
        id="os:fixture",
        name="Debian",
        version="13",
        family=DistributionFamily.DEBIAN,
        root_path=str(root),
        boot_path=str(root / "boot"),
        esp_path=str(esp) if esp else None,
    )
    config = BootConfiguration(
        root_path=str(root),
        boot_path=str(root / "boot"),
        esp_path=str(esp) if esp else None,
        grub_config_path=str(root / "boot/grub/grub.cfg"),
        kernels=("boot/vmlinuz-6.12-test",),
        initramfs=("boot/initrd.img-6.12-test",),
    )
    draft = BootRepairPlan(
        session_id="boot-test-session",
        diagnostic_id="diagnostic-fixture",
        target_os=target_os,
        target_disk=identity,
        target_esp=(
            BootPartition(
                resource_id="partition:esp",
                device_path="image:esp",
                partition_number=1,
                role="esp",
                filesystem_type="vfat",
                mount_point=str(esp),
            )
            if esp
            else None
        ),
        bootloader=Bootloader(
            kind=BootloaderKind.GRUB,
            distribution_family=DistributionFamily.DEBIAN,
            repair_supported=True,
        ),
        current_configuration=config,
        expected_configuration=config,
        issues=(),
        operations=(
            BootRepairOperation(
                id="install",
                kind=BootOperationKind.INSTALL_GRUB,
                description="Install GRUB fixture",
                mutates_system=True,
            ),
        ),
        boot_impact=BootImpactAssessment(
            level=BootImpactLevel.LIKELY,
            affected_dependencies=(),
            reasons=("fixture",),
            recovery_strategy_available=True,
            executable=True,
        ),
        executable=True,
        fingerprint_sha256="0" * 64,
    )
    return draft.model_copy(update={"fingerprint_sha256": boot_plan_fingerprint(draft)})


def test_firmware_detection_uefi_bios_unknown(tmp_path: Path) -> None:
    uefi = tmp_path / "uefi"
    (uefi / "firmware/efi/efivars").mkdir(parents=True)
    environment, evidence = FirmwareDetectionTool(uefi).inspect()
    assert environment.mode is FirmwareMode.UEFI
    assert environment.efivars_available is True
    assert evidence[0].confidence > 0.9

    bios = tmp_path / "bios"
    (bios / "firmware").mkdir(parents=True)
    environment, _ = FirmwareDetectionTool(bios).inspect()
    assert environment.mode is FirmwareMode.BIOS

    environment, _ = FirmwareDetectionTool(tmp_path / "missing").inspect()
    assert environment.mode is FirmwareMode.UNKNOWN


def test_parse_efibootmgr_and_bootloader_detection(tmp_path: Path) -> None:
    entries = parse_efibootmgr(
        "Boot0001* debian HD(1,GPT,x,0x800,0x1000)File(\\\\EFI\\\\debian\\\\grubx64.efi)\n"
        "Boot00ZZ* invalid\n"
    )
    assert len(entries) == 1
    assert entries[0].number == "0001"
    assert entries[0].active is True
    assert "grubx64.efi" in (entries[0].loader_path or "")

    root = _root(tmp_path)
    runner = _Runner()
    tools = BootRepairToolSuite(runner=runner, test_mode=True, runtime_root=tmp_path / "run")
    grub_esp = _esp(tmp_path, "EFI/debian/grubx64.efi")
    loader, config, target_os, evidence = tools.bootloader.inspect(root, grub_esp)
    assert loader.kind is BootloaderKind.GRUB
    assert target_os.family is DistributionFamily.DEBIAN
    assert config.kernels and config.initramfs
    assert len(evidence) == 2

    windows = _esp(tmp_path / "windows", "EFI/Microsoft/Boot/bootmgfw.efi")
    root_without_grub = _root(tmp_path / "windows-root", grub=False)
    loader, _, _, _ = tools.bootloader.inspect(root_without_grub, windows)
    assert loader.kind is BootloaderKind.WINDOWS_BOOT_MANAGER

    systemd = _esp(tmp_path / "systemd", "EFI/systemd/systemd-bootx64.efi")
    root_without_grub = _root(tmp_path / "systemd-root", grub=False)
    loader, _, _, _ = tools.bootloader.inspect(root_without_grub, systemd)
    assert loader.kind is BootloaderKind.SYSTEMD_BOOT


def test_fstab_is_read_only_and_detects_invalid_reference(tmp_path: Path) -> None:
    root = _root(tmp_path)
    tools = BootRepairToolSuite(runner=_Runner(), test_mode=True, runtime_root=tmp_path / "run")
    _, config, _, _ = tools.bootloader.inspect(root, None)
    updated, evidence = tools.fstab.inspect(config, None)
    assert updated.fstab_references == ("UUID=missing",)
    assert updated.invalid_fstab_references == ()
    assert evidence[0].source == "fstab"
    assert (root / "etc/fstab").read_text(encoding="utf-8").startswith("UUID=missing")


async def test_grub_adapter_fixture_and_offline_verification(tmp_path: Path) -> None:
    root = _root(tmp_path, grub=False, initramfs=False)
    esp = _esp(tmp_path, "EFI/debian/placeholder.efi")
    runner = _Runner()
    tools = BootRepairToolSuite(runner=runner, test_mode=True, runtime_root=tmp_path / "run")
    plan = _plan(root, esp)
    environment = await tools.environment.prepare(plan)
    adapter = GrubAdapter(runner, test_mode=True)
    await adapter.install(plan, environment)
    await adapter.regenerate_config(plan, environment)
    await adapter.regenerate_initramfs(plan, environment)
    await tools.entries.create_grub_entry(
        disk_path=plan.target_disk.canonical_path,
        partition_number=1,
        loader_path="\\EFI\\debian\\grubx64.efi",
    )
    verification = await tools.verification.verify(plan)
    assert verification.status is BootVerificationStatus.PARTIAL
    assert verification.confidence is BootVerificationConfidence.LIMITED
    assert verification.bootloader_verified is True
    assert verification.kernel_verified is True
    assert verification.initramfs_verified is True
    assert verification.esp_verified is True
    cleaned = await tools.environment.cleanup(environment)
    assert cleaned.cleaned is True


async def test_grub_adapter_rejects_non_debian_and_missing_kernel(tmp_path: Path) -> None:
    root = _root(tmp_path, initramfs=False)
    plan = _plan(root).model_copy(
        update={
            "target_os": BootTargetOS(
                id="os:fedora",
                name="Fedora",
                family=DistributionFamily.FEDORA,
                root_path=str(root),
            )
        }
    )
    runner = _Runner()
    environment = await BootRepairToolSuite(
        runner=runner, test_mode=True, runtime_root=tmp_path / "run"
    ).environment.prepare(plan)
    with pytest.raises(BootToolError, match="GRUB_REPAIR_UNSUPPORTED_DISTRIBUTION"):
        await GrubAdapter(runner, test_mode=True).install(plan, environment)

    (root / "boot/vmlinuz-6.12-test").unlink()
    with pytest.raises(BootToolError, match="KERNEL_REQUIRED_FOR_INITRAMFS"):
        await GrubAdapter(runner, test_mode=True).regenerate_initramfs(plan, environment)
