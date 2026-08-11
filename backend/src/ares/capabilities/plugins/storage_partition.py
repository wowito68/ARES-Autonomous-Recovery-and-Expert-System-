"""Storage partition capability family backed by the Storage Operation Engine."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from ares.actions.storage_operations import (
    ExecuteStorageTransactionAction,
    InspectStorageLayoutAction,
    ProjectStorageLayoutGraphAction,
    ProjectStorageTransactionGraphAction,
    ValidateAuthorizedStorageOperationAction,
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
from ares.storage_operations.engine import StorageOperationEngine
from ares.storage_operations.models import (
    PartitionInspectInput,
    PartitionInspectResult,
    StorageOperationExecutionInput,
    StorageOperationResult,
    StorageVerification,
    StorageVerificationStatus,
)
from ares.storage_operations.store import StorageOperationStore
from ares.workflows import StageMode, WorkflowDefinition, WorkflowStage, WorkflowStep
from ares.workflows.models import StepOutputs

_COMPATIBILITY = OSCompatibility(
    families=("debian",), architectures=("amd64",), minimum_version="13"
)
_STORAGE_PERMISSIONS = (
    PermissionRequirement(
        id="storage.partition.observe",
        reason="Inspect partition table, identity and dependency metadata without mutation.",
    ),
    PermissionRequirement(
        id="storage.partition.write-via-broker",
        reason="Permit only exact plan-bound partition-table writes through the root broker.",
    ),
    PermissionRequirement(
        id="storage.transaction.write-local",
        reason="Persist plans, checkpoints, transactions and verification evidence.",
    ),
    PermissionRequirement(
        id="knowledge.graph.write",
        reason="Project storage layout and transaction history into the Knowledge Graph.",
    ),
)


class _StorageVerificationPostcheck:
    async def __call__(self, output: dict[str, Any], state: StepOutputs) -> bool:
        del state
        try:
            verification = StorageVerification.model_validate(output.get("verification"))
        except ValueError:
            return False
        return verification.status in {
            StorageVerificationStatus.VERIFIED,
            StorageVerificationStatus.PARTIAL,
            StorageVerificationStatus.FAILED,
            StorageVerificationStatus.UNKNOWN,
        }


class PartitionInspectCapability:
    input_model = PartitionInspectInput
    output_model = PartitionInspectResult
    metadata = CapabilityMetadata(
        id="storage.partition.inspect",
        version="1.0.0",
        name="Partition Inspect",
        description=(
            "Inspects GPT/MBR layout, free regions, filesystems, mounts, encryption, LVM/RAID, "
            "operating systems and boot dependencies without modifying storage."
        ),
        objective="Produce exact storage-layout evidence for declarative partition planning.",
        category=CapabilityCategory.STORAGE,
        operation=OperationClass.OBSERVE,
        mode=CapabilityMode.READ_ONLY,
        os_compatibility=_COMPATIBILITY,
        risk=RiskLevel.LOW,
        estimated_duration_seconds=10,
        permissions=(_STORAGE_PERMISSIONS[0], _STORAGE_PERMISSIONS[2], _STORAGE_PERMISSIONS[3]),
        internal_actions=("storage.partition.inspect-layout", "knowledge.project-storage-layout"),
        postchecks=("layout-typed", "knowledge-graph-revision-advanced"),
        rollback=RollbackPolicy(
            supported=False, strategy="Read-only inspection; no rollback applies."
        ),
        supports_dry_run=False,
        supports_verification=True,
        emitted_events=(
            "storage.partition.inspected",
            "knowledge.graph.updated",
        ),
        metrics=("storage.partition.count", "storage.free-region.count"),
        audit=AuditPolicy(
            record_inputs=True,
            record_outputs=True,
            event_names=("storage.partition.inspected", "knowledge.graph.updated"),
        ),
        keywords=("partition", "partición", "gpt", "mbr", "layout", "espacio libre"),
    )

    def __init__(self, engine: StorageOperationEngine) -> None:
        self.inspect = InspectStorageLayoutAction(engine)
        self.project = ProjectStorageLayoutGraphAction()

    def build_workflow(self, payload: BaseModel) -> WorkflowDefinition:
        request = PartitionInspectInput.model_validate(payload)
        return WorkflowDefinition(
            id="storage.partition.inspect.workflow",
            version="1.0.0",
            capability_id=self.metadata.id,
            stages=(
                WorkflowStage(
                    "inspect",
                    StageMode.SEQUENTIAL,
                    (
                        WorkflowStep(
                            id="inspect-layout",
                            action=self.inspect,
                            inputs=lambda _: request.model_dump(mode="json"),
                            timeout_seconds=60,
                        ),
                    ),
                ),
                WorkflowStage(
                    "knowledge",
                    StageMode.SEQUENTIAL,
                    (
                        WorkflowStep(
                            id="project-layout",
                            action=self.project,
                            inputs=lambda state: dict(state["inspect-layout"]),
                            timeout_seconds=60,
                        ),
                    ),
                ),
            ),
            result=lambda state: dict(state["project-layout"]),
        )


class _PartitionMutationCapability:
    input_model = StorageOperationExecutionInput
    output_model = StorageOperationResult
    metadata: CapabilityMetadata

    def __init__(self, engine: StorageOperationEngine, store: StorageOperationStore) -> None:
        self.validate = ValidateAuthorizedStorageOperationAction(store)
        self.execute_operation = ExecuteStorageTransactionAction(engine, store)
        self.project = ProjectStorageTransactionGraphAction()

    def build_workflow(self, payload: BaseModel) -> WorkflowDefinition:
        request = StorageOperationExecutionInput.model_validate(payload)
        validate = WorkflowStep(
            id="validate-authorized-operation",
            action=self.validate,
            inputs=lambda _: request.model_dump(mode="json"),
            timeout_seconds=60,
        )
        execute = WorkflowStep(
            id="execute-storage-transaction",
            action=self.execute_operation,
            inputs=lambda state: dict(state["validate-authorized-operation"]),
            timeout_seconds=3_600,
            postchecks=(_StorageVerificationPostcheck(),),
        )
        project = WorkflowStep(
            id="project-storage-transaction",
            action=self.project,
            inputs=lambda state: dict(state["execute-storage-transaction"]),
            timeout_seconds=60,
        )
        return WorkflowDefinition(
            id=f"{self.metadata.id}.workflow",
            version=self.metadata.version,
            capability_id=self.metadata.id,
            stages=(
                WorkflowStage("preflight", StageMode.SEQUENTIAL, (validate,)),
                WorkflowStage("mutation", StageMode.SEQUENTIAL, (execute,)),
                WorkflowStage("knowledge", StageMode.SEQUENTIAL, (project,)),
            ),
            result=lambda state: dict(state["project-storage-transaction"]),
        )


def _mutation_metadata(
    capability_id: str,
    *,
    name: str,
    description: str,
    objective: str,
    risk: RiskLevel,
    enabled: bool,
    disabled_reason: str | None = None,
) -> CapabilityMetadata:
    return CapabilityMetadata(
        id=capability_id,
        version="1.0.0",
        name=name,
        description=description,
        objective=objective,
        category=CapabilityCategory.STORAGE,
        operation=OperationClass.CHANGE,
        mode=CapabilityMode.MUTATING,
        os_compatibility=_COMPATIBILITY,
        risk=risk,
        estimated_duration_seconds=60,
        permissions=_STORAGE_PERMISSIONS,
        internal_actions=(
            "storage.validate-authorized-operation",
            "storage.execute-transaction",
            "knowledge.project-storage-transaction",
        ),
        postchecks=(
            "exact-device-identity",
            "dry-run-valid",
            "protection-checkpoint-ready",
            "one-use-authorization",
            "partition-geometry-verified",
        ),
        rollback=RollbackPolicy(
            supported=True,
            strategy=(
                "A verified pre-write sfdisk table dump is retained as ProtectionCheckpoint. "
                "Automatic rollback is deliberately not executed after an uncertain write; "
                "UNKNOWN requires reinspection before any recovery action."
            ),
        ),
        requires_authorization=True,
        requires_protection_checkpoint=True,
        supports_dry_run=True,
        supports_verification=True,
        supports_rollback=True,
        required_evidence=(
            "device-identity-fingerprint",
            "partition-table-layout",
            "data-impact-assessment",
            "boot-impact-assessment",
            "verified-protection-checkpoint",
        ),
        emitted_events=(
            "storage.plan.created",
            "storage.preflight.completed",
            "storage.impact.analyzed",
            "storage.checkpoint.created",
            "storage.authorization.requested",
            "storage.transaction.started",
            "storage.partition.created",
            "storage.partition.deleted",
            "storage.partition-table.updated",
            "storage.verification.started",
            "storage.verification.completed",
            "storage.transaction.committed",
            "storage.transaction.failed",
            "storage.transaction.unknown",
            "storage.transaction.aborted",
            "knowledge.graph.updated",
        ),
        metrics=("storage.operation.duration", "storage.operation.verification"),
        audit=AuditPolicy(
            record_inputs=False,
            record_outputs=True,
            event_names=(
                "storage.authorization.requested",
                "storage.transaction.started",
                "storage.partition-table.updated",
                "storage.verification.completed",
                "storage.transaction.committed",
                "storage.transaction.failed",
                "storage.transaction.unknown",
            ),
        ),
        keywords=("partition", "partición", capability_id.rsplit(".", 1)[-1], "storage layout"),
        enabled=enabled,
        disabled_reason=disabled_reason,
    )


class PartitionCreateCapability(_PartitionMutationCapability):
    metadata = _mutation_metadata(
        "storage.partition.create",
        name="Partition Create",
        description=(
            "Creates one partition only inside verified free space on a controlled disk image or "
            "controlled loop target; physical-disk writes are hard-blocked."
        ),
        objective=(
            "Create a partition-table entry without formatting or wiping contained signatures."
        ),
        risk=RiskLevel.MEDIUM,
        enabled=True,
    )


class PartitionDeleteCapability(_PartitionMutationCapability):
    metadata = _mutation_metadata(
        "storage.partition.delete",
        name="Partition Delete",
        description=(
            "Deletes only a blank, unmounted, non-boot, non-encrypted, non-LVM/RAID partition "
            "from a controlled image/loop target after exact protection and authorization."
        ),
        objective=(
            "Remove an eligible test partition table entry with verifiable before/after state."
        ),
        risk=RiskLevel.HIGH,
        enabled=True,
    )


class PartitionResizeCapability(_PartitionMutationCapability):
    metadata = _mutation_metadata(
        "storage.partition.resize",
        name="Partition Resize",
        description=(
            "Models grow/shrink dependencies but has no executable adapter in this increment."
        ),
        objective=(
            "Represent filesystem-aware partition resize safely before future implementation."
        ),
        risk=RiskLevel.HIGH,
        enabled=False,
        disabled_reason="Filesystem-aware grow/shrink recovery policy is not implemented.",
    )


class PartitionMoveCapability(_PartitionMutationCapability):
    metadata = _mutation_metadata(
        "storage.partition.move",
        name="Partition Move",
        description="Models partition movement as a critical operation; execution is disabled.",
        objective="Represent move risk/dependencies without permitting data movement.",
        risk=RiskLevel.CRITICAL,
        enabled=False,
        disabled_reason="Partition data movement and interruption recovery are not implemented.",
    )


class StoragePartitionPlugin:
    manifest = PluginManifest(
        id="ares.storage-partition-management",
        version="1.0.0",
        core_api_version="2.0",
        name="ARES Storage & Partition Management",
        permissions=tuple(item.id for item in _STORAGE_PERMISSIONS),
        os_compatibility=_COMPATIBILITY,
        capabilities=(
            "storage.partition.inspect",
            "storage.partition.create",
            "storage.partition.delete",
            "storage.partition.resize",
            "storage.partition.move",
        ),
    )

    def __init__(self, engine: StorageOperationEngine, store: StorageOperationStore) -> None:
        self._capabilities: tuple[Capability, ...] = (
            PartitionInspectCapability(engine),
            PartitionCreateCapability(engine, store),
            PartitionDeleteCapability(engine, store),
            PartitionResizeCapability(engine, store),
            PartitionMoveCapability(engine, store),
        )

    def capabilities(self) -> tuple[Capability, ...]:
        return self._capabilities
