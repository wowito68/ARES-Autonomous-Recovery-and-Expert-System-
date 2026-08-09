"""Official high-risk filesystem.repair capability."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from ares.actions.filesystem_repair import (
    ExecuteFilesystemRepairAction,
    ProjectFilesystemRepairGraphAction,
    RequestFilesystemRepairAuthorizationAction,
    ValidateFilesystemRepairAction,
    VerifyFilesystemRepairAction,
)
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
from ares.filesystems.executor import FilesystemExecutor
from ares.filesystems.models import (
    FilesystemRepairInput,
    FilesystemRepairResult,
    RepairVerification,
    RepairVerificationStatus,
)
from ares.filesystems.store import FilesystemRepairStore
from ares.workflows import StageMode, WorkflowDefinition, WorkflowStage, WorkflowStep
from ares.workflows.models import StepOutputs

_COMPATIBILITY = OSCompatibility(
    families=("debian",), architectures=("amd64",), minimum_version="13"
)


class _VerifiedRepairPostcheck:
    async def __call__(self, output: dict[str, Any], state: StepOutputs) -> bool:
        del state
        try:
            verification = RepairVerification.model_validate(output.get("verification"))
        except ValueError:
            return False
        return verification.status in {
            RepairVerificationStatus.SUCCESS,
            RepairVerificationStatus.PARTIAL,
            RepairVerificationStatus.FAILED,
            RepairVerificationStatus.UNKNOWN,
        }


class FilesystemRepairCapability:
    input_model = FilesystemRepairInput
    output_model = FilesystemRepairResult
    metadata = CapabilityMetadata(
        id="filesystem.repair",
        version="1.0.0",
        name="Filesystem Repair",
        description=(
            "Repara de forma broker-gated un filesystem soportado después de identidad exacta, "
            "ProtectionCheckpoint verificado, autorización local y preflight de montaje."
        ),
        objective=(
            "Corregir inconsistencias de metadata sin permitir que API, Agent o LLM elijan "
            "comandos, argumentos o un dispositivo diferente al autorizado."
        ),
        category=CapabilityCategory.RECOVERY,
        operation=OperationClass.RECOVER,
        mode=CapabilityMode.MUTATING,
        os_compatibility=_COMPATIBILITY,
        risk=RiskLevel.HIGH,
        estimated_duration_seconds=600,
        permissions=(
            PermissionRequirement(
                id="filesystem.block-device.readwrite-via-broker",
                reason="Permitir escritura únicamente sobre el target exacto mediante broker.",
            ),
            PermissionRequirement(
                id="filesystem.mount.manage-via-broker",
                reason="Desmontar y remontar solo cuando MountSafetyChecker lo autoriza.",
            ),
            PermissionRequirement(
                id="filesystem.metadata.write-local",
                reason="Persistir plan, ejecución y verificación de reparación.",
            ),
            PermissionRequirement(
                id="knowledge.graph.write",
                reason="Conservar estado previo, reparación, checkpoint y estado posterior.",
            ),
        ),
        internal_actions=(
            "filesystem.validate-repair-plan",
            "filesystem.request-repair-authorization",
            "filesystem.execute-repair",
            "filesystem.verify-repair",
            "knowledge.project-filesystem-repair",
        ),
        postchecks=(
            "target-identity-revalidated",
            "protection-checkpoint-exact-resource-match",
            "authorization-one-use-and-plan-bound",
            "filesystem-postcheck-structured",
            "before-after-knowledge-projected",
        ),
        rollback=RollbackPolicy(
            supported=False,
            strategy=(
                "No existe rollback automático de metadata en 1.0. El ProtectionCheckpoint "
                "mantiene una copia verificada de archivos para recuperación posterior."
            ),
        ),
        requires_authorization=True,
        requires_protection_checkpoint=True,
        supported_filesystems=("ext2", "ext3", "ext4", "xfs", "ntfs"),
        requires_unmounted=True,
        supports_dry_run=True,
        supports_verification=True,
        supports_rollback=False,
        required_evidence=(
            "filesystem-type",
            "device-identity-fingerprint",
            "mount-safety",
            "verified-protection-checkpoint",
        ),
        emitted_events=(
            "repair.preflight.completed",
            "repair.authorization.requested",
            "repair.started",
            "filesystem.unmounted",
            "filesystem.repair-command.started",
            "filesystem.repair-command.completed",
            "filesystem.verification.started",
            "filesystem.verification.completed",
            "filesystem.remounted",
            "repair.completed",
            "repair.failed",
            "repair.cancelled",
            "knowledge.graph.updated",
        ),
        metrics=(
            "filesystem.repair.duration",
            "filesystem.repair.verification",
            "filesystem.repair.failures",
        ),
        audit=AuditPolicy(
            record_inputs=False,
            record_outputs=True,
            event_names=(
                "repair.authorization.requested",
                "repair.started",
                "filesystem.repair-command.started",
                "filesystem.repair-command.completed",
                "filesystem.verification.completed",
                "repair.completed",
                "repair.failed",
            ),
        ),
        keywords=(
            "filesystem",
            "file system",
            "fsck",
            "reparar",
            "reparación",
            "partición",
            "inconsistencia",
            "corrupto",
            "ext4",
            "xfs",
            "ntfs",
        ),
    )

    def __init__(self, store: FilesystemRepairStore, executor: FilesystemExecutor) -> None:
        self.validate = ValidateFilesystemRepairAction(store, executor)
        self.authorize = RequestFilesystemRepairAuthorizationAction(store, executor)
        self.execute_repair = ExecuteFilesystemRepairAction(store, executor)
        self.verify = VerifyFilesystemRepairAction(store)
        self.project = ProjectFilesystemRepairGraphAction()

    def build_workflow(self, payload: BaseModel) -> WorkflowDefinition:
        request = FilesystemRepairInput.model_validate(payload)
        validate = WorkflowStep(
            id="validate-repair",
            action=self.validate,
            inputs=lambda _: request.model_dump(mode="json"),
            timeout_seconds=300,
        )
        authorize = WorkflowStep(
            id="authorize-repair",
            action=self.authorize,
            inputs=lambda state: dict(state["validate-repair"]),
            timeout_seconds=900,
        )
        repair = WorkflowStep(
            id="execute-repair",
            action=self.execute_repair,
            inputs=lambda state: dict(state["authorize-repair"]),
            timeout_seconds=28_800,
        )
        verify = WorkflowStep(
            id="verify-repair",
            action=self.verify,
            inputs=lambda state: dict(state["execute-repair"]),
            timeout_seconds=1_800,
            postchecks=(_VerifiedRepairPostcheck(),),
        )
        project = WorkflowStep(
            id="project-repair-knowledge",
            action=self.project,
            inputs=lambda state: dict(state["verify-repair"]),
            timeout_seconds=60,
        )
        return WorkflowDefinition(
            id="filesystem.repair.workflow",
            version="1.0.0",
            capability_id=self.metadata.id,
            stages=(
                WorkflowStage("preflight", StageMode.SEQUENTIAL, (validate,)),
                WorkflowStage("authorization", StageMode.SEQUENTIAL, (authorize,)),
                WorkflowStage("repair", StageMode.SEQUENTIAL, (repair,)),
                WorkflowStage("verification", StageMode.SEQUENTIAL, (verify,)),
                WorkflowStage("knowledge", StageMode.SEQUENTIAL, (project,)),
            ),
            result=lambda state: dict(state["project-repair-knowledge"]),
        )


class FilesystemRepairPlugin:
    manifest = PluginManifest(
        id="ares.filesystem-repair",
        version="1.0.0",
        core_api_version="2.0",
        name="ARES Filesystem Recovery & Repair",
        permissions=(
            "filesystem.block-device.readwrite-via-broker",
            "filesystem.mount.manage-via-broker",
            "filesystem.metadata.write-local",
            "knowledge.graph.write",
        ),
        os_compatibility=_COMPATIBILITY,
        capabilities=("filesystem.repair",),
    )

    def __init__(self, store: FilesystemRepairStore, executor: FilesystemExecutor) -> None:
        self._capabilities: tuple[Capability, ...] = (
            FilesystemRepairCapability(store, executor),
        )

    def capabilities(self) -> tuple[Capability, ...]:
        return self._capabilities
