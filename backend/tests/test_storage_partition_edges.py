from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Literal

import pytest

from ares.storage_operations.integrity import (
    canonical_sha256,
    device_identity_fingerprint,
    storage_plan_fingerprint,
)
from ares.storage_operations.models import (
    BootDependency,
    BootImpactAssessment,
    BootImpactLevel,
    DataImpactAssessment,
    DataImpactLevel,
    EncryptionStatus,
    FilesystemResource,
    MountPointResource,
    OperatingSystemResource,
    PartitionResource,
    PartitionRole,
    PartitionTableType,
    PhysicalDisk,
    StorageDeviceIdentity,
    StorageDeviceTopology,
    StorageLayout,
    StorageOperationPlan,
    StorageOperationType,
    StoragePrimitiveOperation,
    StorageVerificationStep,
    StorageWriteGateDecision,
    VolumeKind,
    VolumeResource,
)
from ares.storage_operations.policy import (
    ProductionStorageWriteGate,
    analyze_boot_impact,
    analyze_data_impact,
)
from ares.tools import partition as partition_tools
from ares.tools.partition import (
    BootDependencyTool,
    DiskIdentityTool,
    MountDependencyTool,
    PartitionCreateTool,
    PartitionDeleteTool,
    PartitionMoveTool,
    PartitionResizeTool,
    PartitionTableTool,
    PartitionTableVerifyTool,
    PartitionToolError,
    SafePartitionProcessRunner,
    StoragePartitionToolSuite,
    build_empty_table,
    layout_with_table,
    new_partition_resource,
    parse_blkid_export,
    parse_sfdisk_json,
    render_sfdisk_script,
    with_partitions,
)
from ares.tools.storage import ProcessResult, ToolAvailability


def _identity(
    path: str = "/fixture.img",
    *,
    kind: Literal["regular_file", "loop", "block"] = "regular_file",
    controlled: bool = True,
    backing: str | None = None,
) -> StorageDeviceIdentity:
    draft = StorageDeviceIdentity(
        requested_path=path,
        canonical_path=path,
        device_kind=kind,
        major_minor="file:1:1" if kind == "regular_file" else "7:1",
        model="fixture",
        size_bytes=256 * 1024 * 1024,
        logical_sector_size=512,
        physical_sector_size=512,
        topology=StorageDeviceTopology(backing_file=backing),
        controlled_test_target=controlled,
        fingerprint_sha256="0" * 64,
    )
    return draft.model_copy(update={"fingerprint_sha256": device_identity_fingerprint(draft)})


def _partition(
    identity: StorageDeviceIdentity,
    number: int = 1,
    *,
    role: PartitionRole = PartitionRole.NORMAL,
    filesystem_id: str | None = None,
    mount_ids: tuple[str, ...] = (),
    volume_ids: tuple[str, ...] = (),
    encryption: EncryptionStatus = EncryptionStatus.NONE,
    bootable: bool = False,
) -> PartitionResource:
    return PartitionResource(
        id=f"partition:test:{number}",
        number=number,
        path=(
            f"image:test:{number}"
            if identity.device_kind == "regular_file"
            else f"/dev/loop0p{number}"
        ),
        start_sector=2048 + (number - 1) * 8192,
        end_sector=2048 + (number - 1) * 8192 + 4095,
        size_sectors=4096,
        size_bytes=4096 * 512,
        type_code="83",
        bootable=bootable,
        role=role,
        filesystem_id=filesystem_id,
        mount_point_ids=mount_ids,
        volume_ids=volume_ids,
        encryption_status=encryption,
    )


