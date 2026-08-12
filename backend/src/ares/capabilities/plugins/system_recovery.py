"""Capabilities for evidence-driven compound Linux recovery."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, RootModel

from ares.actions.recovery import (
    DiagnoseKernelAction,
    DiagnosePackagesAction,
    ExecuteRecoveryChildAction,
    KernelDiagnoseInput,
    PackageDiagnoseInput,
    RecoveryChildExecutionInput,
    RunSystemRecoveryAction,
    SystemRecoveryActionInput,
)
from ares.capabilities.models import (
    AuditPolicy,
    CapabilityCategory,
    CapabilityMetadata,
    CapabilityMode,
    OperationClass,
    OSCompatibility,
    PermissionRequirement,
    PluginManifest,
    RiskLevel,
    RollbackPolicy,
)
from ares.recovery.executor import RecoveryMutationExecutor
from ares.recovery.orchestrator import RecoveryOrchestrator
from ares.tools.recovery import RecoveryProcessRunner
from ares.workflows import StageMode, WorkflowDefinition, WorkflowStage, WorkflowStep

_COMPAT = OSCompatibility(
    families=("debian", "ubuntu", "linux"),
    architectures=("amd64", "arm64"),
    live_modes=("live", "persistent", "recovery"),
)
_PERMISSIONS = (
    PermissionRequirement(id="recovery.read-system", reason="Read bounded Linux recovery evidence."),
    PermissionRequirement(
        id="recovery.write-state", reason="Persist RecoveryCase, plans, operations and verification."
    ),
    PermissionRequirement(
        id="recovery.mutate-via-broker",
        reason="Execute exact protected child mutations only through the root broker.",
    ),
    PermissionRequirement(
        id="knowledge.graph.write", reason="Project recovery evidence and causal relationships."
    ),
)


class RecoveryEnvelope(RootModel[dict[str, Any]]):
    pass


class RecoveryChildResult(BaseModel):
    changes: list[str]
    verification: dict[str, Any]


class EvidenceResult(BaseModel):
    evidence: list[dict[str, Any]]


class PackageEvidenceResult(EvidenceResult):
    manager: str


class _OneStepCapability:
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    metadata: CapabilityMetadata
    action: Any

    def build_workflow(self, payload: BaseModel) -> WorkflowDefinition:
        return WorkflowDefinition(
            id=f"{self.metadata.id}.workflow",
            version=self.metadata.version,
            capability_id=self.metadata.id,
            stages=(
                WorkflowStage(
                    "execute",
                    StageMode.SEQUENTIAL,
                    (
                        WorkflowStep(
                            id="execute",
                            action=self.action,
                            inputs=payload.model_dump(mode="json"),
                        ),
                    ),
                ),
            ),
        )


class SystemRecoveryCapability(_OneStepCapability):
    input_model = SystemRecoveryActionInput
    output_model = RecoveryEnvelope
    metadata = CapabilityMetadata(
        id="system.recovery",
        version="1.0.0",
        name="System Recovery Orchestrator",
        description="Coordinates evidence, plans and independently protected child recovery capabilities.",
        objective="Recover Linux software/configuration failures with minimal evidence-backed intervention.",
        category=CapabilityCategory.RECOVERY,
        operation=OperationClass.RECOVER,
        mode=CapabilityMode.MUTATING,
        os_compatibility=_COMPAT,
        risk=RiskLevel.HIGH,
        estimated_duration_seconds=None,
        permissions=_PERMISSIONS,
        internal_actions=("recovery.system-orchestrate",),
        postchecks=("child-operations-retain-independent-state",),
        rollback=RollbackPolicy(
            supported=False,
            strategy="The case has no global rollback; each child operation declares its own policy.",
        ),
        supports_dry_run=True,
        supports_verification=True,
        requires_authorization=False,
        requires_protection_checkpoint=False,
        emitted_events=("recovery.case.created", "recovery.plan.created", "recovery.case.completed"),
        metrics=("recovery.issue.count", "recovery.operation.count"),
        audit=AuditPolicy(
            record_inputs=True,
            record_outputs=True,
            event_names=("recovery.plan.created", "recovery.verification.completed"),
        ),
        keywords=("recovery", "recuperación", "emergency mode", "modo emergencia", "sistema roto"),
    )

    def __init__(self, orchestrator: RecoveryOrchestrator) -> None:
        self.action = RunSystemRecoveryAction(orchestrator)


class PackageDiagnoseCapability(_OneStepCapability):
    input_model = PackageDiagnoseInput
    output_model = PackageEvidenceResult
    metadata = CapabilityMetadata(
        id="package.diagnose",
        version="1.0.0",
        name="APT Package Diagnosis",
        description="Diagnoses bounded APT/dpkg state without modifying packages.",
        objective="Detect interrupted configuration and package-database/dependency evidence.",
        category=CapabilityCategory.PACKAGES,
        operation=OperationClass.OBSERVE,
        mode=CapabilityMode.READ_ONLY,
        os_compatibility=_COMPAT,
        risk=RiskLevel.LOW,
        estimated_duration_seconds=10,
        permissions=(_PERMISSIONS[0],),
        internal_actions=("recovery.package-diagnose",),
        postchecks=("structured-package-evidence",),
        rollback=RollbackPolicy(supported=False, strategy="Read-only diagnosis."),
        supports_dry_run=False,
        supports_verification=True,
        emitted_events=(),
        metrics=("package.issue.count",),
        audit=AuditPolicy(record_inputs=True, record_outputs=True, event_names=()),
        keywords=("apt", "dpkg", "broken packages", "paquetes rotos"),
    )

    def __init__(self, runner: RecoveryProcessRunner) -> None:
        self.action = DiagnosePackagesAction(runner)


class KernelDiagnoseCapability(_OneStepCapability):
    input_model = KernelDiagnoseInput
    output_model = EvidenceResult
    metadata = CapabilityMetadata(
        id="kernel.diagnose",
        version="1.0.0",
        name="Kernel and Initramfs Diagnosis",
        description="Detects installed kernel/initramfs inconsistencies without mutation.",
        objective="Identify missing or inconsistent kernel/initramfs pairs.",
        category=CapabilityCategory.RECOVERY,
        operation=OperationClass.OBSERVE,
        mode=CapabilityMode.READ_ONLY,
        os_compatibility=_COMPAT,
        risk=RiskLevel.LOW,
        estimated_duration_seconds=5,
        permissions=(_PERMISSIONS[0],),
        internal_actions=("recovery.kernel-diagnose",),
        postchecks=("structured-kernel-evidence",),
        rollback=RollbackPolicy(supported=False, strategy="Read-only diagnosis."),
        supports_dry_run=False,
        supports_verification=True,
        emitted_events=(),
        metrics=("kernel.issue.count",),
        audit=AuditPolicy(record_inputs=True, record_outputs=True, event_names=()),
        keywords=("kernel", "initramfs", "initrd"),
    )

    def __init__(self) -> None:
        self.action = DiagnoseKernelAction()


class _ChildMutationCapability(_OneStepCapability):
    input_model = RecoveryChildExecutionInput
    output_model = RecoveryChildResult

    def __init__(self, executor: RecoveryMutationExecutor, runner: RecoveryProcessRunner) -> None:
        self.action = ExecuteRecoveryChildAction(executor, runner)


class PackageRepairCapability(_ChildMutationCapability):
    metadata = CapabilityMetadata(
        id="package.repair",
        version="1.0.0",
        name="APT Package Repair",
        description="Executes only planned APT/dpkg repairs without generic upgrade or network fallback.",
        objective="Repair interrupted configuration or locally satisfiable broken dependencies.",
        category=CapabilityCategory.PACKAGES,
        operation=OperationClass.RECOVER,
        mode=CapabilityMode.MUTATING,
        os_compatibility=_COMPAT,
        risk=RiskLevel.HIGH,
        estimated_duration_seconds=None,
        permissions=_PERMISSIONS,
        internal_actions=("recovery.child-execute",),
        postchecks=("dpkg-state-consistent",),
        rollback=RollbackPolicy(
            supported=False,
            strategy="Package maintainer scripts can be irreversible; no generic rollback is claimed.",
        ),
        supports_dry_run=True,
        supports_verification=True,
        requires_authorization=False,
        requires_protection_checkpoint=True,
        emitted_events=("recovery.operation.completed",),
        metrics=("package.repair.count",),
        audit=AuditPolicy(record_inputs=True, record_outputs=True, event_names=("recovery.operation.completed",)),
        keywords=("package repair", "apt repair", "dpkg repair"),
    )


class ConfigurationRecoverCapability(_ChildMutationCapability):
    metadata = CapabilityMetadata(
        id="configuration.recover",
        version="1.0.0",
        name="Controlled Configuration Recovery",
        description="Applies only allowlisted, pre-diffed configuration changes with a verified checkpoint.",
        objective="Recover known Linux configuration using minimum reversible changes.",
        category=CapabilityCategory.RECOVERY,
        operation=OperationClass.RECOVER,
        mode=CapabilityMode.MUTATING,
        os_compatibility=_COMPAT,
        risk=RiskLevel.HIGH,
        estimated_duration_seconds=10,
        permissions=_PERMISSIONS,
        internal_actions=("recovery.child-execute",),
        postchecks=("configuration-hash-matches-proposal",),
        rollback=RollbackPolicy(
            supported=True,
            strategy="Restore the exact checkpointed original file when validation fails.",
        ),
        supports_dry_run=True,
        supports_verification=True,
        requires_authorization=False,
        requires_protection_checkpoint=True,
        emitted_events=("recovery.operation.completed", "recovery.operation.rolled-back"),
        metrics=("configuration.recovery.count",),
        audit=AuditPolicy(record_inputs=True, record_outputs=True, event_names=("recovery.operation.completed",)),
        keywords=("fstab", "configuration recover", "configuración"),
    )


class InitramfsRebuildCapability(_ChildMutationCapability):
    metadata = CapabilityMetadata(
        id="initramfs.rebuild",
        version="1.0.0",
        name="Initramfs Rebuild",
        description="Rebuilds an explicitly selected Debian-family initramfs and verifies the artifact.",
        objective="Recover a missing/inconsistent initramfs without changing the active kernel.",
        category=CapabilityCategory.RECOVERY,
        operation=OperationClass.RECOVER,
        mode=CapabilityMode.MUTATING,
        os_compatibility=_COMPAT,
        risk=RiskLevel.HIGH,
        estimated_duration_seconds=None,
        permissions=_PERMISSIONS,
        internal_actions=("recovery.child-execute",),
        postchecks=("initramfs-artifact-exists",),
        rollback=RollbackPolicy(
            supported=True,
            strategy="Restore a checkpointed prior initramfs when one existed; otherwise rollback is limited.",
        ),
        supports_dry_run=True,
        supports_verification=True,
        requires_authorization=False,
        requires_protection_checkpoint=True,
        emitted_events=("recovery.operation.completed",),
        metrics=("initramfs.rebuild.count",),
        audit=AuditPolicy(record_inputs=True, record_outputs=True, event_names=("recovery.operation.completed",)),
        keywords=("initramfs rebuild", "rebuild initrd"),
    )


class SystemRecoveryPlugin:
    manifest = PluginManifest(
        id="ares.system-recovery",
        version="1.0.0",
        name="ARES System Recovery",
        description="Evidence-driven Linux recovery orchestration and bounded child repair capabilities.",
        permissions=_PERMISSIONS,
    )

    def __init__(
        self,
        orchestrator: RecoveryOrchestrator,
        executor: RecoveryMutationExecutor,
        runner: RecoveryProcessRunner,
    ) -> None:
        self._capabilities = (
            SystemRecoveryCapability(orchestrator),
            PackageDiagnoseCapability(runner),
            KernelDiagnoseCapability(),
            PackageRepairCapability(executor, runner),
            ConfigurationRecoverCapability(executor, runner),
            InitramfsRebuildCapability(executor, runner),
        )

    def capabilities(self) -> tuple[Any, ...]:
        return self._capabilities
