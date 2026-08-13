"""Boot Recovery capability family."""

from __future__ import annotations

from pydantic import BaseModel

from ares.actions.boot import (
    BootRepairExecutionInput,
    BootRepairPostcheck,
    DiagnoseBootAction,
    ExecuteBootRepairAction,
    ProjectBootDiagnosisGraphAction,
    ProjectBootRepairGraphAction,
)
from ares.boot.engine import BootRecoveryEngine
from ares.boot.models import BootDiagnoseInput, BootDiagnosticResult, BootRepairRecord
from ares.capabilities.base import Capability
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
from ares.workflows import StageMode, WorkflowDefinition, WorkflowStage, WorkflowStep

_COMPATIBILITY = OSCompatibility(
    families=("debian",), architectures=("amd64",), minimum_version="13"
)
_PERMISSIONS = (
    PermissionRequirement(
        id="boot.observe",
        reason=(
            "Inspect firmware, EFI, boot files and boot configuration without persistent mutation."
        ),
    ),
    PermissionRequirement(
        id="boot.repair-via-broker",
        reason="Perform only exact plan-bound GRUB recovery through the privileged broker.",
    ),
    PermissionRequirement(
        id="boot.state.write-local",
        reason=(
            "Persist boot diagnostics, plans, checkpoints, executions and verification evidence."
        ),
    ),
    PermissionRequirement(
        id="knowledge.graph.write",
        reason="Project boot dependency and recovery evidence into the Knowledge Graph.",
    ),
)


class BootDiagnoseCapability:
    input_model = BootDiagnoseInput
    output_model = BootDiagnosticResult
    metadata = CapabilityMetadata(
        id="boot.diagnose",
        version="1.0.0",
        name="Boot Diagnose",
        description=(
            "Diagnoses firmware, Linux boot chain, GRUB/systemd-boot/Windows loader evidence, "
            "EFI entries, kernels, initramfs and fstab without persistent repair actions."
        ),
        objective="Locate an evidence-backed break in the Linux boot dependency chain.",
        category=CapabilityCategory.RECOVERY,
        operation=OperationClass.OBSERVE,
        mode=CapabilityMode.READ_ONLY,
        os_compatibility=_COMPATIBILITY,
        risk=RiskLevel.LOW,
        estimated_duration_seconds=20,
        permissions=(_PERMISSIONS[0], _PERMISSIONS[2], _PERMISSIONS[3]),
        internal_actions=("boot.diagnose-environment", "knowledge.project-boot-diagnosis"),
        postchecks=("boot-diagnostic-typed", "evidence-backed-issues"),
        rollback=RollbackPolicy(
            supported=False, strategy="Diagnosis is read-only and does not require rollback."
        ),
        supports_dry_run=False,
        supports_verification=True,
        emitted_events=(
            "boot.diagnosis.started",
            "boot.environment.detected",
            "boot.bootloader.detected",
            "boot.issue.detected",
            "knowledge.graph.updated",
        ),
        metrics=("boot.issue.count", "boot.diagnostic.confidence"),
        audit=AuditPolicy(
            record_inputs=True,
            record_outputs=True,
            event_names=(
                "boot.diagnosis.started",
                "boot.environment.detected",
                "boot.issue.detected",
            ),
        ),
        keywords=(
            "boot",
            "arranque",
            "grub",
            "efi",
            "uefi",
            "bios",
            "linux no arranca",
            "bootloader",
        ),
    )

    def __init__(self, engine: BootRecoveryEngine) -> None:
        self.diagnose = DiagnoseBootAction(engine)
        self.project = ProjectBootDiagnosisGraphAction()

    def build_workflow(self, payload: BaseModel) -> WorkflowDefinition:
        request = BootDiagnoseInput.model_validate(payload)
        return WorkflowDefinition(
            id="boot.diagnose.workflow",
            version="1.0.0",
            capability_id=self.metadata.id,
            stages=(
                WorkflowStage(
                    "diagnosis",
                    StageMode.SEQUENTIAL,
                    (
                        WorkflowStep(
                            id="diagnose-boot",
                            action=self.diagnose,
                            inputs=lambda _: request.model_dump(mode="json"),
                            timeout_seconds=120,
                        ),
                    ),
                ),
                WorkflowStage(
                    "knowledge",
                    StageMode.SEQUENTIAL,
                    (
                        WorkflowStep(
                            id="project-boot-diagnosis",
                            action=self.project,
                            inputs=lambda state: dict(state["diagnose-boot"]),
                            timeout_seconds=60,
                        ),
                    ),
                ),
            ),
            result=lambda state: dict(state["project-boot-diagnosis"]),
        )


