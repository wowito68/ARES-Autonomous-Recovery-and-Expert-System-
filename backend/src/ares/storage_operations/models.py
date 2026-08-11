"""Typed contracts for declarative partition operations and durable transactions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from ares.protection import ProtectionCheckpoint


def utc_now() -> datetime:
    return datetime.now(UTC)


class PartitionTableType(StrEnum):
    GPT = "GPT"
    MBR = "MBR"
    UNKNOWN = "UNKNOWN"


class StorageOperationType(StrEnum):
    INSPECT = "inspect"
    CREATE = "create"
    DELETE = "delete"
    RESIZE = "resize"
    MOVE = "move"


class StorageTransactionStatus(StrEnum):
    PLANNED = "PLANNED"
    VALIDATED = "VALIDATED"
    PROTECTED = "PROTECTED"
    AUTHORIZED = "AUTHORIZED"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    COMMITTED = "COMMITTED"
    FAILED = "FAILED"
    ABORTED = "ABORTED"
    UNKNOWN = "UNKNOWN"


class StorageVerificationStatus(StrEnum):
    VERIFIED = "VERIFIED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class DataImpactLevel(StrEnum):
    NONE = "NONE"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"
    UNKNOWN = "UNKNOWN"


class BootImpactLevel(StrEnum):
    NONE = "NONE"
    POSSIBLE = "POSSIBLE"
    LIKELY = "LIKELY"
    CRITICAL = "CRITICAL"
    UNKNOWN = "UNKNOWN"


class EncryptionStatus(StrEnum):
    NONE = "NONE"
    LUKS = "LUKS"
    BITLOCKER = "BITLOCKER"
    UNKNOWN = "UNKNOWN"


class PartitionRole(StrEnum):
    NORMAL = "NORMAL"
    EFI = "EFI"
    BOOT = "BOOT"
    RECOVERY = "RECOVERY"
    SWAP = "SWAP"
    WINDOWS = "WINDOWS"
    LINUX = "LINUX"
    LVM = "LVM"
    RAID = "RAID"
    UNKNOWN = "UNKNOWN"


class VolumeKind(StrEnum):
    LVM_PV = "LVM_PV"
    LVM_VG = "LVM_VG"
    LVM_LV = "LVM_LV"
    MDRAID = "MDRAID"
    HARDWARE_RAID = "HARDWARE_RAID"
    CRYPT = "CRYPT"


class StorageDeviceTopology(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    parent_path: str | None = Field(default=None, max_length=4096)
    backing_file: str | None = Field(default=None, max_length=4096)
    holders: tuple[str, ...] = ()
    slaves: tuple[str, ...] = ()


class StorageDeviceIdentity(BaseModel):
    """Multi-attribute identity bound to a storage operation plan."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    requested_path: str = Field(min_length=1, max_length=4096)
    canonical_path: str = Field(min_length=1, max_length=4096)
    device_kind: Literal["regular_file", "loop", "block"]
    major_minor: str | None = Field(default=None, max_length=64)
    serial_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    wwn_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    model: str | None = Field(default=None, max_length=256)
    size_bytes: int = Field(ge=0)
    logical_sector_size: int = Field(ge=512, le=65536)
    physical_sector_size: int = Field(ge=512, le=65536)
    topology: StorageDeviceTopology = Field(default_factory=StorageDeviceTopology)
    read_only: bool = False
    removable: bool = False
    controlled_test_target: bool = False
    fingerprint_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @property
    def resource_id(self) -> str:
        return f"disk:{self.fingerprint_sha256}"


class FreeRegion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    start_sector: int = Field(ge=0)
    end_sector: int = Field(ge=0)
    size_sectors: int = Field(ge=0)
    size_bytes: int = Field(ge=0)


class FilesystemResource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=3, max_length=192)
    device_path: str = Field(min_length=1, max_length=4096)
    filesystem_type: str | None = Field(default=None, max_length=64)
    uuid: str | None = Field(default=None, max_length=256)
    label: str | None = Field(default=None, max_length=256)
    total_bytes: int | None = Field(default=None, ge=0)
    used_bytes: int | None = Field(default=None, ge=0)
    available_bytes: int | None = Field(default=None, ge=0)
    resize_support: Literal["grow", "shrink_and_grow", "unsupported", "unknown"] = "unknown"


class MountPointResource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=3, max_length=192)
    source: str = Field(min_length=1, max_length=4096)
    path: str = Field(min_length=1, max_length=4096)
    filesystem_type: str | None = Field(default=None, max_length=64)
    options: tuple[str, ...] = ()


