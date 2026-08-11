"""Execution contracts shared by boot service, executors and the privileged broker."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from ares.boot.models import (
    BootCheckpointArtifact,
    BootOperationKind,
    BootVerification,
    RepairEnvironment,
)
from ares.protection import ProtectionCheckpoint


class BootCheckpointBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    checkpoint: ProtectionCheckpoint
    artifact: BootCheckpointArtifact


class BootRepairOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    repair_id: str
    changed_operations: tuple[BootOperationKind, ...]
    environment: RepairEnvironment
    verification: BootVerification | None = None
