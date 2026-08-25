"""Operational agent contracts with explicit authorization boundaries."""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from ares.events.models import utc_now
from ares.resources.models import ResourceCandidate


class AgentRunState(StrEnum):
    UNDERSTANDING = "UNDERSTANDING"
    COLLECTING_EVIDENCE = "COLLECTING_EVIDENCE"
    PLANNING = "PLANNING"
    PLAN_READY = "PLAN_READY"
    READ_ONLY_AUTHORIZATION_REQUIRED = "READ_ONLY_AUTHORIZATION_REQUIRED"
    RUNNING_DIAGNOSTIC = "RUNNING_DIAGNOSTIC"
    USER_INPUT_REQUIRED = "USER_INPUT_REQUIRED"
    PROTECTION_REQUIRED = "PROTECTION_REQUIRED"
    MUTATION_AUTHORIZATION_REQUIRED = "MUTATION_AUTHORIZATION_REQUIRED"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
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


class AgentAuthorizationRequest(BaseModel):
    """Explicit, contextual authorization for one exact read-only plan."""

    model_config = ConfigDict(extra="forbid")

    confirm: bool = False
    understood: Annotated[str | None, Field(max_length=160)] = None


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
    steps: tuple[AgentPlanStep, ...] = ()
    authorization: ReadOnlyAuthorization | None = None
    summary: str = ""
    limitations: tuple[str, ...] = ()
    events: tuple[str, ...] = ()


class AgentRunCollection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    runs: tuple[AgentRun, ...]
    count: int