class VolumeResource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=3, max_length=192)
    kind: VolumeKind
    name: str = Field(min_length=1, max_length=256)
    member_paths: tuple[str, ...] = ()
    mutable_by_storage_engine: Literal[False] = False


class OperatingSystemResource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=3, max_length=192)
    name: str = Field(min_length=1, max_length=256)
    version: str | None = Field(default=None, max_length=256)
    source: str = Field(min_length=1, max_length=4096)
    mount_point: str = Field(min_length=1, max_length=4096)


class BootDependency(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=3, max_length=192)
    kind: str = Field(min_length=1, max_length=96)
    partition_number: int | None = Field(default=None, ge=1)
    resource_path: str | None = Field(default=None, max_length=4096)
    reason: str = Field(min_length=1, max_length=512)
    critical: bool = False


class PartitionResource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=3, max_length=192)
    number: int = Field(ge=1)
    path: str = Field(min_length=1, max_length=4096)
    start_sector: int = Field(ge=0)
    end_sector: int = Field(ge=0)
    size_sectors: int = Field(gt=0)
    size_bytes: int = Field(gt=0)
    type_code: str | None = Field(default=None, max_length=128)
    partuuid: str | None = Field(default=None, max_length=256)
    name: str | None = Field(default=None, max_length=256)
    bootable: bool = False
    attrs: str | None = Field(default=None, max_length=256)
    role: PartitionRole = PartitionRole.UNKNOWN
    filesystem_id: str | None = Field(default=None, max_length=192)
    mount_point_ids: tuple[str, ...] = ()
    operating_system_ids: tuple[str, ...] = ()
    volume_ids: tuple[str, ...] = ()
    encryption_status: EncryptionStatus = EncryptionStatus.UNKNOWN


