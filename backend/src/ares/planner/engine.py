"""Deterministic planner over capability identifiers and dependencies."""

from __future__ import annotations

from uuid import uuid4

from ares.capabilities import CapabilityManager
from ares.planner.models import ExecutionPlan, PlannedCapability, PlanStatus
from ares.reasoning import ReasoningEngine
from ares.reasoning.models import ReasoningRequest, ReasoningStatus


class Planner:
    """Translate a reasoning assessment into an auditable capability plan.

    The planner may order capability dependencies, but cannot see workflow
    actions or operating-system tools and never starts an execution.
    """

    def __init__(self, reasoning: ReasoningEngine, capabilities: CapabilityManager) -> None:
        self.reasoning = reasoning
        self.capabilities = capabilities

    def plan(self, request: ReasoningRequest) -> ExecutionPlan:
        assessment = self.reasoning.assess(request)
        plan_id = uuid4().hex
        if assessment.status is ReasoningStatus.STOPPED:
            return ExecutionPlan(
                id=plan_id,
                goal=request.goal,
                status=PlanStatus.STOPPED,
                hypotheses=assessment.hypotheses,
                stop_reason=assessment.stop_reason,
            )
        if assessment.status is ReasoningStatus.NEEDS_EVIDENCE:
            return ExecutionPlan(
                id=plan_id,
                goal=request.goal,
                status=PlanStatus.NEEDS_EVIDENCE,
                hypotheses=assessment.hypotheses,
                requested_evidence=assessment.requested_evidence,
            )

        capability_id = assessment.selected_capability_id
        if capability_id is None:
            return ExecutionPlan(
                id=plan_id,
                goal=request.goal,
                status=PlanStatus.STOPPED,
                hypotheses=assessment.hypotheses,
                stop_reason="Reasoning did not select a capability.",
            )
        ordered = self.capabilities.resolve_dependencies(capability_id)
        available = {fact.id for fact in request.evidence if fact.confidence >= 0.5}
        missing = tuple(
            sorted(
                {
                    evidence
                    for metadata in ordered
                    for evidence in metadata.required_evidence
                    if evidence not in available
                }
            )
        )
        if missing:
            return ExecutionPlan(
                id=plan_id,
                goal=request.goal,
                status=PlanStatus.NEEDS_EVIDENCE,
                hypotheses=assessment.hypotheses,
                requested_evidence=missing,
            )
        steps = tuple(
            PlannedCapability(
                capability_id=metadata.id,
                version=metadata.version,
                depends_on=metadata.dependencies,
                risk_level=metadata.risk.value,
                operation_class=metadata.operation.value,
                estimated_duration_seconds=metadata.estimated_duration_seconds,
                rationale=(
                    "Required dependency for the selected capability."
                    if metadata.id != capability_id
                    else "Selected by the Reasoning Engine for the stated goal."
                ),
            )
            for metadata in ordered
        )
        return ExecutionPlan(
            id=plan_id,
            goal=request.goal,
            status=PlanStatus.READY,
            hypotheses=assessment.hypotheses,
            steps=steps,
        )
