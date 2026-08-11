"""Official Backup & Snapshot Manager capability plugin (backup subset)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from ares.actions.backup import (
    CreateBackupFilesAction,
    ListBackupsAction,
    LoadBackupForVerificationAction,
    PersistBackupManifestAction,
    ProjectBackupGraphAction,
    RequestBackupAuthorizationAction,
    ValidateBackupPlanAction,
    VerifyBackupAction,
)
from ares.backup.executor import BackupExecutor
from ares.backup.models import (
    Backup,
    BackupCreateInput,
    BackupCreateResult,
    BackupGraphSummary,
    BackupListInput,
    BackupListResult,
    BackupManifest,
    BackupVerification,
    BackupVerifyInput,
    BackupVerifyResult,
)
from ares.backup.store import BackupStore
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
from ares.tools.backup import BackupFilesystemTools
from ares.workflows import StageMode, WorkflowDefinition, WorkflowStage, WorkflowStep
from ares.workflows.models import StepOutputs

_COMPATIBILITY = OSCompatibility(
    families=("debian",), architectures=("amd64",), minimum_version="13"
)


class _BackupResultPostcheck:
    async def __call__(self, output: dict[str, Any], state: StepOutputs) -> bool:
        del state
        try:
            backup = Backup.model_validate(output.get("backup"))
            verification = BackupVerification.model_validate(output.get("verification"))
        except ValueError:
            return False
        return backup.id == verification.backup_id


class BackupCreateCapability:
    input_model = BackupCreateInput
    output_model = BackupCreateResult
    metadata = CapabilityMetadata(
        id="backup.create",
        version="1.0.0",
        name="Create Backup",
        description=(
            "Crea un respaldo local verificado desde un plan inmutable, con autorización "
            "independiente, manifest SHA-256 y ejecución de escritura aislada en el broker."
        ),
        objective=(
            "Proteger archivos o directorios antes de mantenimiento mediante una copia "
            "verificable y trazable sin sobrescribir backups existentes."
        ),
        category=CapabilityCategory.BACKUP,
        operation=OperationClass.CHANGE,
        mode=CapabilityMode.MUTATING,
        os_compatibility=_COMPATIBILITY,
        risk=RiskLevel.MEDIUM,
        estimated_duration_seconds=300,
        permissions=(
            PermissionRequirement(
                id="backup.plan.read", reason="Consumir un BackupPlan exacto ya generado."
            ),
            PermissionRequirement(
                id="backup.destination.write-via-broker",
                reason="Copiar datos únicamente mediante el broker y un grant de un solo uso.",
            ),
            PermissionRequirement(
                id="backup.metadata.write-local",
                reason="Persistir manifest, verificación y estado privado de ARES.",
            ),
            PermissionRequirement(
                id="knowledge.graph.write",
                reason="Relacionar recursos protegidos con su backup verificado.",
            ),
        ),
        internal_actions=(
            "backup.validate-plan",
            "backup.request-authorization",
            "backup.create-files",
            "backup.persist-manifest",
            "backup.verify-integrity",
            "knowledge.project-backup",
        ),
        postchecks=(
            "plan-revalidated-before-authorization",
            "one-use-grant-bound-to-plan",
            "manifest-has-sha256",
            "backup-verification-recorded",
        ),
        rollback=RollbackPolicy(
            supported=False,
            strategy=(
                "Las copias parciales se eliminan antes de publicar el backup; un backup "
                "completado nunca se borra automáticamente como compensación."
            ),
        ),
        required_evidence=(),
        emitted_events=(
            "backup.authorization.requested",
            "backup.started",
            "backup.progress",
            "backup.entry.created",
            "backup.verification.started",
            "backup.verified",
            "backup.completed",
            "backup.failed",
            "backup.cancelled",
        ),
        metrics=(
            "backup.bytes",
            "backup.files",
            "backup.progress.percent",
            "backup.verification.mismatches",
        ),
        audit=AuditPolicy(
            record_inputs=False,
            record_outputs=True,
            event_names=(
                "backup.authorization.requested",
                "backup.started",
                "backup.verification.started",
                "backup.completed",
                "backup.failed",
            ),
        ),
        keywords=(
            "backup",
            "respaldo",
            "respaldar",
            "copia",
            "documentos",
            "proteger",
            "antes",
            "reparar",
        ),
    )

    def __init__(
        self,
        store: BackupStore,
        tools: BackupFilesystemTools,
        executor: BackupExecutor,
    ) -> None:
        self.validate = ValidateBackupPlanAction(store, tools)
        self.authorize = RequestBackupAuthorizationAction(store, executor)
        self.create = CreateBackupFilesAction(store, executor)
        self.persist = PersistBackupManifestAction(store)
        self.verify = VerifyBackupAction(store, executor)
        self.project = ProjectBackupGraphAction()

    def build_workflow(self, payload: BaseModel) -> WorkflowDefinition:
        request = BackupCreateInput.model_validate(payload)
        validate = WorkflowStep(
            id="validate-plan",
            action=self.validate,
            inputs=lambda _: request.model_dump(mode="json"),
            timeout_seconds=300,
        )
        authorize = WorkflowStep(
            id="request-authorization",
            action=self.authorize,
            inputs=lambda state: dict(state["validate-plan"]),
            timeout_seconds=900,
        )
        create = WorkflowStep(
            id="create-files",
            action=self.create,
            inputs=lambda state: dict(state["request-authorization"]),
            timeout_seconds=86_400,
        )
        persist = WorkflowStep(
            id="persist-manifest",
            action=self.persist,
            inputs=lambda state: dict(state["create-files"]),
            timeout_seconds=60,
        )
        verify = WorkflowStep(
            id="verify-backup",
            action=self.verify,
            inputs=lambda state: dict(state["persist-manifest"]),
            timeout_seconds=86_400,
            postchecks=(_BackupResultPostcheck(),),
        )
        project = WorkflowStep(
            id="project-knowledge-graph",
            action=self.project,
            inputs=lambda state: dict(state["verify-backup"]),
            timeout_seconds=60,
        )
        return WorkflowDefinition(
            id="backup.create.workflow",
            version="1.0.0",
            capability_id=self.metadata.id,
            stages=(
                WorkflowStage("validation", StageMode.SEQUENTIAL, (validate,)),
                WorkflowStage("authorization", StageMode.SEQUENTIAL, (authorize,)),
                WorkflowStage("copy", StageMode.SEQUENTIAL, (create, persist)),
                WorkflowStage("verification", StageMode.SEQUENTIAL, (verify,)),
                WorkflowStage("knowledge", StageMode.SEQUENTIAL, (project,)),
            ),
            result=_create_result,
        )


class BackupVerifyCapability:
    input_model = BackupVerifyInput
    output_model = BackupVerifyResult
    metadata = CapabilityMetadata(
        id="backup.verify",
        version="1.0.0",
        name="Verify Backup",
        description="Recalcula integridad de un backup existente contra su manifest SHA-256.",
        objective=(
            "Detectar corrupción, entradas ausentes o cambios de integridad "
            "sin modificar el backup."
        ),
        category=CapabilityCategory.BACKUP,
        operation=OperationClass.OBSERVE,
        mode=CapabilityMode.READ_ONLY,
        os_compatibility=_COMPATIBILITY,
        risk=RiskLevel.LOW,
        estimated_duration_seconds=300,
        permissions=(
            PermissionRequirement(id="backup.metadata.read", reason="Leer metadata y manifest."),
            PermissionRequirement(
                id="backup.verify.read", reason="Leer el destino para verificar hashes."
            ),
            PermissionRequirement(
                id="backup.metadata.write-local", reason="Persistir resultado de verificación."
            ),
            PermissionRequirement(
                id="knowledge.graph.write", reason="Actualizar el estado verificado del grafo."
            ),
        ),
        internal_actions=(
            "backup.load-verification-target",
            "backup.verify-integrity",
            "knowledge.project-backup",
        ),
        postchecks=("verification-structured",),
        rollback=RollbackPolicy(supported=False, strategy="La verificación no modifica el backup."),
        emitted_events=("backup.verification.started", "backup.verified", "backup.failed"),
        metrics=("backup.verification.mismatches",),
        audit=AuditPolicy(
            record_inputs=False,
            record_outputs=True,
            event_names=("backup.verification.started", "backup.verified", "backup.failed"),
        ),
        keywords=("backup", "verify", "verificar", "integridad", "checksum"),
    )

    def __init__(self, store: BackupStore, executor: BackupExecutor) -> None:
        self.load = LoadBackupForVerificationAction(store)
        self.verify = VerifyBackupAction(store, executor)
        self.project = ProjectBackupGraphAction()

    def build_workflow(self, payload: BaseModel) -> WorkflowDefinition:
        request = BackupVerifyInput.model_validate(payload)
        load = WorkflowStep(
            id="load-backup",
            action=self.load,
            inputs=lambda _: request.model_dump(mode="json"),
            timeout_seconds=30,
        )
        verify = WorkflowStep(
            id="verify-backup",
            action=self.verify,
            inputs=lambda state: dict(state["load-backup"]),
            timeout_seconds=86_400,
        )
        project = WorkflowStep(
            id="project-knowledge-graph",
            action=self.project,
            inputs=lambda state: dict(state["verify-backup"]),
            timeout_seconds=60,
        )
        return WorkflowDefinition(
            id="backup.verify.workflow",
            version="1.0.0",
            capability_id=self.metadata.id,
            stages=(WorkflowStage("verify", StageMode.SEQUENTIAL, (load, verify, project)),),
            result=_verify_result,
        )


class BackupListCapability:
    input_model = BackupListInput
    output_model = BackupListResult
    metadata = CapabilityMetadata(
        id="backup.list",
        version="1.0.0",
        name="List Backups",
        description="Lista los backups conocidos por el store privado de ARES.",
        objective="Consultar backups existentes sin tocar sus datos de destino.",
        category=CapabilityCategory.BACKUP,
        operation=OperationClass.OBSERVE,
        mode=CapabilityMode.READ_ONLY,
        os_compatibility=_COMPATIBILITY,
        risk=RiskLevel.LOW,
        estimated_duration_seconds=2,
        permissions=(
            PermissionRequirement(
                id="backup.metadata.read", reason="Leer el índice local de backups."
            ),
        ),
        internal_actions=("backup.list-records",),
        postchecks=("typed-backup-list",),
        rollback=RollbackPolicy(
            supported=False, strategy="La consulta no modifica datos protegidos."
        ),
        emitted_events=(),
        metrics=("backup.list.count",),
        audit=AuditPolicy(record_inputs=False, record_outputs=False, event_names=()),
        keywords=("backup", "backups", "respaldo", "respaldos", "listar"),
    )

    def __init__(self, store: BackupStore) -> None:
        self.list_action = ListBackupsAction(store)

    def build_workflow(self, payload: BaseModel) -> WorkflowDefinition:
        request = BackupListInput.model_validate(payload)
        step = WorkflowStep(
            id="list-backups",
            action=self.list_action,
            inputs=lambda _: request.model_dump(mode="json"),
            timeout_seconds=10,
        )
        return WorkflowDefinition(
            id="backup.list.workflow",
            version="1.0.0",
            capability_id=self.metadata.id,
            stages=(WorkflowStage("list", StageMode.SEQUENTIAL, (step,)),),
            result=lambda state: dict(state["list-backups"]),
        )


class BackupPlugin:
    manifest = PluginManifest(
        id="ares.backup-core",
        version="1.0.0",
        core_api_version="2.0",
        name="ARES Backup Core",
        permissions=(
            "backup.plan.read",
            "backup.destination.write-via-broker",
            "backup.metadata.write-local",
            "backup.metadata.read",
            "backup.verify.read",
            "knowledge.graph.write",
        ),
        os_compatibility=_COMPATIBILITY,
        capabilities=("backup.create", "backup.verify", "backup.list"),
    )

    def __init__(
        self,
        store: BackupStore,
        tools: BackupFilesystemTools,
        executor: BackupExecutor,
    ) -> None:
        self._capabilities: tuple[Capability, ...] = (
            BackupCreateCapability(store, tools, executor),
            BackupVerifyCapability(store, executor),
            BackupListCapability(store),
        )

    def capabilities(self) -> tuple[Capability, ...]:
        return self._capabilities


def _create_result(state: StepOutputs) -> dict[str, Any]:
    output = state["project-knowledge-graph"]
    result = BackupCreateResult(
        backup=Backup.model_validate(output["backup"]),
        manifest=BackupManifest.model_validate(output["manifest"]),
        verification=BackupVerification.model_validate(output["verification"]),
        knowledge_graph=BackupGraphSummary.model_validate(output["knowledge_graph"]),
    )
    return result.model_dump(mode="json")


def _verify_result(state: StepOutputs) -> dict[str, Any]:
    output = state["project-knowledge-graph"]
    result = BackupVerifyResult(
        backup=Backup.model_validate(output["backup"]),
        verification=BackupVerification.model_validate(output["verification"]),
        knowledge_graph=BackupGraphSummary.model_validate(output["knowledge_graph"]),
    )
    return result.model_dump(mode="json")