def _layout(
    identity: StorageDeviceIdentity,
    partitions: tuple[PartitionResource, ...] = (),
    *,
    filesystems: tuple[FilesystemResource, ...] = (),
    mounts: tuple[MountPointResource, ...] = (),
    volumes: tuple[VolumeResource, ...] = (),
    boot: tuple[BootDependency, ...] = (),
) -> StorageLayout:
    table = with_partitions(
        build_empty_table(identity, PartitionTableType.GPT, "fixture-guid"),
        partitions,
    )
    disk = PhysicalDisk(
        id=identity.resource_id,
        identity=identity,
        partition_table_id=f"partition-table:{identity.fingerprint_sha256[:24]}",
    )
    body = {
        "disk": disk.model_dump(mode="json"),
        "partition_table": table.model_dump(mode="json"),
        "filesystems": [item.model_dump(mode="json") for item in filesystems],
        "mount_points": [item.model_dump(mode="json") for item in mounts],
        "volumes": [item.model_dump(mode="json") for item in volumes],
        "boot_dependencies": [item.model_dump(mode="json") for item in boot],
    }
    return StorageLayout(
        disk=disk,
        partition_table=table,
        filesystems=filesystems,
        mount_points=mounts,
        volumes=volumes,
        boot_dependencies=boot,
        evidence_sha256=canonical_sha256(body),
    )


class _Runner:
    def __init__(self) -> None:
        self.available: dict[str, bool] = {"sfdisk": True, "blkid": True, "blockdev": True}
        self.results: dict[str, list[ProcessResult | Exception]] = {}
        self.calls: list[tuple[str, tuple[str, ...], str | None]] = []

    def inspect(self, tool: str) -> ToolAvailability:
        available = self.available.get(tool, False)
        return ToolAvailability(
            tool=tool,
            available=available,
            reason=None if available else "missing",
        )

    async def run(
        self,
        tool: str,
        args: tuple[str, ...],
        *,
        timeout_seconds: float,
        stdin_text: str | None = None,
    ) -> ProcessResult:
        del timeout_seconds
        self.calls.append((tool, args, stdin_text))
        queue = self.results.get(tool, [])
        if queue:
            value = queue.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        return ProcessResult(tool=tool, exit_code=0, stdout="", stderr="", duration_ms=1.0)


def _result(tool: str, *, code: int = 0, stdout: str = "", stderr: str = "") -> ProcessResult:
    return ProcessResult(tool=tool, exit_code=code, stdout=stdout, stderr=stderr, duration_ms=1.0)


def _create_plan(identity: StorageDeviceIdentity) -> StorageOperationPlan:
    original = _layout(identity)
    table = build_empty_table(identity, PartitionTableType.GPT, "guid")
    partition = new_partition_resource(
        identity=identity,
        table_type=PartitionTableType.GPT,
        number=1,
        start_sector=2048,
        size_sectors=4096,
        sector_size=512,
        type_code=None,
        name="P1",
    )
    proposed = layout_with_table(original, with_partitions(table, (partition,)))
    draft = StorageOperationPlan(
        capability="storage.partition.create",
        operation=StorageOperationType.CREATE,
        session_id="edge-session-1234",
        target_disk=identity,
        original_layout=original,
        proposed_layout=proposed,
        required_operations=(
            StoragePrimitiveOperation(
                id="storage.write",
                kind="write_partition_table",
                description="write",
                mutates_target=True,
                enabled=True,
            ),
        ),
        affected_resources=(identity.resource_id,),
        risk="medium",
        data_loss_possible=False,
        data_impact=DataImpactAssessment(level=DataImpactLevel.LOW, executable=True),
        boot_impact=BootImpactAssessment(level=BootImpactLevel.NONE, executable=True),
        filesystem_impact="none",
        verification_plan=(StorageVerificationStep(id="verify.table", description="verify"),),
        write_gate=StorageWriteGateDecision(
            allowed=True,
            target_class="test_image",
            reason="test",
        ),
        executable=True,
        fingerprint_sha256="0" * 64,
    )
    return draft.model_copy(update={"fingerprint_sha256": storage_plan_fingerprint(draft)})


