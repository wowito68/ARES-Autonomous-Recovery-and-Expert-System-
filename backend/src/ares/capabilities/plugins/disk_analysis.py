"""Reference plugin: passive Storage / Disk Analysis."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from ares.actions import (
    AnalyzeDiskInventoryAction,
    ReadDiskInventoryAction,
    UpdateStorageGraphAction,
)
from ares.capabilities.base import Capability
from ares.capabilities.models import (
    AuditPolicy,
    CapabilityCategory,
    CapabilityMetadata,
    OperationClass,
    OSCompatibility,
    PermissionRequirement,
    PluginManifest,
    RiskLevel,
    RollbackPolicy,
)
from ares.workflows import (
    RetryPolicy,
    StageMode,
    WorkflowDefinition,
    WorkflowStage,
    WorkflowStep,
)
from ares.workflows.models import StepOutputs

_COMPATIBILITY = OSCompatibility(
    families=("debian",),
    architectures=("amd64",),
    minimum_version="13",
)


class DiskAnalysisInput(BaseModel):
    """The first capability accepts no device path or command from a client."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: Literal["all_detected"] = "all_detected"


class _InventoryPostcheck:
    async def __call__(self, output: dict[str, Any], state: StepOutputs) -> bool:
        del state
        return isinstance(output.get("devices"), list) and output.get("generation") is not None


class _AnalysisPostcheck:
    async def __call__(self, output: dict[str, Any], state: StepOutputs) -> bool:
        del state
        summary = output.get("summary")
        return isinstance(summary, dict) and isinstance(summary.get("disk_count"), int)


class _GraphPostcheck:
    async def __call__(self, output: dict[str, Any], state: StepOutputs) -> bool:
        del state
        revision = output.get("revision")
        return isinstance(revision, int) and revision > 0


class DiskAnalysisCapability:
    """Compose private read/analyze/graph actions into one public capability."""

    metadata = CapabilityMetadata(
        id="storage.disk-analysis",
        version="1.0.0",
        name="Disk Analysis",
        description=(
            "Analiza de forma pasiva el inventario de discos ya recolectado por ARES OS, "
            "sin abrir dispositivos ni modificar almacenamiento."
        ),
        objective=(
            "Producir un resumen verificable de capacidad, tipo y exposición de los discos "
            "detectados, y actualizar el grafo de conocimiento local."
        ),
        category=CapabilityCategory.STORAGE,
        operation=OperationClass.OBSERVE,
        os_compatibility=_COMPATIBILITY,
        risk=RiskLevel.LOW,
        estimated_duration_seconds=2,
        permissions=(
            PermissionRequirement(
                id="hardware.inventory.read-public",
                reason="Leer el inventario de hardware redactado generado durante el arranque.",
            ),
            PermissionRequirement(
                id="knowledge.graph.write",
                reason="Registrar hechos validados de almacenamiento en el grafo local.",
            ),
        ),
        internal_actions=(
            "storage.read-hardware-inventory",
            "storage.analyze-disk-inventory",
            "knowledge.update-storage-graph",
        ),
        postchecks=(
            "inventory-schema-valid",
            "disk-summary-consistent",
            "knowledge-graph-revision-advanced",
        ),
        rollback=RollbackPolicy(
            supported=False,
            strategy="No requerido: todas las acciones observan o proyectan hechos idempotentes.",
        ),
        required_evidence=("hardware.block-devices",),
        emitted_events=(
            "capability.started",
            "action.started",
            "action.completed",
            "workflow.postcheck.passed",
            "knowledge.graph.updated",
            "capability.completed",
        ),
        metrics=(
            "capability.duration_ms",
            "workflow.step.duration_ms",
            "storage.disk_count",
            "storage.total_capacity_bytes",
        ),
        audit=AuditPolicy(
            record_inputs=False,
            record_outputs=True,
            event_names=(
                "capability.started",
                "action.started",
                "action.completed",
                "knowledge.graph.updated",
                "capability.completed",
            ),
        ),
        keywords=("disco", "disk", "storage", "almacenamiento", "capacidad", "hardware"),
    )

    def __init__(self, inventory_path: Path) -> None:
        self._read = ReadDiskInventoryAction(inventory_path)
        self._analyze = AnalyzeDiskInventoryAction()
        self._update_graph = UpdateStorageGraphAction()

    def build_workflow(self, payload: dict[str, Any]) -> WorkflowDefinition:
        DiskAnalysisInput.model_validate(payload)
        read_step = WorkflowStep(
            id="read-inventory",
            action=self._read,
            inputs=lambda _: {},
            timeout_seconds=3,
            retry=RetryPolicy(max_attempts=2, delay_seconds=0.05),
            postchecks=(_InventoryPostcheck(),),
        )
        analyze_step = WorkflowStep(
            id="analyze-inventory",
            action=self._analyze,
            inputs=lambda state: dict(state["read-inventory"]),
            timeout_seconds=2,
            postchecks=(_AnalysisPostcheck(),),
        )
        graph_step = WorkflowStep(
            id="update-storage-graph",
            action=self._update_graph,
            inputs=lambda state: dict(state["analyze-inventory"]),
            timeout_seconds=3,
            retry=RetryPolicy(max_attempts=2, delay_seconds=0.05),
            postchecks=(_GraphPostcheck(),),
        )
        return WorkflowDefinition(
            id="storage.disk-analysis.workflow",
            version="1.0.0",
            capability_id=self.metadata.id,
            stages=(
                WorkflowStage("collect", StageMode.SEQUENTIAL, (read_step,)),
                WorkflowStage("reason", StageMode.SEQUENTIAL, (analyze_step,)),
                WorkflowStage("project", StageMode.SEQUENTIAL, (graph_step,)),
            ),
            result=_public_result,
        )


class DiskAnalysisPlugin:
    """Trusted plugin provider installed by the ARES composition root."""

    manifest = PluginManifest(
        id="ares.storage-core",
        version="1.0.0",
        core_api_version="2.0",
        name="ARES Storage Core",
        permissions=("hardware.inventory.read-public", "knowledge.graph.write"),
        os_compatibility=_COMPATIBILITY,
        capabilities=(DiskAnalysisCapability.metadata.id,),
    )

    def __init__(self, inventory_path: Path) -> None:
        self._capabilities: tuple[Capability, ...] = (DiskAnalysisCapability(inventory_path),)

    def capabilities(self) -> tuple[Capability, ...]:
        return self._capabilities


def _public_result(state: StepOutputs) -> dict[str, Any]:
    analysis = dict(state["analyze-inventory"])
    graph = state["update-storage-graph"]
    analysis["knowledge_graph"] = {
        "revision": graph.get("revision"),
        "node_count": graph.get("node_count"),
        "disk_node_count": graph.get("disk_node_count"),
        "partition_node_count": graph.get("partition_node_count"),
    }
    return analysis