class BootRepairGrubCapability:
    input_model = BootRepairExecutionInput
    output_model = BootRepairRecord
    metadata = CapabilityMetadata(
        id="boot.repair.grub",
        version="1.0.0",
        name="GRUB Boot Repair",
        description=(
            "Repairs an explicitly approved Debian-family GRUB boot chain after a boot-state "
            "ProtectionCheckpoint and independent authorization."
        ),
        objective="Recover the minimum GRUB/EFI/configuration state supported by evidence.",
        category=CapabilityCategory.RECOVERY,
        operation=OperationClass.RECOVER,
        mode=CapabilityMode.MUTATING,
        os_compatibility=_COMPATIBILITY,
        risk=RiskLevel.HIGH,
        estimated_duration_seconds=180,
        permissions=_PERMISSIONS,
        internal_actions=("boot.execute-authorized-repair", "knowledge.project-boot-repair"),
        required_evidence=(
            "boot-diagnostic",
            "device-identity-fingerprint",
            "boot-repair-plan-fingerprint",
            "boot-protection-checkpoint",
            "independent-authorization",
        ),
        postchecks=("boot-verification-partial-or-verified",),
        rollback=RollbackPolicy(
            supported=True,
            strategy=(
                "The pre-repair boot-state checkpoint preserves selected EFI, GRUB and boot "
                "configuration artifacts. Automatic rollback is not attempted after an UNKNOWN "
                "state; evidence-based reconciliation is required."
            ),
        ),
        requires_authorization=True,
        requires_protection_checkpoint=True,
        supports_dry_run=True,
        supports_verification=True,
        supports_rollback=True,
        emitted_events=(
            "boot.protection-checkpoint.created",
            "boot.repair-authorization.requested",
            "boot.repair-environment.created",
            "boot.repair.started",
            "boot.bootloader.installed",
            "boot.configuration.regenerated",
            "boot.initramfs.regenerated",
            "boot.entry.updated",
            "boot.verification.started",
            "boot.verification.completed",
            "boot.repair.completed",
            "boot.repair.failed",
            "boot.repair.aborted",
            "knowledge.graph.updated",
        ),
        metrics=("boot.repair.duration", "boot.verification.status"),
        audit=AuditPolicy(
            record_inputs=False,
            record_outputs=True,
            event_names=(
                "boot.repair-authorization.requested",
                "boot.repair.started",
                "boot.bootloader.installed",
                "boot.configuration.regenerated",
                "boot.verification.completed",
                "boot.repair.completed",
                "boot.repair.failed",
            ),
        ),
        keywords=("grub repair", "reparar grub", "boot recovery", "recuperar arranque"),
    )

    def __init__(self, engine: BootRecoveryEngine) -> None:
        self.execute = ExecuteBootRepairAction(engine)
        self.project = ProjectBootRepairGraphAction()

    def build_workflow(self, payload: BaseModel) -> WorkflowDefinition:
        request = BootRepairExecutionInput.model_validate(payload)
        return WorkflowDefinition(
            id="boot.repair.grub.workflow",
            version="1.0.0",
            capability_id=self.metadata.id,
            stages=(
                WorkflowStage(
                    "repair",
                    StageMode.SEQUENTIAL,
                    (
                        WorkflowStep(
                            id="execute-boot-repair",
                            action=self.execute,
                            inputs=lambda _: request.model_dump(mode="json"),
                            timeout_seconds=3600,
                            postchecks=(BootRepairPostcheck(),),
                        ),
                    ),
                ),
                WorkflowStage(
                    "knowledge",
                    StageMode.SEQUENTIAL,
                    (
                        WorkflowStep(
                            id="project-boot-repair",
                            action=self.project,
                            inputs=lambda state: dict(state["execute-boot-repair"]),
                            timeout_seconds=60,
                        ),
                    ),
                ),
            ),
            result=lambda state: dict(state["project-boot-repair"]),
        )


class BootRecoveryPlugin:
    manifest = PluginManifest(
        id="ares.boot-recovery",
        version="1.0.0",
        core_api_version="2.0",
        name="ARES Boot Recovery & Bootloader Management",
        permissions=tuple(item.id for item in _PERMISSIONS),
        os_compatibility=_COMPATIBILITY,
        capabilities=("boot.diagnose", "boot.repair.grub"),
    )

    def __init__(self, engine: BootRecoveryEngine) -> None:
        self._capabilities: tuple[Capability, ...] = (
            BootDiagnoseCapability(engine),
            BootRepairGrubCapability(engine),
        )

    def capabilities(self) -> tuple[Capability, ...]:
        return self._capabilities
