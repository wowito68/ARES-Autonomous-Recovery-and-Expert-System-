"""Evidence-first Boot Recovery Engine."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from ares.boot.executor import BootExecutorError, BootRepairExecutor
from ares.boot.integrity import boot_plan_fingerprint, boot_plan_integrity_valid
from ares.boot.models import (
    BootAuthorizationGrant,
    BootConfiguration,
    BootDependency,
    BootDiagnoseInput,
    BootDiagnosticResult,
    BootEntry,
    BootEnvironment,
    BootEvidence,
    BootIssue,
    BootIssueCode,
    BootIssueSeverity,
    Bootloader,
    BootloaderKind,
    BootOperationKind,
    BootPartition,
    BootRepairExecution,
    BootRepairOperation,
    BootRepairPlan,
    BootRepairPlanInput,
    BootRepairStatus,
    BootTargetOS,
    BootVerification,
    BootVerificationStatus,
    BootVerificationStrategy,
    DistributionFamily,
    FirmwareMode,
)
from ares.boot.store import BootRecoveryStore
from ares.protection import ProtectionCheckpointStatus, ProtectionCheckpointStore
from ares.storage_operations import StorageOperationEngine, StorageOperationEngineError
from ares.storage_operations.models import (
    BootImpactAssessment,
    BootImpactLevel,
    PartitionRole,
    StorageLayout,
)
from ares.tools.boot import BootRepairToolSuite, BootToolError

ChallengeCallback = Callable[[str], Awaitable[None]]
StageCallback = Callable[[str, dict[str, object]], Awaitable[None]]


class BootRecoveryEngineError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class BootRecoveryEngine:
    def __init__(
        self,
        *,
        tools: BootRepairToolSuite,
        storage: StorageOperationEngine,
        store: BootRecoveryStore,
        checkpoints: ProtectionCheckpointStore,
        executor: BootRepairExecutor,
    ) -> None:
        self.tools = tools
        self.storage = storage
        self.store = store
        self.checkpoints = checkpoints
        self.executor = executor
        self._grants: dict[str, BootAuthorizationGrant] = {}

    async def diagnose(
        self,
        request: BootDiagnoseInput,
        *,
        session_id: str,
    ) -> BootDiagnosticResult:
        del session_id
        firmware, firmware_evidence = self.tools.firmware.inspect()
        layout: StorageLayout | None = None
        if request.target_disk:
            try:
                layout = await self.storage.inspect(request.target_disk)
            except StorageOperationEngineError as exc:
                raise BootRecoveryEngineError("BOOT_STORAGE_INSPECTION_FAILED") from exc
        entries: tuple[BootEntry, ...] = ()
        efi_evidence: tuple[BootEvidence, ...] = ()
        if firmware.mode is FirmwareMode.UEFI:
            try:
                entries, efi_evidence = await self.tools.efi.inspect()
            except BootToolError as exc:
                raise BootRecoveryEngineError(exc.code) from exc
        roots = self._candidate_roots(request, layout)
        operating_systems: list[BootTargetOS] = []
        selected_loader = Bootloader(kind=BootloaderKind.UNKNOWN)
        selected_config = BootConfiguration()
        selected_evidence: tuple[BootEvidence, ...] = ()
        selected_root: Path | None = None
        esp = _esp_partition(layout)
        esp_path = Path(esp.mount_point) if esp and esp.mount_point else None
        for root in roots:
            try:
                loader, config, target_os, evidence = self.tools.bootloader.inspect(root, esp_path)
            except BootToolError:
                continue
            operating_systems.append(target_os)
            if selected_root is None:
                selected_root = root
                selected_loader = loader
                selected_config = config
                selected_evidence = evidence
        if selected_root is not None:
            selected_config, fstab_evidence = self.tools.fstab.inspect(selected_config, layout)
        else:
            fstab_evidence = ()
        root_partition = _root_partition(
            layout, operating_systems[0] if operating_systems else None
        )
        boot_partition = _boot_partition(layout)
        all_evidence = (*firmware_evidence, *efi_evidence, *selected_evidence, *fstab_evidence)
        issues = _issues(
            firmware=firmware.mode,
            bootloader=selected_loader,
            configuration=selected_config,
            operating_systems=tuple(operating_systems),
            entries=entries,
            esp=esp,
            evidence=all_evidence,
        )
        environment = BootEnvironment(
            firmware=firmware.model_copy(update={"boot_entries_available": bool(entries)}),
            bootloader=selected_loader,
            target_disk=layout.disk.identity if layout else None,
            boot_disk_id=layout.disk.id if layout else None,
            esp=esp,
            root_partition=root_partition,
            boot_partition=boot_partition,
            operating_systems=tuple(operating_systems),
            boot_entries=entries,
            configuration=selected_config,
            dependencies=_dependencies(
                firmware.mode,
                selected_loader,
                selected_config,
                operating_systems[0] if operating_systems else None,
                esp,
                all_evidence,
            ),
            evidence=all_evidence,
        )
        result = BootDiagnosticResult(
            environment=environment,
            issues=issues,
            evidence=all_evidence,
            confidence=_diagnostic_confidence(issues, all_evidence),
            severity=_max_severity(issues),
            recommended_action=_recommended_action(issues, operating_systems),
        )
        await self.store.put_diagnostic(result)
        return result

    async def plan(
        self,
        request: BootRepairPlanInput,
        *,
        session_id: str,
    ) -> BootRepairPlan:
        diagnostic = await self.store.get_diagnostic(request.diagnostic_id)
        if diagnostic is None:
            raise BootRecoveryEngineError("BOOT_DIAGNOSTIC_NOT_FOUND")
        environment = diagnostic.environment
        if environment.target_disk is None:
            raise BootRecoveryEngineError("BOOT_TARGET_DISK_REQUIRED")
        target_os = _select_os(environment.operating_systems, request.target_os_id)
        operations = _repair_operations(diagnostic)
        limitations: list[str] = []
        repairable_issue = any(item.repairable_automatically for item in diagnostic.issues)
        if target_os.family is not DistributionFamily.DEBIAN:
            limitations.append("grub_repair_supported_only_for_debian_family")
        if environment.bootloader.kind is BootloaderKind.WINDOWS_BOOT_MANAGER:
            limitations.append("windows_boot_manager_repair_not_enabled")
        if environment.bootloader.kind is BootloaderKind.SYSTEMD_BOOT:
            limitations.append("systemd_boot_repair_not_enabled")
        if environment.firmware.mode is FirmwareMode.UEFI and environment.esp is None:
            limitations.append("uefi_repair_requires_detected_esp")
        if not repairable_issue:
            limitations.append("no_automatic_boot_repair_issue_detected")
        root_path = target_os.root_path
        if root_path is None:
            limitations.append("selected_linux_root_is_not_accessible")
        executable = not limitations
        expected = environment.configuration.model_copy(
            update={
                "grub_config_path": environment.configuration.grub_config_path
                or (str(Path(root_path) / "boot/grub/grub.cfg") if root_path else None)
            }
        )
        affected = tuple(item.id for item in environment.dependencies if item.critical)
        boot_impact = BootImpactAssessment(
            level=BootImpactLevel.LIKELY,
            affected_dependencies=affected,
            reasons=("bootloader_configuration_will_change",),
            recovery_strategy_available=True,
            executable=executable,
        )
        draft = BootRepairPlan(
            session_id=session_id,
            diagnostic_id=diagnostic.id,
            target_os=target_os,
            target_disk=environment.target_disk,
            target_esp=environment.esp,
            bootloader=environment.bootloader,
            current_configuration=environment.configuration,
            expected_configuration=expected,
            issues=diagnostic.issues,
            operations=operations,
            dependencies=environment.dependencies,
            boot_impact=boot_impact,
            verification_strategy=BootVerificationStrategy(
                verify_efi_entry=environment.firmware.mode is FirmwareMode.UEFI,
                verify_esp=environment.firmware.mode is FirmwareMode.UEFI,
            ),
            executable=executable,
            limitations=tuple(limitations),
            fingerprint_sha256="0" * 64,
        )
        plan = draft.model_copy(update={"fingerprint_sha256": boot_plan_fingerprint(draft)})
        execution = BootRepairExecution(
            id=plan.repair_id,
            repair_id=plan.repair_id,
            plan_id=plan.id,
            session_id=session_id,
            status=BootRepairStatus.PLANNED,
            last_known_stage="boot-repair-planned",
        )
        await self.store.put_plan(plan)
        await self.store.put_execution(execution)
        return plan

    async def protect(self, plan_id: str, *, session_id: str) -> BootRepairPlan:
        plan = await self._require_plan(plan_id, session_id)
        if not plan.executable or not boot_plan_integrity_valid(plan):
            raise BootRecoveryEngineError("BOOT_REPAIR_PLAN_NOT_EXECUTABLE")
        try:
            bundle = await self.executor.create_checkpoint(plan)
        except BootExecutorError as exc:
            raise BootRecoveryEngineError(exc.code) from exc
        checkpoint = bundle.checkpoint
        if (
            checkpoint.status is not ProtectionCheckpointStatus.READY
            or checkpoint.session_id != plan.session_id
            or checkpoint.verification_id is None
            or plan.target_disk.resource_id not in checkpoint.protected_resources
            or checkpoint.resource_fingerprints.get(plan.target_disk.resource_id)
            != plan.target_disk.fingerprint_sha256
        ):
            raise BootRecoveryEngineError("BOOT_PROTECTION_CHECKPOINT_INVALID")
        await self.store.put_checkpoint(bundle.artifact)
        await self.checkpoints.put(checkpoint)
        draft = plan.model_copy(
            update={"protection_checkpoint": checkpoint, "fingerprint_sha256": "0" * 64}
        )
        protected = draft.model_copy(update={"fingerprint_sha256": boot_plan_fingerprint(draft)})
        await self.store.put_plan(protected)
        execution = await self.store.get_execution(plan.repair_id)
        if execution is None:
            raise BootRecoveryEngineError("BOOT_REPAIR_NOT_FOUND")
        await self.store.put_execution(
            execution.model_copy(
                update={
                    "status": BootRepairStatus.PROTECTED,
                    "checkpoint_id": checkpoint.id,
                    "last_known_stage": "boot-protection-checkpoint-created",
                }
            )
        )
        return protected

    async def request_authorization(
        self,
        plan_id: str,
        *,
        session_id: str,
        on_challenge: ChallengeCallback,
    ) -> BootAuthorizationGrant:
        plan = await self._require_plan(plan_id, session_id)
        execution = await self.store.get_execution(plan.repair_id)
        if execution is None or execution.status is not BootRepairStatus.PROTECTED:
            raise BootRecoveryEngineError("BOOT_REPAIR_NOT_PROTECTED")
        await self._require_checkpoint(plan)
        try:
            grant = await self.executor.request_authorization(plan, on_challenge=on_challenge)
        except BootExecutorError as exc:
            raise BootRecoveryEngineError(exc.code) from exc
        self._grants[plan.repair_id] = grant
        await self.store.put_execution(
            execution.model_copy(
                update={
                    "status": BootRepairStatus.AUTHORIZED,
                    "authorization_challenge_id": grant.challenge_id,
                    "last_known_stage": "boot-authorization-granted",
                }
            )
        )
        return grant

    async def execute_authorized(
        self,
        repair_id: str,
        *,
        session_id: str,
        on_stage: StageCallback,
    ) -> BootVerification:
        record = await self.store.get_record(repair_id)
        if record is None:
            raise BootRecoveryEngineError("BOOT_REPAIR_NOT_FOUND")
        if record.execution.status is not BootRepairStatus.AUTHORIZED:
            raise BootRecoveryEngineError("BOOT_REPAIR_NOT_AUTHORIZED")
        plan = record.plan
        if plan.session_id != session_id:
            raise BootRecoveryEngineError("BOOT_REPAIR_SESSION_MISMATCH")
        grant = self._grants.pop(repair_id, None)
        if grant is None:
            raise BootRecoveryEngineError("BOOT_AUTHORIZATION_GRANT_UNAVAILABLE")
        if not boot_plan_integrity_valid(plan):
            raise BootRecoveryEngineError("BOOT_REPAIR_PLAN_TAMPERED")
        await self._require_checkpoint(plan)
        await self.store.put_execution(
            record.execution.model_copy(
                update={
                    "status": BootRepairStatus.EXECUTING,
                    "started_at": datetime.now(UTC),
                    "last_known_stage": "boot-repair-started",
                    "error_code": None,
                    "reconciliation_required": False,
                }
            )
        )

        async def stage(name: str, payload: dict[str, object]) -> None:
            current = await self.store.get_execution(repair_id)
            if current is not None:
                await self.store.put_execution(
                    current.model_copy(update={"last_known_stage": name})
                )
            await on_stage(name, payload)

        try:
            outcome = await self.executor.execute(plan, grant, on_stage=stage)
            current = await self.store.get_execution(repair_id)
            if current is None:
                raise BootRecoveryEngineError("BOOT_REPAIR_NOT_FOUND")
            await self.store.put_execution(
                current.model_copy(
                    update={
                        "status": BootRepairStatus.VERIFYING,
                        "changed_operations": outcome.changed_operations,
                        "last_known_stage": "boot-verification-started",
                    }
                )
            )
            verification = await self.executor.verify(plan)
        except BootExecutorError as exc:
            await self.mark_failed_or_unknown(repair_id, exc.code)
            raise BootRecoveryEngineError(exc.code) from exc
        await self.store.put_verification(verification)
        current = await self.store.get_execution(repair_id)
        if current is None:
            raise BootRecoveryEngineError("BOOT_REPAIR_NOT_FOUND")
        successful = verification.status in {
            BootVerificationStatus.VERIFIED,
            BootVerificationStatus.PARTIAL,
        }
        await self.store.put_execution(
            current.model_copy(
                update={
                    "status": (
                        BootRepairStatus.COMPLETED if successful else BootRepairStatus.REPAIR_FAILED
                    ),
                    "finished_at": datetime.now(UTC),
                    "last_known_stage": "boot-verification-completed",
                    "error_code": None if successful else "BOOT_VERIFICATION_FAILED",
                }
            )
        )
        return verification

    async def reconcile_unknown(self, repair_id: str, *, session_id: str) -> BootVerification:
        record = await self.store.get_record(repair_id)
        if record is None:
            raise BootRecoveryEngineError("BOOT_REPAIR_NOT_FOUND")
        if record.plan.session_id != session_id:
            raise BootRecoveryEngineError("BOOT_REPAIR_SESSION_MISMATCH")
        if record.execution.status is not BootRepairStatus.UNKNOWN:
            raise BootRecoveryEngineError("BOOT_REPAIR_NOT_UNKNOWN")
        try:
            verification = await self.executor.verify(record.plan)
        except BootExecutorError as exc:
            raise BootRecoveryEngineError(exc.code) from exc
        await self.store.put_verification(verification)
        if verification.status in {
            BootVerificationStatus.VERIFIED,
            BootVerificationStatus.PARTIAL,
        }:
            await self.store.put_execution(
                record.execution.model_copy(
                    update={
                        "status": BootRepairStatus.COMPLETED,
                        "reconciliation_required": False,
                        "finished_at": datetime.now(UTC),
                        "last_known_stage": "boot-reconciled-from-evidence",
                    }
                )
            )
        return verification

    async def mark_aborted(self, repair_id: str, *, code: str) -> None:
        execution = await self.store.get_execution(repair_id)
        if execution is None:
            return
        await self.store.put_execution(
            execution.model_copy(
                update={
                    "status": BootRepairStatus.ABORTED,
                    "error_code": code,
                    "finished_at": datetime.now(UTC),
                    "last_known_stage": "boot-repair-aborted",
                }
            )
        )

    async def mark_failed_or_unknown(self, repair_id: str, code: str) -> None:
        execution = await self.store.get_execution(repair_id)
        if execution is None:
            return
        uncertain = code in {
            "BOOT_BROKER_DISCONNECTED",
            "BOOT_BROKER_TIMEOUT",
            "BOOT_RECONCILIATION_REQUIRED",
        }
        await self.store.put_execution(
            execution.model_copy(
                update={
                    "status": (
                        BootRepairStatus.UNKNOWN if uncertain else BootRepairStatus.REPAIR_FAILED
                    ),
                    "reconciliation_required": uncertain,
                    "error_code": code,
                    "finished_at": None if uncertain else datetime.now(UTC),
                    "last_known_stage": (
                        "boot-repair-unknown" if uncertain else "boot-repair-failed"
                    ),
                }
            )
        )

    async def _require_plan(self, plan_id: str, session_id: str) -> BootRepairPlan:
        plan = await self.store.get_plan(plan_id)
        if plan is None:
            raise BootRecoveryEngineError("BOOT_REPAIR_PLAN_NOT_FOUND")
        if plan.session_id != session_id:
            raise BootRecoveryEngineError("BOOT_REPAIR_SESSION_MISMATCH")
        if plan.expires_at <= datetime.now(UTC):
            raise BootRecoveryEngineError("BOOT_REPAIR_PLAN_EXPIRED")
        return plan

    async def _require_checkpoint(self, plan: BootRepairPlan) -> None:
        checkpoint = plan.protection_checkpoint
        if checkpoint is None or checkpoint.status is not ProtectionCheckpointStatus.READY:
            raise BootRecoveryEngineError("BOOT_PROTECTION_CHECKPOINT_INVALID")
        durable = await self.checkpoints.get(checkpoint.id)
        artifact = await self.store.get_checkpoint(checkpoint.id)
        if durable != checkpoint or artifact is None:
            raise BootRecoveryEngineError("BOOT_PROTECTION_CHECKPOINT_INVALID")

    @staticmethod
    def _candidate_roots(
        request: BootDiagnoseInput, layout: StorageLayout | None
    ) -> tuple[Path, ...]:
        if request.root_path:
            return (Path(request.root_path),)
        if layout is None:
            return ()
        roots = [
            Path(item.mount_point)
            for item in layout.operating_systems
            if item.mount_point and Path(item.mount_point).is_dir()
        ]
        return tuple(dict.fromkeys(roots))


def _select_os(values: tuple[BootTargetOS, ...], target_id: str | None) -> BootTargetOS:
    if not values:
        raise BootRecoveryEngineError("BOOT_LINUX_INSTALLATION_NOT_FOUND")
    if target_id is None:
        if len(values) != 1:
            raise BootRecoveryEngineError("BOOT_TARGET_OS_REQUIRED")
        return values[0]
    for item in values:
        if item.id == target_id:
            return item
    raise BootRecoveryEngineError("BOOT_TARGET_OS_NOT_FOUND")


def _issues(
    *,
    firmware: FirmwareMode,
    bootloader: Bootloader,
    configuration: BootConfiguration,
    operating_systems: tuple[BootTargetOS, ...],
    entries: tuple[BootEntry, ...],
    esp: BootPartition | None,
    evidence: tuple[BootEvidence, ...],
) -> tuple[BootIssue, ...]:
    ids = tuple(item.id for item in evidence)
    if not ids:
        return ()
    issues: list[BootIssue] = []
    if len(operating_systems) > 1:
        issues.append(
            BootIssue(
                code=BootIssueCode.MULTIPLE_LINUX_INSTALLATIONS,
                severity=BootIssueSeverity.HIGH,
                summary=(
                    "Multiple Linux installations were detected; a repair target must be selected."
                ),
                evidence_ids=ids,
                confidence=0.95,
                recommended_action="Select the Linux installation that should be recovered.",
            )
        )
    if operating_systems and bootloader.kind is BootloaderKind.UNKNOWN:
        issues.append(
            BootIssue(
                code=BootIssueCode.GRUB_MISSING,
                severity=BootIssueSeverity.HIGH,
                summary="Linux and kernel evidence exists but GRUB artifacts were not detected.",
                evidence_ids=ids,
                confidence=0.88,
                recommended_action="Plan a GRUB reinstall after protecting current boot state.",
                repairable_automatically=True,
            )
        )
    if bootloader.kind is BootloaderKind.WINDOWS_BOOT_MANAGER:
        issues.append(
            BootIssue(
                code=BootIssueCode.WINDOWS_BOOT_REPAIR_UNSUPPORTED,
                severity=BootIssueSeverity.MEDIUM,
                summary=(
                    "Windows Boot Manager was detected; automatic Windows boot repair is disabled."
                ),
                evidence_ids=ids,
                confidence=0.95,
                recommended_action="Use Windows recovery tooling or perform a manual review.",
            )
        )
    if bootloader.kind is BootloaderKind.SYSTEMD_BOOT:
        issues.append(
            BootIssue(
                code=BootIssueCode.UNSUPPORTED_BOOTLOADER,
                severity=BootIssueSeverity.MEDIUM,
                summary=(
                    "systemd-boot was detected; automatic repair is not enabled in this version."
                ),
                evidence_ids=ids,
                confidence=0.95,
                recommended_action="Perform manual recovery or use a future systemd-boot adapter.",
            )
        )
    if operating_systems and not configuration.kernels:
        issues.append(
            BootIssue(
                code=BootIssueCode.KERNEL_MISSING,
                severity=BootIssueSeverity.CRITICAL,
                summary="The selected Linux installation has no detected kernel image under /boot.",
                evidence_ids=ids,
                confidence=0.93,
                recommended_action=(
                    "Restore or reinstall a kernel before relying on bootloader repair."
                ),
            )
        )
    if configuration.kernels and not configuration.initramfs:
        issues.append(
            BootIssue(
                code=BootIssueCode.INITRAMFS_MISSING,
                severity=BootIssueSeverity.HIGH,
                summary="A kernel is present but no matching initramfs artifact was detected.",
                evidence_ids=ids,
                confidence=0.9,
                recommended_action="Regenerate initramfs as an explicit repair operation.",
                repairable_automatically=True,
            )
        )
    if (
        operating_systems
        and configuration.grub_config_path is None
        and bootloader.kind is BootloaderKind.GRUB
    ):
        issues.append(
            BootIssue(
                code=BootIssueCode.GRUB_CONFIG_MISSING,
                severity=BootIssueSeverity.HIGH,
                summary="GRUB loader artifacts exist but grub.cfg is missing.",
                evidence_ids=ids,
                confidence=0.9,
                recommended_action="Regenerate GRUB configuration after a protection checkpoint.",
                repairable_automatically=True,
            )
        )
    if configuration.invalid_fstab_references:
        issues.append(
            BootIssue(
                code=BootIssueCode.FSTAB_REFERENCE_INVALID,
                severity=BootIssueSeverity.HIGH,
                summary="fstab contains invalid, missing, or duplicate storage references.",
                evidence_ids=ids,
                confidence=0.93,
                recommended_action=(
                    "Manual fstab review is required; ARES will not edit fstab automatically."
                ),
            )
        )
    if firmware is FirmwareMode.UEFI and esp is None:
        issues.append(
            BootIssue(
                code=BootIssueCode.ESP_NOT_MOUNTED,
                severity=BootIssueSeverity.HIGH,
                summary=(
                    "The system is running in UEFI mode but no EFI System Partition was identified."
                ),
                evidence_ids=ids,
                confidence=0.86,
                recommended_action="Identify and mount the correct ESP before GRUB repair.",
            )
        )
    if (
        firmware is FirmwareMode.UEFI
        and esp is not None
        and any("grub" in item.lower() for item in bootloader.efi_loader_paths)
        and not any(
            "grub" in item.label.lower() or "debian" in item.label.lower() for item in entries
        )
    ):
        issues.append(
            BootIssue(
                code=BootIssueCode.EFI_ENTRY_MISSING,
                severity=BootIssueSeverity.HIGH,
                summary=(
                    "GRUB EFI loader files exist, but no matching firmware boot entry was found."
                ),
                evidence_ids=ids,
                confidence=0.94,
                recommended_action="Create the GRUB EFI entry and verify the loader path.",
                repairable_automatically=True,
            )
        )
    return tuple(issues)


def _repair_operations(diagnostic: BootDiagnosticResult) -> tuple[BootRepairOperation, ...]:
    codes = {item.code for item in diagnostic.issues}
    operations: list[BootRepairOperation] = [
        BootRepairOperation(
            id="boot.checkpoint",
            kind=BootOperationKind.CREATE_CHECKPOINT,
            description=(
                "Create a verified snapshot of relevant EFI, GRUB and boot configuration state."
            ),
            mutates_system=False,
        ),
        BootRepairOperation(
            id="boot.environment",
            kind=BootOperationKind.PREPARE_REPAIR_ENVIRONMENT,
            description="Prepare an explicit temporary repair environment.",
            mutates_system=False,
        ),
    ]
    if codes & {
        BootIssueCode.GRUB_MISSING,
        BootIssueCode.GRUB_CONFIG_MISSING,
        BootIssueCode.GRUB_CONFIG_CORRUPT,
        BootIssueCode.EFI_ENTRY_MISSING,
    }:
        operations.extend(
            (
                BootRepairOperation(
                    id="boot.grub.install",
                    kind=BootOperationKind.INSTALL_GRUB,
                    description=(
                        "Install or reinstall GRUB for the approved target OS and firmware mode."
                    ),
                    mutates_system=True,
                ),
                BootRepairOperation(
                    id="boot.grub.config",
                    kind=BootOperationKind.REGENERATE_GRUB_CONFIG,
                    description="Regenerate GRUB configuration in the approved target root.",
                    mutates_system=True,
                ),
            )
        )
    if BootIssueCode.INITRAMFS_MISSING in codes:
        operations.append(
            BootRepairOperation(
                id="boot.initramfs.regenerate",
                kind=BootOperationKind.REGENERATE_INITRAMFS,
                description="Regenerate initramfs explicitly for installed kernels.",
                mutates_system=True,
            )
        )
    if BootIssueCode.EFI_ENTRY_MISSING in codes:
        operations.append(
            BootRepairOperation(
                id="boot.efi.entry",
                kind=BootOperationKind.UPDATE_EFI_ENTRY,
                description="Create the exact GRUB EFI firmware entry for the approved ESP.",
                mutates_system=True,
            )
        )
    operations.extend(
        (
            BootRepairOperation(
                id="boot.verify",
                kind=BootOperationKind.VERIFY_BOOT_CHAIN,
                description=(
                    "Verify firmware, ESP, bootloader, config, kernel, initramfs and root chain."
                ),
                mutates_system=False,
            ),
            BootRepairOperation(
                id="boot.cleanup",
                kind=BootOperationKind.CLEANUP_REPAIR_ENVIRONMENT,
                description="Remove all temporary repair mounts and runtime state.",
                mutates_system=False,
            ),
        )
    )
    return tuple(operations)


def _esp_partition(layout: StorageLayout | None) -> BootPartition | None:
    if layout is None:
        return None
    fs_by_id = {item.id: item for item in layout.filesystems}
    mount_by_id = {item.id: item for item in layout.mount_points}
    for partition in layout.partition_table.partitions:
        if partition.role is not PartitionRole.EFI:
            continue
        fs = fs_by_id.get(partition.filesystem_id or "")
        mount = next(
            (mount_by_id[item] for item in partition.mount_point_ids if item in mount_by_id), None
        )
        return BootPartition(
            resource_id=partition.id,
            device_path=partition.path,
            partition_number=partition.number,
            role="esp",
            filesystem_type=fs.filesystem_type if fs else None,
            uuid=fs.uuid if fs else None,
            partuuid=partition.partuuid,
            mount_point=mount.path if mount else None,
        )
    return None


def _root_partition(
    layout: StorageLayout | None, target_os: BootTargetOS | None
) -> BootPartition | None:
    if layout is None or target_os is None:
        return None
    mounts = {item.id: item for item in layout.mount_points}
    fs_by_id = {item.id: item for item in layout.filesystems}
    for partition in layout.partition_table.partitions:
        for mount_id in partition.mount_point_ids:
            mount = mounts.get(mount_id)
            if mount and mount.path == target_os.root_path:
                fs = fs_by_id.get(partition.filesystem_id or "")
                return BootPartition(
                    resource_id=partition.id,
                    device_path=partition.path,
                    partition_number=partition.number,
                    role="root",
                    filesystem_type=fs.filesystem_type if fs else None,
                    uuid=fs.uuid if fs else None,
                    partuuid=partition.partuuid,
                    mount_point=mount.path,
                )
    return None


def _boot_partition(layout: StorageLayout | None) -> BootPartition | None:
    if layout is None:
        return None
    mounts = {item.id: item for item in layout.mount_points}
    fs_by_id = {item.id: item for item in layout.filesystems}
    for partition in layout.partition_table.partitions:
        for mount_id in partition.mount_point_ids:
            mount = mounts.get(mount_id)
            if mount and mount.path == "/boot":
                fs = fs_by_id.get(partition.filesystem_id or "")
                return BootPartition(
                    resource_id=partition.id,
                    device_path=partition.path,
                    partition_number=partition.number,
                    role="boot",
                    filesystem_type=fs.filesystem_type if fs else None,
                    uuid=fs.uuid if fs else None,
                    partuuid=partition.partuuid,
                    mount_point=mount.path,
                )
    return None


def _dependencies(
    firmware: FirmwareMode,
    loader: Bootloader,
    config: BootConfiguration,
    target_os: BootTargetOS | None,
    esp: BootPartition | None,
    evidence: tuple[BootEvidence, ...],
) -> tuple[BootDependency, ...]:
    evidence_ids = tuple(item.id for item in evidence)
    dependencies: list[BootDependency] = []
    firmware_id = f"firmware:{firmware.value.lower()}"
    loader_id = f"bootloader:{loader.kind.value.lower()}"
    if esp is not None:
        dependencies.extend(
            (
                BootDependency(
                    id="boot-dependency:firmware-esp",
                    source_id=firmware_id,
                    relation="depends_on",
                    target_id=esp.resource_id,
                    evidence_ids=evidence_ids,
                ),
                BootDependency(
                    id="boot-dependency:loader-esp",
                    source_id=loader_id,
                    relation="depends_on",
                    target_id=esp.resource_id,
                    evidence_ids=evidence_ids,
                ),
            )
        )
    if config.grub_config_path:
        dependencies.append(
            BootDependency(
                id="boot-dependency:loader-config",
                source_id=loader_id,
                relation="configured_by",
                target_id="boot-config:grub",
                evidence_ids=evidence_ids,
            )
        )
    if target_os is not None:
        for index, kernel in enumerate(config.kernels):
            kernel_id = f"kernel:{index}:{Path(kernel).name}"
            dependencies.append(
                BootDependency(
                    id=f"boot-dependency:loader-kernel-{index}",
                    source_id=loader_id,
                    relation="loads",
                    target_id=kernel_id,
                    evidence_ids=evidence_ids,
                )
            )
            if index < len(config.initramfs):
                initramfs_id = f"initramfs:{index}:{Path(config.initramfs[index]).name}"
                dependencies.extend(
                    (
                        BootDependency(
                            id=f"boot-dependency:loader-initramfs-{index}",
                            source_id=loader_id,
                            relation="loads",
                            target_id=initramfs_id,
                            evidence_ids=evidence_ids,
                        ),
                        BootDependency(
                            id=f"boot-dependency:kernel-root-{index}",
                            source_id=kernel_id,
                            relation="boots",
                            target_id=target_os.id,
                            evidence_ids=evidence_ids,
                        ),
                    )
                )
    return tuple(dependencies)


def _max_severity(issues: tuple[BootIssue, ...]) -> BootIssueSeverity:
    order = {
        BootIssueSeverity.INFO: 0,
        BootIssueSeverity.LOW: 1,
        BootIssueSeverity.MEDIUM: 2,
        BootIssueSeverity.HIGH: 3,
        BootIssueSeverity.CRITICAL: 4,
    }
    if not issues:
        return BootIssueSeverity.INFO
    return max(issues, key=lambda item: order[item.severity]).severity


def _diagnostic_confidence(
    issues: tuple[BootIssue, ...], evidence: tuple[BootEvidence, ...]
) -> float:
    values = [item.confidence for item in issues] or [item.confidence for item in evidence]
    return round(sum(values) / len(values), 4) if values else 0.0


def _recommended_action(
    issues: tuple[BootIssue, ...], operating_systems: list[BootTargetOS]
) -> str:
    if len(operating_systems) > 1:
        return "Select which Linux installation should be recovered before creating a repair plan."
    if any(item.repairable_automatically for item in issues):
        return (
            "Create a minimal BootRepairPlan, protect current boot state, "
            "then request authorization."
        )
    if issues:
        return issues[0].recommended_action
    return "No repair should be attempted without additional evidence of a boot-chain fault."