def test_identity_rejects_unsafe_missing_symlink_and_outside_controlled_root(tmp_path: Path) -> None:
    identity = DiskIdentityTool()
    with pytest.raises(PartitionToolError, match="STORAGE_TARGET_INVALID"):
        identity.identify("relative.img")
    with pytest.raises(PartitionToolError, match="STORAGE_TARGET_INVALID"):
        identity.identify("/tmp/bad\x00name")
    with pytest.raises(PartitionToolError, match="STORAGE_TARGET_NOT_FOUND"):
        identity.identify(str(tmp_path / "missing.img"))

    image = tmp_path / "disk.img"
    image.write_bytes(b"\0" * 4096)
    with pytest.raises(PartitionToolError, match="STORAGE_IMAGE_OUTSIDE_CONTROLLED_ROOT"):
        identity.identify(str(image))

    link = tmp_path / "link.img"
    link.symlink_to(image)
    with pytest.raises(PartitionToolError, match="STORAGE_TARGET_SYMLINK_REJECTED"):
        DiskIdentityTool(allow_regular_file_targets=True).identify(str(link))

    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(PartitionToolError, match="STORAGE_TARGET_NOT_DISK_OR_IMAGE"):
        DiskIdentityTool(allow_regular_file_targets=True).identify(str(directory))


def test_identity_controlled_root_and_revalidation_detect_size_change(tmp_path: Path) -> None:
    root = tmp_path / "controlled"
    root.mkdir()
    image = root / "disk.img"
    image.write_bytes(b"\0" * 4096)
    tool = DiskIdentityTool(controlled_image_roots=(root,))
    first = tool.identify(str(image))
    assert first.controlled_test_target is True
    assert tool.revalidate(first) == first
    with image.open("ab") as handle:
        handle.write(b"changed")
    with pytest.raises(PartitionToolError, match="STORAGE_DEVICE_IDENTITY_CHANGED"):
        tool.revalidate(first)


def test_write_gate_covers_image_loop_and_physical_policies(tmp_path: Path) -> None:
    root = tmp_path / "images"
    root.mkdir()
    inside = _identity(str(root / "disk.img"), controlled=True)
    outside = _identity(str(tmp_path / "outside.img"), controlled=False)
    gate = ProductionStorageWriteGate(controlled_image_roots=(root,))
    assert gate.evaluate(inside).allowed is True
    assert gate.evaluate(outside).allowed is False
    assert ProductionStorageWriteGate(test_mode=True).evaluate(outside).allowed is True

    controlled_loop = _identity(
        "/dev/loop0",
        kind="loop",
        backing=str(root / "disk.img"),
    )
    untrusted_loop = _identity(
        "/dev/loop1",
        kind="loop",
        backing=str(tmp_path / "outside.img"),
    )
    assert gate.evaluate(controlled_loop).target_class == "controlled_loop"
    assert gate.evaluate(untrusted_loop).allowed is False
    assert ProductionStorageWriteGate(test_mode=True).evaluate(untrusted_loop).allowed is True

    physical = _identity("/dev/sda", kind="block", controlled=False)
    assert gate.evaluate(physical).target_class == "physical_disk"
    with pytest.raises(PermissionError, match="PRODUCTION_STORAGE_WRITE_GATE_BLOCKED"):
        gate.require_write_allowed(physical)


