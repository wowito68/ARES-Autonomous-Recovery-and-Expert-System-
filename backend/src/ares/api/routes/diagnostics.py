"""Diagnostic retrieval API."""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Request

from ares.core.problems import AresProblem, ProblemDetail
from ares.diagnostics import DiagnosticResult
from ares.storage.service import StorageAnalysisService

router = APIRouter()


@router.get(
    "/{diagnostic_id}",
    response_model=DiagnosticResult,
    responses={
        404: {
            "description": "Diagnostic not found",
            "content": {"application/problem+json": {"schema": ProblemDetail.model_json_schema()}},
        }
    },
    summary="Read a persisted structured diagnostic",
)
async def diagnostic(diagnostic_id: str, request: Request) -> DiagnosticResult:
    result = await _service(request).diagnostic(diagnostic_id)
    if result is None:
        raise AresProblem(
            status=404,
            code="DIAGNOSTIC_NOT_FOUND",
            title="Diagnostic not found",
            detail="No diagnostic with that identifier is available.",
        )
    return result


def _service(request: Request) -> StorageAnalysisService:
    return cast(StorageAnalysisService, request.app.state.storage_analysis_service)
