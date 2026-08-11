"""Deterministic safety policy for partition operations."""

from __future__ import annotations

from pathlib import Path

from ares.storage_operations.models import (
    BootImpactAssessment,
    BootImpactLevel,
    DataImpactAssessment,
    DataImpactLevel,
    EncryptionStatus,
    PartitionResource,
    PartitionRole,
    StorageDeviceIdentity,
    StorageLayout,
    StorageOperationType,
    StorageWriteGateDecision,
    VolumeKind,
)


class ProductionStorageWriteGate:
    """Hard gate that keeps physical-disk writes disabled for this increment."""

    def __init__(
        self,
        *,
        test_mode: bool = False,
        controlled_image_roots: tuple[Path, ...] = (Path("/var/lib/ares/storage-images"),),
    ) -> None:
        self.test_mode = test_mode
        self.controlled_image_roots = tuple(
            root.resolve(strict=False) for root in controlled_image_roots
        )

    def evaluate(self, identity: StorageDeviceIdentity) -> StorageWriteGateDecision:
        if identity.device_kind == "regular_file":
            if self.test_mode:
                return StorageWriteGateDecision(
                    allowed=True,
                    target_class="test_image",
                    reason="Regular-file image explicitly allowed by the test environment.",
                )
            if _under_controlled_root(identity.canonical_path, self.controlled_image_roots):
                return StorageWriteGateDecision(
                    allowed=True,
                    target_class="test_image",
                    reason="Disk image is inside the root-owned controlled image workspace.",
                )
            return StorageWriteGateDecision(
                allowed=False,
                target_class="unknown",
                reason="Regular-file target is outside the controlled storage-image workspace.",
            )
        if identity.device_kind == "loop":
            backing = identity.topology.backing_file
            if self.test_mode or (
                backing is not None and _under_controlled_root(backing, self.controlled_image_roots)
            ):
                return StorageWriteGateDecision(
                    allowed=True,
                    target_class="controlled_loop",
                    reason="Loop device is bound to an explicitly controlled test image.",
                )
            return StorageWriteGateDecision(
                allowed=False,
                target_class="unknown",
                reason="Loop device backing file is not an approved controlled image.",
            )
        return StorageWriteGateDecision(
            allowed=False,
            target_class="physical_disk",
            reason=(
                "Physical-disk partition writes are disabled by ProductionStorageWriteGate "
                "for this increment, regardless of caller or channel."
            ),
        )

    def require_write_allowed(self, identity: StorageDeviceIdentity) -> StorageWriteGateDecision:
        decision = self.evaluate(identity)
        if not decision.allowed:
            raise PermissionError("PRODUCTION_STORAGE_WRITE_GATE_BLOCKED")
        return decision


