"""Private workflow actions for typed read-only diagnostic capabilities."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel

from ares.actions.base import ActionContext
from ares.diagnostic_capabilities import DiagnosticInput, DiagnosticToolSuite, SpaceAnalysisInput
from ares.events import AresEvent


class CollectDiagnosticAction:
    """Invoke one fixed collector selected by trusted capability code."""

    idempotent = True

    def __init__(
        self,
        *,
        action_id: str,
        capability_id: str,
        input_model: type[BaseModel],
        collector: Callable[[Any], Awaitable[BaseModel]],
    ) -> None:
        self.id = action_id
        self.capability_id = capability_id
        self.input_model = input_model
        self.collector = collector

    async def run(self, inputs: dict[str, Any], context: ActionContext) -> dict[str, Any]:
        validated = self.input_model.model_validate(inputs)
        result = await self.collector(validated)
        await context.event_bus.publish(
            AresEvent(
                name=f"{self.capability_id}.completed",
                source=self.id,
                correlation_id=context.execution_id,
                payload={
                    "capability_id": self.capability_id,
                    "target_resource_id": getattr(result, "target_resource_id", None),
                    "finding_count": len(getattr(result, "findings", ())),
                    "evidence_count": len(getattr(result, "evidence", ())),
                },
            )
        )
        return result.model_dump(mode="json")

    async def compensate(self, output: dict[str, Any], context: ActionContext) -> None:
        del output, context


def space_action(tools: DiagnosticToolSuite) -> CollectDiagnosticAction:
    return CollectDiagnosticAction(
        action_id="storage.collect-space-analysis",
        capability_id="storage.space-analysis",
        input_model=SpaceAnalysisInput,
        collector=tools.space,
    )


def memory_action(tools: DiagnosticToolSuite) -> CollectDiagnosticAction:
    return CollectDiagnosticAction(
        action_id="system.collect-memory-analysis",
        capability_id="system.memory-analysis",
        input_model=DiagnosticInput,
        collector=tools.memory,
    )


def packages_action(tools: DiagnosticToolSuite) -> CollectDiagnosticAction:
    return CollectDiagnosticAction(
        action_id="packages.collect-health-check",
        capability_id="packages.health-check",
        input_model=DiagnosticInput,
        collector=tools.packages,
    )


def services_action(tools: DiagnosticToolSuite) -> CollectDiagnosticAction:
    return CollectDiagnosticAction(
        action_id="services.collect-failure-analysis",
        capability_id="services.failure-analysis",
        input_model=DiagnosticInput,
        collector=tools.services,
    )
