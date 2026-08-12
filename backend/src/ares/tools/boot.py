"""Boot-specific Tool Layer. No shell or caller-provided command execution."""

from __future__ import annotations

import asyncio
import hashlib
import shutil
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Protocol

from ares.boot.integrity import canonical_sha256
from ares.boot.models import (
    BootConfiguration,
    BootEntry,
    BootEvidence,
    Bootloader,
    BootloaderKind,
    BootRepairPlan,
    BootTargetOS,
    BootVerification,
    BootVerificationConfidence,
    BootVerificationStatus,
    DistributionFamily,
    FirmwareEnvironment,
    FirmwareMode,
    RepairEnvironment,
)
from ares.storage_operations.models import StorageLayout

_MAX_OUTPUT = 2 * 1024 * 1024
_MAX_INPUT = 256 * 1024


class BootToolError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class BootProcessResult(Protocol):
    exit_code: int
    stdout: str
    stderr: str


class BootProcessRunner(Protocol):
    def available(self, tool: str) -> bool: ...

    async def run(
        self,
        tool: str,
        args: tuple[str, ...],
        *,
        timeout_seconds: float,
        stdin_text: str | None = None,
    ) -> BootProcessResult: ...


class _Result:
    def __init__(self, exit_code: int, stdout: str, stderr: str) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


