"""Boot diagnostics API projection."""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, Request

from ares.boot import BootDiagnosticInput, BootDiagnosticResult
from ares.core.problems import AresProblem, ProblemDetail

router = APIRouter()
_PROBLEM_SCHEMA = {
    "content": {"application/problem+json": {"schema": ProblemDetail.model_json_schema()}}
}


@router.post(
    "/diagnose",
    response_model=BootDiagnosticResult,
    responses={404: _PROBLEM_SCHEMA, 409: _PROBLEM_SCHEMA},
    summary="Run read-only boot diagnostics from existing ARES evidence",
)
async def diagnose(payload: BootDiagnosticInput, request: Request) -> BootDiagnosticResult:
    try:
        execution = await _capabilities(request).execute(
            "boot.diagnose",
            payload.model_dump(mode="json"),
        )
    except KeyError as exc:
        raise AresProblem(
            status=404,
            code="BOOT_DIAGNOSE_UNAVAILABLE",
            title="Boot diagnostics unavailable",
            detail="The boot.diagnose Capability is not installed.",
        ) from exc
    if getattr(execution.status, "value", execution.status) != "succeeded" or execution.result is None:
        raise AresProblem(
            status=409,
            code=execution.error_code or "BOOT_DIAGNOSE_FAILED",
            title="Boot diagnostics failed",
            detail="ARES could not complete read-only boot diagnostics from current evidence.",
        )
    return BootDiagnosticResult.model_validate(execution.result)


def _capabilities(request: Request) -> Any:
    return cast(Any, request.app.state.capability_manager)
