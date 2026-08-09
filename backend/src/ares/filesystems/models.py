"""Strongly typed contracts for filesystem inspection and repair."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from ares.protection import ProtectionCheckpoint


def utc_now() -> datetime:
    return datetime.now(UTC)


class FilesystemType(StrEnum):
    EXT2 = "ext2"
    EXT3 = "ext3"
    EXT4 = "ext4"
    XFS = "xfs"
    BTRFS = "btrfs"
    NTFS = "ntfs"


class FilesystemHealth(StrEnum):
    HEALTHY = "HEALTHY"
    INCONSISTENT = "INCONSISTENT"
    CORRUPTED = "CORRUPTED"
    UNKNOWN = "UNKNOWN"
    UNSUPPORTED = "UNSUPPORTED"


class RepairVerificationStatus(StrEnum):
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class RepairExecutionStatus(StrEnum):
    PLANNED = "PLANNED"
    AUTHORIZING = "AUTHORIZING"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    ABORTED = "ABORTED"


class ToolRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: str = Field(min_length=1, max_length=64)
    available: bool
    reason: str | None = Field(default=None, max_length=128)


class DeviceIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    requested_path: str = Field(min_length=1, max_length=4096)
    canonical_path: str = Field(min_length=1, max_length=4096)
    major_minor: str = Field(min_length=3, max_length=64)
    uuid: str | None = Field(default=None, max_length=256)
    filesystem_uuid: str | None = Field(default=None, max_length=256)
    partuuid: str | None = Field(default=None, max_length=256)
    serial: str | None = Field(default=None, max_length=256)
    model: str | None = Field(default=None, max_length=256)
    size_bytes: int = Field(ge=0)
    block_device: bool
    fingerprint_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class MountRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mount_point: str = Field(min_length=1, max_length=4096)
    root: str = Field(min_length=1, max_length=4096)
    source: str = Field(min_length=1, max_length=4096)
    filesystem_type: str = Field(min_length=1, max_length=64)
    options: tuple[str, ...] = ()
    bind_mount: bool = False


class MountSafetyReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mounted: bool
    busy: bool
    swap: bool
    mounts: tuple[MountRecord, ...] = ()
    nested_mounts: tuple[str, ...] = ()
    active_processes: tuple[int, ...] = ()
    unsupported_mount_options: tuple[str, ...] = ()
    safe_to_unmount: bool
    safe_to_remount: bool
    reasons: tuple[str, ...] = ()


class FilesystemCheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    filesystem: FilesystemType
    health: FilesystemHealth
    tool: str
    dry_run: bool
    exit_code: int
    problems: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    duration_ms: float = Field(ge=0)


class FilesystemInspection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: uuid4().hex)
    inspected_at: datetime = Field(default_factory=utc_now)
    identity: DeviceIdentity
    filesystem: FilesystemType | None = None
    health: FilesystemHealth = FilesystemHealth.UNKNOWN
    mount: MountSafetyReport
    check: FilesystemCheckResult | None = None
    required_tools: tuple[ToolRequirement, ...] = ()
    writable: bool
    supported: bool
    repair_supported: bool
    limitations: tuple[str, ...] = ()


class RepairAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,95}$")
    description: str = Field(min_length=1, max_length=512)
    mutates_target: bool


class FilesystemRepairPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: uuid4().hex)
    repair_id: str = Field(default_factory=lambda: uuid4().hex)
    version: Literal["1.0"] = "1.0"
    capability_id: Literal["filesystem.repair"] = "filesystem.repair"
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime = Field(default_factory=lambda: utc_now() + timedelta(minutes=15))
    session_id: str = Field(min_length=8, max_length=128)
    target: DeviceIdentity
    filesystem: FilesystemType
    mount: MountSafetyReport
    detected_problems: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    required_tools: tuple[ToolRequirement, ...] = ()
    required_permissions: tuple[str, ...] = ()
    estimated_duration_seconds: float | None = Field(default=None, gt=0)
    risk: Literal["high"] = "high"
    protection_checkpoint: ProtectionCheckpoint | None = None
    repair_actions: tuple[RepairAction, ...]
    verification_steps: tuple[str, ...]
    rollback_strategy: str = Field(min_length=1, max_length=1024)
    limitations: tuple[str, ...] = ()
    requires_authorization: Literal[True] = True
    executable: bool
    fingerprint_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class FilesystemAuthorizationGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    challenge_id: str
    plan_id: str
    repair_id: str
    capability_id: Literal["filesystem.repair"] = "filesystem.repair"
    session_id: str = Field(min_length=8, max_length=128)
    target_fingerprint_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    plan_fingerprint_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    operator_uid: int | None = Field(default=None, ge=0)
    expires_at: datetime


class RepairVerification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: uuid4().hex)
    repair_id: str
    created_at: datetime = Field(default_factory=utc_now)
    status: RepairVerificationStatus
    before: FilesystemCheckResult | None = None
    after: FilesystemCheckResult | None = None
    target_identity_verified: bool
    checkpoint_verified: bool
    remounted: bool | None = None
    evidence: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    message: str = Field(min_length=1, max_length=1024)


class RepairExecution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    plan_id: str
    session_id: str = Field(min_length=8, max_length=128)
    status: RepairExecutionStatus
    started_at: datetime | None = None
    finished_at: datetime | None = None
    authorization_challenge_id: str | None = None
    checkpoint_id: str | None = None
    tool: str | None = None
    safe_arguments: tuple[str, ...] = ()
    error_code: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class FilesystemRepairRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    created_at: datetime = Field(default_factory=utc_now)
    plan: FilesystemRepairPlan
    execution: RepairExecution
    verification: RepairVerification | None = None


class FilesystemRepairOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    before: FilesystemCheckResult
    repair_tool: str
    repair_exit_code: int
    repair_evidence: tuple[str, ...] = ()
    after: FilesystemCheckResult
    remounted: bool | None = None
    limitations: tuple[str, ...] = ()


class FilesystemInspectInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    device: str = Field(min_length=1, max_length=4096)


class FilesystemRepairInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_id: str = Field(min_length=8, max_length=128)
    session_id: str = Field(min_length=8, max_length=128)
    created_by: str = Field(min_length=1, max_length=128)
    protection_checkpoint: ProtectionCheckpoint


class FilesystemRepairResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    repair: FilesystemRepairRecord
    verification: RepairVerification
    knowledge_graph_revision: int = Field(ge=1)
