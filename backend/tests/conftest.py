"""Shared test application and HTTP client fixtures."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from ares.config import Environment, LogFormat, Settings
from ares.main import create_app


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Use isolated storage and disable real process probes in tests."""

    return Settings(
        environment=Environment.TEST,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'ares-test.db'}",
        runtime_state_dir=tmp_path / "run",
        capability_state_dir=tmp_path / "capabilities",
        storage_process_probes_enabled=False,
        log_level="CRITICAL",
        log_format=LogFormat.TEXT,
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
            yield http_client
