"""Read-only platform state assembled from root-owned runtime files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from ares.config import Settings
from ares.core.problems import AresProblem, ProblemDetail
from ares.session import (
    SessionActionRequest,
    SessionActionResult,
    SessionOperation,
    SessionPreflight,
    SystemSessionService,
)
from ares.terminal import (
    TerminalAuthorizationRequest,
    TerminalCleanupEvidence,
    TerminalCloseRequest,
    TerminalContextCollection,
    TerminalPlanRequest,
    TerminalPlanResult,
    TerminalService,
    TerminalServiceError,
    TerminalPlan,
    TerminalSession,
    TerminalSessionCollection,
    TerminalStartResult,
)

router = APIRouter()
_MAX_STATE_FILE_BYTES = 2_000_000
_PROBLEM_SCHEMA = {
    "content": {"application/problem+json": {"schema": ProblemDetail.model_json_schema()}}
}


class SystemOverview(BaseModel):
    """Small snapshot used by the local interface."""

    model_config = ConfigDict(extra="forbid")

    mode: dict[str, Any] | None
    retention: dict[str, Any] | None
    network: dict[str, Any] | None
    integrity: dict[str, Any] | None
    hardware: dict[str, Any] | None


@router.get("/overview", response_model=SystemOverview, summary="Read ARES platform state")
async def overview(request: Request) -> SystemOverview:
    """Read only allowlisted, bounded JSON files from the volatile ARES state."""

    settings = cast(Settings, request.app.state.settings)
    root = settings.runtime_state_dir
    return SystemOverview(
        mode=_read_json(root / "mode.json"),
        retention=_read_json(root / "state/status.json"),
        network=_read_json(root / "state/network.json"),
        integrity=_read_json(root / "integrity.json"),
        hardware=_read_json(root / "hardware/public/inventory-v1.json"),
    )


@router.get(
    "/session/preflight/{operation}",
    response_model=SessionPreflight,
    summary="Explain whether ARES can safely leave the Live session",
)
async def session_preflight(operation: SessionOperation, request: Request) -> SessionPreflight:
    return await _session_service(request).preflight(operation)


@router.post(
    "/session/action",
    response_model=SessionActionResult,
    summary="Execute one closed, confirmed ARES session transition",
)
async def session_action(payload: SessionActionRequest, request: Request) -> SessionActionResult:
    return await _session_service(request).execute(payload)


@router.get(
    "/terminal/contexts",
    response_model=TerminalContextCollection,
    summary="List manual terminal contexts; this does not expose a shell to the LLM",
)
async def terminal_contexts(request: Request) -> TerminalContextCollection:
    return await _terminal_service(request).contexts()


@router.post(
    "/terminal/plans",
    response_model=TerminalPlanResult,
    responses={409: _PROBLEM_SCHEMA},
    summary="Create an exact terminal plan without opening a terminal",
)
async def terminal_plan(payload: TerminalPlanRequest, request: Request) -> TerminalPlanResult:
    try:
        return _public_plan_result(
            await _terminal_service(request).create_plan(payload, session_id=_session(request))
        )
    except TerminalServiceError as exc:
        raise _terminal_problem(exc) from exc


@router.post(
    "/terminal/plans/{plan_id}/authorize-open",
    response_model=TerminalStartResult,
    responses={404: _PROBLEM_SCHEMA, 409: _PROBLEM_SCHEMA, 503: _PROBLEM_SCHEMA},
    summary="Authorize one exact terminal plan and open the local terminal",
)
async def terminal_authorize_open(
    plan_id: str, payload: TerminalAuthorizationRequest, request: Request
) -> TerminalStartResult:
    try:
        return _public_start_result(
            await _terminal_service(request).authorize_and_start(
                plan_id,
                payload,
                operator_uid=1000,
            )
        )
    except TerminalServiceError as exc:
        raise _terminal_problem(exc) from exc


@router.get(
    "/terminal/sessions",
    response_model=TerminalSessionCollection,
    summary="List terminal sessions without terminal contents",
)
async def terminal_sessions(request: Request) -> TerminalSessionCollection:
    return await _terminal_service(request).list_sessions()


@router.get(
    "/terminal/sessions/{session_id}",
    response_model=TerminalSession,
    responses={404: _PROBLEM_SCHEMA},
    summary="Read one terminal session state",
)
async def terminal_session(session_id: str, request: Request) -> TerminalSession:
    try:
        return await _terminal_service(request).get_session(session_id)
    except TerminalServiceError as exc:
        raise _terminal_problem(exc) from exc


@router.post(
    "/terminal/sessions/{session_id}/close",
    response_model=TerminalStartResult,
    responses={404: _PROBLEM_SCHEMA, 409: _PROBLEM_SCHEMA},
    summary="Request deterministic closure and cleanup for one terminal session",
)
async def terminal_close(
    session_id: str, payload: TerminalCloseRequest, request: Request
) -> TerminalStartResult:
    try:
        return _public_start_result(await _terminal_service(request).close(session_id, payload))
    except TerminalServiceError as exc:
        raise _terminal_problem(exc) from exc


@router.get(
    "/terminal/sessions/{session_id}/cleanup",
    response_model=TerminalCleanupEvidence,
    responses={404: _PROBLEM_SCHEMA},
    summary="Read cleanup evidence for one terminal session",
)
async def terminal_cleanup(session_id: str, request: Request) -> TerminalCleanupEvidence:
    try:
        return await _terminal_service(request).cleanup_evidence(session_id)
    except TerminalServiceError as exc:
        raise _terminal_problem(exc) from exc


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        if not path.is_file() or path.stat().st_size > _MAX_STATE_FILE_BYTES:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _session_service(request: Request) -> SystemSessionService:
    return cast(SystemSessionService, request.app.state.system_session_service)


def _terminal_service(request: Request) -> TerminalService:
    return cast(TerminalService, request.app.state.terminal_service)


def _session(request: Request) -> str:
    return str(getattr(request.state, "request_id", "terminal-session"))


def _public_plan_result(result: TerminalPlanResult) -> TerminalPlanResult:
    return result.model_copy(update={"plan": _public_plan(result.plan)})


def _public_start_result(result: TerminalStartResult) -> TerminalStartResult:
    return result.model_copy(update={"plan": _public_plan(result.plan)})


def _public_plan(plan: TerminalPlan) -> TerminalPlan:
    if plan.target is None:
        return plan
    target = plan.target.model_copy(
        update={
            "technical_path": None,
            "technical_details": {},
        }
    )
    return plan.model_copy(update={"target": target})


def _terminal_problem(exc: TerminalServiceError) -> AresProblem:
    code = exc.code
    if code in {"TERMINAL_PLAN_NOT_FOUND", "TERMINAL_SESSION_NOT_FOUND"}:
        status = 404
        title = "Terminal resource not found"
    elif code in {"TERMINAL_BROKER_UNAVAILABLE", "AUDIT_LEDGER_UNAVAILABLE"}:
        status = 503
        title = "Terminal authority unavailable"
    else:
        status = 409
        title = "Terminal request rejected"
    return AresProblem(
        status=status,
        code=code,
        title=title,
        detail="ARES rejected the terminal request because a safety precondition was not satisfied.",
    )
