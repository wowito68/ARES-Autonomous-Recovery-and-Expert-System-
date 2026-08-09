"""Backup API projection over BackupService."""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Request, status
from pydantic import BaseModel, ConfigDict

from ares.backup.models import Backup, BackupManifest, BackupPlan, BackupVerification
from ares.backup.service import (
    BackupAccepted,
    BackupCreateRequest,
    BackupPlanRequest,
    BackupService,
    BackupServiceError,
)
from ares.core.problems import AresProblem, ProblemDetail

router = APIRouter()


class BackupCollection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    backups: tuple[Backup, ...]
    count: int


@router.post(
    "/plan",
    response_model=BackupPlan,
    responses={422: {"content": {"application/problem+json": {"schema": ProblemDetail.model_json_schema()}}}},
    summary="Generate and persist a structured backup plan without copying data",
)
async def plan(payload: BackupPlanRequest, request: Request) -> BackupPlan:
    try:
        return await _service(request).plan(payload, session_id=_session(request))
    except BackupServiceError as exc:
        raise _problem(exc) from exc


@router.post(
    "",
    response_model=BackupAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        404: {"content": {"application/problem+json": {"schema": ProblemDetail.model_json_schema()}}},
        409: {"content": {"application/problem+json": {"schema": ProblemDetail.model_json_schema()}}},
        503: {"content": {"application/problem+json": {"schema": ProblemDetail.model_json_schema()}}},
    },
    summary="Request authorization and start backup.create asynchronously",
)
async def create(payload: BackupCreateRequest, request: Request) -> BackupAccepted:
    try:
        return await _service(request).create(
            payload,
            session_id=_session(request),
            created_by="local-user",
        )
    except BackupServiceError as exc:
        raise _problem(exc) from exc


@router.get("", response_model=BackupCollection, summary="List known backups")
async def list_backups(request: Request) -> BackupCollection:
    try:
        items = await _service(request).list()
    except BackupServiceError as exc:
        raise _problem(exc) from exc
    return BackupCollection(backups=items, count=len(items))


@router.get("/{backup_id}", response_model=Backup, summary="Read one backup record")
async def get_backup(backup_id: str, request: Request) -> Backup:
    backup = await _service(request).get(backup_id)
    if backup is None:
        raise _problem(BackupServiceError("BACKUP_NOT_FOUND"))
    return backup


@router.get(
    "/{backup_id}/manifest",
    response_model=BackupManifest,
    summary="Read the immutable manifest of one backup",
)
async def manifest(backup_id: str, request: Request) -> BackupManifest:
    result = await _service(request).manifest(backup_id)
    if result is None:
        raise _problem(BackupServiceError("BACKUP_MANIFEST_NOT_FOUND"))
    return result


@router.get(
    "/{backup_id}/verification",
    response_model=BackupVerification,
    summary="Read the latest integrity verification of one backup",
)
async def verification(backup_id: str, request: Request) -> BackupVerification:
    result = await _service(request).verification(backup_id)
    if result is None:
        raise _problem(BackupServiceError("BACKUP_VERIFICATION_NOT_FOUND"))
    return result


@router.post(
    "/{backup_id}/cancel",
    response_model=Backup,
    summary="Request cancellation of a running backup",
)
async def cancel(backup_id: str, request: Request) -> Backup:
    try:
        return await _service(request).cancel(backup_id, session_id=_session(request))
    except BackupServiceError as exc:
        raise _problem(exc) from exc


def _service(request: Request) -> BackupService:
    return cast(BackupService, request.app.state.backup_service)


def _session(request: Request) -> str:
    return str(getattr(request.state, "request_id", "backup-session"))


def _problem(exc: BackupServiceError) -> AresProblem:
    code = exc.code
    if code in {
        "BACKUP_NOT_FOUND",
        "BACKUP_PLAN_NOT_FOUND",
        "BACKUP_MANIFEST_NOT_FOUND",
        "BACKUP_VERIFICATION_NOT_FOUND",
    }:
        http_status = 404
        title = "Backup resource not found"
    elif code in {
        "BACKUP_BROKER_UNAVAILABLE",
        "AUDIT_LEDGER_UNAVAILABLE",
        "BACKUP_CANCELLATION_UNAVAILABLE",
    }:
        http_status = 503
        title = "Backup service unavailable"
    elif code in {
        "BACKUP_DESTINATION_NOT_WRITABLE",
        "BACKUP_DESTINATION_READ_ONLY",
        "BACKUP_SOURCE_NOT_READABLE",
    }:
        http_status = 403
        title = "Backup access denied"
    else:
        http_status = 409
        title = "Backup request rejected"
    return AresProblem(
        status=http_status,
        code=code,
        title=title,
        detail="ARES rejected the backup operation because a safety precondition was not satisfied.",
    )
