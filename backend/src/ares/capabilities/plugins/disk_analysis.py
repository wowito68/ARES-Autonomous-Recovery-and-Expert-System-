"""Official read-only Storage / Disk Analysis capability."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from ares.actions import (
    BuildStorageSnapshotAction,
    CollectStorageEvidenceAction,
    PersistStorageSnapshotAction,
    ProjectStorageSnapshotAction,
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
from ares.storage import StorageCapabilityResult, StorageSnapshotStore, SystemStorageSnapshot
from ares.tools import StorageEvidence, StorageToolSuite
from ares.workflows import StageMode, WorkflowDefinition, WorkflowStage, WorkflowStep
from ares.workflows.models import StepOutputs

_COMPATIBILITY = OSCompatibility(
    families=("debian",),
    architectures=("amd64",),
    minimum_version="13",
)


class DiskAnalysisInput(BaseModel):
    """Semantic input: clients can never provide a device, executable or argument."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: Literal["all_detected"] = "all_detected"


class _EvidencePostcheck:
    async def __call__(self, output: dict[str, Any], state: StepOutputs) -> bool:
        del state
        try:
            evidence = StorageEvidence.model_validate(output)
        except ValueError:
            return False
        return bool(evidence.devices) and bool(evidence.tool_availability)


class _SnapshotPostcheck:
    async def __call__(self, output: dict[str, Any], state: StepOutputs) -> bool:
        del state
        try:
            snapshot = SystemStorageSnapshot.model_validate(output)
        except ValueError:
            return False
        return bool(snapshot.id) and len(snapshot.evidence_sha256) == 64


class _GraphPostcheck:
    async def __call__(self, output: dict[str, Any], state: StepOutputs) -> bool:
        del state
        revision = output.get("revision")
        return isinstance(revision, int) and not isinstance(revision, bool) and revision > 0


