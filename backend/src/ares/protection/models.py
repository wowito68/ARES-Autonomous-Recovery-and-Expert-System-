"""Protection checkpoint contracts for future destructive capabilities."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class ProtectionCheckpointStatus(StrEnum):
    PLANNED = "planned"
    READY = "ready"
    INVALID = "invalid"


class ProtectionCheckpoint(BaseModel):
    """Verified protection evidence that a future mutating capability may require."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: uuid4().hex)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    status: ProtectionCheckpointStatus
    protected_resources: tuple[str, ...]
    provider_capability_id: str
    backup_id: str | None = None
    verification_id: str | None = None
    session_id: str = Field(min_length=8, max_length=128)
    evidence_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ProtectionCheckpointRequirement(BaseModel):
    """Declarative requirement consumed by future destructive-capability policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    required: bool = False
    minimum_verification: str = "verified"
