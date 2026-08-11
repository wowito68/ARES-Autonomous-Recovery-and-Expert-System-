"""HTTP projection for Storage Operation Engine lifecycle."""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Query, Request, status

from ares.core.problems import AresProblem, ProblemDetail
from ares.storage_operations.models import (
    StorageLayout,
    StorageOperationPlan,
    StorageOperationRecord,
    StorageVerification,
)
from ares.storage_operations.service import (
    StorageOperationAccepted,
    StorageOperationActionRequest,
    StorageOperationPlanRequest,
    StorageOperationService,
    StorageOperationServiceError,
)

router = APIRouter()


@router.get(
    "/layout",
    response_model=StorageLayout,
    responses={503: {"model": ProblemDetail}},
    summary="Inspect an exact storage layout without modifying it",
)
async def layout(
    request: Request,
    target_disk: str = Query(min_length=1, max_length=4096),
) -> StorageLayout:
    try:
        return await _service(request).layout(target_disk)
    except StorageOperationServiceError as exc:
        raise _problem(exc, "Storage layout unavailable", 503) from exc


@router.post(
    "/operations/plan",
    response_model=StorageOperationPlan,
    summary="Generate a declarative partition operation plan",
)
async def plan(payload: StorageOperationPlanRequest, request: Request) -> StorageOperationPlan:
    try:
        return await _service(request).plan(
            payload,
            session_id=_session(request),
            created_by="local-api-user",
        )
    except StorageOperationServiceError as exc:
        raise _problem(exc, "Storage operation plan rejected", 422) from exc


@router.post(
    "/operations/{operation_id}/validate",
    response_model=StorageOperationPlan,
    summary="Revalidate identity, impact, dry-run and protection checkpoint",
)
async def validate(operation_id: str, request: Request) -> StorageOperationPlan:
    try:
        return await _service(request).validate(operation_id, session_id=_session(request))
    except StorageOperationServiceError as exc:
        raise _problem(exc, "Storage operation validation failed", 409) from exc


@router.post(
    "/operations/{operation_id}/authorize",
    response_model=StorageOperationAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Request independent authorization for an exact validated plan",
)
async def authorize(
    operation_id: str,
    payload: StorageOperationActionRequest,
    request: Request,
) -> StorageOperationAccepted:
    try:
        return await _service(request).authorize(
            operation_id,
            payload,
            session_id=_session(request),
        )
    except StorageOperationServiceError as exc:
        raise _problem(exc, "Storage authorization request rejected", 409) from exc


@router.post(
    "/operations/{operation_id}/execute",
    response_model=StorageOperationAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Execute an independently authorized storage operation",
)
async def execute(operation_id: str, request: Request) -> StorageOperationAccepted:
    try:
        return await _service(request).execute(
            operation_id,
            session_id=_session(request),
            created_by="local-api-user",
        )
    except StorageOperationServiceError as exc:
        raise _problem(exc, "Storage operation execution rejected", 409) from exc


@router.get(
    "/operations/{operation_id}",
    response_model=StorageOperationRecord,
    summary="Read durable partition-operation state",
)
async def operation(operation_id: str, request: Request) -> StorageOperationRecord:
    result = await _service(request).get(operation_id)
    if result is None:
        raise _problem_code("STORAGE_OPERATION_NOT_FOUND", "Storage operation not found", 404)
    return result


@router.get(
    "/operations/{operation_id}/verification",
    response_model=StorageVerification,
    summary="Read post-operation verification or UNKNOWN reconciliation evidence",
)
async def verification(operation_id: str, request: Request) -> StorageVerification:
    result = await _service(request).verification(operation_id)
    if result is None:
        raise _problem_code("STORAGE_VERIFICATION_NOT_FOUND", "Storage verification not found", 404)
    return result


@router.post(
    "/operations/{operation_id}/reconcile",
    response_model=StorageVerification,
    summary="Reinspect an UNKNOWN transaction without retrying its mutation",
)
async def reconcile(operation_id: str, request: Request) -> StorageVerification:
    try:
        return await _service(request).reconcile_unknown(operation_id, session_id=_session(request))
    except StorageOperationServiceError as exc:
        raise _problem(exc, "Storage transaction reconciliation failed", 409) from exc


def _service(request: Request) -> StorageOperationService:
    return cast(StorageOperationService, request.app.state.storage_operation_service)


def _session(request: Request) -> str:
    return str(
        getattr(request.state, "session_id", getattr(request.state, "request_id", "storage"))
    )


def _problem(exc: StorageOperationServiceError, title: str, status_code: int) -> AresProblem:
    return _problem_code(exc.code, title, status_code)


def _problem_code(code: str, title: str, status_code: int) -> AresProblem:
    return AresProblem(
        status=status_code,
        code=code,
        title=title,
        detail="ARES refused or could not complete the requested storage-operation lifecycle step.",
    )
