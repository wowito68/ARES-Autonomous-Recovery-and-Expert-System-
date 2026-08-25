"""Official read-only Boot Diagnostics capability."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from ares.actions import DiagnoseBootAction
from ares.boot import BootDiagnosticInput, BootDiagnosticResult
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
from ares.storage.store import StorageSnapshotStore
from ares.workflows import StageMode, WorkflowDefinition, WorkflowStage, WorkflowStep
from ares.workflows.models import StepOutputs

_COMPATIBILITY = OSCompatibility(
    families=("debian",),
    architectures=("amd64",),
    minimum_version="13",
)


class _BootDiagnosticPostcheck:
    async def __call__(self, output: dict[str, Any], state: StepOutputs) -> bool:
        del state
        try:
            result = BootDiagnosticResult.model_validate(output)
        except ValueError:
            return False
        return bool(result.snapshot_id) and bool(result.summary)


class BootDiagnosticsCapability:
    """Diagnose boot evidence without mounting, repairing or installing bootloaders."""

    input_model = BootDiagnosticInput
    output_model = BootDiagnosticResult
    metadata = CapabilityMetadata(
        id="boot.diagnose",
        version="1.0.0",
        name="Boot Diagnostics",
        description=(
            "Interpreta evidencia pasiva de arranque, firmware, sistemas instalados, "
            "particiones EFI y GRUB visible sin modificar dispositivos."
        ),
        objective=(
            "Determinar qué evidencia de arranque existe y qué falta antes de proponer "
            "cualquier reparación de bootloader."
        ),
        category=CapabilityCategory.RECOVERY,
        operation=OperationClass.OBSERVE,
        os_compatibility=_COMPATIBILITY,
        risk=RiskLevel.LOW,
        estimated_duration_seconds=3,
        permissions=(
            PermissionRequirement(
                id="storage.snapshot.read",
                reason="Leer el snapshot de almacenamiento persistido por ARES.",
            ),
            PermissionRequirement(
                id="boot.evidence.read-only",
                reason="Comprobar únicamente evidencia de firmware y archivos ya visibles.",
            ),
        ),
        dependencies=("storage.disk-analysis",),
        internal_actions=("boot.diagnose-read-only",),
        postchecks=("boot-diagnostic-result-typed",),
        rollback=RollbackPolicy(
            supported=False,
            strategy="No aplica: la Capability solo lee evidencia y no modifica el sistema.",
        ),
        required_evidence=("hardware.block-devices",),
        emitted_events=(
            "capability.started",
            "boot.diagnostic.completed",
            "capability.completed",
        ),
        metrics=(
            "capability.duration_ms",
            "boot.installed_system_count",
            "boot.finding_count",
        ),
        audit=AuditPolicy(
            record_inputs=True,
            record_outputs=True,
            event_names=(
                "capability.started",
                "boot.diagnostic.completed",
                "capability.completed",
            ),
        ),
        keywords=(
            "boot",
            "arranque",
            "inicia",
            "grub",
            "uefi",
            "bios",
            "bootloader",
            "sistema",
        ),
    )

    def __init__(self, snapshots: StorageSnapshotStore) -> None:
        self._diagnose = DiagnoseBootAction(snapshots)

    def build_workflow(self, payload: BaseModel) -> WorkflowDefinition:
        validated = BootDiagnosticInput.model_validate(payload)
        diagnose = WorkflowStep(
            id="diagnose-boot",
            action=self._diagnose,
            inputs=lambda _: validated.model_dump(mode="json"),
            timeout_seconds=5,
            postchecks=(_BootDiagnosticPostcheck(),),
        )
        return WorkflowDefinition(
            id="boot.diagnose.workflow",
            version="1.0.0",
            capability_id=self.metadata.id,
            stages=(WorkflowStage("diagnose", StageMode.SEQUENTIAL, (diagnose,)),),
            result=_public_result,
        )


class BootDiagnosticsPlugin:
    """Trusted built-in boot provider installed by the composition root."""

    manifest = PluginManifest(
        id="ares.boot-core",
        version="1.0.0",
        core_api_version="2.0",
        name="ARES Boot Core",
        permissions=("storage.snapshot.read", "boot.evidence.read-only"),
        os_compatibility=_COMPATIBILITY,
        dependencies=("ares.storage-core",),
        capabilities=(BootDiagnosticsCapability.metadata.id,),
    )

    def __init__(self, snapshots: StorageSnapshotStore) -> None:
        self._capabilities: tuple[Capability, ...] = (BootDiagnosticsCapability(snapshots),)

    def capabilities(self) -> tuple[Capability, ...]:
        return self._capabilities


def _public_result(state: StepOutputs) -> dict[str, Any]:
    return BootDiagnosticResult.model_validate(state["diagnose-boot"]).model_dump(mode="json")
