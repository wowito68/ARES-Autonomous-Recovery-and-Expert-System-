"""Operational AgentRun API: model intent to safe backend execution."""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Request, status

from ares.agent import (
    AgentAuthorizationRequest,
    AgentOrchestrator,
    AgentOrchestratorError,
    AgentRun,
    AgentRunCollection,
    AgentRunRequest,
    AgentTimelineEntry,
)
from ares.core.problems import AresProblem, ProblemDetail

router = APIRouter()
_PROBLEM_SCHEMA = {
    "content": {"application/problem+json": {"schema": ProblemDetail.model_json_schema()}}
}


@router.post(
    "/runs",
    response_model=AgentRun,
    status_code=status.HTTP_201_CREATED,
    responses={404: _PROBLEM_SCHEMA, 409: _PROBLEM_SCHEMA},
    summary="Create an operational run from a user objective without executing commands",
)
async def create_run(payload: AgentRunRequest, request: Request) -> AgentRun:
    try:
        return await _service(request).start(payload, session_id=_session(request))
    except AgentOrchestratorError as exc:
        raise _problem(exc) from exc


@router.get("/runs", response_model=AgentRunCollection, summary="List recent agent runs")
async def list_runs(request: Request) -> AgentRunCollection:
    runs = await _service(request).list()
    return AgentRunCollection(runs=runs, count=len(runs))


@router.get(
    "/runs/{run_id}",
    response_model=AgentRun,
    responses={404: _PROBLEM_SCHEMA},
    summary="Read one operational run",
)
async def get_run(run_id: str, request: Request) -> AgentRun:
    run = await _service(request).get(run_id)
    if run is None:
        raise _problem(AgentOrchestratorError("AGENT_RUN_NOT_FOUND"))
    return run


@router.post(
    "/runs/{run_id}/authorize-read-only",
    response_model=AgentRun,
    responses={404: _PROBLEM_SCHEMA, 409: _PROBLEM_SCHEMA},
    summary="Grant a short-lived exact authorization for read-only diagnostic steps",
)
async def authorize_read_only(
    run_id: str, payload: AgentAuthorizationRequest, request: Request
) -> AgentRun:
    try:
        return await _service(request).authorize_read_only(
            run_id,
            payload,
            session_id=_session(request),
            operator="local-user",
        )
    except AgentOrchestratorError as exc:
        raise _problem(exc) from exc


@router.post(
    "/runs/{run_id}/authorize-mutation",
    response_model=AgentRun,
    responses={404: _PROBLEM_SCHEMA, 409: _PROBLEM_SCHEMA},
    summary="Grant a short-lived exact authorization for one scoped mutating envelope",
)
async def authorize_mutation(
    run_id: str, payload: AgentAuthorizationRequest, request: Request
) -> AgentRun:
    try:
        return await _service(request).authorize_mutation(
            run_id,
            payload,
            session_id=_session(request),
            operator="local-user",
        )
    except AgentOrchestratorError as exc:
        raise _problem(exc) from exc


@router.post(
    "/runs/{run_id}/execute",
    response_model=AgentRun,
    responses={404: _PROBLEM_SCHEMA, 409: _PROBLEM_SCHEMA, 503: _PROBLEM_SCHEMA},
    summary="Execute already-authorized read-only diagnostic steps",
)
async def execute(run_id: str, request: Request) -> AgentRun:
    try:
        return await _service(request).execute(run_id, session_id=_session(request))
    except AgentOrchestratorError as exc:
        raise _problem(exc) from exc


@router.post(
    "/runs/{run_id}/continue",
    response_model=AgentRun,
    responses={404: _PROBLEM_SCHEMA, 409: _PROBLEM_SCHEMA, 503: _PROBLEM_SCHEMA},
    summary="Continue an already-authorized operational run",
)
async def continue_run(run_id: str, request: Request) -> AgentRun:
    try:
        return await _service(request).continue_run(run_id, session_id=_session(request))
    except AgentOrchestratorError as exc:
        raise _problem(exc) from exc


@router.post(
    "/runs/{run_id}/cancel",
    response_model=AgentRun,
    responses={404: _PROBLEM_SCHEMA},
    summary="Cancel a pending or running operational run when safe",
)
async def cancel(run_id: str, request: Request) -> AgentRun:
    try:
        return await _service(request).cancel(run_id, session_id=_session(request))
    except AgentOrchestratorError as exc:
        raise _problem(exc) from exc


@router.get(
    "/runs/{run_id}/timeline",
    response_model=tuple[AgentTimelineEntry, ...],
    responses={404: _PROBLEM_SCHEMA},
    summary="Read the immutable timeline for one operational run",
)
async def timeline(run_id: str, request: Request) -> tuple[AgentTimelineEntry, ...]:
    try:
        return await _service(request).timeline(run_id)
    except AgentOrchestratorError as exc:
        raise _problem(exc) from exc


def _service(request: Request) -> AgentOrchestrator:
    return cast(AgentOrchestrator, request.app.state.agent_orchestrator)


def _session(request: Request) -> str:
    return str(getattr(request.state, "request_id", "agent-session"))


def _problem(exc: AgentOrchestratorError) -> AresProblem:
    code = exc.code
    if code in {"AGENT_RUN_NOT_FOUND", "AGENT_RESOURCE_NOT_FOUND"}:
        http_status = 404
        title = "Agent resource not found"
    elif code in {
        "AGENT_AUTHORIZATION_REQUIRED",
        "AGENT_AUTHORIZATION_NOT_REQUIRED",
        "AGENT_AUTHORIZATION_CONFIRMATION_REQUIRED",
        "AGENT_AUTHORIZATION_ENVELOPE_REQUIRED",
        "AGENT_AUTHORIZATION_EXPIRED",
    }:
        http_status = 409
        title = "Agent authorization state mismatch"
    elif code in {
        "AGENT_LIMIT_STEPS_EXCEEDED",
        "AGENT_LIMIT_INVOCATIONS_EXCEEDED",
        "AGENT_CAPABILITY_OUT_OF_SCOPE",
    }:
        http_status = 409
        title = "Agent autonomy limit exceeded"
    else:
        http_status = 409
        title = "Agent run rejected"
    return AresProblem(
        status=http_status,
        code=code,
        title=title,
        detail="ARES rejected the operational run because a safety precondition was not satisfied.",
    )
