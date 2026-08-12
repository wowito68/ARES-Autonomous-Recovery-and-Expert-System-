"""Typed evidence, plans, operations and durable case state for System Recovery."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from ares.capabilities.models import RiskLevel
from ares.protection import ProtectionCheckpoint


def utc_now() -> datetime:
    return datetime.now(UTC)


class RecoveryStatus(StrEnum):
    DISCOVERY = "DISCOVERY"
    DIAGNOSING = "DIAGNOSING"
    PLANNED = "PLANNED"
    PROTECTED = "PROTECTED"
    AUTHORIZED = "AUTHORIZED"
    RECOVERING = "RECOVERING"
    VERIFYING = "VERIFYING"
    RECOVERED = "RECOVERED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    ABORTED = "ABORTED"


class RecoveryMode(StrEnum):
    READ_ONLY = "READ_ONLY"
    ASSISTED = "ASSISTED"
    REPAIR = "REPAIR"
    ADVANCED = "ADVANCED"


class RecoveryVerificationStatus(StrEnum):
    RECOVERED = "RECOVERED"
    PARTIALLY_RECOVERED = "PARTIALLY_RECOVERED"
    NOT_RECOVERED = "NOT_RECOVERED"
    UNKNOWN = "UNKNOWN"


class RecoveryOperationStatus(StrEnum):
    PLANNED = "PLANNED"
    PROTECTED = "PROTECTED"
    AUTHORIZATION_PENDING = "AUTHORIZATION_PENDING"
    AUTHORIZED = "AUTHORIZED"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    ABORTED = "ABORTED"
    ROLLED_BACK = "ROLLED_BACK"
    BLOCKED = "BLOCKED"


class RecoveryLayer(StrEnum):
    HARDWARE = "hardware"
    STORAGE = "storage"
    FILESYSTEM = "filesystem"
    BOOT = "boot"
    KERNEL = "kernel"
    INITRAMFS = "initramfs"
    SYSTEMD = "systemd"
    SERVICES = "services"
    PACKAGES = "packages"
    CONFIGURATION = "configuration"
    APPLICATION = "application"


class RecoveryIssueCode(StrEnum):
    HARDWARE_DEGRADED = "HARDWARE_DEGRADED"
    STORAGE_UNAVAILABLE = "STORAGE_UNAVAILABLE"
    FILESYSTEM_DEGRADED = "FILESYSTEM_DEGRADED"
    MOUNT_FAILURE = "MOUNT_FAILURE"
    BOOT_DEGRADED = "BOOT_DEGRADED"
    KERNEL_INITRAMFS_MISMATCH = "KERNEL_INITRAMFS_MISMATCH"
    INITRAMFS_MISSING = "INITRAMFS_MISSING"
    SYSTEMD_DEGRADED = "SYSTEMD_DEGRADED"
    CRITICAL_SERVICE_FAILED = "CRITICAL_SERVICE_FAILED"
    SERVICE_DEPENDENCY_FAILED = "SERVICE_DEPENDENCY_FAILED"
    FSTAB_INVALID_REFERENCE = "FSTAB_INVALID_REFERENCE"
    PACKAGE_BROKEN_DEPENDENCIES = "PACKAGE_BROKEN_DEPENDENCIES"
    PACKAGE_INTERRUPTED = "PACKAGE_INTERRUPTED"
    PACKAGE_PENDING_CONFIGURATION = "PACKAGE_PENDING_CONFIGURATION"
    PACKAGE_DATABASE_INCONSISTENT = "PACKAGE_DATABASE_INCONSISTENT"
    PACKAGE_REPAIR_EXTERNAL_DEPENDENCY = "PACKAGE_REPAIR_EXTERNAL_DEPENDENCY"
    IO_ERROR = "IO_ERROR"
    OOM = "OOM"
    PERMISSION_ERROR = "PERMISSION_ERROR"
    MISSING_DEVICE = "MISSING_DEVICE"
    EMERGENCY_MODE = "EMERGENCY_MODE"
    UNKNOWN = "UNKNOWN"


class EvidenceSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class SystemBootMode(StrEnum):
    EMERGENCY = "EMERGENCY"
    RESCUE = "RESCUE"
    SINGLE_USER = "SINGLE_USER"
    NORMAL = "NORMAL"
    UNKNOWN = "UNKNOWN"


class PackageManagerKind(StrEnum):
    APT = "APT"
    DNF = "DNF"
    PACMAN = "PACMAN"
    ZYPPER = "ZYPPER"
    UNKNOWN = "UNKNOWN"


class RecoveryStrategyKind(StrEnum):
    FILESYSTEM = "FilesystemRecovery"
    BOOTLOADER = "BootloaderRecovery"
    INITRAMFS = "InitramfsRecovery"
    PACKAGE = "PackageRecovery"
    CONFIGURATION = "ConfigurationRecovery"
    SERVICE = "ServiceRecovery"


class DiagnosticEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: uuid4().hex)
    source: str = Field(min_length=1, max_length=128)
    timestamp: datetime = Field(default_factory=utc_now)
    severity: EvidenceSeverity
    subsystem: str = Field(min_length=1, max_length=128)
    event: str = Field(min_length=1, max_length=128)
    normalized_message: str = Field(min_length=1, max_length=2048)
    confidence: float = Field(ge=0, le=1)
    resource_ids: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)


class RecoveryIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: uuid4().hex)
    code: RecoveryIssueCode
    layer: RecoveryLayer
    severity: EvidenceSeverity
    summary: str = Field(min_length=1, max_length=1024)
    evidence_ids: tuple[str, ...]
    confidence: float = Field(ge=0, le=1)
    affected_components: tuple[str, ...] = ()


class RootCauseHypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: uuid4().hex)
    hypothesis: str = Field(min_length=1, max_length=2048)
    evidence: tuple[str, ...]
    confidence: float = Field(ge=0, le=1)
    affected_components: tuple[str, ...]
    alternative_hypotheses: tuple[str, ...] = ()
    root_issue_ids: tuple[str, ...] = ()


class RecoveryDependency(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    upstream: str
    downstream: str
    relation: str = Field(min_length=1, max_length=128)


class RecoveryStrategy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: RecoveryStrategyKind
    preconditions: tuple[str, ...]
    required_tools: tuple[str, ...]
    risk: RiskLevel
    dependencies: tuple[str, ...]
    verification: tuple[str, ...]
    rollback_supported: bool
    rollback_strategy: str
    rollback_limitations: tuple[str, ...] = ()


class ConfigurationDiff(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    current_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    current_text: str = Field(max_length=64_000)
    proposed_text: str = Field(max_length=64_000)
    unified_diff: tuple[str, ...]


class RecoveryOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: str = Field(default_factory=lambda: uuid4().hex)
    capability_id: str = Field(min_length=4, max_length=128)
    strategy: RecoveryStrategyKind
    description: str = Field(min_length=1, max_length=1024)
    risk: RiskLevel
    status: RecoveryOperationStatus = RecoveryOperationStatus.PLANNED
    depends_on: tuple[str, ...] = ()
    target_resources: tuple[str, ...] = ()
    preconditions: tuple[str, ...] = ()
    protection_checkpoint: ProtectionCheckpoint | None = None
    authorization_challenge_id: str | None = None
    rollback_supported: bool = False
    rollback_strategy: str = "No automatic rollback declared."
    rollback_limitations: tuple[str, ...] = ()
    verification_requirements: tuple[str, ...] = ()
    payload: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_code: str | None = None


class SystemRecoveryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: uuid4().hex)
    case_id: str
    created_at: datetime = Field(default_factory=utc_now)
    mode: RecoveryMode
    problem: str = Field(min_length=1, max_length=2048)
    evidence_ids: tuple[str, ...]
    root_cause_hypothesis_ids: tuple[str, ...]
    operations: tuple[RecoveryOperation, ...]
    dependencies: tuple[RecoveryDependency, ...]
    minimum_change_rationale: str = Field(min_length=1, max_length=2048)
    executable: bool
    blocked_reasons: tuple[str, ...] = ()
    fingerprint_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class RecoveryExecutionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: str
    capability_id: str
    status: RecoveryOperationStatus
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_code: str | None = None
    verification_summary: str | None = None


class SystemRecoveryVerification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: uuid4().hex)
    case_id: str
    created_at: datetime = Field(default_factory=utc_now)
    status: RecoveryVerificationStatus
    hardware_verified: bool | None = None
    storage_verified: bool | None = None
    filesystem_verified: bool | None = None
    boot_verified: bool | None = None
    kernel_verified: bool | None = None
    initramfs_verified: bool | None = None
    systemd_verified: bool | None = None
    critical_services_verified: bool | None = None
    evidence_ids: tuple[str, ...] = ()
    remaining_issue_ids: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    message: str = Field(min_length=1, max_length=2048)


class RecoveryReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    initial_state: str
    detected_problems: tuple[str, ...]
    evidence_summary: tuple[str, ...]
    root_cause: tuple[str, ...]
    actions_planned: tuple[str, ...]
    actions_executed: tuple[str, ...]
    protection: tuple[str, ...]
    changes: tuple[str, ...]
    verification: str
    remaining_problems: tuple[str, ...]
    recommendations: tuple[str, ...]
    user_summary: str


class RecoveryCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(default_factory=lambda: uuid4().hex)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    session_id: str = Field(min_length=8, max_length=128)
    mode: RecoveryMode
    target_os: str | None = None
    target_root: str | None = None
    target_boot: str | None = None
    target_esp: str | None = None
    initial_state: str = "unknown"
    detected_issues: tuple[RecoveryIssue, ...] = ()
    evidence: tuple[DiagnosticEvidence, ...] = ()
    hypotheses: tuple[RootCauseHypothesis, ...] = ()
    recovery_plan: SystemRecoveryPlan | None = None
    executions: tuple[RecoveryExecutionSummary, ...] = ()
    verification: SystemRecoveryVerification | None = None
    final_state: str | None = None
    status: RecoveryStatus = RecoveryStatus.DISCOVERY
    report: RecoveryReport | None = None


class RecoveryDiagnoseInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target_root: Annotated[str | None, Field(max_length=4096)] = None
    target_disk: Annotated[str | None, Field(max_length=4096)] = None
    mode: RecoveryMode = RecoveryMode.READ_ONLY


class RecoveryPlanInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(min_length=8, max_length=128)
    mode: RecoveryMode = RecoveryMode.ASSISTED


class RecoveryActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_authorization: bool = True
