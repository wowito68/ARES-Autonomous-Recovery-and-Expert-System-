"""Versioned API router."""

from fastapi import APIRouter

from ares.api.routes.ai import router as ai_router
from ares.api.routes.assistant import router as assistant_router
from ares.api.routes.capabilities import router as capabilities_router
from ares.api.routes.diagnostics import router as diagnostics_router
from ares.api.routes.health import router as health_router
from ares.api.routes.knowledge import router as knowledge_router
from ares.api.routes.planner import router as planner_router
from ares.api.routes.reasoning import router as reasoning_router
from ares.api.routes.storage import router as storage_router
from ares.api.routes.system import router as system_router

api_router = APIRouter()
api_router.include_router(health_router, prefix="/health", tags=["health"])
api_router.include_router(ai_router, prefix="/ai", tags=["ai"])
api_router.include_router(assistant_router, prefix="/assistant", tags=["assistant"])
api_router.include_router(system_router, prefix="/system", tags=["system"])
api_router.include_router(capabilities_router, prefix="/capabilities", tags=["capabilities"])
api_router.include_router(knowledge_router, prefix="/knowledge", tags=["knowledge"])
api_router.include_router(reasoning_router, prefix="/reasoning", tags=["reasoning"])
api_router.include_router(planner_router, prefix="/planner", tags=["planner"])
api_router.include_router(storage_router, prefix="/storage", tags=["storage"])
api_router.include_router(diagnostics_router, prefix="/diagnostics", tags=["diagnostics"])