def test_data_and_boot_impact_cover_all_blocking_dependencies() -> None:
    identity = _identity()
    fs = FilesystemResource(id="filesystem:1", device_path="image:test:1", filesystem_type="ext4")
    mount = MountPointResource(id="mount:1", source="image:test:1", path="/", filesystem_type="ext4")
    lvm = VolumeResource(id="volume:lvm", kind=VolumeKind.LVM_PV, name="pv")
    raid = VolumeResource(id="volume:raid", kind=VolumeKind.MDRAID, name="md")
    target = _partition(
        identity,
        role=PartitionRole.EFI,
        filesystem_id=fs.id,
        mount_ids=(mount.id,),
        volume_ids=(lvm.id, raid.id),
        encryption=EncryptionStatus.LUKS,
    )
    boot = BootDependency(
        id="boot:efi",
        kind="efi",
        partition_number=1,
        reason="EFI dependency",
        critical=True,
    )
    layout = _layout(
        identity,
        (target,),
        filesystems=(fs,),
        mounts=(mount,),
        volumes=(lvm, raid),
        boot=(boot,),
    )

    created = analyze_data_impact(StorageOperationType.CREATE, layout, None)
    assert created.executable is True and created.data_loss_possible is False
    missing = analyze_data_impact(StorageOperationType.DELETE, layout, None)
    assert missing.executable is False
    delete = analyze_data_impact(StorageOperationType.DELETE, layout, target)
    assert delete.executable is False
    assert delete.mounted and delete.lvm_detected and delete.raid_detected
    assert delete.encryption_detected
    assert analyze_data_impact(StorageOperationType.RESIZE, layout, target).executable is False
    assert analyze_data_impact(StorageOperationType.MOVE, layout, target).level.value == "CRITICAL"

    assert analyze_boot_impact(StorageOperationType.CREATE, layout, None).executable is True
    assert analyze_boot_impact(StorageOperationType.DELETE, layout, None).level.value == "UNKNOWN"
    critical = analyze_boot_impact(StorageOperationType.DELETE, layout, target)
    assert critical.level.value == "CRITICAL" and critical.affected_dependencies == (boot.id,)
    recovery = target.model_copy(update={"role": PartitionRole.RECOVERY})
    no_boot = _layout(identity, (recovery,))
    assert analyze_boot_impact(StorageOperationType.DELETE, no_boot, recovery).level.value == "LIKELY"
    normal = target.model_copy(
        update={
            "role": PartitionRole.NORMAL,
            "filesystem_id": None,
            "mount_point_ids": (),
            "volume_ids": (),
            "encryption_status": EncryptionStatus.NONE,
        }
    )
    plain = _layout(identity, (normal,))
    assert analyze_boot_impact(StorageOperationType.MOVE, plain, normal).level.value == "UNKNOWN"
    assert analyze_boot_impact(StorageOperationType.RESIZE, plain, normal).level.value == "POSSIBLE"
    assert analyze_boot_impact(StorageOperationType.DELETE, plain, normal).executable is True


def test_sfdisk_parser_handles_unknown_defaults_invalid_and_skipped_entries() -> None:
    identity = _identity()
    unknown = parse_sfdisk_json(
        json.dumps(
            {
                "partitiontable": {
                    "label": "sun",
                    "partitions": [
                        "bad",
                        {"node": "bad", "start": "no", "size": 10},
                        {"node": "/dev/sda7", "start": "1", "size": "10", "type": "83"},
                    ],
                }
            }
        ),
        identity,
    )
    assert unknown.type is PartitionTableType.UNKNOWN
    assert unknown.partitions[0].number == 7
    assert unknown.first_usable_sector == 2048

    with pytest.raises(PartitionToolError, match="STORAGE_PARTITION_TABLE_PARSE_FAILED"):
        parse_sfdisk_json("not-json", identity)
    with pytest.raises(PartitionToolError, match="STORAGE_PARTITION_TABLE_PARSE_FAILED"):
        parse_sfdisk_json("[]", identity)

    exported = parse_blkid_export("TYPE=ext4\nBAD=value\nUUID=abc\nline-without-equals\n")
    assert exported == {"TYPE": "ext4", "UUID": "abc"}


