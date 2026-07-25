"""Definitions and immutable execution records for the workflow engine."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from ares.actions import Action
from ares.events.models import utc_now

StepOutputs = Mapping[str, dict[str, Any]]
InputFactory = Callable[[StepOutputs], dict[str, Any]]
StepCondition = Callable[[StepOutputs], bool]
ResultFactory = Callable[[StepOutputs], dict[str, Any]]


class PostCheck(Protocol):
    """Replaceable asynchronous verification after an action."""

    async def __call__(self, output: dict[str, Any], state: StepOutputs) -> bool:
        """Return true only when the action result is acceptable."""


class StageMode(StrEnum):
    """Execution policy for all steps in a stage."""

    SEQUENTIAL = "sequential"
    PARALLEL = "parallel"


class StepStatus(StrEnum):
    """Terminal state of one workflow step."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class ExecutionStatus(StrEnum):
    """Terminal state of one workflow execution."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Bounded retry policy; attempts include the first execution."""

    max_attempts: int = 1
    delay_seconds: float = 0.0

    def __post_init__(self) -> None:
        if not 1 <= self.max_attempts <= 5:
            raise ValueError("max_attempts must be between one and five")
        if not 0 <= self.delay_seconds <= 30:
            raise ValueError("delay_seconds must be between zero and thirty")


@dataclass(frozen=True, slots=True)
class WorkflowStep:
    """One private action plus its control and verification policy."""

    id: str
    action: Action
    inputs: InputFactory
    timeout_seconds: float = 10.0
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    condition: StepCondition | None = None
    postchecks: tuple[PostCheck, ...] = ()
    compensate_on_failure: bool = False

    def __post_init__(self) -> None:
        if not self.id or len(self.id) > 96:
            raise ValueError("workflow step id is invalid")
        if not 0 < self.timeout_seconds <= 300:
            raise ValueError("workflow step timeout is invalid")
        if self.retry.max_attempts > 1 and not self.action.idempotent:
            raise ValueError("only idempotent actions may be retried")


@dataclass(frozen=True, slots=True)
class WorkflowStage:
    """A sequential or parallel group of independent steps."""

    id: str
    mode: StageMode
    steps: tuple[WorkflowStep, ...]

    def __post_init__(self) -> None:
        if not self.id or not self.steps:
            raise ValueError("workflow stage must have an id and at least one step")


@dataclass(frozen=True, slots=True)
class WorkflowDefinition:
    """Complete, versioned execution plan produced by one capability."""

    id: str
    version: str
    capability_id: str
    stages: tuple[WorkflowStage, ...]
    result: ResultFactory

    def __post_init__(self) -> None:
        step_ids = [step.id for stage in self.stages for step in stage.steps]
        if not self.id or not self.stages or len(step_ids) != len(set(step_ids)):
            raise ValueError("workflow definition is empty or contains duplicate step ids")


class StepExecution(BaseModel):
    """Auditable result of one private step."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: str
    action_id: str
    status: StepStatus
    attempts: int
    started_at: datetime
    finished_at: datetime
    duration_ms: float = Field(ge=0)
    error_code: str | None = None


class WorkflowExecution(BaseModel):
    """Public execution record. It intentionally contains no command line."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    workflow_id: str
    workflow_version: str
    capability_id: str
    status: ExecutionStatus
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime
    duration_ms: float = Field(ge=0)
    steps: tuple[StepExecution, ...]
    result: dict[str, Any] | None = None
    error_code: str | None = None
    rollback_performed: bool = False


async def passing_postcheck(
    output: dict[str, Any],
    state: StepOutputs,
) -> bool:
    """Reusable no-op verifier for declarative workflows."""

    del output, state
    return True
