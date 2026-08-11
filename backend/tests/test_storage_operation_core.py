from __future__ import annotations

from pathlib import Path
from typing import Literal

from ares.storage_operations.models import (
    BootDependency,
    EncryptionStatus,
    PartitionResource,
    PartitionRole,
    PartitionTable,
    PartitionTableType,
    PhysicalDisk,
    StorageDeviceIdentity,
    StorageLayout,
    StorageOperationType,
    StorageTransaction,
    StorageTransactionStatus,
    VolumeKind,
    VolumeResource,
)
from ares.storage_operations.policy import (
    ProductionStorageWriteGate,
    analyze_boot_impact,
    analyze_data_impact,
)
from ares.storage_operations.store import StorageOperationStore


def _identity(
    path: str,
    *,
    kind: Literal["regular_file", "loop", "block"] = "regular_file",
    controlled: bool = True,
) -> StorageDeviceIdentity:
    return StorageDeviceIdentity(
        requested_path=path,
        canonical_path=path,
        device_kind=kind,
        major_minor="file:1:1" if kind == "regular_file" else "8:0",
        size_bytes=128 * 1024 * 1024,
        logical_sector_size=512,
        physical_sector_size=4096,
        controlled_test_target=controlled,
        fingerprint_sha256="a" * 64,
    )


def _partition(*, role: PartitionRole = PartitionRole.NORMAL) -> PartitionResource:
    return PartitionResource(
        id="partition:test:1",
        number=1,
        path="/fixture.img1",
        start_sector=2048,
        end_sector=4095,
        size_sectors=2048,
        size_bytes=1024 * 1024,
        role=role,
        encryption_status=EncryptionStatus.UNKNOWN,
    )


def _layout(
    partition: PartitionResource,
    *,
    volumes: tuple[VolumeResource, ...] = (),
    boot: tuple[BootDependency, ...] = (),
) -> StorageLayout:
    identity = _identity("/fixture.img")
    table = PartitionTable(
        type=PartitionTableType.GPT,
        guid="fixture-guid",
        sector_size=512,
        first_usable_sector=34,
        last_usable_sector=262110,
        total_sectors=262144,
        partitions=(partition,),
        fingerprint_sha256="b" * 64,
    )
    return StorageLayout(
        disk=PhysicalDisk(
            id=identity.resource_id,
            identity=identity,
            partition_table_id="partition-table:test",
        ),
        partition_table=table,
        volumes=volumes,
        boot_dependencies=boot,
        evidence_sha256="c" * 64,
    )


def test_production_storage_write_gate_blocks_physical_disk_and_allows_test_image(
    tmp_path: Path,
) -> None:
    gate = ProductionStorageWriteGate(test_mode=True)
    image = _identity(str(tmp_path / "disk.img"))
    physical = _identity("/dev/sda", kind="block", controlled=False)

    assert gate.evaluate(image).allowed is True
    blocked = gate.evaluate(physical)
    assert blocked.allowed is False
    assert blocked.target_class == "physical_disk"
    assert blocked.physical_disk_writes_enabled is False


def test_delete_impact_blocks_lvm_raid_encryption_and_boot() -> None:
    target = _partition(role=PartitionRole.EFI).model_copy(
        update={"volume_ids": ("volume:lvm",), "encryption_status": EncryptionStatus.LUKS}
    )
    layout = _layout(
        target,
        volumes=(
            VolumeResource(
                id="volume:lvm",
                kind=VolumeKind.LVM_PV,
                name="fixture-pv",
                member_paths=(target.path,),
            ),
        ),
        boot=(
            BootDependency(
                id="boot:efi:test",
                kind="efi-system-partition",
                partition_number=1,
                reason="fixture boot dependency",
                critical=True,
            ),
        ),
    )
    data = analyze_data_impact(StorageOperationType.DELETE, layout, target)
    boot = analyze_boot_impact(StorageOperationType.DELETE, layout, target)

    assert data.executable is False
    assert data.lvm_detected is True
    assert data.encryption_detected is True
    assert boot.executable is False
    assert boot.level.value == "CRITICAL"


def test_resize_and_move_are_modeled_but_never_executable() -> None:
    target = _partition()
    layout = _layout(target)
    resize = analyze_data_impact(StorageOperationType.RESIZE, layout, target)
    move = analyze_data_impact(StorageOperationType.MOVE, layout, target)
    assert resize.executable is False
    assert move.executable is False
    assert move.level.value == "CRITICAL"


def test_store_reconciles_active_write_to_unknown_and_authorized_to_aborted(tmp_path: Path) -> None:
    store = StorageOperationStore(tmp_path / "storage")
    store.prepare()
    executing = StorageTransaction(
        id="operation-executing",
        operation_id="operation-executing",
        plan_id="plan-executing",
        session_id="session-12345678",
        status=StorageTransactionStatus.EXECUTING,
    )
    authorized = StorageTransaction(
        id="operation-authorized",
        operation_id="operation-authorized",
        plan_id="plan-authorized",
        session_id="session-12345678",
        status=StorageTransactionStatus.AUTHORIZED,
    )
    # Use the store's durable writer through the public method before simulating restart.
    import asyncio

    asyncio.run(store.put_transaction(executing))
    asyncio.run(store.put_transaction(authorized))
    reloaded = StorageOperationStore(tmp_path / "storage")
    reloaded.prepare()
    unknown = asyncio.run(reloaded.get_transaction(executing.id))
    aborted = asyncio.run(reloaded.get_transaction(authorized.id))
    assert unknown is not None and unknown.status is StorageTransactionStatus.UNKNOWN
    assert unknown.reconciliation_required is True
    assert aborted is not None and aborted.status is StorageTransactionStatus.ABORTED