def test_partition_roles_and_script_validation_cover_gpt_mbr_types() -> None:
    identity = _identity()
    gpt_types = {
        "C12A7328-F81F-11D2-BA4B-00A0C93EC93B": PartitionRole.EFI,
        "0657FD6D-A4AB-43C4-84E5-0933C84B4F4F": PartitionRole.SWAP,
        "E6D6D379-F507-44C2-A23C-238F2A3DF928": PartitionRole.LVM,
        "A19D880F-05FC-4D3B-A006-743F0F84911E": PartitionRole.RAID,
        "DE94BBA4-06D1-4D40-A16A-BFD50179D6AC": PartitionRole.RECOVERY,
        "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7": PartitionRole.WINDOWS,
    }
    for index, (type_code, role) in enumerate(gpt_types.items(), start=1):
        resource = new_partition_resource(
            identity=identity,
            table_type=PartitionTableType.GPT,
            number=index,
            start_sector=2048 + index * 8192,
            size_sectors=4096,
            sector_size=512,
            type_code=type_code,
            name=f"P{index}",
        )
        assert resource.role is role

    mbr_payload = {
        "partitiontable": {
            "label": "dos",
            "sectorsize": 512,
            "partitions": [
                {"node": "/dev/sda1", "start": 2048, "size": 10, "type": "ef"},
                {"node": "/dev/sda2", "start": 4096, "size": 10, "type": "82"},
                {"node": "/dev/sda3", "start": 8192, "size": 10, "type": "8e"},
                {"node": "/dev/sda4", "start": 12288, "size": 10, "type": "fd"},
                {
                    "node": "/dev/sda5",
                    "start": 16384,
                    "size": 10,
                    "type": "83",
                    "bootable": True,
                },
            ],
        }
    }
    roles = [item.role for item in parse_sfdisk_json(json.dumps(mbr_payload), identity).partitions]
    assert roles == [
        PartitionRole.EFI,
        PartitionRole.SWAP,
        PartitionRole.LVM,
        PartitionRole.RAID,
        PartitionRole.BOOT,
    ]

    table = build_empty_table(identity, PartitionTableType.MBR, "mbr-id")
    p1 = new_partition_resource(
        identity=identity,
        table_type=PartitionTableType.MBR,
        number=1,
        start_sector=2048,
        size_sectors=4096,
        sector_size=512,
        type_code=None,
        name=None,
    )
    script = render_sfdisk_script(with_partitions(table, (p1,)), identity)
    assert "label: dos" in script and "type=83" in script
    unknown = table.model_copy(update={"type": PartitionTableType.UNKNOWN})
    with pytest.raises(PartitionToolError, match="STORAGE_PARTITION_TABLE_TYPE_REQUIRED"):
        render_sfdisk_script(unknown, identity)
    with pytest.raises(PartitionToolError, match="STORAGE_PARTITION_TABLE_TYPE_REQUIRED"):
        build_empty_table(identity, PartitionTableType.UNKNOWN, "unknown")


def test_layout_helpers_free_regions_and_dependency_tools() -> None:
    identity = _identity()
    table = build_empty_table(identity, PartitionTableType.GPT, "guid")
    p2 = new_partition_resource(
        identity=identity,
        table_type=PartitionTableType.GPT,
        number=2,
        start_sector=20_000,
        size_sectors=1000,
        sector_size=512,
        type_code=None,
        name="SECOND",
    )
    p1 = new_partition_resource(
        identity=identity,
        table_type=PartitionTableType.GPT,
        number=1,
        start_sector=4096,
        size_sectors=1000,
        sector_size=512,
        type_code=None,
        name="FIRST",
    )
    combined = with_partitions(table, (p2, p1))
    assert combined.partitions[0].number == 1
    assert len(combined.free_regions) >= 2
    proposed = layout_with_table(_layout(identity), combined)
    assert proposed.warnings

    mount = MountPointResource(
        id="mount:x", source="/dev/x", path="/mnt", filesystem_type="ext4"
    )
    deps_layout = proposed.model_copy(update={"mount_points": (mount,), "swap_partitions": (3,)})
    assert MountDependencyTool().inspect(deps_layout) == ("/mnt", "swap:3")
    dependency = BootDependency(id="boot:x", kind="root", reason="root", critical=True)
    deps_layout = deps_layout.model_copy(update={"boot_dependencies": (dependency,)})
    assert BootDependencyTool().inspect(deps_layout) == (dependency,)


