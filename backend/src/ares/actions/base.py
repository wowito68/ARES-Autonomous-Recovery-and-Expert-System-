"""Private action boundary for the workflow engine."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ares.events import EventBus
from ares.knowledge import KnowledgeGraph


class ActionError(Exception):
    """Safe action failure; only its stable code crosses layer boundaries."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ActionContext:
    """Server-owned dependencies available to a private action."""

    execution_id: str
    event_bus: EventBus
    graph: KnowledgeGraph


class Action(Protocol):
    """A bounded operation selected by a capability, never by an LLM."""

    id: str
    idempotent: bool

    async def run(self, inputs: dict[str, Any], context: ActionContext) -> dict[str, Any]:
        """Execute the fixed action contract."""

    async def compensate(
        self,
        output: dict[str, Any],
        context: ActionContext,
    ) -> None:
        """Undo a completed action when supported."""
