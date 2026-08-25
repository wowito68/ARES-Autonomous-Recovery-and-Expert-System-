"""Semantic, command-free contracts for privileged recovery operations."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

_IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9:._-]{7,191}$"
_SHA256_PATTERN = r"^[a-f0-9]{64}$"


class RecoveryExecutionStatus(StrEnum):
    """Terminal status expected from a future recovery broker."""

    VERIFIED = "verified"
    PARTIAL = "partial"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    UNKNOWN = "unknown"


class RecoveryExecutionRequest(BaseModel):
    """Exact mutation request bound to a server-generated plan and checkpoint."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: Annotated[str, Field(min_length=8, max_length=128)]
    target_resource_id: Annotated[str, Field(pattern=_IDENTIFIER_PATTERN)]
    target_resource_fingerprint_sha256: Annotated[str, Field(pattern=_SHA256_PATTERN)]
    plan_id: Annotated[str, Field(pattern=_IDENTIFIER_PATTERN)]
    plan_fingerprint_sha256: Annotated[str, Field(pattern=_SHA256_PATTERN)]
    protection_checkpoint_id: Annotated[str, Field(pattern=_IDENTIFIER_PATTERN)]
    authorization_id: Annotated[str, Field(pattern=_IDENTIFIER_PATTERN)]
    dry_run: bool = True


class CriticalRecoveryRequest(RecoveryExecutionRequest):
    """Critical recovery additionally requires a local physical-presence challenge."""

    physical_presence_challenge_id: Annotated[str, Field(pattern=_IDENTIFIER_PATTERN)]


class FileRecoveryRequest(RecoveryExecutionRequest):
    """Recover selected evidence to a distinct, exact destination resource."""

    destination_resource_id: Annotated[str, Field(pattern=_IDENTIFIER_PATTERN)]
    destination_resource_fingerprint_sha256: Annotated[str, Field(pattern=_SHA256_PATTERN)]
    recovery_manifest_id: Annotated[str, Field(pattern=_IDENTIFIER_PATTERN)]


class SystemCheckpointRecoveryRequest(RecoveryExecutionRequest):
    """Restore only elements enumerated in a verified system checkpoint."""

    source_checkpoint_id: Annotated[str, Field(pattern=_IDENTIFIER_PATTERN)]
    protected_element_ids: Annotated[tuple[str, ...], Field(min_length=1, max_length=256)]


class AccountRecoveryRequest(CriticalRecoveryRequest):
    """Recover one local account selected by opaque identity, never by shell input."""

    account_id: Annotated[str, Field(pattern=_IDENTIFIER_PATTERN)]
    recovery_method: Annotated[
        str,
        Field(pattern=r"^(unlock|reset-local-password|restore-admin-membership)$"),
    ]
class RecoveryExecutionResult(BaseModel):
    """Verifiable outcome required before a mutation may be reported as complete."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capability_id: str
    target_resource_id: str
    target_resource_fingerprint_sha256: Annotated[str, Field(pattern=_SHA256_PATTERN)]
    plan_fingerprint_sha256: Annotated[str, Field(pattern=_SHA256_PATTERN)]
    checkpoint_id: str
    authorization_id: str
    status: RecoveryExecutionStatus
    changed_elements: tuple[str, ...] = ()
    verification_evidence_ids: tuple[str, ...] = ()
    rollback_attempted: bool = False
    limitations: tuple[str, ...] = ()
