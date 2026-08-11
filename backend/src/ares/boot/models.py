"""Typed domain models for Boot Recovery & Bootloader Management."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from ares.protection import ProtectionCheckpoint
from ares.storage_operations.models import BootImpactAssessment, StorageDeviceIdentity


def utc_now() -> datetime:
    return datetime.now(UTC)


class FirmwareMode(StrEnum):
    UEFI = "UEFI"
    BIOS = "BIOS"
    UNKNOWN = "UNKNOWN"


class BootloaderKind(StrEnum):
    GRUB = "GRUB"
    SYSTEMD_BOOT = "SYSTEMD_BOOT"
    WINDOWS_BOOT_MANAGER = "WINDOWS_BOOT_MANAGER"
    UNKNOWN = "UNKNOWN"


class DistributionFamily(StrEnum):
    DEBIAN = "DEBIAN"
    FEDORA = "FEDORA"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


class BootIssueSeverity(StrEnum):
    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class BootIssueCode(StrEnum):
    GRUB_MISSING = "GRUB_MISSING"
    GRUB_CONFIG_MISSING = "GRUB_CONFIG_MISSING"
    GRUB_CONFIG_CORRUPT = "GRUB_CONFIG_CORRUPT"
    EFI_ENTRY_MISSING = "EFI_ENTRY_MISSING"
    ESP_NOT_MOUNTED = "ESP_NOT_MOUNTED"
    BOOT_INACCESSIBLE = "BOOT_INACCESSIBLE"
    KERNEL_MISSING = "KERNEL_MISSING"
    INITRAMFS_MISSING = "INITRAMFS_MISSING"
    FSTAB_REFERENCE_INVALID = "FSTAB_REFERENCE_INVALID"
    MULTIPLE_LINUX_INSTALLATIONS = "MULTIPLE_LINUX_INSTALLATIONS"
    MULTIPLE_BOOT_DISKS = "MULTIPLE_BOOT_DISKS"
    UNSUPPORTED_BOOTLOADER = "UNSUPPORTED_BOOTLOADER"
    WINDOWS_BOOT_REPAIR_UNSUPPORTED = "WINDOWS_BOOT_REPAIR_UNSUPPORTED"


class BootRepairStatus(StrEnum):
    PLANNED = "PLANNED"
    PROTECTED = "PROTECTED"
    AUTHORIZED = "AUTHORIZED"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    REPAIR_FAILED = "REPAIR_FAILED"
    ABORTED = "ABORTED"
    UNKNOWN = "UNKNOWN"


class BootVerificationStatus(StrEnum):
    VERIFIED = "VERIFIED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class BootVerificationConfidence(StrEnum):
    HIGH = "HIGH"
    LIMITED = "LIMITED"
    UNKNOWN = "UNKNOWN"


class BootOperationKind(StrEnum):
    CREATE_CHECKPOINT = "CREATE_CHECKPOINT"
    MOUNT_ROOT = "MOUNT_ROOT"
    MOUNT_BOOT = "MOUNT_BOOT"
    MOUNT_ESP = "MOUNT_ESP"
    PREPARE_REPAIR_ENVIRONMENT = "PREPARE_REPAIR_ENVIRONMENT"
    INSTALL_GRUB = "INSTALL_GRUB"
    REGENERATE_GRUB_CONFIG = "REGENERATE_GRUB_CONFIG"
    REGENERATE_INITRAMFS = "REGENERATE_INITRAMFS"
    UPDATE_EFI_ENTRY = "UPDATE_EFI_ENTRY"
    VERIFY_BOOT_CHAIN = "VERIFY_BOOT_CHAIN"
    CLEANUP_REPAIR_ENVIRONMENT = "CLEANUP_REPAIR_ENVIRONMENT"


class BootEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(default_factory=lambda: uuid4().hex)
    source: str = Field(min_length=1, max_length=128)
    observation: str = Field(min_length=1, max_length=1024)
    resource_id: str | None = Field(default=None, max_length=256)
    confidence: float = Field(ge=0.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class FirmwareEnvironment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    mode: FirmwareMode
    efivars_available: bool
    boot_entries_available: bool
    evidence_ids: tuple[str, ...] = ()


class BootEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    number: str | None = Field(default=None, max_length=16)
    label: str = Field(min_length=1, max_length=256)
    loader_path: str | None = Field(default=None, max_length=1024)
    active: bool = True
    disk_path: str | None = Field(default=None, max_length=1024)
    partition_number: int | None = Field(default=None, ge=1)
    evidence_ids: tuple[str, ...] = ()


class BootPartition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    resource_id: str
    device_path: str
    partition_number: int = Field(ge=1)
    role: Literal["root", "boot", "esp", "other"]
    filesystem_type: str | None = Field(default=None, max_length=64)
    uuid: str | None = Field(default=None, max_length=256)
    partuuid: str | None = Field(default=None, max_length=256)
    mount_point: str | None = Field(default=None, max_length=4096)


class Bootloader(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: BootloaderKind
    version: str | None = Field(default=None, max_length=128)
    distribution_family: DistributionFamily = DistributionFamily.UNKNOWN
    config_paths: tuple[str, ...] = ()
    efi_loader_paths: tuple[str, ...] = ()
    repair_supported: bool = False
    evidence_ids: tuple[str, ...] = ()


class BootConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    root_path: str | None = Field(default=None, max_length=4096)
    boot_path: str | None = Field(default=None, max_length=4096)
    esp_path: str | None = Field(default=None, max_length=4096)
    grub_config_path: str | None = Field(default=None, max_length=4096)
    grub_default_path: str | None = Field(default=None, max_length=4096)
    fstab_path: str | None = Field(default=None, max_length=4096)
    kernels: tuple[str, ...] = ()
    initramfs: tuple[str, ...] = ()
    fstab_references: tuple[str, ...] = ()
    invalid_fstab_references: tuple[str, ...] = ()


class BootTargetOS(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    name: str = Field(min_length=1, max_length=256)
    version: str | None = Field(default=None, max_length=128)
    family: DistributionFamily
    root_partition_id: str | None = None
    root_path: str | None = Field(default=None, max_length=4096)
    boot_path: str | None = Field(default=None, max_length=4096)
    esp_path: str | None = Field(default=None, max_length=4096)


class BootDependency(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    source_id: str
    relation: Literal["depends_on", "loads", "boots", "stored_on", "configured_by"]
    target_id: str
    critical: bool = True
    evidence_ids: tuple[str, ...] = ()


class BootIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(default_factory=lambda: uuid4().hex)
    code: BootIssueCode
    severity: BootIssueSeverity
    summary: str = Field(min_length=1, max_length=1024)
    evidence_ids: tuple[str, ...] = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    recommended_action: str = Field(min_length=1, max_length=1024)
    repairable_automatically: bool = False


class BootEnvironment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(default_factory=lambda: uuid4().hex)
    created_at: datetime = Field(default_factory=utc_now)
    firmware: FirmwareEnvironment
    bootloader: Bootloader
    target_disk: StorageDeviceIdentity | None = None
    boot_disk_id: str | None = None
    esp: BootPartition | None = None
    root_partition: BootPartition | None = None
    boot_partition: BootPartition | None = None
    operating_systems: tuple[BootTargetOS, ...] = ()
    boot_entries: tuple[BootEntry, ...] = ()
    configuration: BootConfiguration
    dependencies: tuple[BootDependency, ...] = ()
    evidence: tuple[BootEvidence, ...] = ()


class BootDiagnosticResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(default_factory=lambda: uuid4().hex)
    created_at: datetime = Field(default_factory=utc_now)
    environment: BootEnvironment
    issues: tuple[BootIssue, ...] = ()
    evidence: tuple[BootEvidence, ...] = ()
    confidence: float = Field(ge=0.0, le=1.0)
    severity: BootIssueSeverity
    recommended_action: str = Field(min_length=1, max_length=1024)
    knowledge_graph_revision: int | None = Field(default=None, ge=1)


class BootRepairOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    kind: BootOperationKind
    description: str = Field(min_length=1, max_length=1024)
    mutates_system: bool
    enabled: bool = True


class BootVerificationStrategy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    verify_firmware: bool = True
    verify_efi_entry: bool = True
    verify_esp: bool = True
    verify_bootloader: bool = True
    verify_grub_configuration: bool = True
    verify_kernel: bool = True
    verify_initramfs: bool = True
    verify_root_filesystem: bool = True
    reboot_proof_available: bool = False


class BootCheckpointArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    checkpoint_id: str
    repair_id: str
    created_at: datetime = Field(default_factory=utc_now)
    root_path: str
    target_disk_fingerprint_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    file_hashes: dict[str, str] = Field(default_factory=dict)
    missing_paths: tuple[str, ...] = ()
    efi_entries_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    artifact_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class BootRepairPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(default_factory=lambda: uuid4().hex)
    repair_id: str = Field(default_factory=lambda: uuid4().hex)
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime = Field(default_factory=lambda: utc_now() + timedelta(minutes=30))
    session_id: str = Field(min_length=8, max_length=128)
    diagnostic_id: str
    target_os: BootTargetOS
    target_disk: StorageDeviceIdentity
    target_esp: BootPartition | None = None
    bootloader: Bootloader
    current_configuration: BootConfiguration
    expected_configuration: BootConfiguration
    issues: tuple[BootIssue, ...]
    operations: tuple[BootRepairOperation, ...]
    dependencies: tuple[BootDependency, ...] = ()
    boot_impact: BootImpactAssessment
    risk: Literal["high", "critical"] = "high"
    protection_checkpoint: ProtectionCheckpoint | None = None
    authorization_required: Literal[True] = True
    verification_strategy: BootVerificationStrategy = Field(default_factory=BootVerificationStrategy)
    executable: bool = False
    limitations: tuple[str, ...] = ()
    fingerprint_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class BootAuthorizationGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(default_factory=lambda: uuid4().hex)
    challenge_id: str
    repair_id: str
    plan_id: str
    session_id: str
    plan_fingerprint_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    target_disk_fingerprint_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    operator_uid: int = Field(ge=0)
    expires_at: datetime


class RepairEnvironment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(default_factory=lambda: uuid4().hex)
    repair_id: str
    root_path: str
    boot_path: str | None = None
    esp_path: str | None = None
    work_root: str
    mounted_paths: tuple[str, ...] = ()
    bind_mounts: tuple[str, ...] = ()
    cleaned: bool = False


class BootRepairExecution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    repair_id: str
    plan_id: str
    session_id: str
    status: BootRepairStatus
    created_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    checkpoint_id: str | None = None
    authorization_challenge_id: str | None = None
    last_known_stage: str | None = Field(default=None, max_length=128)
    changed_operations: tuple[BootOperationKind, ...] = ()
    skipped_operations: tuple[BootOperationKind, ...] = ()
    error_code: str | None = Field(default=None, max_length=128)
    reconciliation_required: bool = False


class BootVerification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(default_factory=lambda: uuid4().hex)
    repair_id: str
    created_at: datetime = Field(default_factory=utc_now)
    status: BootVerificationStatus
    confidence: BootVerificationConfidence
    firmware_verified: bool
    efi_entry_verified: bool | None = None
    esp_verified: bool | None = None
    bootloader_verified: bool
    grub_configuration_verified: bool | None = None
    kernel_verified: bool
    initramfs_verified: bool
    root_filesystem_verified: bool
    evidence: tuple[BootEvidence, ...] = ()
    limitations: tuple[str, ...] = ()
    message: str = Field(min_length=1, max_length=1024)


class BootRepairRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    plan: BootRepairPlan
    execution: BootRepairExecution
    verification: BootVerification | None = None


class BootDiagnoseInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    target_disk: str | None = Field(default=None, max_length=4096)
    root_path: str | None = Field(default=None, max_length=4096)


class BootRepairPlanInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    diagnostic_id: str
    target_os_id: str | None = None


class BootRepairStartInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    plan_id: str
    request_authorization: Literal[True] = True
