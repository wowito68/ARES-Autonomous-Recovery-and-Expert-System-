"""Storage-specific API projection over the shared application service."""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from ares.core.problems import AresProblem, ProblemDetail
from ares.storage.models import DiskSnapshot, SystemStorageSnapshot
from ares.storage.service import StorageAnalysisError, StorageAnalysisResponse, StorageAnalysisService

router = APIRouter()


class DiskList(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    disks: tuple[DiskSnapshot, ...]
    count: int


@router.get("/disks", response_model=DiskList, summary="List disks from the latest snapshot")
async def disks(request: Request) -> DiskList:
    items = await _service(request).disks()
    return DiskList(disks=items, count=len(items))


@router.post(
    "/analyze",
    response_model=StorageAnalysisResponse,
    responses={
        503: {
            "description": "Storage evidence or analysis unavailable",
            "content": {"application/problem+json": {"schema": ProblemDetail.model_json_schema()}},
        }
    },
    summary="Execute the read-only storage.disk-analysis vertical slice",
)
async def analyze(request: Request) -> StorageAnalysisResponse:
    session_id = str(getattr(request.state, "request_id", "storage-session"))
    try:
        return await _service(request).analyze(session_id=session_id)
    except StorageAnalysisError as exc:
        raise AresProblem(
            status=503,
            code=exc.code,
            title="Storage analysis unavailable",
            detail="ARES could not produce a verified storage analysis from current evidence.",
        ) from exc


@router.get(
    "/snapshots/{snapshot_id}",
    response_model=SystemStorageSnapshot,
    responses={
        404: {
            "description": "Snapshot not found",
            "content": {"application/problem+json": {"schema": ProblemDetail.model_json_schema()}},
        }
    },
    summary="Read one persisted storage snapshot",
)
async def snapshot(snapshot_id: str, request: Request) -> SystemStorageSnapshot:
    result = await _service(request).snapshot(snapshot_id)
    if result is None:
        raise AresProblem(
            status=404,
            code="STORAGE_SNAPSHOT_NOT_FOUND",
            title="Storage snapshot not found",
            detail="No storage snapshot with that identifier is available.",
        )
    return result


def _service(request: Request) -> StorageAnalysisService:
    return cast(StorageAnalysisService, request.app.state.storage_analysis_service)