async def test_partition_table_tool_error_paths_dump_and_probe(tmp_path: Path) -> None:
    image = tmp_path / "image.img"
    image.write_bytes(b"\0" * 8192)
    identity_tool = DiskIdentityTool(allow_regular_file_targets=True)
    runner = _Runner()
    runner.available["sfdisk"] = False
    table_tool = PartitionTableTool(runner, identity_tool)
    with pytest.raises(PartitionToolError, match="SFDISK_UNAVAILABLE"):
        await table_tool.inspect(str(image))

    runner.available["sfdisk"] = True
    runner.results["sfdisk"] = [
        _result("sfdisk", code=1, stderr="does not contain partition table")
    ]
    layout = await table_tool.inspect(str(image))
    assert layout.partition_table.type is PartitionTableType.UNKNOWN

    runner.results["sfdisk"] = [_result("sfdisk", code=1, stderr="permission denied")]
    with pytest.raises(PartitionToolError, match="STORAGE_PARTITION_TABLE_INSPECTION_FAILED"):
        await table_tool.inspect(str(image))

    expected = identity_tool.identify(str(image))
    runner.results["sfdisk"] = [
        _result("sfdisk", code=1, stderr="unrecognized partition table")
    ]
    assert await table_tool.dump(expected) == ""
    runner.results["sfdisk"] = [_result("sfdisk", code=1, stderr="bad io")]
    with pytest.raises(PartitionToolError, match="STORAGE_PARTITION_TABLE_DUMP_FAILED"):
        await table_tool.dump(expected)
    runner.results["sfdisk"] = [TimeoutError()]
    with pytest.raises(PartitionToolError, match="STORAGE_TOOL_TIMEOUT"):
        await table_tool.dump(expected)
    runner.results["sfdisk"] = [ValueError("bad")]
    with pytest.raises(PartitionToolError, match="STORAGE_TOOL_FAILED"):
        await table_tool.dump(expected)

    table = build_empty_table(expected, PartitionTableType.GPT, "guid")
    partition = new_partition_resource(
        identity=expected,
        table_type=PartitionTableType.GPT,
        number=1,
        start_sector=2048,
        size_sectors=4096,
        sector_size=512,
        type_code=None,
        name="P1",
    )
    runner.results["blkid"] = [
        _result("blkid", code=0, stdout="TYPE=ext4\nUUID=test-uuid\n")
    ]
    probe = await table_tool._probe_partition(expected, partition)
    assert probe["TYPE"] == "ext4"
    runner.results["blkid"] = [_result("blkid", code=3, stdout="TYPE=ext4")]
    assert await table_tool._probe_partition(expected, partition) == {}
    runner.available["blkid"] = False
    assert await table_tool._probe_partition(expected, partition) == {}
    assert table.type is PartitionTableType.GPT


