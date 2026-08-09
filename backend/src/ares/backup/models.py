"""Strongly typed contracts for ARES backup capabilities."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


class BackupStatus(StrEnum):
    PLANNED = "PLANNED"
    VALIDATING = "VALIDATING"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    CORRUPTED = "CORRUPTED"


class BackupVerificationStatus(StrEnum):
    NOT_RUN = "NOT_RUN"
    RUNNING = "RUNNING"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    CORRUPTED = "CORRUPTED"


class BackupDestinationKind(StrEnum):
    LOCAL_FILESYSTEM = "local_filesystem"
    MOUNTED_EXTERNAL_DISK = "mounted_external_disk"
    USB_STORAGE = "usb_storage"


class BackupEntryType(StrEnum):
    FILE = "file"
    DIRECTORY = "directory"


class BackupSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1, max_length=4096)
    device_id: str = Field(min_length=3, max_length=64)
    mount_point: str = Field(min_length=1, max_length=4096)
    filesystem_type: str = Field(min_length=1, max_length=64)
    estimated_size_bytes: int = Field(ge=0)
    estimated_file_count: int = Field(ge=0)
    estimated_directory_count: int = Field(ge=0)


class BackupDestination(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    root_path: str = Field(min_length=1, max_length=4096)
    backup_path: str = Field(min_length=1, max_length=4096)
    device_id: str = Field(min_length=3, max_length=64)
    mount_point: str = Field(min_length=1, max_length=4096)
    filesystem_type: str = Field(min_length=1, max_length=64)
    kind: BackupDestinationKind
    available_bytes: int = Field(ge=0)


class BackupExclusion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    relative_path: str = Field(min_length=1, max_length=4096)
    reason: str = Field(min_length=2, max_length=128)


class BackupPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    overwrite: Literal[False] = False
    follow_symlinks: Literal[False] = False
    allow_same_filesystem: Literal[False] = False
    cross_filesystems: Literal[False] = False
    checksum_algorithm: Literal["sha256"] = "sha256"
    excluded_names: tuple[str, ...] = (
        "$RECYCLE.BIN",
        "System Volume Information",
        "lost+found",
    )


class BackupPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: uuid4().hex)
    backup_id: str = Field(default_factory=lambda: uuid4().hex)
    version: Literal["1.0"] = "1.0"
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime = Field(default_factory=lambda: utc_now() + timedelta(minutes=15))
    source: BackupSource
    destination: BackupDestination
    policy: BackupPolicy
    required_bytes: int = Field(ge=0)
    included_file_count: int = Field(ge=0)
    included_directory_count: int = Field(ge=0)
    exclusions: tuple[BackupExclusion, ...] = ()
    risk: Literal["medium"] = "medium"
    estimated_duration_seconds: float | None = Field(default=None, gt=0)
    authorization_required: Literal[True] = True
    fingerprint_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class BackupEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    relative_path: str = Field(min_length=1, max_length=4096)
    size_bytes: int = Field(ge=0)
    mtime_ns: int = Field(ge=0)
    mode: int = Field(ge=0, le=0o7777)
    entry_type: BackupEntryType
    checksum_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class BackupManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    backup_id: str
    format_version: Literal["1.0"] = "1.0"
    created_at: datetime = Field(default_factory=utc_now)
    source: BackupSource
    entries: tuple[BackupEntry, ...]
    file_count: int = Field(ge=0)
    directory_count: int = Field(ge=0)
    total_size_bytes: int = Field(ge=0)
    manifest_checksum_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class BackupVerification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: uuid4().hex)
    backup_id: str
    created_at: datetime = Field(default_factory=utc_now)
    status: BackupVerificationStatus
    expected_file_count: int = Field(ge=0)
    verified_file_count: int = Field(ge=0)
    expected_size_bytes: int = Field(ge=0)
    verified_size_bytes: int = Field(ge=0)
    manifest_valid: bool
    checksum_mismatches: tuple[str, ...] = ()
    missing_entries: tuple[str, ...] = ()
    unexpected_entries: tuple[str, ...] = ()
    source_changed_entries: tuple[str, ...] = ()
    message: str = Field(min_length=1, max_length=512)


class BackupProgress(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    files_completed: int = Field(ge=0)
    files_total: int = Field(ge=0)
    bytes_completed: int = Field(ge=0)
    bytes_total: int = Field(ge=0)
    percent: float = Field(ge=0, le=100)
    speed_bytes_per_second: float | None = Field(default=None, ge=0)
    eta_seconds: float | None = Field(default=None, ge=0)


class BackupExecution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: uuid4().hex)
    backup_id: str
    workflow_execution_id: str | None = None
    status: BackupStatus
    started_at: datetime | None = None
    finished_at: datetime | None = None
    progress: BackupProgress
    authorization_challenge_id: str | None = None
    error_code: str | None = None


class Backup(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    version: Literal["1.0"] = "1.0"
    created_at: datetime
    source: BackupSource
    destination: BackupDestination
    size: int = Field(ge=0)
    file_count: int = Field(ge=0)
    status: BackupStatus
    checksum: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    verification_status: BackupVerificationStatus = BackupVerificationStatus.NOT_RUN
    encryption_status: Literal["none"] = "none"
    compression_status: Literal["none"] = "none"
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_by: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=8, max_length=128)
    plan_id: str
    execution: BackupExecution


class AuthorizationGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    challenge_id: str
    plan_id: str
    plan_fingerprint_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    expires_at: datetime


class BackupGraphSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    revision: int = Field(ge=1)
    node_count: int = Field(ge=0)
    edge_count: int = Field(ge=0)


class BackupCreateResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    backup: Backup
    manifest: BackupManifest
    verification: BackupVerification
    knowledge_graph: BackupGraphSummary


class BackupVerifyResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    backup: Backup
    verification: BackupVerification
    knowledge_graph: BackupGraphSummary


class BackupListResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    backups: tuple[Backup, ...]


class BackupCreateInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_id: str = Field(min_length=8, max_length=128)
    session_id: str = Field(min_length=8, max_length=128)
    created_by: str = Field(min_length=1, max_length=128)


class BackupVerifyInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    backup_id: str = Field(min_length=8, max_length=128)
    session_id: str = Field(min_length=8, max_length=128)


class BackupListInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    limit: int = Field(default=100, ge=1, le=500)
