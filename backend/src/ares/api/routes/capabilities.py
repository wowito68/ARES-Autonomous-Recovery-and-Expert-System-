"""Public capability catalog and execution boundary."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict

from ares.capabilities import CapabilityCategory, CapabilityManager, CapabilityMetadata
from ares.core.problems import AresProblem, ProblemDetail
from ares.workflows import WorkflowEngine, WorkflowExecution

router = APIRouter()


class PublicCapability(BaseModel):
    """Capability-level contract; private actions and tools remain hidden."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    version: str
    name: str
    description: str
    objective: str
    category: str
    risk_level: str
    operation_class: str
    estimated_duration_seconds: float
    required_permissions: tuple[str, ...]
    required_evidence: tuple[str, ...]
    postchecks: tuple[str, ...]
    rollback_strategy: str
    emitted_events: tuple[str, ...]
    metrics: tuple[str, ...]

    @classmethod
    def from_metadata(cls, metadata: CapabilityMetadata) -> PublicCapability:
        return cls(
            id=metadata.id,
            version=metadata.version,
            name=metadata.name,
            description=metadata.description,
            objective=metadata.objective,
            category=metadata.category.value,
            risk_level=metadata.risk.value,
            operation_class=metadata.operation.value,
            estimated_duration_seconds=metadata.estimated_duration_seconds,
            required_permissions=tuple(item.id for item in metadata.permissions),
            required_evidence=metadata.required_evidence,
            postchecks=metadata.postchecks,
            rollback_strategy=metadata.rollback.strategy,
            emitted_events=metadata.emitted_events,
            metrics=metadata.metrics,
        )


class CapabilityCatalog(BaseModel):
    """Search result with stable ordering."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capabilities: tuple[PublicCapability, ...]
    count: int


class CapabilityExecutionRequest(BaseModel):
    """No path, executable, argument or shell field exists."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: Literal["all_detected"] = "all_detected"


class PublicStepExecution(BaseModel):
    """Step evidence without a private action or tool identifier."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: str
    status: str
    attempts: int
    started_at: datetime
    finished_at: datetime
    duration_ms: float
    error_code: str | None


class PublicWorkflowExecution(BaseModel):
    """Public execution record with internal action names removed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    workflow_id: str
    workflow_version: str
    capability_id: str
    status: str
    started_at: datetime
    finished_at: datetime
    duration_ms: float
    steps: tuple[PublicStepExecution, ...]
    result: dict[str, Any] | None
    error_code: str | None
    rollback_performed: bool

    @classmethod
    def from_execution(cls, execution: WorkflowExecution) -> PublicWorkflowExecution:
        return cls(
            id=execution.id,
            workflow_id=execution.workflow_id,
            workflow_version=execution.workflow_version,
            capability_id=execution.capability_id,
            status=execution.status.value,
            started_at=execution.started_at,
            finished_at=execution.finished_at,
            duration_ms=execution.duration_ms,
            steps=tuple(
                PublicStepExecution(
                    step_id=step.step_id,
                    status=step.status.value,
                    attempts=step.attempts,
                    started_at=step.started_at,
                    finished_at=step.finished_at,
                    duration_ms=step.duration_ms,
                    error_code=step.error_code,
                )
                for step in execution.steps
            ),
            result=execution.result,
            error_code=execution.error_code,
            rollback_performed=execution.rollback_performed,
        )


@router.get("", response_model=CapabilityCatalog, summary="Search installed capabilities")
async def catalog(
    request: Request,
    query: Annotated[str | None, Query(min_length=2, max_length=128)] = None,
    category: CapabilityCategory | None = None,
) -> CapabilityCatalog:
    matches = tuple(
        PublicCapability.from_metadata(metadata)
        for metadata in _manager(request).catalog(query=query, category=category)
    )
    return CapabilityCatalog(capabilities=matches, count=len(matches))


@router.get(
    "/executions/{execution_id}",
    response_model=PublicWorkflowExecution,
    responses={
        404: {
            "description": "Execution not found",
            "content": {"application/problem+json": {"schema": ProblemDetail.model_json_schema()}},
        }
    },
    summary="Read an execution record from the current boot",
)
async def execution(execution_id: str, request: Request) -> PublicWorkflowExecution:
    record = _workflow_engine(request).get(execution_id)
    if record is None:
        raise AresProblem(
            status=404,
            code="CAPABILITY_EXECUTION_NOT_FOUND",
            title="Capability execution not found",
            detail="No capability execution with that identifier exists in this boot.",
        )
    return PublicWorkflowExecution.from_execution(record)


@router.get(
    "/{capability_id}",
    response_model=PublicCapability,
    responses={
        404: {
            "description": "Capability not found",
            "content": {"application/problem+json": {"schema": ProblemDetail.model_json_schema()}},
        }
    },
    summary="Read one installed capability",
)
async def capability(capability_id: str, request: Request) -> PublicCapability:
    metadata = _manager(request).get(capability_id)
    if metadata is None:
        raise _not_found()
    return PublicCapability.from_metadata(metadata)


@router.post(
    "/{capability_id}/executions",
    response_model=PublicWorkflowExecution,
    responses={
        404: {
            "description": "Capability not found",
            "content": {"application/problem+json": {"schema": ProblemDetail.model_json_schema()}},
        }
    },
    summary="Execute a validated capability workflow",
)
async def execute_capability(
    capability_id: str,
    payload: CapabilityExecutionRequest,
    request: Request,
) -> PublicWorkflowExecution:
    manager = _manager(request)
    if manager.get(capability_id) is None:
        raise _not_found()
    record = await manager.execute(capability_id, payload.model_dump(mode="json"))
    return PublicWorkflowExecution.from_execution(record)


def _manager(request: Request) -> CapabilityManager:
    return cast(CapabilityManager, request.app.state.capability_manager)


def _workflow_engine(request: Request) -> WorkflowEngine:
    return cast(WorkflowEngine, request.app.state.workflow_engine)


def _not_found() -> AresProblem:
    return AresProblem(
        status=404,
        code="CAPABILITY_NOT_FOUND",
        title="Capability not found",
        detail="The requested capability is not installed.",
    )
