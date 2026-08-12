"""HTTP projection for evidence-driven System Recovery cases."""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Request, status

from ares.core.problems import AresProblem, ProblemDetail
from ares.recovery.models import (
    RecoveryCase,
    RecoveryDiagnoseInput,
    RecoveryPlanInput,
    SystemRecoveryPlan,
    SystemRecoveryVerification,
)
from ares.recovery.service import RecoveryAccepted, RecoveryService, RecoveryServiceError

router = APIRouter(prefix="/recovery", tags=["recovery"])


@router.post(
    "/diagnose",
    response_model=RecoveryCase,
    responses={422: {"model": ProblemDetail}},
)
async def diagnose(payload: RecoveryDiagnoseInput, request: Request) -> RecoveryCase:
    try:
        return await _service(request).diagnose(payload, session_id=_session(request))
    except RecoveryServiceError as exc:
        raise _problem(exc, "Recovery diagnosis rejected", 422) from exc


@router.post(
    "/plan",
    response_model=SystemRecoveryPlan,
    responses={409: {"model": ProblemDetail}},
)
async def plan(payload: RecoveryPlanInput, request: Request) -> SystemRecoveryPlan:
    try:
        return await _service(request).plan(payload)
    except RecoveryServiceError as exc:
        raise _problem(exc, "Recovery plan rejected", 409) from exc


@router.post(
    "/{case_id}/authorize",
    response_model=RecoveryAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    responses={409: {"model": ProblemDetail}},
)
async def authorize(case_id: str, request: Request) -> RecoveryAccepted:
    try:
        return await _service(request).authorize(case_id)
    except RecoveryServiceError as exc:
        raise _problem(exc, "Recovery authorization request rejected", 409) from exc


@router.post(
    "/{case_id}/execute",
    response_model=RecoveryAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    responses={409: {"model": ProblemDetail}},
)
async def execute(case_id: str, request: Request) -> RecoveryAccepted:
    try:
        return await _service(request).execute(case_id)
    except RecoveryServiceError as exc:
        raise _problem(exc, "Recovery execution rejected", 409) from exc


@router.get(
    "/{case_id}",
    response_model=RecoveryCase,
    responses={404: {"model": ProblemDetail}},
)
async def get_case(case_id: str, request: Request) -> RecoveryCase:
    result = await _service(request).get(case_id)
    if result is None:
        raise _problem_code("RECOVERY_CASE_NOT_FOUND", "Recovery case not found", 404)
    return result


@router.get(
    "/{case_id}/verification",
    response_model=SystemRecoveryVerification,
    responses={404: {"model": ProblemDetail}},
)
async def verification(case_id: str, request: Request) -> SystemRecoveryVerification:
    result = await _service(request).verification(case_id)
    if result is None:
        raise _problem_code(
            "RECOVERY_VERIFICATION_NOT_FOUND", "Recovery verification not found", 404
        )
    return result


@router.post(
    "/{case_id}/abort",
    response_model=RecoveryCase,
    responses={409: {"model": ProblemDetail}},
)
async def abort(case_id: str, request: Request) -> RecoveryCase:
    try:
        return await _service(request).abort(case_id)
    except RecoveryServiceError as exc:
        raise _problem(exc, "Recovery abort rejected", 409) from exc


def _service(request: Request) -> RecoveryService:
    return cast(RecoveryService, request.app.state.recovery_service)


def _session(request: Request) -> str:
    return str(
        getattr(request.state, "session_id", getattr(request.state, "request_id", "recovery"))
    )


def _problem(exc: RecoveryServiceError, title: str, status_code: int) -> AresProblem:
    return _problem_code(exc.code, title, status_code)


def _problem_code(code: str, title: str, status_code: int) -> AresProblem:
    return AresProblem(
        status=status_code,
        code=code,
        title=title,
        detail="ARES refused or could not complete the requested recovery lifecycle step.",
    )