class DiskAnalysisCapability:
    """Collect, normalize, persist and project storage evidence without disk writes."""

    input_model = DiskAnalysisInput
    output_model = StorageCapabilityResult
    metadata = CapabilityMetadata(
        id="storage.disk-analysis",
        version="1.1.0",
        name="Disk Analysis",
        description=(
            "Analiza almacenamiento mediante probes pasivos y evidencia de arranque, construye "
            "un snapshot estructurado y actualiza el Knowledge Graph sin modificar discos."
        ),
        objective=(
            "Observar discos, particiones, filesystems, montajes, uso, sistemas operativos y "
            "estado SMART disponible para producir evidencia auditable de diagnóstico."
        ),
        category=CapabilityCategory.STORAGE,
        operation=OperationClass.OBSERVE,
        os_compatibility=_COMPATIBILITY,
        risk=RiskLevel.LOW,
        estimated_duration_seconds=8,
        permissions=(
            PermissionRequirement(
                id="hardware.inventory.read-public",
                reason="Usar el inventario público como fallback de evidencia de bloques.",
            ),
            PermissionRequirement(
                id="system.process.observe",
                reason="Ejecutar probes pasivos allowlisted sin shell ni argumentos del cliente.",
            ),
            PermissionRequirement(
                id="storage.snapshot.write-local",
                reason="Persistir únicamente el snapshot privado de ARES, nunca el dispositivo.",
            ),
            PermissionRequirement(
                id="knowledge.graph.write",
                reason="Registrar hechos validados de almacenamiento en el grafo local.",
            ),
        ),
        internal_actions=(
            "storage.collect-evidence",
            "storage.build-snapshot",
            "storage.persist-snapshot",
            "knowledge.project-storage-snapshot",
        ),
        postchecks=(
            "storage-evidence-typed-and-nonempty",
            "snapshot-has-evidence-fingerprint",
            "snapshot-persisted-before-projection",
            "knowledge-graph-revision-advanced",
        ),
        rollback=RollbackPolicy(
            supported=False,
            strategy=(
                "No aplica: la Capability es read-only sobre almacenamiento; solo persiste "
                "evidencia local idempotente de ARES."
            ),
        ),
        required_evidence=("hardware.block-devices",),
        emitted_events=(
            "capability.started",
            "tool.execution.started",
            "tool.execution.completed",
            "storage.disk.detected",
            "storage.partition.detected",
            "storage.smart.analyzed",
            "storage.snapshot.created",
            "knowledge.graph.updated",
            "capability.completed",
        ),
        metrics=(
            "capability.duration_ms",
            "workflow.step.duration_ms",
            "storage.disk_count",
            "storage.partition_count",
            "storage.total_capacity_bytes",
        ),
        audit=AuditPolicy(
            record_inputs=False,
            record_outputs=True,
            event_names=(
                "capability.started",
                "tool.execution.started",
                "tool.execution.completed",
                "storage.snapshot.created",
                "knowledge.graph.updated",
                "capability.completed",
            ),
        ),
        keywords=(
            "disco",
            "disk",
            "storage",
            "almacenamiento",
            "particion",
            "filesystem",
            "smart",
            "montaje",
        ),
    )

    def __init__(self, tools: StorageToolSuite, snapshots: StorageSnapshotStore) -> None:
        self._collect = CollectStorageEvidenceAction(tools)
        self._build = BuildStorageSnapshotAction()
        self._persist = PersistStorageSnapshotAction(snapshots)
        self._graph = ProjectStorageSnapshotAction()

    def build_workflow(self, payload: BaseModel) -> WorkflowDefinition:
        validated = DiskAnalysisInput.model_validate(payload)
        if validated.scope != "all_detected":
            raise ValueError("unsupported disk analysis scope")
        collect = WorkflowStep(
            id="collect-evidence",
            action=self._collect,
            inputs=lambda _: {},
            timeout_seconds=15,
            postchecks=(_EvidencePostcheck(),),
        )
        build = WorkflowStep(
            id="build-snapshot",
            action=self._build,
            inputs=lambda state: dict(state["collect-evidence"]),
            timeout_seconds=3,
            postchecks=(_SnapshotPostcheck(),),
        )
        persist = WorkflowStep(
            id="persist-snapshot",
            action=self._persist,
            inputs=lambda state: dict(state["build-snapshot"]),
            timeout_seconds=3,
            postchecks=(_SnapshotPostcheck(),),
        )
        project = WorkflowStep(
            id="project-knowledge-graph",
            action=self._graph,
            inputs=lambda state: dict(state["persist-snapshot"]),
            timeout_seconds=4,
            postchecks=(_GraphPostcheck(),),
        )
        return WorkflowDefinition(
            id="storage.disk-analysis.workflow",
            version="1.1.0",
            capability_id=self.metadata.id,
            stages=(
                WorkflowStage("collect", StageMode.SEQUENTIAL, (collect,)),
                WorkflowStage("snapshot", StageMode.SEQUENTIAL, (build, persist)),
                WorkflowStage("project", StageMode.SEQUENTIAL, (project,)),
            ),
            result=_public_result,
        )


class DiskAnalysisPlugin:
    """Trusted built-in storage provider installed by the composition root."""

    manifest = PluginManifest(
        id="ares.storage-core",
        version="1.1.0",
        core_api_version="2.0",
        name="ARES Storage Core",
        permissions=(
            "hardware.inventory.read-public",
            "system.process.observe",
            "storage.snapshot.write-local",
            "knowledge.graph.write",
        ),
        os_compatibility=_COMPATIBILITY,
        capabilities=(DiskAnalysisCapability.metadata.id,),
    )

    def __init__(self, tools: StorageToolSuite, snapshots: StorageSnapshotStore) -> None:
        self._capabilities: tuple[Capability, ...] = (DiskAnalysisCapability(tools, snapshots),)

    def capabilities(self) -> tuple[Capability, ...]:
        return self._capabilities


def _public_result(state: StepOutputs) -> dict[str, Any]:
    snapshot = SystemStorageSnapshot.model_validate(state["persist-snapshot"])
    graph = state["project-knowledge-graph"]
    result = StorageCapabilityResult(
        snapshot=snapshot,
        knowledge_graph={
            "revision": graph.get("revision"),
            "node_count": graph.get("node_count"),
            "edge_count": graph.get("edge_count"),
        },
    )
    return result.model_dump(mode="json")
