"""Filesystem inspection and repair API over FilesystemRepairService."""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Request, status

from ares.core.problems import AresProblem, ProblemDetail
from ares.filesystems import (
    FilesystemInspectRequest,
    FilesystemInspection,
    FilesystemRepairPlan,
    FilesystemRepairPlanRequest,
    FilesystemRepairRecord,
    FilesystemRepairService,
    FilesystemRepairStartRequest,
    FilesystemServiceError,
    RepairVerification,
)

router = APIRouter()
_PROBLEM_SCHEMA = {
    "content": {"application/problem+json": {"schema": ProblemDetail.model_json_schema()}}
}


@router.post(
    "/inspect",
    response_model=FilesystemInspection,
    responses={404: _PROBLEM_SCHEMA, 409: _PROBLEM_SCHEMA, 503: _PROBLEM_SCHEMA},
    summary="Inspect exact filesystem identity, mount safety and health without modifying it",
)
async def inspect(
    payload: FilesystemInspectRequest, request: Request
) -> FilesystemInspection:
    try:
        return await _service(request).inspect(payload, session_id=_session(request))
    except FilesystemServiceError as exc:
        raise _problem(exc) from exc


@router.post(
    "/repair/plan",
    response_model=FilesystemRepairPlan,
    responses={404: _PROBLEM_SCHEMA, 409: _PROBLEM_SCHEMA, 503: _PROBLEM_SCHEMA},
    summary="Generate a structured repair plan and bind a verified protection checkpoint",
)
async def plan(
    payload: FilesystemRepairPlanRequest, request: Request
) -> FilesystemRepairPlan:
    try:
        return await _service(request).plan(payload, session_id=_session(request))
    except FilesystemServiceError as exc:
        raise _problem(exc) from exc


@router.post(
    "/repair",
    response_model=FilesystemRepairRecord,
    status_code=status.HTTP_202_ACCEPTED,
    responses={404: _PROBLEM_SCHEMA, 409: _PROBLEM_SCHEMA, 503: _PROBLEM_SCHEMA},
    summary="Request independent authorization and start an exact filesystem repair plan",
)
async def repair(
    payload: FilesystemRepairStartRequest, request: Request
) -> FilesystemRepairRecord:
    try:
        return await _service(request).start(
            payload,
            session_id=_session(request),
            created_by="local-user",
        )
    except FilesystemServiceError as exc:
        raise _problem(exc) from exc


@router.get(
    "/repairs/{repair_id}",
    response_model=FilesystemRepairRecord,
    responses={404: _PROBLEM_SCHEMA},
    summary="Read filesystem repair state",
)
async def repair_status(repair_id: str, request: Request) -> FilesystemRepairRecord:
    result = await _service(request).get(repair_id)
    if result is None:
        raise _problem(FilesystemServiceError("FILESYSTEM_REPAIR_NOT_FOUND"))
    return result


@router.get(
    "/repairs/{repair_id}/verification",
    response_model=RepairVerification,
    responses={404: _PROBLEM_SCHEMA},
    summary="Read structured post-repair verification",
)
async def verification(repair_id: str, request: Request) -> RepairVerification:
    result = await _service(request).verification(repair_id)
    if result is None:
        raise _problem(FilesystemServiceError("FILESYSTEM_VERIFICATION_NOT_FOUND"))
    return result


@router.post(
    "/repairs/{repair_id}/cancel",
    response_model=FilesystemRepairRecord,
    responses={404: _PROBLEM_SCHEMA, 409: _PROBLEM_SCHEMA},
    summary="Cancel only before destructive filesystem repair has started",
)
async def cancel(repair_id: str, request: Request) -> FilesystemRepairRecord:
    try:
        return await _service(request).cancel(repair_id, session_id=_session(request))
    except FilesystemServiceError as exc:
        raise _problem(exc) from exc


def _service(request: Request) -> FilesystemRepairService:
    return cast(FilesystemRepairService, request.app.state.filesystem_repair_service)


def _session(request: Request) -> str:
    return str(getattr(request.state, "request_id", "filesystem-session"))


def _problem(exc: FilesystemServiceError) -> AresProblem:
    code = exc.code
    if code in {
        "FILESYSTEM_TARGET_NOT_FOUND",
        "FILESYSTEM_REPAIR_PLAN_NOT_FOUND",
        "FILESYSTEM_REPAIR_NOT_FOUND",
        "FILESYSTEM_VERIFICATION_NOT_FOUND",
    }:
        http_status = 404
        title = "Filesystem resource not found"
    elif code in {
        "FILESYSTEM_BROKER_UNAVAILABLE",
        "FILESYSTEM_BROKER_DISCONNECTED",
        "AUDIT_LEDGER_UNAVAILABLE",
    }:
        http_status = 503
        title = "Filesystem repair service unavailable"
    elif code in {
        "FILESYSTEM_TARGET_NOT_WRITABLE",
        "FILESYSTEM_TARGET_NOT_BLOCK_DEVICE",
    }:
        http_status = 403
        title = "Filesystem access denied"
    else:
        http_status = 409
        title = "Filesystem operation rejected"
    return AresProblem(
        status=http_status,
        code=code,
        title=title,
        detail=(
            "ARES rejected the filesystem operation because a high-risk safety precondition "
            "was not satisfied."
        ),
    )
