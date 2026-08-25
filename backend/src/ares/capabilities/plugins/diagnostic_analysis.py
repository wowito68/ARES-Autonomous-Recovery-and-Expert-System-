"""Built-in plugin for bounded read-only system diagnostics."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from ares.actions.diagnostic_capabilities import (
    memory_action,
    packages_action,
    services_action,
    space_action,
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
from ares.diagnostic_capabilities import (
    DiagnosticInput,
    DiagnosticToolSuite,
    MemoryAnalysisResult,
    PackageHealthResult,
    ServiceFailureResult,
    SpaceAnalysisInput,
    SpaceAnalysisResult,
)
from ares.workflows import StageMode, WorkflowDefinition, WorkflowStage, WorkflowStep
from ares.workflows.models import StepOutputs

_COMPATIBILITY = OSCompatibility(
    families=("debian",),
    architectures=("amd64",),
    minimum_version="13",
)
_READ_ONLY_ROLLBACK = RollbackPolicy(
    supported=False,
    strategy="No aplica: solo se recopila y normaliza evidencia; el target no se modifica.",
)


class _TypedPostcheck:
    def __init__(self, output_model: type[BaseModel]) -> None:
        self.output_model = output_model

    async def __call__(self, output: dict[str, Any], state: StepOutputs) -> bool:
        del state
        try:
            validated = self.output_model.model_validate(output)
        except ValueError:
            return False
        return bool(getattr(validated, "target_fingerprint", "")) and bool(
            getattr(validated, "summary", "")
        )


class _DiagnosticCapability:
    def __init__(
        self,
        *,
        metadata: CapabilityMetadata,
        input_model: type[BaseModel],
        output_model: type[BaseModel],
        action: Any,
    ) -> None:
        self.metadata = metadata
        self.input_model = input_model
        self.output_model = output_model
        self._action = action

    def build_workflow(self, payload: BaseModel) -> WorkflowDefinition:
        validated = self.input_model.model_validate(payload)
        collect = WorkflowStep(
            id="collect-normalized-evidence",
            action=self._action,
            inputs=lambda _: validated.model_dump(mode="json"),
            timeout_seconds=self.metadata.estimated_duration_seconds + 5,
            postchecks=(_TypedPostcheck(self.output_model),),
        )
        output_model = self.output_model

        def public_result(state: StepOutputs) -> dict[str, Any]:
            return output_model.model_validate(state["collect-normalized-evidence"]).model_dump(
                mode="json"
            )

        return WorkflowDefinition(
            id=f"{self.metadata.id}.workflow",
            version=self.metadata.version,
            capability_id=self.metadata.id,
            stages=(WorkflowStage("diagnose", StageMode.SEQUENTIAL, (collect,)),),
            result=public_result,
        )


def _metadata(
    *,
    capability_id: str,
    name: str,
    description: str,
    objective: str,
    category: CapabilityCategory,
    duration: float,
    permission: str,
    action: str,
    event: str,
    metrics: tuple[str, ...],
    keywords: tuple[str, ...],
) -> CapabilityMetadata:
    return CapabilityMetadata(
        id=capability_id,
        version="1.0.0",
        name=name,
        description=description,
        objective=objective,
        category=category,
        operation=OperationClass.OBSERVE,
        os_compatibility=_COMPATIBILITY,
        risk=RiskLevel.LOW,
        estimated_duration_seconds=duration,
        permissions=(
            PermissionRequirement(
                id="storage.snapshot.read",
                reason="Resolver un target semántico desde evidencia persistida de ARES.",
            ),
            PermissionRequirement(id=permission, reason=description),
        ),
        dependencies=("storage.disk-analysis",),
        internal_actions=(action,),
        postchecks=("diagnostic-output-typed", "target-fingerprint-present"),
        rollback=_READ_ONLY_ROLLBACK,
        required_evidence=("hardware.block-devices",),
        emitted_events=("capability.started", event, "capability.completed"),
        metrics=("capability.duration_ms", *metrics),
        audit=AuditPolicy(
            record_inputs=True,
            record_outputs=True,
            event_names=("capability.started", event, "capability.completed"),
        ),
        keywords=keywords,
    )


class DiagnosticAnalysisPlugin:
    """Trusted plugin whose public inputs cannot contain commands or filesystem paths."""

    manifest = PluginManifest(
        id="ares.diagnostic-analysis",
        version="1.0.0",
        core_api_version="2.0",
        name="ARES Diagnostic Analysis",
        permissions=(
            "storage.snapshot.read",
            "storage.space.read-only",
            "system.memory.read-only",
            "packages.database.read-only",
            "services.state.read-only",
        ),
        dependencies=("ares.storage-core",),
        os_compatibility=_COMPATIBILITY,
        capabilities=(
            "storage.space-analysis",
            "system.memory-analysis",
            "packages.health-check",
            "services.failure-analysis",
        ),
    )

    def __init__(self, tools: DiagnosticToolSuite) -> None:
        self._capabilities: tuple[Capability, ...] = (
            _DiagnosticCapability(
                metadata=_metadata(
                    capability_id="storage.space-analysis",
                    name="Storage Space Analysis",
                    description=(
                        "Mide capacidad, inodos y categorías consumidoras mediante recorridos "
                        "acotados que no leen el contenido de archivos personales."
                    ),
                    objective=(
                        "Explicar qué consume espacio y estimar categorías recuperables sin borrar "
                        "ni modificar archivos."
                    ),
                    category=CapabilityCategory.STORAGE,
                    duration=20,
                    permission="storage.space.read-only",
                    action="storage.collect-space-analysis",
                    event="storage.space-analysis.completed",
                    metrics=("storage.used_percent", "storage.inode_used_percent"),
                    keywords=(
                        "espacio",
                        "lleno",
                        "capacidad",
                        "archivos grandes",
                        "liberar",
                        "cache",
                        "disk usage",
                    ),
                ),
                input_model=SpaceAnalysisInput,
                output_model=SpaceAnalysisResult,
                action=space_action(tools),
            ),
            _DiagnosticCapability(
                metadata=_metadata(
                    capability_id="system.memory-analysis",
                    name="System Memory Analysis",
                    description=(
                        "Distingue RAM disponible, caché, swap, PSI y evidencia OOM sin terminar "
                        "procesos ni alterar sysctl."
                    ),
                    objective=(
                        "Diferenciar presión real de memoria, uso normal de caché y evidencia "
                        "histórica de un target offline."
                    ),
                    category=CapabilityCategory.OPTIMIZATION,
                    duration=8,
                    permission="system.memory.read-only",
                    action="system.collect-memory-analysis",
                    event="system.memory-analysis.completed",
                    metrics=("memory.available_bytes", "memory.pressure_avg10"),
                    keywords=("memoria", "ram", "swap", "oom", "memory", "caché"),
                ),
                input_model=DiagnosticInput,
                output_model=MemoryAnalysisResult,
                action=memory_action(tools),
            ),
            _DiagnosticCapability(
                metadata=_metadata(
                    capability_id="packages.health-check",
                    name="Package Health Check",
                    description=(
                        "Analiza metadatos locales dpkg/APT, estados incompletos y actualizaciones "
                        "interrumpidas sin red ni scripts de paquetes."
                    ),
                    objective=(
                        "Determinar si la base de paquetes Debian/Ubuntu requiere reparación antes "
                        "de autorizar cambios."
                    ),
                    category=CapabilityCategory.PACKAGES,
                    duration=8,
                    permission="packages.database.read-only",
                    action="packages.collect-health-check",
                    event="packages.health-check.completed",
                    metrics=("packages.installed_count", "packages.inconsistent_count"),
                    keywords=("paquetes", "apt", "dpkg", "actualización", "dependencias"),
                ),
                input_model=DiagnosticInput,
                output_model=PackageHealthResult,
                action=packages_action(tools),
            ),
            _DiagnosticCapability(
                metadata=_metadata(
                    capability_id="services.failure-analysis",
                    name="Service Failure Analysis",
                    description=(
                        "Normaliza unidades systemd fallidas y evidencia persistente acotada sin "
                        "reiniciar, habilitar o modificar servicios."
                    ),
                    objective=(
                        "Identificar servicios fallidos y correlacionarlos después con memoria, "
                        "espacio o paquetes."
                    ),
                    category=CapabilityCategory.RECOVERY,
                    duration=10,
                    permission="services.state.read-only",
                    action="services.collect-failure-analysis",
                    event="services.failure-analysis.completed",
                    metrics=("services.failed_count", "services.error_event_count"),
                    keywords=("servicios", "systemd", "daemon", "fallaron", "failed service"),
                ),
                input_model=DiagnosticInput,
                output_model=ServiceFailureResult,
                action=services_action(tools),
            ),
        )

    def capabilities(self) -> tuple[Capability, ...]:
        return self._capabilities
