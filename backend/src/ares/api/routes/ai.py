"""Status endpoint for the optional local AI runtime."""

from __future__ import annotations

from typing import Literal, cast

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from ares.llm import AIRuntime

router = APIRouter()


class AIStatusResponse(BaseModel):
    """Public, non-secret model readiness state."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ready", "model_missing", "runtime_unavailable"]
    runtime: Literal["ollama"] = "ollama"
    model: str
    installed: bool
    local_only: Literal[True] = True
    tools_enabled: Literal[False] = False


@router.get("/status", response_model=AIStatusResponse, summary="Inspect local AI readiness")
async def status(request: Request) -> AIStatusResponse:
    """Report degraded state without making API readiness depend on the model."""

    runtime = cast(AIRuntime, request.app.state.ai_runtime)
    state = await runtime.status()
    return AIStatusResponse(
        status=state.state,
        model=state.model,
        installed=state.state == "ready",
    )
