"""Read-only view of the local System Knowledge Graph."""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Request

from ares.knowledge import GraphSnapshot, KnowledgeGraph

router = APIRouter()


@router.get("/graph", response_model=GraphSnapshot, summary="Read the system knowledge graph")
async def graph(request: Request) -> GraphSnapshot:
    store = cast(KnowledgeGraph, request.app.state.knowledge_graph)
    return await store.snapshot()
