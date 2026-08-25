"""Catalog contracts for recovery operations awaiting dedicated root brokers."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

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
from ares.recovery import (
    AccountRecoveryRequest,
    CriticalRecoveryRequest,
    FileRecoveryRequest,
    RecoveryExecutionRequest,
    RecoveryExecutionResult,
    SystemCheckpointRecoveryRequest,
)
from ares.workflows import WorkflowDefinition

_COMPATIBILITY = OSCompatibility(
    families=("debian",),
    architectures=("amd64",),
    minimum_version="13",
    live_modes=("live", "persistent", "recovery"),
)
_STATE_PERMISSIONS = (
    PermissionRequirement(
        id="recovery.plan.read-local",
        reason="Read the exact server-generated recovery plan and its fingerprint.",
    ),
    PermissionRequirement(
        id="recovery.checkpoint.read-local",
        reason="Validate a verified ProtectionCheckpoint for the exact target.",
    ),
    PermissionRequirement(
        id="recovery.audit.write-local",
        reason="Persist authorization, execution, verification and rollback evidence.",
    ),
    PermissionRequirement(
        id="knowledge.graph.write",
        reason="Project verified before/after recovery state into the Knowledge Graph.",
    ),
)


@dataclass(frozen=True, slots=True)
class _RecoveryDefinition:
    capability_id: str
    name: str
    description: str
    objective: str
    category: CapabilityCategory
    risk: RiskLevel
    broker_permission: str
    broker_reason: str
    disabled_reason: str
    dependencies: tuple[str, ...]
    evidence: tuple[str, ...]
    rollback_supported: bool
    rollback_strategy: str
    input_model: type[BaseModel] = RecoveryExecutionRequest
    duration_seconds: float = 900


_DEFINITIONS = (
    _RecoveryDefinition(
        capability_id="boot.repair-grub",
        name="Repair GRUB",
        description=(
            "Reinstala el cargador adecuado y regenera una configuración GRUB verificable sobre "
            "un sistema instalado identificado exactamente por ARES."
        ),
        objective="Restaurar el arranque GRUB sin permitir comandos, paths ni dispositivos libres.",
        category=CapabilityCategory.RECOVERY,
        risk=RiskLevel.CRITICAL,
        broker_permission="recovery.boot.write-via-broker",
        broker_reason="Write bootloader sectors, ESP files and generated GRUB configuration.",
        disabled_reason=(
            "Bloqueada: falta el broker boot.* con namespace de montaje, selección BIOS/UEFI, "
            "escritura atómica, cleanup y verificación de arranque en VM."
        ),
        dependencies=("boot.diagnose",),
        evidence=("boot-diagnostic", "firmware-mode", "boot-disk-identity", "esp-identity"),
        rollback_supported=True,
        rollback_strategy="Restaurar sectores/archivos EFI y configuración desde el checkpoint.",
        input_model=CriticalRecoveryRequest,
    ),
    _RecoveryDefinition(
        capability_id="boot.repair-efi-entry",
        name="Repair EFI Entry",
        description=(
            "Recrea una entrada UEFI exacta y valida que apunte a un cargador firmado existente."
        ),
        objective="Recuperar una entrada NVRAM sin aceptar números, rutas EFI o argv del modelo.",
        category=CapabilityCategory.RECOVERY,
        risk=RiskLevel.HIGH,
        broker_permission="recovery.efi.write-via-broker",
        broker_reason="Create or update one plan-bound UEFI NVRAM entry.",
        disabled_reason=(
            "Bloqueada: no existe broker efivarfs/NVRAM con inventario previo, límites de "
            "escritura, fallback seguro y verificación del cargador firmado."
        ),
        dependencies=("boot.diagnose",),
        evidence=("boot-diagnostic", "uefi-runtime", "esp-identity", "signed-loader-evidence"),
        rollback_supported=True,
        rollback_strategy="Restaurar el inventario y BootOrder NVRAM capturados previamente.",
    ),
    _RecoveryDefinition(
        capability_id="boot.rebuild-initramfs",
        name="Rebuild initramfs",
        description=(
            "Reconstruye initramfs para kernels instalados seleccionados por un plan tipado."
        ),
        objective="Regenerar initramfs y verificar contenido/boot sin exponer un chroot genérico.",
        category=CapabilityCategory.RECOVERY,
        risk=RiskLevel.HIGH,
        broker_permission="recovery.initramfs.write-via-broker",
        broker_reason="Rebuild initramfs inside an exact installed-system namespace.",
        disabled_reason=(
            "Bloqueada: falta un broker initramfs con mount namespace cerrado, resolución de "
            "kernels, espacio mínimo, actualización atómica y cleanup comprobable."
        ),
        dependencies=("boot.diagnose", "packages.health-check"),
        evidence=("kernel-inventory", "package-health", "boot-space-budget"),
        rollback_supported=True,
        rollback_strategy="Restaurar imágenes initramfs y metadatos desde el checkpoint.",
    ),
    _RecoveryDefinition(
        capability_id="boot.repair-fstab",
        name="Repair fstab",
        description=(
            "Corrige únicamente referencias inválidas demostradas por inventario y evidencia."
        ),
        objective="Generar y aplicar un diff mínimo de fstab con validación antes y después.",
        category=CapabilityCategory.RECOVERY,
        risk=RiskLevel.HIGH,
        broker_permission="recovery.config.write-via-broker",
        broker_reason="Atomically replace one protected configuration file after validation.",
        disabled_reason=(
            "Bloqueada: falta el editor broker-gated de configuraciones con AST de fstab, "
            "resolución UUID/PARTUUID, escritura atómica y prueba de montaje sin efectos."
        ),
        dependencies=("boot.diagnose", "storage.disk-analysis"),
        evidence=("fstab-parse", "block-identity", "mount-dry-run"),
        rollback_supported=True,
        rollback_strategy="Restaurar byte por byte el fstab protegido y volver a validarlo.",
    ),
    _RecoveryDefinition(
        capability_id="kernel.rollback",
        name="Kernel Rollback",
        description=(
            "Vuelve al kernel instalado anterior que tenga evidencia de arranque funcional."
        ),
        objective="Cambiar el kernel preferido conservando al menos un fallback verificable.",
        category=CapabilityCategory.RECOVERY,
        risk=RiskLevel.HIGH,
        broker_permission="recovery.kernel.write-via-broker",
        broker_reason="Change installed kernel packages and boot selection as one transaction.",
        disabled_reason=(
            "Bloqueada: falta un broker de transacciones de kernel con historial de boot, "
            "retención obligatoria de fallback, initramfs/GRUB y prueba de reinicio."
        ),
        dependencies=("boot.diagnose", "packages.health-check"),
        evidence=("kernel-inventory", "last-known-good-boot", "package-health"),
        rollback_supported=True,
        rollback_strategy="Reactivar el kernel previo y restaurar configuración de arranque.",
        duration_seconds=1_800,
    ),
    _RecoveryDefinition(
        capability_id="kernel.reinstall",
        name="Kernel Reinstall",
        description="Reinstala el kernel seleccionado y sus módulos desde fuentes verificadas.",
        objective="Recuperar kernel/módulos corruptos mediante una transacción reproducible.",
        category=CapabilityCategory.RECOVERY,
        risk=RiskLevel.HIGH,
        broker_permission="recovery.kernel.write-via-broker",
        broker_reason="Reinstall exact kernel package versions and rebuild boot artifacts.",
        disabled_reason=(
            "Bloqueada: falta el broker de paquetes/kernel con repositorios confiables, pin de "
            "versión, presupuesto de espacio, initramfs/GRUB y rollback transaccional."
        ),
        dependencies=("boot.diagnose", "packages.health-check"),
        evidence=("kernel-package-integrity", "trusted-package-source", "boot-space-budget"),
        rollback_supported=True,
        rollback_strategy="Restaurar paquetes y artefactos de boot desde el checkpoint.",
        duration_seconds=1_800,
    ),
    _RecoveryDefinition(
        capability_id="packages.rollback",
        name="Package Rollback",
        description="Revierte un conjunto exacto de paquetes asociado a una actualización fallida.",
        objective="Deshacer una actualización problemática sin resolver paquetes arbitrarios.",
        category=CapabilityCategory.PACKAGES,
        risk=RiskLevel.HIGH,
        broker_permission="recovery.packages.write-via-broker",
        broker_reason="Execute a pinned, simulated and authorized package transaction.",
        disabled_reason=(
            "Bloqueada: falta el broker de paquetes con snapshot de estado, versiones fijadas, "
            "simulación, fuentes verificadas, scripts confinados y rollback comprobable."
        ),
        dependencies=("packages.health-check",),
        evidence=("package-health", "package-transaction-history", "candidate-version-integrity"),
        rollback_supported=True,
        rollback_strategy="Restaurar la selección/versiones y configuración protegida.",
        duration_seconds=1_800,
    ),
    _RecoveryDefinition(
        capability_id="services.restore-configuration",
        name="Restore Service Configuration",
        description="Restaura archivos protegidos de un servicio desde un checkpoint verificado.",
        objective="Recuperar configuración con diff exacto, validación y restart independiente.",
        category=CapabilityCategory.RECOVERY,
        risk=RiskLevel.HIGH,
        broker_permission="recovery.config.write-via-broker",
        broker_reason="Restore allowlisted service configuration files atomically.",
        disabled_reason=(
            "Bloqueada: falta un broker de configuración con manifest allowlist, validadores por "
            "servicio, reemplazo atómico y separación explícita del restart."
        ),
        dependencies=("services.failure-analysis",),
        evidence=("service-failure-analysis", "configuration-checkpoint", "config-validator"),
        rollback_supported=True,
        rollback_strategy="Restaurar la versión anterior protegida y repetir el validador.",
    ),
    _RecoveryDefinition(
        capability_id="network.restore-configuration",
        name="Restore Network Configuration",
        description="Restaura configuración de red local desde un checkpoint seleccionado.",
        objective="Recuperar conectividad conservando una vía local y un rollback temporizado.",
        category=CapabilityCategory.NETWORK,
        risk=RiskLevel.HIGH,
        broker_permission="recovery.network.write-via-broker",
        broker_reason="Apply one validated network checkpoint with timed rollback.",
        disabled_reason=(
            "Bloqueada: falta un broker de red con detector NetworkManager/networkd, canal local "
            "obligatorio, prueba de conectividad y rollback automático temporizado."
        ),
        dependencies=(),
        evidence=("network-backend", "configuration-checkpoint", "local-console-presence"),
        rollback_supported=True,
        rollback_strategy="Rollback automático temporizado si no se confirma conectividad local.",
    ),
    _RecoveryDefinition(
        capability_id="files.recover",
        name="Recover Files",
        description=(
            "Recupera archivos identificados desde una fuente preservada hacia otro dispositivo."
        ),
        objective="Maximizar recuperación sin escribir en el dispositivo fuente.",
        category=CapabilityCategory.RECOVERY,
        risk=RiskLevel.HIGH,
        broker_permission="recovery.files.write-via-broker",
        broker_reason="Read an immutable source and write a manifest to a distinct destination.",
        disabled_reason=(
            "Bloqueada: falta un broker forense de adquisición/recuperación que imponga fuente "
            "read-only, destino físico distinto, cuotas, hashes y reanudación segura."
        ),
        dependencies=("storage.disk-analysis",),
        evidence=("source-device-health", "read-only-source", "distinct-destination", "free-space"),
        rollback_supported=False,
        rollback_strategy="No modifica la fuente; los artefactos parciales se aíslan en destino.",
        input_model=FileRecoveryRequest,
        duration_seconds=14_400,
    ),
    _RecoveryDefinition(
        capability_id="system.rollback-checkpoint",
        name="System Checkpoint Rollback",
        description="Restaura solo elementos enumerados en un checkpoint de sistema verificado.",
        objective="Recuperar estado protegido sin presentar el checkpoint como imagen completa.",
        category=CapabilityCategory.RECOVERY,
        risk=RiskLevel.HIGH,
        broker_permission="recovery.system.write-via-broker",
        broker_reason="Restore protected elements from a versioned checkpoint provider.",
        disabled_reason=(
            "Bloqueada: el checkpoint actual de ARES es backup de archivos y no define un "
            "rollback de sistema transaccional, ordenado ni arrancable."
        ),
        dependencies=("storage.disk-analysis",),
        evidence=("system-checkpoint-manifest", "checkpoint-verification", "restore-order"),
        rollback_supported=False,
        rollback_strategy="Requiere checkpoint secundario; no hay rollback automático anidado.",
        input_model=SystemCheckpointRecoveryRequest,
        duration_seconds=7_200,
    ),
    _RecoveryDefinition(
        capability_id="user.account-recovery",
        name="Local Account Recovery",
        description="Recupera acceso a una cuenta local mediante un flujo físico y auditable.",
        objective="Restaurar acceso sin revelar secretos ni aceptar comandos de administración.",
        category=CapabilityCategory.SECURITY,
        risk=RiskLevel.CRITICAL,
        broker_permission="recovery.account.write-via-broker",
        broker_reason="Modify one local account through a physical-presence-gated broker.",
        disabled_reason=(
            "Bloqueada: falta un broker PAM/passwd con presencia física local, selección de cuenta "
            "no ambigua, política de credenciales, secreto fuera de logs y auditoría reforzada."
        ),
        dependencies=(),
        evidence=("local-account-inventory", "physical-presence", "credential-policy"),
        rollback_supported=True,
        rollback_strategy="Restaurar estado de cuenta y membresías desde el checkpoint protegido.",
        input_model=AccountRecoveryRequest,
    ),
)


class UnavailableRecoveryCapability:
    """Self-documenting contract that fails closed until its broker is installed."""

    output_model = RecoveryExecutionResult

    def __init__(self, definition: _RecoveryDefinition) -> None:
        self.input_model = definition.input_model
        self.metadata = _metadata(definition)

    def build_workflow(self, payload: BaseModel) -> WorkflowDefinition:
        del payload
        raise PermissionError(self.metadata.disabled_reason or "recovery capability disabled")


def _metadata(definition: _RecoveryDefinition) -> CapabilityMetadata:
    action_prefix = definition.capability_id
    permissions = (
        PermissionRequirement(
            id=definition.broker_permission,
            reason=definition.broker_reason,
        ),
        *_STATE_PERMISSIONS,
    )
    return CapabilityMetadata(
        id=definition.capability_id,
        version="0.1.0",
        name=definition.name,
        description=definition.description,
        objective=definition.objective,
        category=definition.category,
        operation=OperationClass.RECOVER,
        mode=CapabilityMode.MUTATING,
        os_compatibility=_COMPATIBILITY,
        risk=definition.risk,
        estimated_duration_seconds=definition.duration_seconds,
        permissions=permissions,
        dependencies=definition.dependencies,
        internal_actions=(
            "recovery.validate-exact-plan",
            "recovery.request-mutation-authorization",
            f"{action_prefix}.execute-via-broker",
            f"{action_prefix}.verify",
            "knowledge.project-recovery",
        ),
        postchecks=(
            "target-identity-revalidated",
            "protection-checkpoint-exact-resource-match",
            "authorization-one-use-plan-and-target-bound",
            "mutation-provider-specific-verification",
            "before-after-evidence-persisted",
        ),
        rollback=RollbackPolicy(
            supported=definition.rollback_supported,
            strategy=definition.rollback_strategy,
        ),
        requires_authorization=True,
        requires_protection_checkpoint=True,
        supports_dry_run=True,
        supports_verification=True,
        supports_rollback=definition.rollback_supported,
        required_evidence=(
            *definition.evidence,
            "exact-target-fingerprint",
            "verified-protection-checkpoint",
            "plan-fingerprint",
        ),
        emitted_events=(
            "recovery.plan.validated",
            "recovery.authorization.requested",
            "recovery.started",
            "recovery.verification.completed",
            "recovery.completed",
            "recovery.failed",
            "recovery.rolled-back",
            "knowledge.graph.updated",
        ),
        metrics=("recovery.duration", "recovery.verification", "recovery.rollback"),
        audit=AuditPolicy(
            record_inputs=False,
            record_outputs=True,
            event_names=(
                "recovery.authorization.requested",
                "recovery.started",
                "recovery.verification.completed",
                "recovery.completed",
                "recovery.failed",
                "recovery.rolled-back",
            ),
        ),
        keywords=tuple(
            dict.fromkeys(
                (
                    *definition.capability_id.replace(".", " ").replace("-", " ").split(),
                    *definition.name.casefold().split(),
                    "reparar",
                    "recuperar",
                    "restaurar",
                    "rollback",
                )
            )
        ),
        enabled=False,
        disabled_reason=definition.disabled_reason,
    )


class RecoveryCorePlugin:
    """Official fail-closed catalog provider for the recovery roadmap."""

    def __init__(self) -> None:
        self._capabilities = tuple(
            UnavailableRecoveryCapability(definition) for definition in _DEFINITIONS
        )
        permissions = tuple(
            dict.fromkeys(
                permission.id
                for capability in self._capabilities
                for permission in capability.metadata.permissions
            )
        )
        self.manifest = PluginManifest(
            id="ares.recovery-core",
            version="0.1.0",
            core_api_version="2.0",
            name="ARES Recovery Core Contracts",
            permissions=permissions,
            os_compatibility=_COMPATIBILITY,
            capabilities=tuple(item.metadata.id for item in self._capabilities),
        )

    def capabilities(self) -> tuple[Capability, ...]:
        return self._capabilities
