"""Public, normalized output contracts for diagnostic capabilities."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from ares.events.models import utc_now


class DiagnosticScope(StrEnum):
    ARES_LIVE_RUNTIME = "ares_live_runtime"
    ACTIVE_SYSTEM_RUNTIME = "active_system_runtime"
    INSTALLED_SYSTEM_OFFLINE = "installed_system_offline"


class DiagnosticSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class DiagnosticFinding(BaseModel):
    """One bounded conclusion. Evidence strings are data, never model instructions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{2,127}$")]
    severity: DiagnosticSeverity
    confidence: Annotated[float, Field(ge=0, le=1)]
    title: Annotated[str, Field(min_length=3, max_length=160)]
    detail: Annotated[str, Field(min_length=3, max_length=800)]
    evidence: tuple[str, ...] = ()
    recommendation: Annotated[str | None, Field(max_length=500)] = None
    recommended_capability: Annotated[str | None, Field(pattern=r"^[a-z][a-z0-9.-]{4,127}$")] = None


class EvidenceReference(BaseModel):
    """Provenance for normalized evidence without exposing an unbounded raw dump."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Annotated[str, Field(pattern=r"^evidence:[a-f0-9]{64}$")]
    source: Annotated[str, Field(min_length=1, max_length=240)]
    collector: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{2,95}$")]
    sha256: Annotated[str, Field(pattern=r"^sha256:[a-f0-9]{64}$")]
    captured_at: datetime = Field(default_factory=utc_now)
    truncated: bool = False


class DiagnosticInput(BaseModel):
    """Semantic target selection; paths and command material are deliberately absent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: Annotated[str | None, Field(min_length=8, max_length=128)] = None
    target_resource_id: Annotated[str | None, Field(min_length=3, max_length=160)] = None


class SpaceAnalysisInput(DiagnosticInput):
    analysis_depth: Literal["quick", "standard"] = "standard"


class SpaceConsumer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: Annotated[str, Field(min_length=1, max_length=320)]
    category: Literal[
        "logs",
        "package_cache",
        "cache",
        "temporary",
        "crash_data",
        "personal_data",
        "system_data",
        "unknown",
    ]
    size_bytes: Annotated[int, Field(ge=0)]
    entries_scanned: Annotated[int, Field(ge=0)] = 0
    truncated: bool = False


class ReclaimableEstimate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    category: str
    estimated_bytes: Annotated[int, Field(ge=0)]
    risk: Literal["low", "review"]
    path: Annotated[str, Field(min_length=1, max_length=320)]
    corrective_capability: str | None = None


class SpaceAnalysisResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target_resource_id: str
    target_fingerprint: str
    scope: DiagnosticScope
    total_bytes: Annotated[int, Field(ge=0)]
    used_bytes: Annotated[int, Field(ge=0)]
    available_bytes: Annotated[int, Field(ge=0)]
    used_percent: Annotated[float, Field(ge=0, le=100)]
    inode_total: Annotated[int, Field(ge=0)]
    inode_used: Annotated[int, Field(ge=0)]
    inode_used_percent: Annotated[float, Field(ge=0, le=100)]
    consumers: tuple[SpaceConsumer, ...]
    reclaimable: tuple[ReclaimableEstimate, ...]
    findings: tuple[DiagnosticFinding, ...]
    evidence: tuple[EvidenceReference, ...]
    limitations: tuple[str, ...] = ()
    summary: str


class MemoryConsumer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pid: Annotated[int, Field(ge=1)]
    name: Annotated[str, Field(min_length=1, max_length=96)]
    resident_bytes: Annotated[int, Field(ge=0)]


class MemoryAnalysisResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target_resource_id: str
    target_fingerprint: str
    scope: DiagnosticScope
    status: Literal["healthy", "pressure", "historical_only", "insufficient_evidence"]
    total_bytes: Annotated[int | None, Field(ge=0)] = None
    available_bytes: Annotated[int | None, Field(ge=0)] = None
    cache_bytes: Annotated[int | None, Field(ge=0)] = None
    swap_total_bytes: Annotated[int | None, Field(ge=0)] = None
    swap_used_bytes: Annotated[int | None, Field(ge=0)] = None
    pressure_avg10: Annotated[float | None, Field(ge=0)] = None
    oom_events: Annotated[int, Field(ge=0)] = 0
    consumers: tuple[MemoryConsumer, ...] = ()
    findings: tuple[DiagnosticFinding, ...]
    evidence: tuple[EvidenceReference, ...]
    limitations: tuple[str, ...] = ()
    summary: str


class PackageHealthResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target_resource_id: str
    target_fingerprint: str
    scope: DiagnosticScope
    status: Literal[
        "healthy",
        "warnings",
        "repair_required",
        "metadata_stale",
        "unsupported",
        "insufficient_evidence",
    ]
    package_manager: str | None = None
    installed_packages: Annotated[int, Field(ge=0)] = 0
    inconsistent_packages: tuple[str, ...] = ()
    held_packages: tuple[str, ...] = ()
    auto_installed_packages: Annotated[int, Field(ge=0)] = 0
    interrupted_update_fragments: Annotated[int, Field(ge=0)] = 0
    metadata_age_days: Annotated[int | None, Field(ge=0)] = None
    repository_entries: Annotated[int, Field(ge=0)] = 0
    findings: tuple[DiagnosticFinding, ...]
    evidence: tuple[EvidenceReference, ...]
    limitations: tuple[str, ...] = ()
    summary: str


class FailedService(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    unit: Annotated[str, Field(min_length=1, max_length=160)]
    state: Annotated[str, Field(min_length=1, max_length=64)] = "failed"
    reason: Annotated[str | None, Field(max_length=320)] = None


class ServiceFailureResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target_resource_id: str
    target_fingerprint: str
    scope: DiagnosticScope
    status: Literal["healthy", "failures_detected", "historical_only", "insufficient_evidence"]
    service_manager: str | None = None
    failed_services: tuple[FailedService, ...] = ()
    error_events: Annotated[int, Field(ge=0)] = 0
    findings: tuple[DiagnosticFinding, ...]
    evidence: tuple[EvidenceReference, ...]
    limitations: tuple[str, ...] = ()
    summary: str
