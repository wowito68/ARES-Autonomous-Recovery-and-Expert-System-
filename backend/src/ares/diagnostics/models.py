"""Structured diagnostic contracts produced from storage snapshots."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class DiagnosticSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class DiagnosticEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str
    resource: str
    observation: str


class DiagnosticFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    resource: str
    type: str
    status: DiagnosticSeverity
    message: str
    evidence: tuple[DiagnosticEvidence, ...] = ()


class DiagnosticRecommendation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    priority: int = Field(ge=1, le=5)
    message: str


class DiagnosticResult(BaseModel):
    """Machine-readable diagnosis from which user-facing text is rendered."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: uuid4().hex)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    snapshot_id: str
    summary: str
    severity: DiagnosticSeverity
    confidence: float = Field(ge=0, le=1)
    findings: tuple[DiagnosticFinding, ...]
    evidence: tuple[DiagnosticEvidence, ...]
    affected_resources: tuple[str, ...]
    recommendations: tuple[DiagnosticRecommendation, ...]
    suggested_capabilities: tuple[str, ...]
    warnings: tuple[str, ...]
    limitations: tuple[str, ...]
    needs_more_evidence: bool


def render_diagnostic_text(result: DiagnosticResult) -> str:
    """Render text strictly from the structured diagnosis."""

    lines = [
        result.summary,
        f"Estado: {result.severity.value}. Confianza: {result.confidence:.0%}.",
    ]
    if result.findings:
        lines.append("Hallazgos:")
        lines.extend(f"- {item.message}" for item in result.findings)
    if result.recommendations:
        lines.append("Recomendaciones:")
        lines.extend(f"- {item.message}" for item in result.recommendations)
    if result.limitations:
        lines.append("Limitaciones:")
        lines.extend(f"- {item}" for item in result.limitations)
    return "\n".join(lines)
