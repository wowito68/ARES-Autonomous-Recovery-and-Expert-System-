"""Human resource catalog over real ARES evidence snapshots."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class ResourceKind(StrEnum):
    """Kinds exposed to the UI without requiring technical paths."""

    DISK = "disk"
    PARTITION = "partition"
    FILESYSTEM = "filesystem"
    OPERATING_SYSTEM = "operating_system"
    MOUNT = "mount"
    RECOVERY_ENVIRONMENT = "recovery_environment"


class MountState(StrEnum):
    """Conservative mount state for resources."""

    MOUNTED = "mounted"
    UNMOUNTED = "unmounted"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


class ResourceCandidate(BaseModel):
    """One selectable resource with human and technical identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    resource_id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.:-]{2,160}$")]
    human_name: Annotated[str, Field(min_length=3, max_length=160)]
    kind: ResourceKind
    operating_system: str | None = None
    size_bytes: Annotated[int | None, Field(ge=0)] = None
    filesystem: str | None = None
    mount_state: MountState = MountState.UNKNOWN
    health: str = "unknown"
    technical_path: str | None = None
    stable_identity: Annotated[str, Field(min_length=8, max_length=256)]
    confidence: Annotated[float, Field(ge=0, le=1)]
    recommended: bool = False
    ambiguity_reason: str | None = None
    snapshot_id: str | None = None
    parent_resource_id: str | None = None
    related_resource_ids: tuple[str, ...] = ()
    technical_details: dict[str, str] = Field(default_factory=dict)


class ResourceCatalog(BaseModel):
    """Current catalog generated from the latest real storage snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: str | None
    resources: tuple[ResourceCandidate, ...]
    count: int
    generated_from: str
    warnings: tuple[str, ...] = ()


class ResourceResolution(BaseModel):
    """Result of resolving one UI resource id back to technical identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    resource: ResourceCandidate
    valid: bool
    reason: str | None = None
