"""Liveness and dependency-aware readiness endpoints."""

from __future__ import annotations

import asyncio
from typing import Literal, cast

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import SQLAlchemyError

from ares.config import Settings
from ares.core.problems import AresProblem, ProblemDetail
from ares.database import Database

router = APIRouter()


class LiveStatus(BaseModel):
    """Process liveness response."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    service: str
    version: str


class ReadyStatus(LiveStatus):
    """Dependency readiness response."""

    checks: dict[str, Literal["ok"]]


@router.get("/live", response_model=LiveStatus, summary="Check process liveness")
async def live(request: Request) -> LiveStatus:
    """Return success while the API process can serve requests."""

    settings = _settings(request)
    return LiveStatus(service=settings.service_name, version=settings.version)


@router.get(
    "/ready",
    response_model=ReadyStatus,
    responses={
        503: {
            "description": "A dependency is unavailable",
            "content": {"application/problem+json": {"schema": ProblemDetail.model_json_schema()}},
        }
    },
    summary="Check service readiness",
)
async def ready(request: Request) -> ReadyStatus:
    """Return success only when the local database answers a real query."""

    settings = _settings(request)
    database = _database(request)
    try:
        async with asyncio.timeout(settings.readiness_timeout_seconds):
            await database.check()
    except (TimeoutError, OSError, SQLAlchemyError) as exc:
        raise AresProblem(
            status=503,
            code="DEPENDENCY_UNAVAILABLE",
            title="Service unavailable",
            detail="The local database is not ready.",
        ) from exc
    return ReadyStatus(
        service=settings.service_name,
        version=settings.version,
        checks={"database": "ok"},
    )


def _settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def _database(request: Request) -> Database:
    return cast(Database, request.app.state.database)
