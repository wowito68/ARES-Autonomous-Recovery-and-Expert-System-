"""Explainable capability selection and deterministic diagnostic interpretation."""

from __future__ import annotations

import re

from ares.capabilities import CapabilityManager
from ares.diagnostics import (
    DiagnosticEvidence,
    DiagnosticFinding,
    DiagnosticRecommendation,
    DiagnosticResult,
    DiagnosticSeverity,
)
from ares.reasoning.models import (
    Hypothesis,
    ReasoningAssessment,
    ReasoningRequest,
    ReasoningStatus,
)
from ares.storage import StorageHealth, SystemStorageSnapshot

_TOKEN = re.compile(r"[a-záéíóúüñ0-9]{3,}", re.IGNORECASE)
_STOP_WORDS = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "this",
    "that",
    "una",
    "uno",
    "unos",
    "unas",
    "del",
    "las",
    "los",
    "por",
    "para",
    "con",
    "que",
    "mis",
    "sus",
    "antes",
}
_SEVERITY_RANK = {
    DiagnosticSeverity.INFO: 0,
    DiagnosticSeverity.WARNING: 1,
    DiagnosticSeverity.ERROR: 2,
    DiagnosticSeverity.CRITICAL: 3,
}


class ReasoningEngine:
    """Reason over public capability contracts and already-collected evidence."""

    def __init__(self, capabilities: CapabilityManager) -> None:
        self.capabilities = capabilities

    def assess(self, request: ReasoningRequest) -> ReasoningAssessment:
        goal_terms = _meaningful_terms(request.goal)
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
            metadata_terms = _meaningful_terms(searchable)
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

    def diagnose_storage(self, snapshot: SystemStorageSnapshot) -> DiagnosticResult:
        """Interpret a storage snapshot conservatively without invoking an LLM or tools."""

        findings: list[DiagnosticFinding] = []
        evidence: list[DiagnosticEvidence] = []
        recommendations: list[DiagnosticRecommendation] = []
        limitations: list[str] = []
        affected: set[str] = set()

        for disk in snapshot.disks:
            observation = DiagnosticEvidence(
                source=f"snapshot:{snapshot.id}",
                resource=disk.id,
                observation=f"disk size={disk.size_bytes} read_only={disk.read_only}",
            )
            evidence.append(observation)
            if disk.read_only:
                findings.append(
                    DiagnosticFinding(
                        resource=disk.id,
                        type="storage_access",
                        status=DiagnosticSeverity.INFO,
                        message=f"El disco {disk.name} se presenta en modo solo lectura.",
                        evidence=(observation,),
                    )
                )

        for mount in snapshot.mounts:
            if mount.used_percent is None:
                continue
            observation = DiagnosticEvidence(
                source=f"snapshot:{snapshot.id}",
                resource=mount.id,
                observation=f"used_percent={mount.used_percent:.1f}",
            )
            evidence.append(observation)
            if mount.used_percent >= 95:
                severity = DiagnosticSeverity.ERROR
                message = f"El punto {mount.path} está al {mount.used_percent:.0f}% de uso."
            elif mount.used_percent >= 85:
                severity = DiagnosticSeverity.WARNING
                message = f"El punto {mount.path} tiene uso elevado ({mount.used_percent:.0f}%)."
            else:
                continue
            findings.append(
                DiagnosticFinding(
                    resource=mount.id,
                    type="storage_capacity",
                    status=severity,
                    message=message,
                    evidence=(observation,),
                )
            )
            affected.add(mount.id)
            recommendations.append(
                DiagnosticRecommendation(
                    priority=1 if severity is DiagnosticSeverity.ERROR else 2,
                    message=(
                        f"Revisar qué consume espacio en {mount.path} antes de realizar "
                        "cualquier operación de mantenimiento."
                    ),
                )
            )

        for smart in snapshot.smart:
            observation = DiagnosticEvidence(
                source=f"snapshot:{snapshot.id}",
                resource=smart.disk_id,
                observation=f"smart_status={smart.status.value}",
            )
            evidence.append(observation)
            if smart.status is StorageHealth.ERROR:
                findings.append(
                    DiagnosticFinding(
                        resource=smart.disk_id,
                        type="storage_health",
                        status=DiagnosticSeverity.ERROR,
                        message="SMART reportó un estado de salud fallido.",
                        evidence=(observation,),
                    )
                )
                affected.add(smart.disk_id)
                recommendations.append(
                    DiagnosticRecommendation(
                        priority=1,
                        message=(
                            "Preservar los datos importantes y solicitar una Capability de "
                            "diagnóstico SMART ampliado cuando esté disponible."
                        ),
                    )
                )
            elif smart.status is StorageHealth.UNAVAILABLE:
                limitations.append(
                    f"SMART no disponible para {smart.disk_id}: {smart.reason or 'sin razón'}"
                )

        filesystems_by_id = {item.id: item for item in snapshot.filesystems}
        for partition in snapshot.partitions:
            filesystem = (
                filesystems_by_id.get(partition.filesystem_id)
                if partition.filesystem_id is not None
                else None
            )
            if filesystem is None or filesystem.filesystem_type is None:
                findings.append(
                    DiagnosticFinding(
                        resource=partition.id,
                        type="filesystem_identification",
                        status=DiagnosticSeverity.INFO,
                        message=f"No se identificó filesystem para {partition.path}.",
                    )
                )
                limitations.append(f"filesystem desconocido en {partition.path}")

        if not snapshot.disks:
            findings.append(
                DiagnosticFinding(
                    resource="system:local",
                    type="storage_inventory",
                    status=DiagnosticSeverity.WARNING,
                    message="No se detectaron discos en la evidencia disponible.",
                )
            )
            affected.add("system:local")

        for tool in snapshot.tool_availability:
            if not tool.available:
                limitations.append(f"{tool.tool}: {tool.reason or 'unavailable'}")

        severity = max(
            (item.status for item in findings),
            key=lambda item: _SEVERITY_RANK[item],
            default=DiagnosticSeverity.INFO,
        )
        missing_tools = sum(not item.available for item in snapshot.tool_availability)
        confidence = max(0.5, min(0.99, 0.98 - missing_tools * 0.04 - len(snapshot.errors) * 0.08))
        unique_limitations = tuple(dict.fromkeys((*snapshot.errors, *limitations)))
        unique_warnings = tuple(dict.fromkeys(snapshot.warnings))
        needs_more = bool(unique_limitations)
        summary = (
            f"Se analizaron {snapshot.summary.disk_count} discos y "
            f"{snapshot.summary.partition_count} particiones."
        )
        return DiagnosticResult(
            snapshot_id=snapshot.id,
            summary=summary,
            severity=severity,
            confidence=confidence,
            findings=tuple(findings),
            evidence=tuple(evidence),
            affected_resources=tuple(sorted(affected)),
            recommendations=tuple(recommendations),
            suggested_capabilities=(),
            warnings=unique_warnings,
            limitations=unique_limitations,
            needs_more_evidence=needs_more,
        )


def _meaningful_terms(value: str) -> set[str]:
    return {
        token
        for match in _TOKEN.finditer(value)
        if (token := match.group(0).casefold()) not in _STOP_WORDS
    }
