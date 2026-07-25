"""FastAPI composition root."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from ares.api.router import api_router
from ares.capabilities import CapabilityManager
from ares.capabilities.plugins import DiskAnalysisPlugin
from ares.config import Environment, Settings, get_settings
from ares.core.logging import configure_logging
from ares.core.middleware import RequestContextMiddleware
from ares.core.problems import install_problem_handlers
from ares.database import Database
from ares.events import EventBus, JsonlEventSink
from ares.knowledge import KnowledgeGraph
from ares.llm import AIRuntime, OllamaRuntime
from ares.reasoning import ReasoningEngine
from ares.workflows import WorkflowEngine


def create_app(
    settings: Settings | None = None,
    *,
    ai_runtime: AIRuntime | None = None,
) -> FastAPI:
    """Build an isolated application instance for production or tests."""

    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings)
    database = Database(
        resolved_settings.database_url,
        echo=resolved_settings.database_echo,
        busy_timeout_ms=resolved_settings.database_busy_timeout_ms,
        require_existing_file=resolved_settings.environment is Environment.PRODUCTION,
    )
    capability_state_dir = resolved_settings.capability_state_dir
    if capability_state_dir is None:
        capability_state_dir = (
            database.database_path.parent / "capabilities"
            if database.database_path is not None
            else resolved_settings.runtime_state_dir / "capabilities"
        )
    event_bus = EventBus(JsonlEventSink(capability_state_dir / "events.jsonl"))
    knowledge_graph = KnowledgeGraph(capability_state_dir / "knowledge-graph.json")
    workflow_engine = WorkflowEngine(event_bus, knowledge_graph)
    capability_manager = CapabilityManager(
        workflow_engine,
        live_mode=_read_live_mode(resolved_settings),
    )
    capability_manager.load(
        (
            DiskAnalysisPlugin(
                resolved_settings.runtime_state_dir / "hardware/public/inventory-v1.json"
            ),
        )
    )
    capability_manager.seal()
    reasoning_engine = ReasoningEngine(capability_manager)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        database.prepare_storage()
        event_bus.prepare()
        knowledge_graph.prepare()
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
    application.state.ai_runtime = ai_runtime or OllamaRuntime(resolved_settings)
    application.state.event_bus = event_bus
    application.state.knowledge_graph = knowledge_graph
    application.state.workflow_engine = workflow_engine
    application.state.capability_manager = capability_manager
    application.state.reasoning_engine = reasoning_engine
    application.add_middleware(RequestContextMiddleware)
    install_problem_handlers(application)
    application.include_router(api_router, prefix=resolved_settings.api_prefix)
    if resolved_settings.static_dir is not None and resolved_settings.static_dir.is_dir():
        application.mount(
            "/",
            StaticFiles(directory=resolved_settings.static_dir, html=True),
            name="ares-interface",
        )
    return application


def _read_live_mode(settings: Settings) -> str:
    try:
        mode = (settings.runtime_state_dir / "mode").read_text(encoding="ascii").strip()
    except OSError:
        return "live"
    return mode if mode in {"live", "persistent", "forensic", "recovery"} else "live"