def analyze_data_impact(
    operation: StorageOperationType,
    layout: StorageLayout,
    target: PartitionResource | None,
) -> DataImpactAssessment:
    if operation is StorageOperationType.CREATE:
        return DataImpactAssessment(
            level=DataImpactLevel.LOW,
            reasons=("Partition table metadata will change only inside verified free space.",),
            data_loss_possible=False,
            executable=True,
        )
    if operation in {StorageOperationType.RESIZE, StorageOperationType.MOVE}:
        return DataImpactAssessment(
            level=(
                DataImpactLevel.CRITICAL
                if operation is StorageOperationType.MOVE
                else DataImpactLevel.UNKNOWN
            ),
            affected_resources=(target.id,) if target is not None else (),
            reasons=(
                (
                    "Partition resize/move requires filesystem-aware data movement policy "
                    "that is not enabled."
                ),
            ),
            filesystem_change_required=True,
            data_loss_possible=True,
            executable=False,
        )
    if target is None:
        return DataImpactAssessment(
            level=DataImpactLevel.UNKNOWN,
            reasons=("Target partition evidence is missing.",),
            data_loss_possible=True,
            executable=False,
        )

    filesystems = {item.id for item in layout.filesystems}
    mounts = {item.id for item in layout.mount_points}
    volumes = {item.id: item for item in layout.volumes}
    has_filesystem = target.filesystem_id is not None and target.filesystem_id in filesystems
    mounted = any(item in mounts for item in target.mount_point_ids)
    member_volumes = tuple(volumes[item] for item in target.volume_ids if item in volumes)
    lvm = any(
        item.kind in {VolumeKind.LVM_PV, VolumeKind.LVM_VG, VolumeKind.LVM_LV}
        for item in member_volumes
    )
    raid = any(
        item.kind in {VolumeKind.MDRAID, VolumeKind.HARDWARE_RAID} for item in member_volumes
    )
    encrypted = target.encryption_status not in {EncryptionStatus.NONE, EncryptionStatus.UNKNOWN}
    reasons: list[str] = [
        "Deleting a partition removes its table entry and can make contained data inaccessible."
    ]
    if has_filesystem:
        reasons.append("A filesystem is detected on the target partition.")
    if mounted:
        reasons.append("The target partition is mounted.")
    if lvm:
        reasons.append("LVM membership is detected and LVM mutation is unsupported.")
    if raid:
        reasons.append("RAID membership is detected and RAID mutation is unsupported.")
    if encrypted:
        reasons.append("Encryption is detected and encrypted-volume mutation is unsupported.")
    if target.role in {
        PartitionRole.EFI,
        PartitionRole.BOOT,
        PartitionRole.RECOVERY,
        PartitionRole.SWAP,
    }:
        reasons.append(f"Partition has protected role {target.role.value}.")
    blocking = bool(
        has_filesystem
        or mounted
        or lvm
        or raid
        or encrypted
        or target.role
        in {PartitionRole.EFI, PartitionRole.BOOT, PartitionRole.RECOVERY, PartitionRole.SWAP}
    )
    return DataImpactAssessment(
        level=DataImpactLevel.CRITICAL if blocking else DataImpactLevel.MEDIUM,
        affected_resources=(target.id,),
        reasons=tuple(reasons),
        mounted=mounted,
        filesystem_change_required=has_filesystem,
        lvm_detected=lvm,
        raid_detected=raid,
        encryption_detected=encrypted,
        data_loss_possible=True,
        executable=not blocking,
    )


def analyze_boot_impact(
    operation: StorageOperationType,
    layout: StorageLayout,
    target: PartitionResource | None,
) -> BootImpactAssessment:
    if operation is StorageOperationType.CREATE:
        return BootImpactAssessment(
            level=BootImpactLevel.NONE,
            reasons=("New partition is confined to verified free space.",),
            recovery_strategy_available=True,
            executable=True,
        )
    if target is None:
        return BootImpactAssessment(
            level=BootImpactLevel.UNKNOWN,
            reasons=("Target partition evidence is missing.",),
            executable=False,
        )
    related = tuple(
        item for item in layout.boot_dependencies if item.partition_number == target.number
    )
    if related or target.role in {PartitionRole.EFI, PartitionRole.BOOT}:
        return BootImpactAssessment(
            level=BootImpactLevel.CRITICAL,
            affected_dependencies=tuple(item.id for item in related),
            reasons=tuple(item.reason for item in related)
            or ("Target partition is classified as a boot-critical partition.",),
            recovery_strategy_available=False,
            executable=False,
        )
    if target.role is PartitionRole.RECOVERY:
        return BootImpactAssessment(
            level=BootImpactLevel.LIKELY,
            reasons=("Recovery partition modification can remove a boot/recovery path.",),
            recovery_strategy_available=False,
            executable=False,
        )
    if operation is StorageOperationType.MOVE:
        return BootImpactAssessment(
            level=BootImpactLevel.UNKNOWN,
            reasons=("Move can invalidate boot references; implementation is disabled.",),
            recovery_strategy_available=False,
            executable=False,
        )
    if operation is StorageOperationType.RESIZE:
        return BootImpactAssessment(
            level=BootImpactLevel.POSSIBLE,
            reasons=(
                "Resize requires filesystem and boot-reference verification not enabled yet.",
            ),
            recovery_strategy_available=False,
            executable=False,
        )
    return BootImpactAssessment(
        level=BootImpactLevel.NONE,
        reasons=("No boot dependency is associated with the target partition.",),
        recovery_strategy_available=True,
        executable=True,
    )


def _under_controlled_root(value: str, roots: tuple[Path, ...]) -> bool:
    candidate = Path(value).resolve(strict=False)
    return any(candidate == root or root in candidate.parents for root in roots)
