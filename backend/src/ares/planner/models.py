"""Public, command-free planning contracts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from ares.events.models import utc_now
from ares.reasoning.models import Hypothesis


class PlanStatus(StrEnum):
    """Outcome of converting reasoning into a capability-level plan."""

    READY = "ready"
    NEEDS_EVIDENCE = "needs_evidence"
    STOPPED = "stopped"


class PlannedCapability(BaseModel):
    """One capability invocation; actions and tools are intentionally absent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capability_id: str
    version: str
    depends_on: tuple[str, ...] = ()
    risk_level: str
    operation_class: str
    estimated_duration_seconds: Annotated[float, Field(gt=0)]
    rationale: str


class ExecutionPlan(BaseModel):
    """Immutable plan produced before authorization or execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    goal: str
    status: PlanStatus
    created_at: datetime = Field(default_factory=utc_now)
    hypotheses: tuple[Hypothesis, ...] = ()
    requested_evidence: tuple[str, ...] = ()
    steps: tuple[PlannedCapability, ...] = ()
    stop_reason: str | None = None
