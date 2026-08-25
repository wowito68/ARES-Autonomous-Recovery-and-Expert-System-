"""Typed contracts for manual contextual recovery terminals.

The browser and LLM never receive commands, paths chosen by the caller, PTYs,
file descriptors, socket names or mount namespace identifiers.  Public DTOs are
bounded operational state only.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from ares.events.models import utc_now
from ares.resources.models import MountState, ResourceCandidate, ResourceKind


class TerminalContextKind(StrEnum):
    ARES_LOCAL = "ares_local"
    INSTALLED_SYSTEM_READ_ONLY = "installed_system_read_only"
    INSTALLED_SYSTEM_CHROOT_READ_ONLY = "installed_system_chroot_read_only"
    INSTALLED_SYSTEM_ADMIN = "installed_system_admin"


class TerminalSessionStatus(StrEnum):
    PLANNED = "PLANNED"
    AUTHORIZATION_REQUIRED = "AUTHORIZATION_REQUIRED"
    AUTHORIZED = "AUTHORIZED"
    STARTING = "STARTING"
    ACTIVE = "ACTIVE"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    EXPIRED = "EXPIRED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    ORPHANED = "ORPHANED"
    CLEANUP_REQUIRED = "CLEANUP_REQUIRED"
    CLEANUP_FAILED = "CLEANUP_FAILED"


class TerminalCleanupStatus(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    NOT_REQUIRED = "NOT_REQUIRED"
    VERIFIED = "VERIFIED"
    REQUIRED = "REQUIRED"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"


class TerminalRisk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class TerminalMountPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    root_read_only: Literal[True] = True
    nosuid: Literal[True] = True
    nodev: Literal[True] = True
    noexec: bool = True
    private_mount_namespace: Literal[True] = True
    mount_base: str = "/run/ares/terminals"


class TerminalTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    resource_id: str
    human_name: str
    kind: ResourceKind
    operating_system: str | None = None
    version: str | None = None
    filesystem: str | None = None
    size_bytes: Annotated[int | None, Field(ge=0)] = None
    current_mount_state: MountState
    technical_path: str | None = None
    technical_details: dict[str, str] = Field(default_factory=dict)
    stable_identity: str
    fingerprint: Annotated[str, Field(min_length=16, max_length=128)]
    confidence: Annotated[float, Field(ge=0, le=1)]
    limitations: tuple[str, ...] = ()

    @classmethod
    def from_resource(cls, resource: ResourceCandidate) -> "TerminalTarget":
        version = resource.technical_details.get("version") or None
        return cls(
            resource_id=resource.resource_id,
            human_name=resource.human_name,
            kind=resource.kind,
            operating_system=resource.operating_system,
            version=version,
            filesystem=resource.filesystem,
            size_bytes=resource.size_bytes,
            current_mount_state=resource.mount_state,
            technical_path=resource.technical_path,
            technical_details=dict(resource.technical_details),
            stable_identity=resource.stable_identity,
            fingerprint=_target_fingerprint(resource),
            confidence=resource.confidence,
            limitations=tuple(
                item
                for item in (
                    resource.ambiguity_reason,
                    "technical_path_missing" if not resource.technical_path else None,
                )
                if item
            ),
        )


class TerminalContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: TerminalContextKind
    label: str
    available: bool
    requires_authorization: bool
    privilege: str
    explanation: str
    risk: TerminalRisk
    resource_id: str | None = None
    blocked_reason: str | None = None
    requirements: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()


class TerminalContextCollection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contexts: tuple[TerminalContext, ...]
    count: int


class TerminalPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context_kind: TerminalContextKind
    resource_id: str | None = None


class TerminalAuthorizationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: bool = False
    understood: Annotated[str | None, Field(max_length=160)] = None


class TerminalPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: uuid4().hex)
    session_id: str
    context_kind: TerminalContextKind
    target: TerminalTarget | None = None
    target_fingerprint: str | None = None
    mount_policy: TerminalMountPolicy = Field(default_factory=TerminalMountPolicy)
    read_only: bool = True
    execute_target_binaries: bool = False
    network_policy: Literal["disabled"] = "disabled"
    device_policy: Literal["minimal"] = "minimal"
    risk: TerminalRisk = TerminalRisk.LOW
    expected_changes: str = "Ninguno."
    expected_non_changes: tuple[str, ...] = (
        "No se modifica GRUB.",
        "No se escriben discos, particiones ni filesystems instalados.",
        "No se ejecutan binarios del sistema instalado.",
    )
    requires_authorization: bool = True
    confirmation_phrase: str = "AUTORIZO TERMINAL DE SOLO LECTURA"
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime = Field(default_factory=lambda: utc_now() + timedelta(minutes=5))
    fingerprint_sha256: Annotated[str, Field(min_length=71, max_length=71)]
    limitations: tuple[str, ...] = ()

    @classmethod
    def build(
        cls,
        *,
        session_id: str,
        context_kind: TerminalContextKind,
        target: TerminalTarget | None,
        limitations: tuple[str, ...] = (),
    ) -> "TerminalPlan":
        requires_authorization = context_kind is not TerminalContextKind.ARES_LOCAL
        plan = cls(
            session_id=session_id,
            context_kind=context_kind,
            target=target,
            target_fingerprint=target.fingerprint if target is not None else None,
            risk=TerminalRisk.MEDIUM
            if context_kind is TerminalContextKind.INSTALLED_SYSTEM_READ_ONLY
            else TerminalRisk.LOW,
            limitations=limitations,
            requires_authorization=requires_authorization,
            confirmation_phrase=(
                "AUTORIZO TERMINAL DE SOLO LECTURA" if requires_authorization else ""
            ),
            fingerprint_sha256="sha256:" + ("0" * 64),
        )
        return plan.model_copy(update={"fingerprint_sha256": terminal_plan_fingerprint(plan)})


class TerminalSession(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: uuid4().hex)
    plan_id: str
    context_kind: TerminalContextKind
    status: TerminalSessionStatus
    created_at: datetime = Field(default_factory=utc_now)
    authorized_at: datetime | None = None
    started_at: datetime | None = None
    closed_at: datetime | None = None
    expires_at: datetime
    cleanup_status: TerminalCleanupStatus = TerminalCleanupStatus.NOT_STARTED
    error_code: str | None = None
    technical_details: dict[str, str] = Field(default_factory=dict)


class TerminalPlanResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plan: TerminalPlan
    session: TerminalSession


class TerminalSessionCollection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sessions: tuple[TerminalSession, ...]
    count: int


class TerminalStartResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    accepted: bool
    session: TerminalSession
    plan: TerminalPlan
    message: str


class TerminalCloseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: bool = False


class TerminalCleanupEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str
    status: TerminalCleanupStatus
    process_active: bool
    mount_active: bool
    socket_active: bool
    cleanup_verified: bool
    evidence: tuple[str, ...] = ()


class TerminalAuthorizationGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: uuid4().hex)
    plan_id: str
    session_id: str
    context_kind: TerminalContextKind
    target_fingerprint: str | None
    plan_fingerprint_sha256: str
    operator_uid: int
    expires_at: datetime
    consumed: bool = False


def terminal_plan_fingerprint(plan: TerminalPlan) -> str:
    payload = plan.model_dump(mode="json")
    payload.pop("fingerprint_sha256", None)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _target_fingerprint(resource: ResourceCandidate) -> str:
    payload = {
        "resource_id": resource.resource_id,
        "kind": resource.kind.value,
        "technical_path": resource.technical_path,
        "stable_identity": resource.stable_identity,
        "filesystem": resource.filesystem,
        "size_bytes": resource.size_bytes,
        "mount_state": resource.mount_state.value,
        "confidence": resource.confidence,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
