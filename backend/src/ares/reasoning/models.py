"""Public contracts of the independent Reasoning Engine."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class ReasoningStatus(StrEnum):
    """Reasoning outcome before a workflow is authorized."""

    NEEDS_EVIDENCE = "needs_evidence"
    CAPABILITY_SELECTED = "capability_selected"
    STOPPED = "stopped"


class EvidenceFact(BaseModel):
    """Evidence known to exist; raw content remains outside this contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{2,95}$")]
    confidence: Annotated[float, Field(ge=0, le=1)]


class ReasoningRequest(BaseModel):
    """Goal and explicit evidence supplied to the deterministic engine."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    goal: Annotated[str, Field(min_length=3, max_length=1_000)]
    evidence: tuple[EvidenceFact, ...] = ()


class Hypothesis(BaseModel):
    """One explainable capability-level hypothesis."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capability_id: str
    statement: str
    confidence: Annotated[float, Field(ge=0, le=1)]


class ReasoningAssessment(BaseModel):
    """No commands or action identifiers are present by design."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: ReasoningStatus
    hypotheses: tuple[Hypothesis, ...]
    requested_evidence: tuple[str, ...] = ()
    selected_capability_id: str | None = None
    stop_reason: str | None = None
