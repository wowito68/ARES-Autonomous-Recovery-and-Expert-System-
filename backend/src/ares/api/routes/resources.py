"""Resource catalog API backed by real ARES snapshots."""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Request

from ares.core.problems import AresProblem, ProblemDetail
from ares.resources.models import ResourceCatalog, ResourceResolution
from ares.resources.service import ResourceResolver
from ares.storage.service import StorageAnalysisError, StorageAnalysisResponse, StorageAnalysisService

router = APIRouter()
_PROBLEM_SCHEMA = {
    "content": {"application/problem+json": {"schema": ProblemDetail.model_json_schema()}}
}


@router.get(
    "",
    response_model=ResourceCatalog,
    summary="List human-selectable resources from the latest real snapshot",
)
async def catalog(request: Request) -> ResourceCatalog:
    return await _resolver(request).catalog()


@router.post(
    "/refresh",
    response_model=StorageAnalysisResponse,
    responses={503: _PROBLEM_SCHEMA},
    summary="Refresh read-only storage evidence before resolving resources",
)
async def refresh(request: Request) -> StorageAnalysisResponse:
    session_id = str(getattr(request.state, "request_id", "resource-refresh"))
    try:
        return await _storage(request).analyze(session_id=session_id)
    except StorageAnalysisError as exc:
        raise AresProblem(
            status=503,
            code=exc.code,
            title="Resource discovery unavailable",
            detail="ARES could not refresh storage evidence for resource resolution.",
        ) from exc


@router.get(
    "/{resource_id:path}",
    response_model=ResourceResolution,
    responses={404: _PROBLEM_SCHEMA},
    summary="Resolve one human resource id to its validated technical identity",
)
async def resolve(resource_id: str, request: Request) -> ResourceResolution:
    result = await _resolver(request).resolve(resource_id)
    if result is None:
        raise AresProblem(
            status=404,
            code="RESOURCE_NOT_FOUND",
            title="Resource not found",
            detail="No current ARES resource matches that identifier.",
        )
    return result


def _resolver(request: Request) -> ResourceResolver:
    return cast(ResourceResolver, request.app.state.resource_resolver)


def _storage(request: Request) -> StorageAnalysisService:
    return cast(StorageAnalysisService, request.app.state.storage_analysis_service)