class SafeBootProcessRunner:
    """Execute only hard-coded boot tooling with argv; never a shell."""

    _TOOLS = frozenset({"efibootmgr", "mount", "umount", "grub-install", "chroot"})

    def available(self, tool: str) -> bool:
        return tool in self._TOOLS and shutil.which(tool) is not None

    async def run(
        self,
        tool: str,
        args: tuple[str, ...],
        *,
        timeout_seconds: float,
        stdin_text: str | None = None,
    ) -> _Result:
        if tool not in self._TOOLS:
            raise BootToolError("BOOT_TOOL_NOT_ALLOWED")
        executable = shutil.which(tool)
        if executable is None:
            raise BootToolError(f"BOOT_TOOL_UNAVAILABLE_{tool.upper().replace('-', '_')}")
        input_bytes = stdin_text.encode("utf-8") if stdin_text is not None else None
        if input_bytes is not None and len(input_bytes) > _MAX_INPUT:
            raise BootToolError("BOOT_TOOL_INPUT_TOO_LARGE")
        process = await asyncio.create_subprocess_exec(
            executable,
            *args,
            stdin=(
                asyncio.subprocess.PIPE if input_bytes is not None else asyncio.subprocess.DEVNULL
            ),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd="/",
            env={"LANG": "C", "LC_ALL": "C", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(input=input_bytes), timeout=timeout_seconds
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise BootToolError("BOOT_TOOL_TIMEOUT") from exc
        if len(stdout) > _MAX_OUTPUT or len(stderr) > _MAX_OUTPUT:
            raise BootToolError("BOOT_TOOL_OUTPUT_TOO_LARGE")
        return _Result(
            process.returncode or 0,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace")[:65_536],
        )


class FirmwareDetectionTool:
    def __init__(self, sys_root: Path = Path("/sys")) -> None:
        self.sys_root = sys_root

    def inspect(self) -> tuple[FirmwareEnvironment, tuple[BootEvidence, ...]]:
        efi_root = self.sys_root / "firmware/efi"
        efivars = efi_root / "efivars"
        if efi_root.is_dir():
            mode = FirmwareMode.UEFI
            observation = "EFI firmware interface is present."
            confidence = 0.99
        elif (self.sys_root / "firmware").exists():
            mode = FirmwareMode.BIOS
            observation = "EFI firmware interface is absent in the running Live environment."
            confidence = 0.85
        else:
            mode = FirmwareMode.UNKNOWN
            observation = "Firmware interface could not be established from sysfs."
            confidence = 0.35
        evidence = BootEvidence(source="sysfs", observation=observation, confidence=confidence)
        return (
            FirmwareEnvironment(
                mode=mode,
                efivars_available=efivars.is_dir(),
                boot_entries_available=False,
                evidence_ids=(evidence.id,),
            ),
            (evidence,),
        )


class EFIVariablesTool:
    def __init__(self, runner: BootProcessRunner) -> None:
        self.runner = runner

    async def inspect(self) -> tuple[tuple[BootEntry, ...], tuple[BootEvidence, ...]]:
        if not self.runner.available("efibootmgr"):
            evidence = BootEvidence(
                source="efibootmgr",
                observation="efibootmgr is unavailable; EFI boot entries were not enumerated.",
                confidence=0.4,
            )
            return (), (evidence,)
        result = await self.runner.run("efibootmgr", ("-v",), timeout_seconds=10)
        if result.exit_code != 0:
            evidence = BootEvidence(
                source="efibootmgr",
                observation="EFI variables could not be read by efibootmgr.",
                confidence=0.5,
            )
            return (), (evidence,)
        entries = parse_efibootmgr(result.stdout)
        evidence = BootEvidence(
            source="efibootmgr",
            observation=f"Enumerated {len(entries)} EFI boot entries.",
            confidence=0.95,
        )
        return (
            tuple(entry.model_copy(update={"evidence_ids": (evidence.id,)}) for entry in entries),
            (evidence,),
        )


class BootEntryTool:
    def __init__(self, runner: BootProcessRunner) -> None:
        self.runner = runner

    async def create_grub_entry(
        self,
        *,
        disk_path: str,
        partition_number: int,
        loader_path: str,
        label: str = "ARES GRUB",
    ) -> None:
        if not self.runner.available("efibootmgr"):
            raise BootToolError("EFIBOOTMGR_UNAVAILABLE")
        result = await self.runner.run(
            "efibootmgr",
            (
                "--create",
                "--disk",
                disk_path,
                "--part",
                str(partition_number),
                "--label",
                label,
                "--loader",
                loader_path,
            ),
            timeout_seconds=30,
        )
        if result.exit_code != 0:
            raise BootToolError("EFI_BOOT_ENTRY_UPDATE_FAILED")


class BootloaderDetectionTool:
    def inspect(
        self,
        root_path: Path,
        esp_path: Path | None,
    ) -> tuple[Bootloader, BootConfiguration, BootTargetOS, tuple[BootEvidence, ...]]:
        root = _safe_root(root_path)
        os_release = _read_os_release(root / "etc/os-release")
        family = _distribution_family(os_release)
        name = os_release.get("PRETTY_NAME") or os_release.get("NAME") or "Linux"
        version = os_release.get("VERSION_ID")
        grub_config = root / "boot/grub/grub.cfg"
        default_grub = root / "etc/default/grub"
        fstab = root / "etc/fstab"
        kernels = _relative_matches(root, "boot/vmlinuz*")
        initramfs = tuple(
            sorted(
                {
                    *_relative_matches(root, "boot/initrd.img*"),
                    *_relative_matches(root, "boot/initramfs*"),
                }
            )
        )
        efi_paths: tuple[str, ...] = ()
        if esp_path is not None and esp_path.is_dir():
            efi_root = _safe_root(esp_path)
            efi_paths = tuple(
                sorted(
                    str(path.relative_to(efi_root))
                    for path in efi_root.glob("EFI/**/*.efi")
                    if path.is_file() and not path.is_symlink()
                )
            )
        grub_files = grub_config.is_file() or any("grub" in item.lower() for item in efi_paths)
        systemd_boot = any("systemd" in item.lower() for item in efi_paths)
        windows = any("microsoft/boot/bootmgfw.efi" in item.lower() for item in efi_paths)
        if grub_files:
            kind = BootloaderKind.GRUB
        elif windows:
            kind = BootloaderKind.WINDOWS_BOOT_MANAGER
        elif systemd_boot:
            kind = BootloaderKind.SYSTEMD_BOOT
        else:
            kind = BootloaderKind.UNKNOWN
        evidence = (
            BootEvidence(
                source="filesystem",
                observation=(
                    f"Bootloader evidence: kind={kind.value}, grub_config={grub_config.is_file()}, "
                    f"efi_loader_count={len(efi_paths)}."
                ),
                confidence=0.92 if kind is not BootloaderKind.UNKNOWN else 0.55,
            ),
            BootEvidence(
                source="os-release",
                observation=f"Detected installed system {name}; family={family.value}.",
                confidence=0.95 if os_release else 0.5,
            ),
        )
        configuration = BootConfiguration(
            root_path=str(root),
            boot_path=str(root / "boot") if (root / "boot").exists() else None,
            esp_path=str(esp_path) if esp_path is not None else None,
            grub_config_path=str(grub_config) if grub_config.exists() else None,
            grub_default_path=str(default_grub) if default_grub.exists() else None,
            fstab_path=str(fstab) if fstab.exists() else None,
            kernels=kernels,
            initramfs=initramfs,
        )
        target_os = BootTargetOS(
            id=f"os:{hashlib.sha256(str(root).encode()).hexdigest()[:24]}",
            name=name,
            version=version,
            family=family,
            root_path=str(root),
            boot_path=configuration.boot_path,
            esp_path=configuration.esp_path,
        )
        return (
            Bootloader(
                kind=kind,
                distribution_family=family,
                config_paths=tuple(
                    value
                    for value in (configuration.grub_config_path, configuration.grub_default_path)
                    if value is not None
                ),
                efi_loader_paths=efi_paths,
                repair_supported=(
                    kind is BootloaderKind.GRUB and family is DistributionFamily.DEBIAN
                ),
                evidence_ids=tuple(item.id for item in evidence),
            ),
            configuration,
            target_os,
            evidence,
        )


class FstabAnalysisTool:
    def inspect(
        self,
        configuration: BootConfiguration,
        layout: StorageLayout | None,
    ) -> tuple[BootConfiguration, tuple[BootEvidence, ...]]:
        if configuration.fstab_path is None:
            evidence = BootEvidence(
                source="fstab",
                observation="/etc/fstab was not found in the selected Linux root.",
                confidence=0.7,
            )
            return configuration, (evidence,)
        path = Path(configuration.fstab_path)
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            evidence = BootEvidence(
                source="fstab",
                observation="/etc/fstab could not be read.",
                confidence=0.75,
            )
            return configuration, (evidence,)
        references: list[str] = []
        invalid: list[str] = []
        known_uuid: set[str] = set()
        known_partuuid: set[str] = set()
        if layout is not None:
            known_uuid = {item.uuid for item in layout.filesystems if item.uuid}
            known_partuuid = {
                item.partuuid for item in layout.partition_table.partitions if item.partuuid
            }
        seen_mounts: set[str] = set()
        for raw in content.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            fields = line.split()
            if len(fields) < 2:
                continue
            source, mountpoint = fields[0], fields[1]
            if source.startswith(("UUID=", "PARTUUID=")):
                references.append(source)
                prefix, value = source.split("=", 1)
                if layout is not None and (
                    (prefix == "UUID" and value not in known_uuid)
                    or (prefix == "PARTUUID" and value not in known_partuuid)
                ):
                    invalid.append(source)
            if mountpoint in seen_mounts:
                invalid.append(f"DUPLICATE_MOUNT:{mountpoint}")
            seen_mounts.add(mountpoint)
        evidence = BootEvidence(
            source="fstab",
            observation=(
                f"Analyzed {len(references)} UUID/PARTUUID references; "
                f"invalid_or_duplicate={len(invalid)}."
            ),
            confidence=0.93,
        )
        return (
            configuration.model_copy(
                update={
                    "fstab_references": tuple(references),
                    "invalid_fstab_references": tuple(dict.fromkeys(invalid)),
                }
            ),
            (evidence,),
        )


class RepairEnvironmentTool:
    def __init__(
        self,
        runner: BootProcessRunner,
        *,
        test_mode: bool = False,
        runtime_root: Path = Path("/run/ares/boot-repair"),
    ) -> None:
        self.runner = runner
        self.test_mode = test_mode
        self.runtime_root = runtime_root

    async def prepare(self, plan: BootRepairPlan) -> RepairEnvironment:
        root = _safe_root(Path(plan.current_configuration.root_path or ""))
        work = self.runtime_root / plan.repair_id
        work.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.test_mode:
            return RepairEnvironment(
                repair_id=plan.repair_id,
                root_path=str(root),
                boot_path=plan.current_configuration.boot_path,
                esp_path=plan.current_configuration.esp_path,
                work_root=str(work),
            )
        mounted: list[str] = []
        for source in ("/dev", "/proc", "/sys", "/run"):
            target = root / source.lstrip("/")
            target.mkdir(parents=True, exist_ok=True)
            result = await self.runner.run(
                "mount", ("--bind", source, str(target)), timeout_seconds=30
            )
            if result.exit_code != 0:
                await self.cleanup_paths(tuple(reversed(mounted)))
                raise BootToolError("BOOT_REPAIR_ENVIRONMENT_MOUNT_FAILED")
            mounted.append(str(target))
        return RepairEnvironment(
            repair_id=plan.repair_id,
            root_path=str(root),
            boot_path=plan.current_configuration.boot_path,
            esp_path=plan.current_configuration.esp_path,
            work_root=str(work),
            bind_mounts=tuple(mounted),
        )

    async def cleanup(self, environment: RepairEnvironment) -> RepairEnvironment:
        if not self.test_mode:
            await self.cleanup_paths(tuple(reversed(environment.bind_mounts)))
        with suppress(OSError):
            await asyncio.to_thread(Path(environment.work_root).rmdir)
        return environment.model_copy(update={"cleaned": True})

    async def cleanup_paths(self, paths: tuple[str, ...]) -> None:
        for path in paths:
            if not self.runner.available("umount"):
                raise BootToolError("UMOUNT_UNAVAILABLE")
            result = await self.runner.run("umount", (path,), timeout_seconds=30)
            if result.exit_code != 0:
                raise BootToolError("BOOT_REPAIR_ENVIRONMENT_CLEANUP_FAILED")


class GrubAdapter:
    def __init__(self, runner: BootProcessRunner, *, test_mode: bool = False) -> None:
        self.runner = runner
        self.test_mode = test_mode

    def supported(self, plan: BootRepairPlan) -> bool:
        return (
            plan.bootloader.kind in {BootloaderKind.GRUB, BootloaderKind.UNKNOWN}
            and plan.target_os.family is DistributionFamily.DEBIAN
        )

    async def install(self, plan: BootRepairPlan, environment: RepairEnvironment) -> None:
        if not self.supported(plan):
            raise BootToolError("GRUB_REPAIR_UNSUPPORTED_DISTRIBUTION")
        root = _safe_root(Path(environment.root_path))
        if self.test_mode:
            grub_dir = root / "boot/grub"
            grub_dir.mkdir(parents=True, exist_ok=True)
            (grub_dir / ".ares-grub-installed").write_text("installed\n", encoding="utf-8")
            if plan.target_esp is not None and environment.esp_path:
                esp = _safe_root(Path(environment.esp_path))
                loader = esp / "EFI/debian/grubx64.efi"
                loader.parent.mkdir(parents=True, exist_ok=True)
                loader.write_bytes(b"ARES-GRUB-EFI-FIXTURE\n")
            return
        if not self.runner.available("grub-install"):
            raise BootToolError("GRUB_INSTALL_UNAVAILABLE")
        boot_directory = environment.boot_path or str(root / "boot")
        args: tuple[str, ...]
        if plan.target_esp is not None:
            if environment.esp_path is None:
                raise BootToolError("BOOT_ESP_PATH_UNAVAILABLE")
            args = (
                "--target=x86_64-efi",
                f"--efi-directory={environment.esp_path}",
                f"--boot-directory={boot_directory}",
                "--bootloader-id=debian",
                "--recheck",
            )
        else:
            args = (
                "--target=i386-pc",
                f"--boot-directory={boot_directory}",
                plan.target_disk.canonical_path,
            )
        result = await self.runner.run("grub-install", args, timeout_seconds=180)
        if result.exit_code != 0:
            raise BootToolError("GRUB_INSTALL_FAILED")

    async def regenerate_config(self, plan: BootRepairPlan, environment: RepairEnvironment) -> None:
        del plan
        root = _safe_root(Path(environment.root_path))
        if self.test_mode:
            config = root / "boot/grub/grub.cfg"
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_text(
                "# ARES fixture generated GRUB config\nmenuentry 'Linux' { linux /vmlinuz }\n",
                encoding="utf-8",
            )
            return
        if not self.runner.available("chroot"):
            raise BootToolError("CHROOT_UNAVAILABLE")
        result = await self.runner.run(
            "chroot", (str(root), "/usr/sbin/update-grub"), timeout_seconds=180
        )
        if result.exit_code != 0:
            raise BootToolError("GRUB_CONFIGURATION_REGENERATION_FAILED")

    async def regenerate_initramfs(
        self, plan: BootRepairPlan, environment: RepairEnvironment
    ) -> None:
        del plan
        root = _safe_root(Path(environment.root_path))
        if self.test_mode:
            kernels = sorted((root / "boot").glob("vmlinuz*"))
            if not kernels:
                raise BootToolError("KERNEL_REQUIRED_FOR_INITRAMFS")
            suffix = kernels[-1].name.removeprefix("vmlinuz-")
            (root / "boot" / f"initrd.img-{suffix}").write_bytes(b"ARES-INITRAMFS-FIXTURE\n")
            return
        if not self.runner.available("chroot"):
            raise BootToolError("CHROOT_UNAVAILABLE")
        result = await self.runner.run(
            "chroot",
            (str(root), "/usr/sbin/update-initramfs", "-u", "-k", "all"),
            timeout_seconds=300,
        )
        if result.exit_code != 0:
            raise BootToolError("INITRAMFS_REGENERATION_FAILED")


class BootVerificationTool:
    def __init__(self, efi: EFIVariablesTool) -> None:
        self.efi = efi

    async def verify(self, plan: BootRepairPlan) -> BootVerification:
        root = _safe_root(Path(plan.current_configuration.root_path or ""))
        grub_config = root / "boot/grub/grub.cfg"
        kernels = tuple(root.glob("boot/vmlinuz*"))
        initramfs = tuple(root.glob("boot/initrd.img*")) + tuple(root.glob("boot/initramfs*"))
        entries: tuple[BootEntry, ...] = ()
        efi_evidence: tuple[BootEvidence, ...] = ()
        if plan.target_esp is not None:
            entries, efi_evidence = await self.efi.inspect()
        efi_loader_ok: bool | None = None
        efi_entry_ok: bool | None = None
        if plan.target_esp is not None:
            esp_path = plan.current_configuration.esp_path
            if esp_path:
                efi_loader_ok = any(
                    path.is_file() for path in _safe_root(Path(esp_path)).glob("EFI/**/*.efi")
                )
            else:
                efi_loader_ok = False
            efi_entry_ok = any(
                "grub" in item.label.lower() or "debian" in item.label.lower() for item in entries
            )
        bootloader_ok = grub_config.is_file() and grub_config.stat().st_size > 0
        kernel_ok = bool(kernels)
        initramfs_ok = bool(initramfs)
        root_ok = (root / "etc/os-release").is_file()
        static_ok = bootloader_ok and kernel_ok and initramfs_ok and root_ok
        if plan.target_esp is not None:
            static_ok = static_ok and bool(efi_loader_ok)
        status = BootVerificationStatus.PARTIAL if static_ok else BootVerificationStatus.FAILED
        message = (
            "Boot-chain artifacts are internally consistent, but an offline repair cannot prove "
            "that firmware will complete a real reboot."
            if static_ok
            else "Boot-chain verification found missing or inconsistent required artifacts."
        )
        evidence = (
            BootEvidence(
                source="boot-verification",
                observation=(
                    f"grub={bootloader_ok}, kernel={kernel_ok}, initramfs={initramfs_ok}, "
                    f"root={root_ok}, efi_loader={efi_loader_ok}, efi_entry={efi_entry_ok}."
                ),
                confidence=0.92,
            ),
            *efi_evidence,
        )
        return BootVerification(
            repair_id=plan.repair_id,
            status=status,
            confidence=BootVerificationConfidence.LIMITED,
            firmware_verified=True,
            efi_entry_verified=efi_entry_ok,
            esp_verified=efi_loader_ok,
            bootloader_verified=bootloader_ok,
            grub_configuration_verified=bootloader_ok,
            kernel_verified=kernel_ok,
            initramfs_verified=initramfs_ok,
            root_filesystem_verified=root_ok,
            evidence=evidence,
            limitations=("offline_verification_cannot_prove_successful_reboot",),
            message=message,
        )


class BootRepairToolSuite:
    def __init__(
        self,
        *,
        runner: BootProcessRunner | None = None,
        sys_root: Path = Path("/sys"),
        test_mode: bool = False,
        runtime_root: Path = Path("/run/ares/boot-repair"),
    ) -> None:
        resolved = runner or SafeBootProcessRunner()
        self.runner = resolved
        self.firmware = FirmwareDetectionTool(sys_root)
        self.efi = EFIVariablesTool(resolved)
        self.entries = BootEntryTool(resolved)
        self.bootloader = BootloaderDetectionTool()
        self.fstab = FstabAnalysisTool()
        self.environment = RepairEnvironmentTool(
            resolved, test_mode=test_mode, runtime_root=runtime_root
        )
        self.grub = GrubAdapter(resolved, test_mode=test_mode)
        self.verification = BootVerificationTool(self.efi)
        self.test_mode = test_mode


StageCallback = Callable[[str, dict[str, object]], Awaitable[None]]


def parse_efibootmgr(output: str) -> tuple[BootEntry, ...]:
    entries: list[BootEntry] = []
    for raw in output.splitlines():
        line = raw.strip()
        if not line.startswith("Boot") or len(line) < 9:
            continue
        number = line[4:8]
        if not all(character in "0123456789abcdefABCDEF" for character in number):
            continue
        active = len(line) > 8 and line[8] == "*"
        body = line[9:].strip() if active else line[8:].strip()
        label = body.split("HD(", 1)[0].strip() or "Unnamed EFI entry"
        loader_path: str | None = None
        marker = "File("
        start = body.find(marker)
        if start >= 0:
            end = body.find(")", start + len(marker))
            if end > start:
                loader_path = body[start + len(marker) : end]
        entries.append(
            BootEntry(
                id=f"efi-entry:{number.lower()}",
                number=number.upper(),
                label=label[:256],
                loader_path=loader_path[:1024] if loader_path else None,
                active=active,
            )
        )
    return tuple(entries)


def _distribution_family(values: dict[str, str]) -> DistributionFamily:
    tokens = " ".join(
        (values.get("ID", ""), values.get("ID_LIKE", ""), values.get("NAME", ""))
    ).lower()
    if any(item in tokens for item in ("debian", "ubuntu", "mint")):
        return DistributionFamily.DEBIAN
    if any(item in tokens for item in ("fedora", "rhel", "centos")):
        return DistributionFamily.FEDORA
    if tokens.strip():
        return DistributionFamily.OTHER
    return DistributionFamily.UNKNOWN


def _read_os_release(path: Path) -> dict[str, str]:
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    values: dict[str, str] = {}
    for line in content.splitlines():
        if "=" not in line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"')[:1024]
    return values


def _relative_matches(root: Path, pattern: str) -> tuple[str, ...]:
    return tuple(
        sorted(
            str(path.relative_to(root))
            for path in root.glob(pattern)
            if path.is_file() and not path.is_symlink()
        )
    )


def _safe_root(path: Path) -> Path:
    if not path.is_absolute() or "\x00" in str(path):
        raise BootToolError("BOOT_ROOT_PATH_INVALID")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise BootToolError("BOOT_ROOT_PATH_UNAVAILABLE") from exc
    if resolved.is_symlink() or not resolved.is_dir():
        raise BootToolError("BOOT_ROOT_PATH_INVALID")
    return resolved


def hash_file(path: Path, *, limit: int = 64 * 1024 * 1024) -> str:
    if path.is_symlink() or not path.is_file():
        raise BootToolError("BOOT_CHECKPOINT_FILE_INVALID")
    if path.stat().st_size > limit:
        raise BootToolError("BOOT_CHECKPOINT_FILE_TOO_LARGE")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def checkpoint_evidence_hash(entries: dict[str, str], efi_entries: tuple[BootEntry, ...]) -> str:
    return canonical_sha256(
        {
            "files": entries,
            "efi_entries": [entry.model_dump(mode="json") for entry in efi_entries],
        }
    )
