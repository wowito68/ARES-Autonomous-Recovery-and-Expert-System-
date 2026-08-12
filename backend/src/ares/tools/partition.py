"""Specialized, non-shell Tool Layer for partition inspection and controlled mutation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import stat
from collections.abc import Awaitable, Callable
from pathlib import Path
from time import monotonic
from typing import Literal, Protocol
from uuid import uuid4

from ares.storage_operations.integrity import (
    canonical_sha256,
    device_identity_fingerprint,
    layout_fingerprint,
    partition_geometry_signature,
    partition_table_fingerprint,
    token_sha256,
)
from ares.storage_operations.models import (
    BootDependency,
    EncryptionStatus,
    FilesystemResource,
    FreeRegion,
    MountPointResource,
    OperatingSystemResource,
    PartitionResource,
    PartitionRole,
    PartitionTable,
    PartitionTableCheckpointArtifact,
    PartitionTableType,
    PhysicalDisk,
    StorageDeviceIdentity,
    StorageDeviceTopology,
    StorageDryRunResult,
    StorageLayout,
    StorageOperationOutcome,
    StorageOperationPlan,
    StorageOperationType,
    VolumeKind,
    VolumeResource,
)
from ares.storage_operations.policy import ProductionStorageWriteGate
from ares.tools.storage import ProcessResult, ToolAvailability

_MAX_OUTPUT_BYTES = 4_000_000
_MAX_INPUT_BYTES = 1_000_000
_SAFE_NAME = re.compile(r"^[A-Za-z0-9 ._+:-]{1,96}$")
_SAFE_DEVICE = re.compile(r"^/dev/[A-Za-z0-9_.:+/-]{1,128}$")
_GPT_EFI = "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"
_GPT_LINUX_SWAP = "0657FD6D-A4AB-43C4-84E5-0933C84B4F4F"
_GPT_LINUX_LVM = "E6D6D379-F507-44C2-A23C-238F2A3DF928"
_GPT_LINUX_RAID = "A19D880F-05FC-4D3B-A006-743F0F84911E"
_GPT_WINDOWS_RECOVERY = "DE94BBA4-06D1-4D40-A16A-BFD50179D6AC"
_GPT_WINDOWS_BASIC = "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7"
_MBR_EFI = "ef"
_MBR_LINUX_SWAP = "82"
_MBR_LINUX_LVM = "8e"
_MBR_LINUX_RAID = "fd"

StageCallback = Callable[[str, dict[str, object]], Awaitable[None]]


class PartitionToolError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class PartitionProcessRunner(Protocol):
    def inspect(self, tool: str) -> ToolAvailability: ...

    async def run(
        self,
        tool: str,
        args: tuple[str, ...],
        *,
        timeout_seconds: float,
        stdin_text: str | None = None,
    ) -> ProcessResult: ...


class SafePartitionProcessRunner:
    """Run fixed root-owned utilities with bounded stdin/stdout and no shell."""

    def inspect(self, tool: str) -> ToolAvailability:
        executable = shutil.which(tool)
        if executable is None:
            return ToolAvailability(tool=tool, available=False, reason="tool_not_installed")
        try:
            info = os.stat(executable, follow_symlinks=True)
        except OSError:
            return ToolAvailability(tool=tool, available=False, reason="tool_unavailable")
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            return ToolAvailability(tool=tool, available=False, reason="untrusted_executable")
        return ToolAvailability(tool=tool, available=True)

    async def run(
        self,
        tool: str,
        args: tuple[str, ...],
        *,
        timeout_seconds: float,
        stdin_text: str | None = None,
    ) -> ProcessResult:
        state = self.inspect(tool)
        if not state.available:
            raise FileNotFoundError(state.reason or "tool_unavailable")
        executable = shutil.which(tool)
        if executable is None:  # pragma: no cover
            raise FileNotFoundError("tool_not_installed")
        input_bytes = None if stdin_text is None else stdin_text.encode("utf-8")
        if input_bytes is not None and len(input_bytes) > _MAX_INPUT_BYTES:
            raise ValueError("tool_input_too_large")
        started = monotonic()
        process = await asyncio.create_subprocess_exec(
            executable,
            *args,
            stdin=asyncio.subprocess.PIPE
            if input_bytes is not None
            else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd="/",
            env={"LANG": "C", "LC_ALL": "C", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(input=input_bytes), timeout=timeout_seconds
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            raise
        if len(stdout) > _MAX_OUTPUT_BYTES or len(stderr) > _MAX_OUTPUT_BYTES:
            raise ValueError("tool_output_too_large")
        return ProcessResult(
            tool=tool,
            exit_code=process.returncode or 0,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace")[:32_768],
            duration_ms=(monotonic() - started) * 1_000,
        )


class DiskIdentityTool:
    def __init__(
        self,
        *,
        allow_regular_file_targets: bool = False,
        controlled_image_roots: tuple[Path, ...] = (Path("/var/lib/ares/storage-images"),),
    ) -> None:
        self.allow_regular_file_targets = allow_regular_file_targets
        self.controlled_image_roots = tuple(
            root.resolve(strict=False) for root in controlled_image_roots
        )

    def identify(self, requested: str) -> StorageDeviceIdentity:
        if "\x00" in requested or not requested.startswith("/"):
            raise PartitionToolError("STORAGE_TARGET_INVALID")
        path = Path(requested)
        try:
            if path.is_symlink():
                raise PartitionToolError("STORAGE_TARGET_SYMLINK_REJECTED")
            canonical = path.resolve(strict=True)
            info = canonical.stat()
        except FileNotFoundError as exc:
            raise PartitionToolError("STORAGE_TARGET_NOT_FOUND") from exc
        except OSError as exc:
            raise PartitionToolError("STORAGE_TARGET_UNAVAILABLE") from exc
        if stat.S_ISREG(info.st_mode):
            if not self.allow_regular_file_targets and not self._controlled(canonical):
                raise PartitionToolError("STORAGE_IMAGE_OUTSIDE_CONTROLLED_ROOT")
            kind: Literal["regular_file", "loop", "block"] = "regular_file"
            major_minor = f"file:{info.st_dev}:{info.st_ino}"
            size_bytes = info.st_size
            logical_sector = physical_sector = 512
            topology = StorageDeviceTopology()
            read_only = not os.access(canonical, os.W_OK)
            removable = False
        elif stat.S_ISBLK(info.st_mode):
            major = os.major(info.st_rdev)
            minor = os.minor(info.st_rdev)
            major_minor = f"{major}:{minor}"
            sysfs = Path("/sys/dev/block") / major_minor
            backing = _read_sysfs(sysfs / "loop/backing_file")
            kind = "loop" if backing is not None or canonical.name.startswith("loop") else "block"
            size_sectors = _int_sysfs(sysfs / "size")
            logical_sector = _int_sysfs(sysfs / "queue/logical_block_size") or 512
            physical_sector = _int_sysfs(sysfs / "queue/physical_block_size") or logical_sector
            size_bytes = (size_sectors or 0) * 512
            holders = _child_names(sysfs / "holders")
            slaves = _child_names(sysfs / "slaves")
            topology = StorageDeviceTopology(
                parent_path=None,
                backing_file=(f"/{backing.lstrip('/')}" if backing else None),
                holders=holders,
                slaves=slaves,
            )
            read_only = (_int_sysfs(sysfs / "ro") or 0) != 0
            removable = (_int_sysfs(sysfs / "removable") or 0) != 0
        else:
            raise PartitionToolError("STORAGE_TARGET_NOT_DISK_OR_IMAGE")

        serial = _read_identity_value(info, "serial") if kind != "regular_file" else None
        wwn = _read_identity_value(info, "wwid") if kind != "regular_file" else None
        model = _read_identity_value(info, "model") if kind != "regular_file" else "disk-image"
        controlled = kind == "regular_file" and (
            self.allow_regular_file_targets or self._controlled(canonical)
        )
        if kind == "loop" and topology.backing_file is not None:
            controlled = self.allow_regular_file_targets or self._controlled(
                Path(topology.backing_file)
            )
        draft = StorageDeviceIdentity(
            requested_path=requested,
            canonical_path=str(canonical),
            device_kind=kind,
            major_minor=major_minor,
            serial_sha256=token_sha256(serial) if serial else None,
            wwn_sha256=token_sha256(wwn) if wwn else None,
            model=model,
            size_bytes=size_bytes,
            logical_sector_size=logical_sector,
            physical_sector_size=physical_sector,
            topology=topology,
            read_only=read_only,
            removable=removable,
            controlled_test_target=controlled,
            fingerprint_sha256="0" * 64,
        )
        return draft.model_copy(update={"fingerprint_sha256": device_identity_fingerprint(draft)})

    def revalidate(self, expected: StorageDeviceIdentity) -> StorageDeviceIdentity:
        current = self.identify(expected.requested_path)
        if current.fingerprint_sha256 != expected.fingerprint_sha256:
            raise PartitionToolError("STORAGE_DEVICE_IDENTITY_CHANGED")
        return current

    def _controlled(self, path: Path) -> bool:
        candidate = path.resolve(strict=False)
        return any(
            candidate == root or root in candidate.parents for root in self.controlled_image_roots
        )


class PartitionTableTool:
    def __init__(self, runner: PartitionProcessRunner, identity: DiskIdentityTool) -> None:
        self.runner = runner
        self.identity = identity

    async def inspect(self, target: str) -> StorageLayout:
        identity = await asyncio.to_thread(self.identity.identify, target)
        state = self.runner.inspect("sfdisk")
        if not state.available:
            raise PartitionToolError("SFDISK_UNAVAILABLE")
        result = await self._run("sfdisk", ("--json", identity.canonical_path), 10)
        if result.exit_code == 0:
            table = parse_sfdisk_json(result.stdout, identity)
        elif identity.device_kind == "regular_file" and _looks_unpartitioned(result.stderr):
            table = _empty_table(identity)
        else:
            raise PartitionToolError("STORAGE_PARTITION_TABLE_INSPECTION_FAILED")
        return await self._enrich(identity, table)

    async def dump(self, expected: StorageDeviceIdentity) -> str:
        await asyncio.to_thread(self.identity.revalidate, expected)
        result = await self._run("sfdisk", ("--dump", expected.canonical_path), 10)
        if result.exit_code != 0:
            if _looks_unpartitioned(result.stderr):
                return ""
            raise PartitionToolError("STORAGE_PARTITION_TABLE_DUMP_FAILED")
        if len(result.stdout.encode("utf-8")) > _MAX_INPUT_BYTES:
            raise PartitionToolError("STORAGE_PARTITION_TABLE_DUMP_TOO_LARGE")
        return result.stdout

    async def _enrich(
        self, identity: StorageDeviceIdentity, table: PartitionTable
    ) -> StorageLayout:
        filesystems: list[FilesystemResource] = []
        mounts: list[MountPointResource] = []
        volumes: list[VolumeResource] = []
        operating_systems: list[OperatingSystemResource] = []
        boot_dependencies: list[BootDependency] = []
        swap: list[int] = []
        recovery: list[int] = []
        updated_partitions: list[PartitionResource] = []
        mount_records = _mount_records()
        swap_sources = _swap_sources()
        for partition in table.partitions:
            probe = await self._probe_partition(identity, partition)
            filesystem_id: str | None = None
            volume_ids: list[str] = []
            encryption = partition.encryption_status
            role = partition.role
            if probe:
                usage = probe.get("USAGE", "").casefold()
                fs_type = probe.get("TYPE")
                fs_uuid = probe.get("UUID")
                label = probe.get("LABEL")
                if fs_type == "crypto_LUKS":
                    encryption = EncryptionStatus.LUKS
                    volume_id = f"volume:crypt:{partition.id}"
                    volumes.append(
                        VolumeResource(
                            id=volume_id,
                            kind=VolumeKind.CRYPT,
                            name=label or "LUKS",
                            member_paths=(partition.path,),
                        )
                    )
                    volume_ids.append(volume_id)
                elif fs_type and "bitlocker" in fs_type.casefold():
                    encryption = EncryptionStatus.BITLOCKER
                    volume_id = f"volume:crypt:{partition.id}"
                    volumes.append(
                        VolumeResource(
                            id=volume_id,
                            kind=VolumeKind.CRYPT,
                            name=label or "BitLocker",
                            member_paths=(partition.path,),
                        )
                    )
                    volume_ids.append(volume_id)
                elif fs_type == "LVM2_member" or usage == "lvm":
                    role = PartitionRole.LVM
                    volume_id = f"volume:lvm-pv:{partition.id}"
                    volumes.append(
                        VolumeResource(
                            id=volume_id,
                            kind=VolumeKind.LVM_PV,
                            name=label or fs_uuid or "LVM PV",
                            member_paths=(partition.path,),
                        )
                    )
                    volume_ids.append(volume_id)
                elif usage == "raid" or (fs_type and "raid" in fs_type.casefold()):
                    role = PartitionRole.RAID
                    volume_id = f"volume:mdraid:{partition.id}"
                    volumes.append(
                        VolumeResource(
                            id=volume_id,
                            kind=VolumeKind.MDRAID,
                            name=label or fs_uuid or "RAID member",
                            member_paths=(partition.path,),
                        )
                    )
                    volume_ids.append(volume_id)
                elif fs_type == "swap":
                    role = PartitionRole.SWAP
                    swap.append(partition.number)
                elif fs_type:
                    filesystem_id = f"filesystem:{token_sha256(partition.id + ':' + fs_type)[:24]}"
                    filesystems.append(
                        FilesystemResource(
                            id=filesystem_id,
                            device_path=partition.path,
                            filesystem_type=fs_type,
                            uuid=fs_uuid,
                            label=label,
                            resize_support=_filesystem_resize_support(fs_type),
                        )
                    )
            matching_mounts = tuple(item for item in mount_records if item.source == partition.path)
            mount_ids: list[str] = []
            os_ids: list[str] = []
            for raw_mount in matching_mounts:
                source = raw_mount.source
                target = raw_mount.path
                fs_type = raw_mount.filesystem_type
                options = raw_mount.options
                mount_id = f"mount:{token_sha256(source + ':' + target)[:24]}"
                mounts.append(
                    MountPointResource(
                        id=mount_id,
                        source=source,
                        path=target,
                        filesystem_type=fs_type,
                        options=options,
                    )
                )
                mount_ids.append(mount_id)
                os_item = _detect_os(source, target)
                if os_item is not None:
                    operating_systems.append(os_item)
                    os_ids.append(os_item.id)
                if target in {"/", "/boot", "/boot/efi"}:
                    kind = "linux-root" if target == "/" else "boot-mount"
                    boot_dependencies.append(
                        BootDependency(
                            id=f"boot:{token_sha256(source + ':' + target)[:24]}",
                            kind=kind,
                            partition_number=partition.number,
                            resource_path=target,
                            reason=f"Partition is mounted at boot-sensitive path {target}.",
                            critical=True,
                        )
                    )
            if partition.path in swap_sources and partition.number not in swap:
                swap.append(partition.number)
                role = PartitionRole.SWAP
            if role is PartitionRole.EFI:
                boot_dependencies.append(
                    BootDependency(
                        id=f"boot:efi:{partition.id}",
                        kind="efi-system-partition",
                        partition_number=partition.number,
                        resource_path=partition.path,
                        reason="Partition type identifies an EFI System Partition.",
                        critical=True,
                    )
                )
            if partition.bootable:
                boot_dependencies.append(
                    BootDependency(
                        id=f"boot:legacy:{partition.id}",
                        kind="legacy-bootable",
                        partition_number=partition.number,
                        resource_path=partition.path,
                        reason="MBR bootable flag is set.",
                        critical=True,
                    )
                )
            if role is PartitionRole.RECOVERY:
                recovery.append(partition.number)
            updated_partitions.append(
                partition.model_copy(
                    update={
                        "filesystem_id": filesystem_id,
                        "mount_point_ids": tuple(mount_ids),
                        "operating_system_ids": tuple(os_ids),
                        "volume_ids": tuple(volume_ids),
                        "encryption_status": encryption,
                        "role": role,
                    }
                )
            )
        enriched_draft = table.model_copy(
            update={"partitions": tuple(updated_partitions), "fingerprint_sha256": "0" * 64}
        )
        enriched_table = enriched_draft.model_copy(
            update={"fingerprint_sha256": partition_table_fingerprint(enriched_draft)}
        )
        disk = PhysicalDisk(
            id=identity.resource_id,
            identity=identity,
            partition_table_id=f"partition-table:{identity.fingerprint_sha256[:24]}",
        )
        body = {
            "disk": disk.model_dump(mode="json"),
            "partition_table": enriched_table.model_dump(mode="json"),
            "filesystems": [item.model_dump(mode="json") for item in filesystems],
            "mount_points": [item.model_dump(mode="json") for item in mounts],
            "volumes": [item.model_dump(mode="json") for item in volumes],
            "operating_systems": [item.model_dump(mode="json") for item in operating_systems],
            "boot_dependencies": [item.model_dump(mode="json") for item in boot_dependencies],
            "swap_partitions": sorted(set(swap)),
            "recovery_partitions": sorted(set(recovery)),
        }
        evidence = canonical_sha256(body)
        return StorageLayout(
            disk=disk,
            partition_table=enriched_table,
            filesystems=tuple(filesystems),
            mount_points=tuple(mounts),
            volumes=tuple(volumes),
            operating_systems=tuple(operating_systems),
            boot_dependencies=tuple(boot_dependencies),
            swap_partitions=tuple(sorted(set(swap))),
            recovery_partitions=tuple(sorted(set(recovery))),
            evidence_sha256=evidence,
        )

    async def _probe_partition(
        self, identity: StorageDeviceIdentity, partition: PartitionResource
    ) -> dict[str, str]:
        state = self.runner.inspect("blkid")
        if not state.available:
            return {}
        args: tuple[str, ...]
        if identity.device_kind == "regular_file":
            offset = partition.start_sector * identity.logical_sector_size
            size = partition.size_sectors * identity.logical_sector_size
            args = (
                "--probe",
                "--output",
                "export",
                "--offset",
                str(offset),
                "--size",
                str(size),
                identity.canonical_path,
            )
        else:
            if _SAFE_DEVICE.fullmatch(partition.path) is None:
                return {}
            args = ("--probe", "--output", "export", partition.path)
        try:
            result = await self._run("blkid", args, 5)
        except PartitionToolError:
            return {}
        if result.exit_code not in {0, 2}:
            return {}
        return parse_blkid_export(result.stdout)

    async def _run(self, tool: str, args: tuple[str, ...], timeout_seconds: float) -> ProcessResult:
        try:
            return await self.runner.run(tool, args, timeout_seconds=timeout_seconds)
        except TimeoutError as exc:
            raise PartitionToolError("STORAGE_TOOL_TIMEOUT") from exc
        except (FileNotFoundError, PermissionError, OSError, ValueError) as exc:
            raise PartitionToolError("STORAGE_TOOL_FAILED") from exc


class PartitionTableVerifyTool:
    def __init__(self, table_tool: PartitionTableTool, runner: PartitionProcessRunner) -> None:
        self.table_tool = table_tool
        self.runner = runner

    async def verify(self, plan: StorageOperationPlan) -> StorageLayout:
        current = await self.table_tool.inspect(plan.target_disk.requested_path)
        if current.disk.identity.fingerprint_sha256 != plan.target_disk.fingerprint_sha256:
            raise PartitionToolError("STORAGE_DEVICE_IDENTITY_CHANGED")
        expected = partition_geometry_signature(plan.proposed_layout.partition_table)
        actual = partition_geometry_signature(current.partition_table)
        if expected != actual:
            raise PartitionToolError("STORAGE_PARTITION_GEOMETRY_MISMATCH")
        result = await self.runner.run(
            "sfdisk",
            ("--verify", plan.target_disk.canonical_path),
            timeout_seconds=10,
        )
        if result.exit_code != 0:
            raise PartitionToolError("STORAGE_PARTITION_TABLE_VERIFY_FAILED")
        return current


class PartitionCreateTool:
    def __init__(self, runner: PartitionProcessRunner) -> None:
        self.runner = runner

    async def execute(self, plan: StorageOperationPlan) -> int:
        if plan.operation is not StorageOperationType.CREATE:
            raise PartitionToolError("STORAGE_OPERATION_TYPE_MISMATCH")
        return await _write_table(self.runner, plan)


class PartitionDeleteTool:
    def __init__(self, runner: PartitionProcessRunner) -> None:
        self.runner = runner

    async def execute(self, plan: StorageOperationPlan) -> int:
        if plan.operation is not StorageOperationType.DELETE:
            raise PartitionToolError("STORAGE_OPERATION_TYPE_MISMATCH")
        return await _write_table(self.runner, plan)


class PartitionResizeTool:
    async def execute(self, plan: StorageOperationPlan) -> int:
        del plan
        raise PartitionToolError("STORAGE_PARTITION_RESIZE_DISABLED")


class PartitionMoveTool:
    async def execute(self, plan: StorageOperationPlan) -> int:
        del plan
        raise PartitionToolError("STORAGE_PARTITION_MOVE_DISABLED")


class MountDependencyTool:
    def inspect(self, layout: StorageLayout) -> tuple[str, ...]:
        dependencies = [item.path for item in layout.mount_points]
        dependencies.extend(f"swap:{number}" for number in layout.swap_partitions)
        return tuple(dependencies)


class BootDependencyTool:
    def inspect(self, layout: StorageLayout) -> tuple[BootDependency, ...]:
        return layout.boot_dependencies


class StoragePartitionToolSuite:
    """Composition of specialized tools; planning/authorization live above this layer."""

    def __init__(
        self,
        *,
        runner: PartitionProcessRunner | None = None,
        allow_regular_file_targets: bool = False,
        controlled_image_roots: tuple[Path, ...] = (Path("/var/lib/ares/storage-images"),),
        write_gate: ProductionStorageWriteGate | None = None,
    ) -> None:
        self.runner = runner or SafePartitionProcessRunner()
        self.identity = DiskIdentityTool(
            allow_regular_file_targets=allow_regular_file_targets,
            controlled_image_roots=controlled_image_roots,
        )
        self.table = PartitionTableTool(self.runner, self.identity)
        self.create = PartitionCreateTool(self.runner)
        self.delete = PartitionDeleteTool(self.runner)
        self.resize = PartitionResizeTool()
        self.move = PartitionMoveTool()
        self.verify_tool = PartitionTableVerifyTool(self.table, self.runner)
        self.mount_dependencies = MountDependencyTool()
        self.boot_dependencies = BootDependencyTool()
        self.write_gate = write_gate or ProductionStorageWriteGate(
            test_mode=allow_regular_file_targets,
            controlled_image_roots=controlled_image_roots,
        )

    async def inspect(self, target: str) -> StorageLayout:
        return await self.table.inspect(target)

    async def dry_run(self, plan: StorageOperationPlan) -> StorageDryRunResult:
        await asyncio.to_thread(self.identity.revalidate, plan.target_disk)
        script = render_sfdisk_script(plan.proposed_layout.partition_table, plan.target_disk)
        try:
            result = await self.runner.run(
                "sfdisk",
                (
                    "--no-act",
                    "--lock",
                    "--wipe",
                    "never",
                    "--wipe-partitions",
                    "never",
                    plan.target_disk.canonical_path,
                ),
                timeout_seconds=10,
                stdin_text=script,
            )
        except TimeoutError as exc:
            raise PartitionToolError("STORAGE_DRY_RUN_TIMEOUT") from exc
        except (OSError, ValueError, FileNotFoundError, PermissionError) as exc:
            raise PartitionToolError("STORAGE_DRY_RUN_FAILED") from exc
        return StorageDryRunResult(
            original_layout_sha256=layout_fingerprint(plan.original_layout),
            proposed_layout_sha256=layout_fingerprint(plan.proposed_layout),
            script_sha256=hashlib.sha256(script.encode("utf-8")).hexdigest(),
            exit_code=result.exit_code,
            valid=result.exit_code == 0,
            warnings=tuple(line for line in result.stderr.splitlines() if line.strip())[:32],
        )

    async def create_checkpoint(
        self, plan: StorageOperationPlan, checkpoint_id: str
    ) -> PartitionTableCheckpointArtifact:
        await asyncio.to_thread(self.identity.revalidate, plan.target_disk)
        dump = await self.table.dump(plan.target_disk)
        return PartitionTableCheckpointArtifact(
            checkpoint_id=checkpoint_id,
            operation_id=plan.operation_id,
            target_fingerprint_sha256=plan.target_disk.fingerprint_sha256,
            partition_table_fingerprint_sha256=plan.original_layout.partition_table.fingerprint_sha256,
            dump_sha256=hashlib.sha256(dump.encode("utf-8")).hexdigest(),
            sfdisk_dump=dump,
        )

    async def execute(
        self,
        plan: StorageOperationPlan,
        *,
        on_stage: StageCallback,
    ) -> StorageOperationOutcome:
        if not self.write_gate.evaluate(plan.target_disk).allowed:
            raise PartitionToolError("PRODUCTION_STORAGE_WRITE_GATE_BLOCKED")
        await asyncio.to_thread(self.identity.revalidate, plan.target_disk)
        before = await self.inspect(plan.target_disk.requested_path)
        if partition_geometry_signature(before.partition_table) != partition_geometry_signature(
            plan.original_layout.partition_table
        ):
            raise PartitionToolError("STORAGE_LAYOUT_CHANGED")
        await on_stage(
            "storage.partition-table.write.started",
            {
                "operation": plan.operation.value,
                "target_fingerprint": plan.target_disk.fingerprint_sha256,
            },
        )
        if plan.operation is StorageOperationType.CREATE:
            exit_code = await self.create.execute(plan)
        elif plan.operation is StorageOperationType.DELETE:
            exit_code = await self.delete.execute(plan)
        elif plan.operation is StorageOperationType.RESIZE:
            exit_code = await self.resize.execute(plan)
        else:
            exit_code = await self.move.execute(plan)
        if exit_code != 0:
            raise PartitionToolError("STORAGE_PARTITION_TABLE_WRITE_FAILED")
        await on_stage(
            "storage.partition-table.write.completed",
            {"operation": plan.operation.value, "exit_code": exit_code},
        )
        kernel_reread: bool | None = None
        if plan.target_disk.device_kind in {"loop", "block"}:
            kernel_reread = await self._reread_kernel(plan.target_disk)
        after = await self.verify_tool.verify(plan)
        return StorageOperationOutcome(
            operation_id=plan.operation_id,
            operation=plan.operation,
            exit_code=exit_code,
            kernel_reread=kernel_reread,
            before=before,
            after=after,
            evidence=(
                f"before={layout_fingerprint(before)}",
                f"after={layout_fingerprint(after)}",
                f"partition_table={after.partition_table.fingerprint_sha256}",
            ),
        )

    async def _reread_kernel(self, identity: StorageDeviceIdentity) -> bool:
        state = self.runner.inspect("blockdev")
        if not state.available:
            raise PartitionToolError("BLOCKDEV_UNAVAILABLE")
        try:
            result = await self.runner.run(
                "blockdev",
                ("--rereadpt", identity.canonical_path),
                timeout_seconds=10,
            )
        except (TimeoutError, OSError, ValueError, FileNotFoundError, PermissionError) as exc:
            raise PartitionToolError("STORAGE_KERNEL_REREAD_FAILED") from exc
        if result.exit_code != 0:
            raise PartitionToolError("STORAGE_KERNEL_REREAD_FAILED")
        return True


async def _write_table(runner: PartitionProcessRunner, plan: StorageOperationPlan) -> int:
    script = render_sfdisk_script(plan.proposed_layout.partition_table, plan.target_disk)
    try:
        result = await runner.run(
            "sfdisk",
            (
                "--lock",
                "--wipe",
                "never",
                "--wipe-partitions",
                "never",
                plan.target_disk.canonical_path,
            ),
            timeout_seconds=60,
            stdin_text=script,
        )
    except TimeoutError as exc:
        raise PartitionToolError("STORAGE_PARTITION_TABLE_WRITE_TIMEOUT") from exc
    except (OSError, ValueError, FileNotFoundError, PermissionError) as exc:
        raise PartitionToolError("STORAGE_PARTITION_TABLE_WRITE_FAILED") from exc
    return result.exit_code


def parse_sfdisk_json(text: str, identity: StorageDeviceIdentity) -> PartitionTable:
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise PartitionToolError("STORAGE_PARTITION_TABLE_PARSE_FAILED") from exc
    raw_table = payload.get("partitiontable") if isinstance(payload, dict) else None
    if not isinstance(raw_table, dict):
        raise PartitionToolError("STORAGE_PARTITION_TABLE_PARSE_FAILED")
    raw_label = raw_table.get("label")
    table_type = (
        PartitionTableType.GPT
        if raw_label == "gpt"
        else PartitionTableType.MBR
        if raw_label == "dos"
        else PartitionTableType.UNKNOWN
    )
    sector_size = _positive_int(raw_table.get("sectorsize")) or identity.logical_sector_size
    total_sectors = identity.size_bytes // sector_size
    first = _nonnegative_int(raw_table.get("firstlba"))
    last = _nonnegative_int(raw_table.get("lastlba"))
    if table_type is PartitionTableType.GPT:
        first = first if first is not None else min(2048, max(34, total_sectors - 34))
        last = last if last is not None else max(first, total_sectors - 34)
    else:
        first = first if first is not None else (2048 if total_sectors > 4096 else 1)
        last = last if last is not None else max(first, total_sectors - 1)
    partitions: list[PartitionResource] = []
    raw_partitions = raw_table.get("partitions")
    if isinstance(raw_partitions, list):
        for index, raw in enumerate(raw_partitions, start=1):
            if not isinstance(raw, dict):
                continue
            start = _nonnegative_int(raw.get("start"))
            size = _positive_int(raw.get("size"))
            if start is None or size is None:
                continue
            number = _partition_number(raw.get("node"), index)
            type_code = _clean(raw.get("type"), 128)
            partuuid = _clean(raw.get("uuid"), 256)
            name = _clean(raw.get("name"), 256)
            attrs = _clean(raw.get("attrs"), 256)
            bootable = bool(raw.get("bootable"))
            role = _role(table_type, type_code, bootable)
            path = _partition_path(identity, raw.get("node"), number)
            partitions.append(
                PartitionResource(
                    id=_partition_id(identity, number, start, size, partuuid),
                    number=number,
                    path=path,
                    start_sector=start,
                    end_sector=start + size - 1,
                    size_sectors=size,
                    size_bytes=size * sector_size,
                    type_code=type_code,
                    partuuid=partuuid,
                    name=name,
                    bootable=bootable,
                    attrs=attrs,
                    role=role,
                    encryption_status=EncryptionStatus.UNKNOWN,
                )
            )
    partitions.sort(key=lambda item: (item.start_sector, item.number))
    free = _free_regions(tuple(partitions), first, last, sector_size)
    draft = PartitionTable(
        type=table_type,
        guid=_clean(raw_table.get("id"), 256),
        sector_size=sector_size,
        first_usable_sector=first,
        last_usable_sector=last,
        total_sectors=total_sectors,
        partitions=tuple(partitions),
        free_regions=free,
        fingerprint_sha256="0" * 64,
    )
    return draft.model_copy(update={"fingerprint_sha256": partition_table_fingerprint(draft)})


def parse_blkid_export(text: str) -> dict[str, str]:
    output: dict[str, str] = {}
    for line in text.splitlines()[:128]:
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in {"TYPE", "UUID", "LABEL", "USAGE", "VERSION", "PART_ENTRY_TYPE"}:
            output[key] = value[:512]
    return output


def render_sfdisk_script(table: PartitionTable, identity: StorageDeviceIdentity) -> str:
    if table.type not in {PartitionTableType.GPT, PartitionTableType.MBR}:
        raise PartitionToolError("STORAGE_PARTITION_TABLE_TYPE_REQUIRED")
    lines = [
        f"label: {'gpt' if table.type is PartitionTableType.GPT else 'dos'}",
        "unit: sectors",
        f"sector-size: {table.sector_size}",
    ]
    if table.guid:
        lines.append(f"label-id: {table.guid}")
    if table.type is PartitionTableType.GPT:
        lines.extend(
            (
                f"first-lba: {table.first_usable_sector}",
                f"last-lba: {table.last_usable_sector}",
            )
        )
    lines.append("")
    for partition in sorted(table.partitions, key=lambda item: item.number):
        fields = [
            f"start={partition.start_sector}",
            f"size={partition.size_sectors}",
        ]
        if partition.type_code:
            fields.append(f"type={partition.type_code}")
        if partition.partuuid and table.type is PartitionTableType.GPT:
            fields.append(f"uuid={partition.partuuid}")
        if partition.name and table.type is PartitionTableType.GPT:
            if _SAFE_NAME.fullmatch(partition.name) is None:
                raise PartitionToolError("STORAGE_PARTITION_NAME_UNSAFE")
            fields.append(f'name="{partition.name}"')
        if partition.attrs and table.type is PartitionTableType.GPT:
            fields.append(f"attrs={partition.attrs}")
        if partition.bootable and table.type is PartitionTableType.MBR:
            fields.append("bootable")
        lines.append(", ".join(fields))
    script = "\n".join(lines) + "\n"
    if len(script.encode("utf-8")) > _MAX_INPUT_BYTES:
        raise PartitionToolError("STORAGE_PARTITION_SCRIPT_TOO_LARGE")
    return script


def new_partition_resource(
    *,
    identity: StorageDeviceIdentity,
    table_type: PartitionTableType,
    number: int,
    start_sector: int,
    size_sectors: int,
    sector_size: int,
    type_code: str | None,
    name: str | None,
) -> PartitionResource:
    if name is not None and _SAFE_NAME.fullmatch(name) is None:
        raise PartitionToolError("STORAGE_PARTITION_NAME_UNSAFE")
    partuuid = str(uuid4()).upper() if table_type is PartitionTableType.GPT else None
    normalized_type = type_code or _default_partition_type(table_type)
    return PartitionResource(
        id=_partition_id(identity, number, start_sector, size_sectors, partuuid),
        number=number,
        path=_partition_path(identity, None, number),
        start_sector=start_sector,
        end_sector=start_sector + size_sectors - 1,
        size_sectors=size_sectors,
        size_bytes=size_sectors * sector_size,
        type_code=normalized_type,
        partuuid=partuuid,
        name=name,
        role=_role(table_type, normalized_type, False),
        encryption_status=EncryptionStatus.UNKNOWN,
    )


def _empty_table(identity: StorageDeviceIdentity) -> PartitionTable:
    sector = identity.logical_sector_size
    total = identity.size_bytes // sector
    first = 2048 if total > 4096 else 1
    last = max(first, total - 1)
    draft = PartitionTable(
        type=PartitionTableType.UNKNOWN,
        sector_size=sector,
        first_usable_sector=first,
        last_usable_sector=last,
        total_sectors=total,
        free_regions=_free_regions((), first, last, sector),
        fingerprint_sha256="0" * 64,
    )
    return draft.model_copy(update={"fingerprint_sha256": partition_table_fingerprint(draft)})


def build_empty_table(
    identity: StorageDeviceIdentity, table_type: PartitionTableType, guid: str
) -> PartitionTable:
    sector = identity.logical_sector_size
    total = identity.size_bytes // sector
    if table_type is PartitionTableType.GPT:
        first = 2048 if total > 4096 else 34
        last = max(first, total - 34)
    elif table_type is PartitionTableType.MBR:
        first = 2048 if total > 4096 else 1
        last = max(first, total - 1)
    else:
        raise PartitionToolError("STORAGE_PARTITION_TABLE_TYPE_REQUIRED")
    draft = PartitionTable(
        type=table_type,
        guid=guid,
        sector_size=sector,
        first_usable_sector=first,
        last_usable_sector=last,
        total_sectors=total,
        free_regions=_free_regions((), first, last, sector),
        fingerprint_sha256="0" * 64,
    )
    return draft.model_copy(update={"fingerprint_sha256": partition_table_fingerprint(draft)})


def with_partitions(
    table: PartitionTable, partitions: tuple[PartitionResource, ...]
) -> PartitionTable:
    free = _free_regions(
        tuple(sorted(partitions, key=lambda item: (item.start_sector, item.number))),
        table.first_usable_sector,
        table.last_usable_sector,
        table.sector_size,
    )
    draft = table.model_copy(
        update={
            "partitions": tuple(
                sorted(partitions, key=lambda item: (item.start_sector, item.number))
            ),
            "free_regions": free,
            "fingerprint_sha256": "0" * 64,
        }
    )
    return draft.model_copy(update={"fingerprint_sha256": partition_table_fingerprint(draft)})


def layout_with_table(original: StorageLayout, table: PartitionTable) -> StorageLayout:
    disk = original.disk
    body = {
        "disk": disk.model_dump(mode="json"),
        "partition_table": table.model_dump(mode="json"),
        "filesystems": [],
        "mount_points": [],
        "volumes": [],
        "operating_systems": [],
        "boot_dependencies": [],
        "swap_partitions": [],
        "recovery_partitions": [],
    }
    return StorageLayout(
        disk=disk,
        partition_table=table,
        evidence_sha256=canonical_sha256(body),
        warnings=("proposed_layout_contains_geometry_only_until_post-write-verification",),
    )


def _free_regions(
    partitions: tuple[PartitionResource, ...], first: int, last: int, sector_size: int
) -> tuple[FreeRegion, ...]:
    if last < first:
        return ()
    output: list[FreeRegion] = []
    cursor = first
    for partition in sorted(partitions, key=lambda item: item.start_sector):
        if partition.start_sector > cursor:
            output.append(_free(cursor, partition.start_sector - 1, sector_size))
        cursor = max(cursor, partition.end_sector + 1)
    if cursor <= last:
        output.append(_free(cursor, last, sector_size))
    return tuple(output)


def _free(start: int, end: int, sector_size: int) -> FreeRegion:
    sectors = max(0, end - start + 1)
    return FreeRegion(
        start_sector=start,
        end_sector=end,
        size_sectors=sectors,
        size_bytes=sectors * sector_size,
    )


def _partition_id(
    identity: StorageDeviceIdentity,
    number: int,
    start: int,
    size: int,
    partuuid: str | None,
) -> str:
    value = f"{identity.fingerprint_sha256}:{number}:{start}:{size}:{partuuid or ''}"
    return f"partition:{token_sha256(value)[:24]}"


def _partition_path(identity: StorageDeviceIdentity, raw_node: object, number: int) -> str:
    if (
        identity.device_kind != "regular_file"
        and isinstance(raw_node, str)
        and _SAFE_DEVICE.fullmatch(raw_node)
    ):
        return raw_node
    if identity.device_kind == "regular_file":
        return f"image:{token_sha256(identity.canonical_path)[:16]}:{number}"
    base = identity.canonical_path
    separator = "p" if base[-1:].isdigit() else ""
    return f"{base}{separator}{number}"


def _role(table_type: PartitionTableType, type_code: str | None, bootable: bool) -> PartitionRole:
    value = (type_code or "").upper()
    short = value.lower().removeprefix("0x")
    if table_type is PartitionTableType.GPT:
        if value == _GPT_EFI:
            return PartitionRole.EFI
        if value == _GPT_LINUX_SWAP:
            return PartitionRole.SWAP
        if value == _GPT_LINUX_LVM:
            return PartitionRole.LVM
        if value == _GPT_LINUX_RAID:
            return PartitionRole.RAID
        if value == _GPT_WINDOWS_RECOVERY:
            return PartitionRole.RECOVERY
        if value == _GPT_WINDOWS_BASIC:
            return PartitionRole.WINDOWS
    else:
        if short == _MBR_EFI:
            return PartitionRole.EFI
        if short == _MBR_LINUX_SWAP:
            return PartitionRole.SWAP
        if short == _MBR_LINUX_LVM:
            return PartitionRole.LVM
        if short == _MBR_LINUX_RAID:
            return PartitionRole.RAID
        if bootable:
            return PartitionRole.BOOT
    return PartitionRole.NORMAL


def _default_partition_type(table_type: PartitionTableType) -> str:
    if table_type is PartitionTableType.GPT:
        return "0FC63DAF-8483-4772-8E79-3D69D8477DE4"
    return "83"


def _mount_records() -> tuple[MountPointResource, ...]:
    path = Path("/proc/self/mountinfo")
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ()
    output: list[MountPointResource] = []
    for line in lines[:4096]:
        fields = line.split()
        if "-" not in fields:
            continue
        separator = fields.index("-")
        if separator + 3 > len(fields) or separator < 6:
            continue
        target = _unescape_mount(fields[4])
        options = tuple(fields[5].split(","))
        fs_type = fields[separator + 1]
        source = _unescape_mount(fields[separator + 2])
        if not source.startswith("/dev/") or not target.startswith("/"):
            continue
        output.append(
            MountPointResource(
                id=f"raw-mount:{token_sha256(source + ':' + target)[:24]}",
                source=source,
                path=target,
                filesystem_type=fs_type,
                options=options,
            )
        )
    return tuple(output)


def _swap_sources() -> frozenset[str]:
    try:
        lines = Path("/proc/swaps").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return frozenset()
    return frozenset(line.split()[0] for line in lines[1:] if line.split())


def _detect_os(source: str, mount_point: str) -> OperatingSystemResource | None:
    os_release = Path(mount_point) / "etc/os-release"
    try:
        if (
            os_release.is_symlink()
            or not os_release.is_file()
            or os_release.stat().st_size > 64_000
        ):
            return None
        values: dict[str, str] = {}
        for line in os_release.read_text(encoding="utf-8", errors="replace").splitlines()[:128]:
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key] = value.strip().strip('"')[:256]
        name = values.get("PRETTY_NAME") or values.get("NAME")
        if not name:
            return None
        return OperatingSystemResource(
            id=f"os:{token_sha256(source + ':' + name)[:24]}",
            name=name,
            version=values.get("VERSION_ID") or values.get("VERSION"),
            source=source,
            mount_point=mount_point,
        )
    except OSError:
        return None


def _filesystem_resize_support(
    fs_type: str,
) -> Literal["grow", "shrink_and_grow", "unsupported", "unknown"]:
    normalized = fs_type.casefold()
    if normalized in {"ext2", "ext3", "ext4", "btrfs", "ntfs"}:
        return "shrink_and_grow"
    if normalized in {"xfs"}:
        return "grow"
    if normalized in {"vfat", "fat", "fat32", "exfat"}:
        return "unknown"
    return "unknown"


def _read_identity_value(info: os.stat_result, name: str) -> str | None:
    if not stat.S_ISBLK(info.st_mode):
        return None
    major_minor = f"{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}"
    base = Path("/sys/dev/block") / major_minor
    for candidate in (base / f"device/{name}", base / name):
        value = _read_sysfs(candidate)
        if value:
            return value
    return None


def _read_sysfs(path: Path) -> str | None:
    try:
        if path.is_symlink() and not path.exists():
            return None
        value = path.read_text(encoding="utf-8", errors="replace").strip()
        return value[:4096] or None
    except OSError:
        return None


def _int_sysfs(path: Path) -> int | None:
    value = _read_sysfs(path)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _child_names(path: Path) -> tuple[str, ...]:
    try:
        return tuple(sorted(item.name for item in path.iterdir()))[:128]
    except OSError:
        return ()


def _unescape_mount(value: str) -> str:
    return (
        value.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


def _partition_number(raw: object, fallback: int) -> int:
    if isinstance(raw, str):
        match = re.search(r"(?:p)?([0-9]+)$", raw)
        if match is not None:
            value = int(match.group(1))
            if value >= 1:
                return value
    return fallback


def _clean(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = "".join(
        character for character in value if character.isprintable() and character != "\x00"
    )
    cleaned = cleaned.strip()
    return cleaned[:limit] or None


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        converted = int(value)
    except ValueError:
        return None
    return converted if converted > 0 else None


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        converted = int(value)
    except ValueError:
        return None
    return converted if converted >= 0 else None


def _looks_unpartitioned(stderr: str) -> bool:
    lowered = stderr.casefold()
    return (
        "partition table" in lowered or "unrecognized" in lowered or "does not contain" in lowered
    )
