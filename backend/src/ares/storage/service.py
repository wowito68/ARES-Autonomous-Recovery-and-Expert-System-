"""Application service orchestrating the first storage vertical slice."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from ares.capabilities import CapabilityManager
from ares.diagnostics import DiagnosticResult, DiagnosticStore, render_diagnostic_text
from ares.events import AresEvent, EventBus
from ares.reasoning import EvidenceFact, ReasoningEngine, ReasoningRequest, ReasoningStatus
from ares.storage.models import DiskSnapshot, StorageCapabilityResult, SystemStorageSnapshot
from ares.storage.store import StorageSnapshotStore
from ares.tools import StorageToolSuite

_STORAGE_CAPABILITY = "storage.disk-analysis"


class StorageAnalysisResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    execution_id: str
    snapshot_id: str
    diagnostic_id: str
    diagnostic: DiagnosticResult
    message: str


class StorageAnalysisError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class StorageAnalysisService:
    """Coordinates Reasoning -> Capability -> Snapshot -> Reasoning diagnosis."""

    def __init__(
        self,
        capabilities: CapabilityManager,
        reasoning: ReasoningEngine,
        snapshots: StorageSnapshotStore,
        diagnostics: DiagnosticStore,
        event_bus: EventBus,
        tools: StorageToolSuite,
        inventory_path: Path,
    ) -> None:
        self.capabilities = capabilities
        self.reasoning = reasoning
        self.snapshots = snapshots
        self.diagnostics = diagnostics
        self.event_bus = event_bus
        self.tools = tools
        self.inventory_path = inventory_path

    async def analyze(self, *, session_id: str) -> StorageAnalysisResponse:
        assessment = self.reasoning.assess(
            ReasoningRequest(
                goal="analizar almacenamiento discos particiones filesystem smart montaje",
                evidence=self._preflight_evidence(),
            )
        )
        if assessment.status is ReasoningStatus.NEEDS_EVIDENCE:
            raise StorageAnalysisError("STORAGE_EVIDENCE_UNAVAILABLE")
        if (
            assessment.status is not ReasoningStatus.CAPABILITY_SELECTED
            or assessment.selected_capability_id != _STORAGE_CAPABILITY
        ):
            raise StorageAnalysisError("STORAGE_CAPABILITY_NOT_SELECTED")

        execution = await self.capabilities.execute(_STORAGE_CAPABILITY, {"scope": "all_detected"})
        if execution.result is None or execution.status.value != "succeeded":
            raise StorageAnalysisError(execution.error_code or "STORAGE_ANALYSIS_FAILED")
        try:
            result = StorageCapabilityResult.model_validate(execution.result)
        except ValueError as exc:
            raise StorageAnalysisError("STORAGE_RESULT_INVALID") from exc
        diagnostic = self.reasoning.diagnose_storage(result.snapshot)
        await self.diagnostics.put(diagnostic)
        await self.event_bus.publish(
            AresEvent(
                name="diagnostic.generated",
                source="storage.analysis-service",
                correlation_id=execution.id,
                session_id=session_id,
                payload={
                    "diagnostic_id": diagnostic.id,
                    "snapshot_id": diagnostic.snapshot_id,
                    "severity": diagnostic.severity.value,
                    "confidence": diagnostic.confidence,
                    "actor": "local-user",
                    "reason": "storage analysis requested",
                    "resource": "local-system",
                    "decision": "diagnostic_generated",
                },
            )
        )
        return StorageAnalysisResponse(
            execution_id=execution.id,
            snapshot_id=result.snapshot.id,
            diagnostic_id=diagnostic.id,
            diagnostic=diagnostic,
            message=render_diagnostic_text(diagnostic),
        )

    async def disks(self) -> tuple[DiskSnapshot, ...]:
        snapshot = await self.snapshots.latest()
        return snapshot.disks if snapshot is not None else ()

    async def snapshot(self, snapshot_id: str) -> SystemStorageSnapshot | None:
        return await self.snapshots.get(snapshot_id)

    async def latest_snapshot(self) -> SystemStorageSnapshot | None:
        return await self.snapshots.latest()

    async def diagnostic(self, diagnostic_id: str) -> DiagnosticResult | None:
        return await self.diagnostics.get(diagnostic_id)

    def _preflight_evidence(self) -> tuple[EvidenceFact, ...]:
        if self.inventory_path.is_file() and not self.inventory_path.is_symlink():
            return (EvidenceFact(id="hardware.block-devices", confidence=1.0),)
        if self.tools.process_probes_enabled and self.tools.runner.inspect("lsblk").available:
            return (EvidenceFact(id="hardware.block-devices", confidence=0.6),)
        return ()