class PartitionTable(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: PartitionTableType
    guid: str | None = Field(default=None, max_length=256)
    sector_size: int = Field(ge=512, le=65536)
    first_usable_sector: int = Field(ge=0)
    last_usable_sector: int = Field(ge=0)
    total_sectors: int = Field(ge=0)
    partitions: tuple[PartitionResource, ...] = ()
    free_regions: tuple[FreeRegion, ...] = ()
    fingerprint_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class PhysicalDisk(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=3, max_length=192)
    identity: StorageDeviceIdentity
    partition_table_id: str


class StorageLayout(BaseModel):
    """Exact storage state used as BEFORE/AFTER evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: uuid4().hex)
    observed_at: datetime = Field(default_factory=utc_now)
    disk: PhysicalDisk
    partition_table: PartitionTable
    filesystems: tuple[FilesystemResource, ...] = ()
    mount_points: tuple[MountPointResource, ...] = ()
    volumes: tuple[VolumeResource, ...] = ()
    operating_systems: tuple[OperatingSystemResource, ...] = ()
    boot_dependencies: tuple[BootDependency, ...] = ()
    swap_partitions: tuple[int, ...] = ()
    recovery_partitions: tuple[int, ...] = ()
    warnings: tuple[str, ...] = ()
    evidence_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class DataImpactAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    level: DataImpactLevel
    affected_resources: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    mounted: bool = False
    filesystem_change_required: bool = False
    lvm_detected: bool = False
    raid_detected: bool = False
    encryption_detected: bool = False
    data_loss_possible: bool = False
    executable: bool


class BootImpactAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    level: BootImpactLevel
    affected_dependencies: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    recovery_strategy_available: bool = False
    executable: bool


class StoragePrimitiveOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,95}$")
    kind: Literal[
        "write_partition_table",
        "reread_partition_table",
        "verify_partition_table",
        "resize_partition",
        "move_partition",
    ]
    description: str = Field(min_length=1, max_length=512)
    partition_number: int | None = Field(default=None, ge=1)
    mutates_target: bool
    enabled: bool


class StorageVerificationStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,95}$")
    description: str = Field(min_length=1, max_length=512)
    required: bool = True


class StorageAuthorizationRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    required: Literal[True] = True
    independent: Literal[True] = True
    one_use: Literal[True] = True
    exact_plan_binding: Literal[True] = True


class StorageDryRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: Literal["sfdisk"] = "sfdisk"
    original_layout_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    proposed_layout_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    script_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    exit_code: int
    valid: bool
    warnings: tuple[str, ...] = ()


class StorageWriteGateDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: bool
    target_class: Literal["test_image", "controlled_loop", "physical_disk", "unknown"]
    reason: str = Field(min_length=1, max_length=512)
    physical_disk_writes_enabled: Literal[False] = False


class StorageOperationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: uuid4().hex)
    operation_id: str = Field(default_factory=lambda: uuid4().hex)
    version: Literal["1.0"] = "1.0"
    capability: Literal[
        "storage.partition.create",
        "storage.partition.delete",
        "storage.partition.resize",
        "storage.partition.move",
    ]
    operation: StorageOperationType
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime = Field(default_factory=lambda: utc_now() + timedelta(minutes=20))
    session_id: str = Field(min_length=8, max_length=128)
    target_disk: StorageDeviceIdentity
    target_partition: PartitionResource | None = None
    original_layout: StorageLayout
    proposed_layout: StorageLayout
    required_operations: tuple[StoragePrimitiveOperation, ...]
    affected_resources: tuple[str, ...]
    risk: Literal["low", "medium", "high", "critical"]
    estimated_duration_seconds: float | None = Field(default=None, gt=0)
    data_loss_possible: bool
    data_impact: DataImpactAssessment
    boot_impact: BootImpactAssessment
    filesystem_impact: str = Field(min_length=1, max_length=512)
    protection_checkpoint: ProtectionCheckpoint | None = None
    authorization: StorageAuthorizationRequirement = Field(
        default_factory=StorageAuthorizationRequirement
    )
    verification_plan: tuple[StorageVerificationStep, ...]
    dry_run: StorageDryRunResult | None = None
    write_gate: StorageWriteGateDecision
    executable: bool
    limitations: tuple[str, ...] = ()
    fingerprint_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @property
    def protected_resource_id(self) -> str:
        return self.target_disk.resource_id


class PartitionTableCheckpointArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    checkpoint_id: str
    operation_id: str
    target_fingerprint_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    partition_table_fingerprint_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    dump_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    sfdisk_dump: str = Field(max_length=1_000_000)
    created_at: datetime = Field(default_factory=utc_now)


class StorageCheckpointBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    checkpoint: ProtectionCheckpoint
    artifact: PartitionTableCheckpointArtifact


class StorageAuthorizationGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=8, max_length=128)
    challenge_id: str = Field(min_length=8, max_length=128)
    operation_id: str = Field(min_length=8, max_length=128)
    plan_id: str = Field(min_length=8, max_length=128)
    session_id: str = Field(min_length=8, max_length=128)
    target_fingerprint_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    plan_fingerprint_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    operator_uid: int | None = Field(default=None, ge=0)
    expires_at: datetime


class StorageTransaction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    operation_id: str
    plan_id: str
    session_id: str = Field(min_length=8, max_length=128)
    status: StorageTransactionStatus
    created_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    checkpoint_id: str | None = None
    authorization_challenge_id: str | None = None
    last_known_stage: str | None = Field(default=None, max_length=128)
    before_layout_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    after_layout_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    verification_id: str | None = None
    reconciliation_required: bool = False
    error_code: str | None = Field(default=None, max_length=128)
    metadata: dict[str, Any] = Field(default_factory=dict)


class StorageVerification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: uuid4().hex)
    operation_id: str
    created_at: datetime = Field(default_factory=utc_now)
    status: StorageVerificationStatus
    identity_verified: bool
    partition_table_verified: bool
    partition_geometry_verified: bool
    filesystem_verified: bool | None = None
    mountability_verified: bool | None = None
    operating_system_verified: bool | None = None
    boot_dependencies_verified: bool | None = None
    before: StorageLayout
    after: StorageLayout | None = None
    evidence: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    message: str = Field(min_length=1, max_length=1024)


class StorageOperationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plan: StorageOperationPlan
    transaction: StorageTransaction
    verification: StorageVerification | None = None


class StorageOperationOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: str
    operation: StorageOperationType
    tool: Literal["sfdisk"] = "sfdisk"
    exit_code: int
    kernel_reread: bool | None = None
    before: StorageLayout
    after: StorageLayout
    evidence: tuple[str, ...] = ()


class PartitionInspectInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target_disk: str = Field(min_length=1, max_length=4096)


class PartitionInspectResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    layout: StorageLayout
    knowledge_graph_revision: int = Field(ge=1)


class StorageOperationExecutionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: str = Field(min_length=8, max_length=128)
    plan_id: str = Field(min_length=8, max_length=128)
    session_id: str = Field(min_length=8, max_length=128)
    protected_resource_id: str = Field(min_length=8, max_length=128)
    protected_resource_fingerprint_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class StorageOperationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    record: StorageOperationRecord
    verification: StorageVerification
    knowledge_graph_revision: int = Field(ge=1)
