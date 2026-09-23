"""Operational agent contracts with explicit authorization boundaries."""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from ares.events.models import utc_now
from ares.resources.models import ResourceCandidate


class AgentRunState(StrEnum):
    CREATED = "CREATED"
    UNDERSTANDING = "UNDERSTANDING"
    WAITING_FOR_USER = "WAITING_FOR_USER"
    COLLECTING_EVIDENCE = "COLLECTING_EVIDENCE"
    PLANNING = "PLANNING"
    PLAN_READY = "PLAN_READY"
    READ_ONLY_AUTHORIZATION_REQUIRED = "READ_ONLY_AUTHORIZATION_REQUIRED"
    RUNNING_DIAGNOSTIC = "RUNNING_DIAGNOSTIC"
    USER_INPUT_REQUIRED = "USER_INPUT_REQUIRED"
    PROTECTION_REQUIRED = "PROTECTION_REQUIRED"
    MUTATION_AUTHORIZATION_REQUIRED = "MUTATION_AUTHORIZATION_REQUIRED"
    EXECUTING = "EXECUTING"
    AUTHORIZED = "AUTHORIZED"
    OBSERVING_RESULT = "OBSERVING_RESULT"
    REPLANNING = "REPLANNING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    INVALIDATED = "INVALIDATED"


class AgentStepState(StrEnum):
    DRAFT = "DRAFT"
    VALIDATING = "VALIDATING"
    READY = "READY"
    AUTHORIZATION_REQUIRED = "AUTHORIZATION_REQUIRED"
    AUTHORIZED = "AUTHORIZED"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    INVALIDATED = "INVALIDATED"


class AgentRunRequest(BaseModel):
    """User objective. The server owns tools and capability mapping."""

    model_config = ConfigDict(extra="forbid")

    objective: Annotated[str, Field(min_length=3, max_length=1_000)]
    resource_id: str | None = None
    destination_resource_id: str | None = None


class AgentAuthorizationRequest(BaseModel):
    """Explicit, contextual authorization for one exact read-only plan."""

    model_config = ConfigDict(extra="forbid")

    confirm: bool = False
    understood: Annotated[str | None, Field(max_length=160)] = None


class AgentProposalType(StrEnum):
    ASK_USER = "ASK_USER"
    REQUEST_EVIDENCE = "REQUEST_EVIDENCE"
    PROPOSE_CAPABILITY = "PROPOSE_CAPABILITY"
    STOP = "STOP"
    EXPLAIN_RESULT = "EXPLAIN_RESULT"


class AgentProposal(BaseModel):
    """Untrusted model proposal after strict validation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    proposal_type: AgentProposalType
    goal_interpretation: Annotated[str, Field(min_length=3, max_length=600)]
    hypotheses: tuple[Annotated[str, Field(min_length=1, max_length=240)], ...] = ()
    requested_evidence: tuple[Annotated[str, Field(min_length=1, max_length=160)], ...] = ()
    selected_capability_id: Annotated[str | None, Field(max_length=128)] = None
    resource_ids: tuple[Annotated[str, Field(min_length=1, max_length=160)], ...] = ()
    reason: Annotated[str, Field(min_length=3, max_length=600)]
    expected_result: Annotated[str, Field(min_length=3, max_length=500)]
    confidence: Annotated[float, Field(ge=0, le=1)] = 0.5
    needs_user_input: bool = False
    user_question: Annotated[str | None, Field(max_length=320)] = None
    source: Literal["model", "deterministic-fallback"] = "deterministic-fallback"
    valid: bool = True
    error_code: str | None = None


class AuthorizationEnvelopeStatus(StrEnum):
    DRAFT = "DRAFT"
    AUTHORIZATION_REQUIRED = "AUTHORIZATION_REQUIRED"
    AUTHORIZED = "AUTHORIZED"
    CONSUMED = "CONSUMED"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"
    INVALIDATED = "INVALIDATED"


class AuthorizationEnvelope(BaseModel):
    """Scope-bound authorization built by the trusted backend."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: uuid4().hex)
    agent_run_id: str
    goal: str
    allowed_capabilities: tuple[str, ...]
    target_resource_ids: tuple[str, ...] = ()
    target_fingerprints: tuple[str, ...] = ()
    risk_ceiling: Literal["low", "medium", "high", "critical"]
    maximum_bytes_written: int = Field(ge=0)
    maximum_bytes_deleted: int = Field(ge=0)
    protected_paths: tuple[str, ...] = ()
    allowed_package_changes: tuple[str, ...] = ()
    maximum_downtime_seconds: int = Field(ge=0)
    network_policy: Literal["disabled", "loopback", "scoped"] = "disabled"
    checkpoint_requirements: tuple[str, ...] = ()
    rollback_policy: str
    verification_requirements: tuple[str, ...]
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime = Field(default_factory=lambda: utc_now() + timedelta(minutes=10))
    plan_fingerprint: Annotated[str, Field(min_length=16, max_length=128)]
    status: AuthorizationEnvelopeStatus = AuthorizationEnvelopeStatus.AUTHORIZATION_REQUIRED
    consumed_invocation_ids: tuple[str, ...] = ()


class AgentAutonomyLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    maximum_steps: int = Field(default=12, ge=1, le=64)
    maximum_capability_invocations: int = Field(default=6, ge=1, le=32)
    maximum_replans: int = Field(default=3, ge=0, le=16)
    maximum_duration_seconds: int = Field(default=900, ge=30, le=86_400)
    risk_ceiling: Literal["low", "medium", "high", "critical"] = "low"
    authorization_expiry_seconds: int = Field(default=600, ge=30, le=3_600)
    allowed_targets: tuple[str, ...] = ()
    allowed_capabilities: tuple[str, ...] = ()


class AgentTimelineEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: uuid4().hex)
    at: datetime = Field(default_factory=utc_now)
    event_type: str
    state: AgentRunState
    reason: str
    evidence_ids: tuple[str, ...] = ()
    error_code: str | None = None


class CapabilityInvocationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    invocation_id: str = Field(default_factory=lambda: uuid4().hex)
    agent_run_id: str
    step_id: str
    capability_id: str
    capability_version: str | None = None
    input_fingerprint: Annotated[str, Field(min_length=16, max_length=128)]
    target_fingerprints: tuple[str, ...] = ()
    authorization_envelope_id: str
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime | None = None
    status: Literal["STARTED", "SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED"] = "STARTED"
    verification_status: Literal["NOT_REQUIRED", "PENDING", "VERIFIED", "FAILED"] = "PENDING"
    evidence_ids: tuple[str, ...] = ()
    error_code: str | None = None


class TechnicalAction(BaseModel):
    """Inspectable technical activity; never arbitrary user-provided shell."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capability_id: str
    operation_class: str
    risk: str
    privileged: bool = False
    commands_visible: bool = True
    command_summary: tuple[str, ...] = ()
    expected_changes: str = "Ninguno."


class AgentPlanStep(BaseModel):
    """One operational step shown by the frontend."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    objective: str
    state: AgentStepState
    capability_id: str | None = None
    target_resource_id: str | None = None
    prerequisites: tuple[str, ...] = ()
    risk: str = "low"
    requires_authorization: bool = False
    requires_protection: bool = False
    action: TechnicalAction | None = None
    expected_result: str
    verification: str
    rollback_strategy: str
    dependencies: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    result: dict[str, object] | None = None
    error_code: str | None = None


class ReadOnlyAuthorization(BaseModel):
    """Short lived, exact authorization for read-only diagnostics."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: uuid4().hex)
    run_id: str
    step_ids: tuple[str, ...]
    capability_ids: tuple[str, ...] = ()
    resource_fingerprints: tuple[str, ...]
    granted_by: str
    objective: str
    risk: str = "low"
    operation_class: str = "observe"
    target_resource_id: str | None = None
    target_resource_name: str | None = None
    plan_fingerprint: Annotated[str, Field(min_length=16, max_length=128)]
    granted_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime = Field(default_factory=lambda: utc_now() + timedelta(minutes=5))
    consumed: bool = False


class AgentRun(BaseModel):
    """Persistent operational run. It is not a chat transcript."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: uuid4().hex)
    objective: str
    state: AgentRunState
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    selected_resource_id: str | None = None
    selected_resource_fingerprint: str | None = None
    resources: tuple[ResourceCandidate, ...] = ()
    backend_plan: dict[str, Any] | None = None
    proposal: AgentProposal | None = None
    authorization_envelope: AuthorizationEnvelope | None = None
    limits: AgentAutonomyLimits = Field(default_factory=AgentAutonomyLimits)
    capability_invocations: tuple[CapabilityInvocationRecord, ...] = ()
    steps: tuple[AgentPlanStep, ...] = ()
    authorization: ReadOnlyAuthorization | None = None
    summary: str = ""
    limitations: tuple[str, ...] = ()
    events: tuple[str, ...] = ()
    timeline: tuple[AgentTimelineEntry, ...] = ()


class AgentRunCollection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    runs: tuple[AgentRun, ...]
    count: int
