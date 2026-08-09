"""Capability and plugin metadata contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field


class CapabilityCategory(StrEnum):
    """Stable top-level catalog categories."""

    STORAGE = "storage"
    BACKUP = "backup"
    RECOVERY = "recovery"
    SECURITY = "security"
    NETWORK = "network"
    PACKAGES = "packages"
    HARDWARE = "hardware"
    OPTIMIZATION = "optimization"
    WINDOWS = "windows"


class RiskLevel(StrEnum):
    """Maximum inherent risk before contextual policy is applied."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class OperationClass(StrEnum):
    """Effect class used by consent and forensic-mode policy."""

    OBSERVE = "observe"
    CHANGE = "change"
    RECOVER = "recover"


class CapabilityMode(StrEnum):
    """Explicit storage/system mutation ceiling for a capability."""

    READ_ONLY = "read_only"
    MUTATING = "mutating"


class PermissionRequirement(BaseModel):
    """Semantic permission; it is mapped to OS privileges outside the plugin."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{2,95}$")]
    reason: Annotated[str, Field(min_length=3, max_length=240)]
    required: bool = True


class OSCompatibility(BaseModel):
    """Operating-system envelope supported by a capability or plugin."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    families: tuple[str, ...]
    architectures: tuple[str, ...]
    minimum_version: Annotated[
        str | None,
        Field(pattern=r"^[0-9]+(?:\.[0-9]+){0,2}$"),
    ] = None
    live_modes: tuple[str, ...] = ("live", "persistent", "forensic", "recovery")


class RollbackPolicy(BaseModel):
    """Declared compensation behavior."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    supported: bool
    strategy: Annotated[str, Field(min_length=3, max_length=320)]


class AuditPolicy(BaseModel):
    """Controls the evidence retained for one capability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    record_inputs: bool
    record_outputs: bool
    redact_sensitive_values: bool = True
    event_names: tuple[str, ...]


class CapabilityMetadata(BaseModel):
    """Self-documenting contract discovered by the Capability Manager."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9.-]{4,127}$")]
    version: Annotated[str, Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")]
    name: Annotated[str, Field(min_length=3, max_length=100)]
    description: Annotated[str, Field(min_length=10, max_length=500)]
    objective: Annotated[str, Field(min_length=10, max_length=500)]
    category: CapabilityCategory
    operation: OperationClass
    mode: CapabilityMode = CapabilityMode.READ_ONLY
    os_compatibility: OSCompatibility
    risk: RiskLevel
    estimated_duration_seconds: Annotated[float, Field(gt=0, le=86_400)]
    permissions: tuple[PermissionRequirement, ...]
    dependencies: tuple[str, ...] = ()
    internal_actions: tuple[str, ...]
    postchecks: tuple[str, ...]
    rollback: RollbackPolicy
    requires_protection_checkpoint: bool = False
    required_evidence: tuple[str, ...] = ()
    emitted_events: tuple[str, ...]
    metrics: tuple[str, ...]
    audit: AuditPolicy
    keywords: tuple[str, ...] = ()


class PluginManifest(BaseModel):
    """Validated plugin envelope loaded without changing the ARES core."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9.-]{4,127}$")]
    version: Annotated[str, Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")]
    core_api_version: Annotated[str, Field(pattern=r"^[0-9]+\.[0-9]+$")]
    name: Annotated[str, Field(min_length=3, max_length=100)]
    permissions: tuple[str, ...]
    dependencies: tuple[str, ...] = ()
    os_compatibility: OSCompatibility
    capabilities: tuple[str, ...]


class CapabilityDescriptor(BaseModel):
    """Generated documentation for one installed capability version."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    metadata: CapabilityMetadata
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] = Field(default_factory=dict)
    plugin_id: str
    plugin_version: str
    active: bool
