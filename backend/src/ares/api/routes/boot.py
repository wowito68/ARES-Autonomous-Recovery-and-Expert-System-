"""HTTP control plane for boot diagnosis and recovery."""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Request, status

from ares.boot.models import (
    BootDiagnosticResult,
    BootRepairPlan,
    BootRepairRecord,
    BootVerification,
)
from ares.boot.service import (
    BootDiagnoseRequest,
    BootRecoveryService,
    BootRecoveryServiceError,
    BootRepairAccepted,
    BootRepairPlanRequest,
    BootRepairRequest,
)
from ares.core.problems import AresProblem

router = APIRouter(prefix="/boot", tags=["boot-recovery"])


@router.post("/diagnose", response_model=BootDiagnosticResult)
async def diagnose(payload: BootDiagnoseRequest, request: Request) -> BootDiagnosticResult:
    try:
        return await _service(request).diagnose(payload, session_id=_session(request))
    except BootRecoveryServiceError as exc:
        raise _problem(exc, status.HTTP_422_UNPROCESSABLE_CONTENT) from exc


@router.post("/repair/plan", response_model=BootRepairPlan)
async def plan(payload: BootRepairPlanRequest, request: Request) -> BootRepairPlan:
    try:
        return await _service(request).plan(payload, session_id=_session(request))
    except BootRecoveryServiceError as exc:
        raise _problem(exc, status.HTTP_422_UNPROCESSABLE_CONTENT) from exc


@router.post(
    "/repair",
    response_model=BootRepairAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def repair(payload: BootRepairRequest, request: Request) -> BootRepairAccepted:
    try:
        return await _service(request).start(
            payload, session_id=_session(request), created_by="api"
        )
    except BootRecoveryServiceError as exc:
        raise _problem(exc, status.HTTP_409_CONFLICT) from exc


@router.get("/repairs/{repair_id}", response_model=BootRepairRecord)
async def repair_status(repair_id: str, request: Request) -> BootRepairRecord:
    record = await _service(request).get(repair_id)
    if record is None:
        raise AresProblem(
            status=status.HTTP_404_NOT_FOUND,
            code="BOOT_REPAIR_NOT_FOUND",
            title="Boot repair not found",
            detail="The requested boot repair does not exist.",
        )
    return record


@router.get("/repairs/{repair_id}/verification", response_model=BootVerification)
async def verification(repair_id: str, request: Request) -> BootVerification:
    result = await _service(request).verification(repair_id)
    if result is None:
        raise AresProblem(
            status=status.HTTP_404_NOT_FOUND,
            code="BOOT_VERIFICATION_NOT_FOUND",
            title="Boot verification not found",
            detail="No post-repair verification evidence exists for this repair.",
        )
    return result


@router.post("/repairs/{repair_id}/cancel", response_model=BootRepairRecord)
async def cancel(repair_id: str, request: Request) -> BootRepairRecord:
    try:
        return await _service(request).cancel(repair_id, session_id=_session(request))
    except BootRecoveryServiceError as exc:
        raise _problem(exc, status.HTTP_409_CONFLICT) from exc


@router.post("/repairs/{repair_id}/reconcile", response_model=BootVerification)
async def reconcile(repair_id: str, request: Request) -> BootVerification:
    try:
        return await _service(request).reconcile(repair_id, session_id=_session(request))
    except BootRecoveryServiceError as exc:
        raise _problem(exc, status.HTTP_409_CONFLICT) from exc


def _service(request: Request) -> BootRecoveryService:
    return cast(BootRecoveryService, request.app.state.boot_recovery_service)


def _session(request: Request) -> str:
    session_id = getattr(request.state, "session_id", None)
    return session_id if isinstance(session_id, str) else "api-session-unknown"


def _problem(error: BootRecoveryServiceError, status_code: int) -> AresProblem:
    return AresProblem(
        status=status_code,
        code=error.code,
        title="Boot recovery request rejected",
        detail="ARES rejected the boot recovery operation at a validated safety boundary.",
    )