async def test_enrichment_classifies_crypto_lvm_raid_swap_filesystem_and_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity()
    table = build_empty_table(identity, PartitionTableType.GPT, "guid")
    partitions = tuple(
        new_partition_resource(
            identity=identity,
            table_type=PartitionTableType.GPT,
            number=index,
            start_sector=2048 + (index - 1) * 8192,
            size_sectors=4096,
            sector_size=512,
            type_code=(
                "C12A7328-F81F-11D2-BA4B-00A0C93EC93B" if index == 6 else None
            ),
            name=f"P{index}",
        )
        for index in range(1, 7)
    )
    table = with_partitions(table, partitions)
    runner = _Runner()
    tool = PartitionTableTool(runner, DiskIdentityTool(allow_regular_file_targets=True))
    probes = {
        1: {"TYPE": "crypto_LUKS", "LABEL": "crypt"},
        2: {"TYPE": "BitLocker", "LABEL": "win"},
        3: {"TYPE": "LVM2_member", "UUID": "pv-uuid"},
        4: {"TYPE": "linux_raid_member", "USAGE": "raid", "UUID": "md-uuid"},
        5: {"TYPE": "swap"},
        6: {"TYPE": "ext4", "UUID": "root-uuid", "LABEL": "root"},
    }

    async def probe(
        current_identity: StorageDeviceIdentity, partition: PartitionResource
    ) -> dict[str, str]:
        del current_identity
        return probes[partition.number]

    monkeypatch.setattr(tool, "_probe_partition", probe)
    monkeypatch.setattr(
        partition_tools,
        "_mount_records",
        lambda: (
            MountPointResource(
                id="raw:1",
                source=partitions[5].path,
                path="/",
                filesystem_type="ext4",
                options=("rw",),
            ),
            MountPointResource(
                id="raw:2",
                source=partitions[5].path,
                path="/boot/efi",
                filesystem_type="vfat",
                options=("rw",),
            ),
        ),
    )
    monkeypatch.setattr(
        partition_tools,
        "_swap_sources",
        lambda: frozenset({partitions[4].path}),
    )
    monkeypatch.setattr(
        partition_tools,
        "_detect_os",
        lambda source, mount: (
            OperatingSystemResource(
                id="os:fixture",
                name="Fixture Linux",
                version="1",
                source=source,
                mount_point=mount,
            )
            if mount == "/"
            else None
        ),
    )
    layout = await tool._enrich(identity, table)
    assert {item.kind for item in layout.volumes} >= {
        VolumeKind.CRYPT,
        VolumeKind.LVM_PV,
        VolumeKind.MDRAID,
    }
    assert layout.partition_table.partitions[0].encryption_status is EncryptionStatus.LUKS
    assert layout.partition_table.partitions[1].encryption_status is EncryptionStatus.BITLOCKER
    assert layout.partition_table.partitions[2].role is PartitionRole.LVM
    assert layout.partition_table.partitions[3].role is PartitionRole.RAID
    assert layout.partition_table.partitions[4].role is PartitionRole.SWAP
    assert layout.filesystems[0].filesystem_type == "ext4"
    assert layout.operating_systems and len(layout.boot_dependencies) >= 3
    assert 5 in layout.swap_partitions


