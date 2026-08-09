"""Protection checkpoint contracts for destructive-capability policy."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class ProtectionCheckpointStatus(StrEnum):
    PLANNED = "planned"
    READY = "ready"
    INVALID = "invalid"


class ProtectionCheckpoint(BaseModel):
    """Verified protection evidence bound to exact protected resources."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: uuid4().hex)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    status: ProtectionCheckpointStatus
    protected_resources: tuple[str, ...]
    resource_fingerprints: dict[str, str] = Field(default_factory=dict)
    provider_capability_id: str
    protection_kind: Literal["backup", "snapshot"] = "backup"
    backup_id: str | None = None
    verification_id: str | None = None
    session_id: str = Field(min_length=8, max_length=128)
    evidence_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    limitations: tuple[str, ...] = ()


class ProtectionCheckpointRequirement(BaseModel):
    """Declarative checkpoint policy consumed by mutating capabilities."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    required: bool = False
    minimum_verification: str = "verified"
