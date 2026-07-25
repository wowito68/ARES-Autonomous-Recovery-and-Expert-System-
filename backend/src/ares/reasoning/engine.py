"""Explainable capability selection over the public catalog."""

from __future__ import annotations

import re

from ares.capabilities import CapabilityManager
from ares.reasoning.models import (
    Hypothesis,
    ReasoningAssessment,
    ReasoningRequest,
    ReasoningStatus,
)

_TOKEN = re.compile(r"[a-záéíóúüñ0-9]{3,}", re.IGNORECASE)


class ReasoningEngine:
    """Form hypotheses and request evidence without constructing OS commands."""

    def __init__(self, capabilities: CapabilityManager) -> None:
        self.capabilities = capabilities

    def assess(self, request: ReasoningRequest) -> ReasoningAssessment:
        goal_terms = {match.group(0).casefold() for match in _TOKEN.finditer(request.goal)}
        candidates: list[tuple[float, str]] = []
        for metadata in self.capabilities.catalog():
            searchable = " ".join(
                (
                    metadata.name,
                    metadata.description,
                    metadata.objective,
                    *metadata.keywords,
                )
            )
            metadata_terms = {match.group(0).casefold() for match in _TOKEN.finditer(searchable)}
            overlap = goal_terms & metadata_terms
            if overlap:
                confidence = min(0.95, 0.45 + 0.1 * len(overlap))
                candidates.append((confidence, metadata.id))
        if not candidates:
            return ReasoningAssessment(
                status=ReasoningStatus.STOPPED,
                hypotheses=(),
                stop_reason="No compatible capability matches the stated goal.",
            )

        candidates.sort(key=lambda item: (-item[0], item[1]))
        confidence, capability_id = candidates[0]
        selected_metadata = self.capabilities.get(capability_id)
        assert selected_metadata is not None
        hypothesis = Hypothesis(
            capability_id=capability_id,
            statement=f"The goal may be addressed by capability {capability_id}.",
            confidence=confidence,
        )
        available = {fact.id for fact in request.evidence if fact.confidence >= 0.5}
        missing = tuple(
            item for item in selected_metadata.required_evidence if item not in available
        )
        if missing:
            return ReasoningAssessment(
                status=ReasoningStatus.NEEDS_EVIDENCE,
                hypotheses=(hypothesis,),
                requested_evidence=missing,
            )
        return ReasoningAssessment(
            status=ReasoningStatus.CAPABILITY_SELECTED,
            hypotheses=(hypothesis,),
            selected_capability_id=capability_id,
        )