async def test_verify_mutation_dry_run_and_kernel_reread_error_paths(tmp_path: Path) -> None:
    image = tmp_path / "tool.img"
    with image.open("wb") as handle:
        handle.truncate(64 * 1024 * 1024)
    runner = _Runner()
    tools = StoragePartitionToolSuite(runner=runner, allow_regular_file_targets=True)
    identity = tools.identity.identify(str(image))
    plan = _create_plan(identity)

    with pytest.raises(PartitionToolError, match="STORAGE_OPERATION_TYPE_MISMATCH"):
        await PartitionDeleteTool(runner).execute(plan)
    delete_plan = plan.model_copy(
        update={"operation": StorageOperationType.DELETE, "capability": "storage.partition.delete"}
    )
    with pytest.raises(PartitionToolError, match="STORAGE_OPERATION_TYPE_MISMATCH"):
        await PartitionCreateTool(runner).execute(delete_plan)
    with pytest.raises(PartitionToolError, match="STORAGE_PARTITION_RESIZE_DISABLED"):
        await PartitionResizeTool().execute(plan)
    with pytest.raises(PartitionToolError, match="STORAGE_PARTITION_MOVE_DISABLED"):
        await PartitionMoveTool().execute(plan)

    runner.results["sfdisk"] = [TimeoutError()]
    with pytest.raises(PartitionToolError, match="STORAGE_DRY_RUN_TIMEOUT"):
        await tools.dry_run(plan)
    runner.results["sfdisk"] = [OSError("bad")]
    with pytest.raises(PartitionToolError, match="STORAGE_DRY_RUN_FAILED"):
        await tools.dry_run(plan)

    runner.available["blockdev"] = False
    loop_identity = identity.model_copy(
        update={
            "device_kind": "loop",
            "canonical_path": "/dev/loop0",
            "requested_path": "/dev/loop0",
        }
    )
    with pytest.raises(PartitionToolError, match="BLOCKDEV_UNAVAILABLE"):
        await tools._reread_kernel(loop_identity)
    runner.available["blockdev"] = True
    runner.results["blockdev"] = [_result("blockdev", code=1)]
    with pytest.raises(PartitionToolError, match="STORAGE_KERNEL_REREAD_FAILED"):
        await tools._reread_kernel(loop_identity)
    runner.results["blockdev"] = [TimeoutError()]
    with pytest.raises(PartitionToolError, match="STORAGE_KERNEL_REREAD_FAILED"):
        await tools._reread_kernel(loop_identity)

    verifier = PartitionTableVerifyTool(tools.table, runner)
    runner.results["sfdisk"] = [
        _result(
            "sfdisk",
            stdout=json.dumps(
                {
                    "partitiontable": {
                        "label": "gpt",
                        "sectorsize": 512,
                        "firstlba": plan.proposed_layout.partition_table.first_usable_sector,
                        "lastlba": plan.proposed_layout.partition_table.last_usable_sector,
                        "partitions": [],
                    }
                }
            ),
        )
    ]
    with pytest.raises(PartitionToolError, match="STORAGE_PARTITION_GEOMETRY_MISMATCH"):
        await verifier.verify(plan)


def test_os_detection_resize_support_mount_unescape_and_numeric_helpers(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "etc").mkdir(parents=True)
    (root / "etc/os-release").write_text(
        'NAME="Fixture"\nPRETTY_NAME="Fixture Linux"\nVERSION_ID="42"\n',
        encoding="utf-8",
    )
    detected = partition_tools._detect_os("/dev/test", str(root))
    assert detected is not None
    assert detected.name == "Fixture Linux" and detected.version == "42"
    (root / "etc/os-release").write_text("NO_NAME=value\n", encoding="utf-8")
    assert partition_tools._detect_os("/dev/test", str(root)) is None
    (root / "etc/os-release").unlink()
    assert partition_tools._detect_os("/dev/test", str(root)) is None

    assert partition_tools._filesystem_resize_support("ext4") == "shrink_and_grow"
    assert partition_tools._filesystem_resize_support("xfs") == "grow"
    assert partition_tools._filesystem_resize_support("vfat") == "unknown"
    assert partition_tools._filesystem_resize_support("mystery") == "unknown"
    assert partition_tools._unescape_mount("/a\\040b\\011c\\012d\\134e") == "/a b\tc\nd\\e"
    assert partition_tools._partition_number("/dev/nvme0n1p12", 1) == 12
    assert partition_tools._partition_number("invalid", 4) == 4
    assert partition_tools._clean("  abc\x00\n", 10) == "abc"
    assert partition_tools._clean(123, 10) is None
    assert partition_tools._positive_int("2") == 2
    assert partition_tools._positive_int(0) is None
    assert partition_tools._positive_int(True) is None
    assert partition_tools._positive_int("bad") is None
    assert partition_tools._nonnegative_int("0") == 0
    assert partition_tools._nonnegative_int(-1) is None
    assert partition_tools._nonnegative_int(False) is None
    assert partition_tools._nonnegative_int("bad") is None
    assert partition_tools._looks_unpartitioned("unrecognized partition table") is True
    assert partition_tools._looks_unpartitioned("permission denied") is False


def test_safe_runner_reports_missing_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(partition_tools.shutil, "which", lambda _: None)
    state = SafePartitionProcessRunner().inspect("definitely-missing")
    assert state.available is False and state.reason == "tool_not_installed"
