"""Capability-level planning API."""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Request

from ares.planner import ExecutionPlan, Planner
from ares.reasoning.models import ReasoningRequest

router = APIRouter()


@router.post(
    "/plan",
    response_model=ExecutionPlan,
    summary="Build a command-free capability execution plan",
)
async def build_plan(payload: ReasoningRequest, request: Request) -> ExecutionPlan:
    return _planner(request).plan(payload)


def _planner(request: Request) -> Planner:
    return cast(Planner, request.app.state.planner)
