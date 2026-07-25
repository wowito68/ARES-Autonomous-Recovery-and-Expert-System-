"""Evidence-first reasoning boundary; it never executes a capability."""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Request

from ares.reasoning import ReasoningAssessment, ReasoningEngine, ReasoningRequest

router = APIRouter()


@router.post(
    "/assess",
    response_model=ReasoningAssessment,
    summary="Build hypotheses and select at most one capability",
)
async def assess(payload: ReasoningRequest, request: Request) -> ReasoningAssessment:
    engine = cast(ReasoningEngine, request.app.state.reasoning_engine)
    return engine.assess(payload)
