"""FastAPI composition root."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from ares.api.router import api_router
from ares.config import Environment, Settings, get_settings
from ares.core.logging import configure_logging
from ares.core.middleware import RequestContextMiddleware
from ares.core.problems import install_problem_handlers
from ares.database import Database


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build an isolated application instance for production or tests."""

    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings)
    database = Database(
        resolved_settings.database_url,
        echo=resolved_settings.database_echo,
        busy_timeout_ms=resolved_settings.database_busy_timeout_ms,
        require_existing_file=resolved_settings.environment is Environment.PRODUCTION,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        database.prepare_storage()
        try:
            yield
        finally:
            await database.dispose()

    application = FastAPI(
        title="ARES API",
        version=resolved_settings.version,
        description="Local control plane for ARES.",
        debug=False,
        docs_url=None,
        redoc_url=None,
        openapi_url=(
            "/openapi.json" if resolved_settings.environment is not Environment.PRODUCTION else None
        ),
        lifespan=lifespan,
    )
    application.state.settings = resolved_settings
    application.state.database = database
    application.add_middleware(RequestContextMiddleware)
    install_problem_handlers(application)
    application.include_router(api_router, prefix=resolved_settings.api_prefix)
    return application
