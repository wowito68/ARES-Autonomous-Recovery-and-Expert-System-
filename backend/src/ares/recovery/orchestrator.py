"""Evidence-driven coordinator for compound Linux recovery cases."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ares.audit import AuditLedger, AuditLedgerError
from ares.boot.service import BootDiagnoseRequest, BootRecoveryService, BootRecoveryServiceError
from ares.capabilities import CapabilityManager
from ares.capabilities.models import RiskLevel
from ares.events import AresEvent, EventBus, EventSeverity
from ares.filesystems.service import (
    FilesystemInspectRequest,
    FilesystemRepairService,
    FilesystemServiceError,
)
from ares.recovery.dependency import RecoveryDependencyResolver
from ares.recovery.executor import (
    RecoveryAuthorizationGrant,
    RecoveryExecutorError,
    RecoveryMutationExecutor,
)
from ares.recovery.integrity import recovery_plan_fingerprint
from ares.recovery.models import (
    ConfigurationDiff,
    DiagnosticEvidence,
    EvidenceSeverity,
    RecoveryCase,
    RecoveryDependency,
    RecoveryDiagnoseInput,
    RecoveryExecutionSummary,
    RecoveryIssue,
    RecoveryIssueCode,
    RecoveryLayer,
    RecoveryMode,
    RecoveryOperation,
    RecoveryOperationStatus,
    RecoveryPlanInput,
    RecoveryReport,
    RecoveryStatus,
    RecoveryStrategyKind,
    RecoveryVerificationStatus,
    RootCauseHypothesis,
    SystemRecoveryPlan,
    SystemRecoveryVerification,
    utc_now,
)
from ares.recovery.safety import RecoverySafetyError, RecoverySafetyGate
from ares.recovery.store import RecoveryStore
from ares.storage.service import StorageAnalysisError, StorageAnalysisService
from ares.tools.recovery import (
    AptPackageManagerAdapter,
    ConfigurationRecoveryTool,
    EmergencyModeTool,
    JournalDiagnosticTool,
    KernelDiagnosticTool,
    PackageManagerKind,
    RecoveryProcessRunner,
    SystemdDiagnosticTool,
    detect_package_manager,
    evidence_to_issues,
)
from ares.workflows import ExecutionStatus


class RecoveryOrchestratorError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class RecoveryOrchestrator:
    """Coordinate existing diagnostics and independently protected child mutations."""

    def __init__(
        self,
        *,
        store: RecoveryStore,
        capabilities: CapabilityManager,
        storage: StorageAnalysisService,
        filesystems: FilesystemRepairService,
        boot: BootRecoveryService,
        runner: RecoveryProcessRunner,
        executor: RecoveryMutationExecutor,
        event_bus: EventBus,
        audit: AuditLedger,
    ) -> None:
        self.store = store
        self.capabilities = capabilities
        self.storage = storage
        self.filesystems = filesystems
        self.boot = boot
        self.runner = runner
        self.executor = executor
        self.event_bus = event_bus
        self.audit = audit
        self.dependencies = RecoveryDependencyResolver()
        self.safety = RecoverySafetyGate()
        self.systemd = SystemdDiagnosticTool(runner)
        self.journal = JournalDiagnosticTool(runner)
        self.packages = AptPackageManagerAdapter(runner)
        self.kernel = KernelDiagnosticTool()
        self.configuration = ConfigurationRecoveryTool()
        self._grants: dict[str, RecoveryAuthorizationGrant] = {}
        self._lock = asyncio.Lock()

    async def diagnose(
        self,
        request: RecoveryDiagnoseInput,
        *,
        session_id: str,
    ) -> RecoveryCase:
        root = self._root(request.target_root)
        case = RecoveryCase(
            session_id=session_id,
            mode=request.mode,
            target_root=str(root),
            target_boot=str(root / "boot"),
            initial_state="diagnosis_started",
            status=RecoveryStatus.DIAGNOSING,
        )
        await self.store.put_case(case)
        await self._event("recovery.case.created", case, {"mode": request.mode.value})
        await self._event("recovery.diagnosis.started", case, {})

        evidence: list[DiagnosticEvidence] = []
        issues: list[RecoveryIssue] = []
        target_os: str | None = None
        target_esp: str | None = None

        try:
            storage = await self.storage.analyze(session_id=session_id)
            evidence.append(
                DiagnosticEvidence(
                    source="storage.disk-analysis",
                    severity=EvidenceSeverity.INFO,
                    subsystem="storage",
                    event="storage-analysis",
                    normalized_message=storage.message[:2048],
                    confidence=storage.diagnostic.confidence,
                    metadata={
                        "snapshot_id": storage.snapshot_id,
                        "diagnostic_id": storage.diagnostic_id,
                    },
                )
            )
        except StorageAnalysisError as exc:
            item = DiagnosticEvidence(
                source="storage.disk-analysis",
                severity=EvidenceSeverity.ERROR,
                subsystem="storage",
                event="storage-unavailable",
                normalized_message=f"storage analysis unavailable: {exc.code}",
                confidence=0.9,
            )
            evidence.append(item)
            issues.append(
                RecoveryIssue(
                    code=RecoveryIssueCode.STORAGE_UNAVAILABLE,
                    layer=RecoveryLayer.STORAGE,
                    severity=EvidenceSeverity.ERROR,
                    summary=item.normalized_message,
                    evidence_ids=(item.id,),
                    confidence=item.confidence,
                    affected_components=("storage", "filesystem", "boot"),
                )
            )

        boot_result = None
        try:
            boot_result = await self.boot.diagnose(
                BootDiagnoseRequest(target_disk=request.target_disk, root_path=str(root)),
                session_id=session_id,
            )
            target_os = (
                boot_result.environment.operating_systems[0].id
                if boot_result.environment.operating_systems
                else None
            )
            target_esp = boot_result.environment.esp.mount_point if boot_result.environment.esp else None
            for item in boot_result.evidence:
                evidence.append(
                    DiagnosticEvidence(
                        id=item.id,
                        source=f"boot.diagnose:{item.source}",
                        severity=EvidenceSeverity.INFO,
                        subsystem="boot",
                        event="boot-evidence",
                        normalized_message=item.observation,
                        confidence=item.confidence,
                        resource_ids=((item.resource_id,) if item.resource_id else ()),
                        metadata={**item.metadata, "boot_diagnostic_id": boot_result.id},
                    )
                )
            for item in boot_result.issues:
                code = (
                    RecoveryIssueCode.FSTAB_INVALID_REFERENCE
                    if item.code.value == "FSTAB_REFERENCE_INVALID"
                    else RecoveryIssueCode.INITRAMFS_MISSING
                    if item.code.value == "INITRAMFS_MISSING"
                    else RecoveryIssueCode.BOOT_DEGRADED
                )
                layer = (
                    RecoveryLayer.CONFIGURATION
                    if code is RecoveryIssueCode.FSTAB_INVALID_REFERENCE
                    else RecoveryLayer.INITRAMFS
                    if code is RecoveryIssueCode.INITRAMFS_MISSING
                    else RecoveryLayer.BOOT
                )
                issues.append(
                    RecoveryIssue(
                        code=code,
                        layer=layer,
                        severity=_severity(item.severity.value),
                        summary=item.summary,
                        evidence_ids=item.evidence_ids,
                        confidence=item.confidence,
                        affected_components=("boot",),
                    )
                )
        except BootRecoveryServiceError as exc:
            item = DiagnosticEvidence(
                source="boot.diagnose",
                severity=EvidenceSeverity.WARNING,
                subsystem="boot",
                event="boot-diagnostic-unavailable",
                normalized_message=f"boot diagnosis unavailable: {exc.code}",
                confidence=0.7,
            )
            evidence.append(item)

        if boot_result is not None and boot_result.environment.root_partition is not None:
            try:
                inspection = await self.filesystems.inspect(
                    FilesystemInspectRequest(device=boot_result.environment.root_partition.device_path),
                    session_id=session_id,
                )
                fs_evidence = DiagnosticEvidence(
                    source="filesystem.repair.inspect",
                    severity=(
                        EvidenceSeverity.INFO
                        if inspection.health.value == "healthy"
                        else EvidenceSeverity.ERROR
                    ),
                    subsystem="filesystem",
                    event="filesystem-health",
                    normalized_message=(
                        f"filesystem {inspection.filesystem.value if inspection.filesystem else 'unknown'} "
                        f"health={inspection.health.value} mounted={inspection.mount.mounted}"
                    ),
                    confidence=0.95,
                    resource_ids=(f"filesystem:{inspection.identity.fingerprint_sha256}",),
                    metadata={"inspection_id": inspection.id, "health": inspection.health.value},
                )
                evidence.append(fs_evidence)
                if inspection.health.value not in {"healthy", "unknown"}:
                    issues.append(
                        RecoveryIssue(
                            code=RecoveryIssueCode.FILESYSTEM_DEGRADED,
                            layer=RecoveryLayer.FILESYSTEM,
                            severity=EvidenceSeverity.ERROR,
                            summary=fs_evidence.normalized_message,
                            evidence_ids=(fs_evidence.id,),
                            confidence=0.95,
                            affected_components=("root_filesystem", "systemd"),
                        )
                    )
            except FilesystemServiceError as exc:
                evidence.append(
                    DiagnosticEvidence(
                        source="filesystem.repair.inspect",
                        severity=EvidenceSeverity.WARNING,
                        subsystem="filesystem",
                        event="filesystem-inspection-unavailable",
                        normalized_message=f"filesystem inspection unavailable: {exc.code}",
                        confidence=0.7,
                    )
                )

        systemd_evidence, journal_evidence, package_evidence = await asyncio.gather(
            self.systemd.inspect(root),
            self.journal.inspect(root),
            self.packages.diagnose(root),
        )
        kernel_evidence = self.kernel.inspect(root)
        evidence.extend(systemd_evidence)
        evidence.extend(journal_evidence)
        evidence.extend(package_evidence)
        evidence.extend(kernel_evidence)
        issues.extend(evidence_to_issues((*systemd_evidence, *journal_evidence, *package_evidence, *kernel_evidence)))
        boot_mode = EmergencyModeTool.classify(systemd_evidence)
        if boot_mode.value == "EMERGENCY":
            item = DiagnosticEvidence(
                source="systemd",
                severity=EvidenceSeverity.ERROR,
                subsystem="systemd",
                event="emergency-mode",
                normalized_message="systemd evidence indicates emergency/maintenance mode",
                confidence=0.9,
            )
            evidence.append(item)
            issues.append(
                RecoveryIssue(
                    code=RecoveryIssueCode.EMERGENCY_MODE,
                    layer=RecoveryLayer.SYSTEMD,
                    severity=EvidenceSeverity.ERROR,
                    summary=item.normalized_message,
                    evidence_ids=(item.id,),
                    confidence=item.confidence,
                    affected_components=("systemd", "mounts", "services"),
                )
            )

        hypotheses = self.dependencies.hypotheses(tuple(issues))
        updated = case.model_copy(
            update={
                "updated_at": utc_now(),
                "target_os": target_os,
                "target_esp": target_esp,
                "evidence": tuple(evidence),
                "detected_issues": tuple(issues),
                "hypotheses": hypotheses,
                "initial_state": boot_mode.value,
                "status": RecoveryStatus.DISCOVERY,
            }
        )
        await self.store.put_case(updated)
        for item in evidence:
            await self._event(
                "recovery.evidence.collected",
                updated,
                {"evidence_id": item.id, "source": item.source, "event": item.event},
            )
        for hypothesis in hypotheses:
            await self._event(
                "recovery.root-cause-hypothesis.created",
                updated,
                {"hypothesis_id": hypothesis.id, "confidence": hypothesis.confidence},
            )
        return updated

    async def build_plan(self, request: RecoveryPlanInput) -> SystemRecoveryPlan:
        case = await self._case(request.case_id)
        if case.status not in {RecoveryStatus.DISCOVERY, RecoveryStatus.PARTIAL, RecoveryStatus.FAILED}:
            raise RecoveryOrchestratorError("RECOVERY_CASE_NOT_PLANNABLE")
        if request.mode is RecoveryMode.READ_ONLY:
            raise RecoveryOrchestratorError("RECOVERY_READ_ONLY_CANNOT_PLAN_MUTATIONS")
        root = self._root(case.target_root)
        operations: list[RecoveryOperation] = []
        blocked: list[str] = []

        config_operation = self._configuration_operation(case, root)
        if config_operation is not None:
            operations.append(config_operation)
        initramfs_operation = self._initramfs_operation(case)
        if initramfs_operation is not None:
            operations.append(initramfs_operation)
        package_operations = self._package_operations(case)
        operations.extend(package_operations)

        if any(item.code is RecoveryIssueCode.FILESYSTEM_DEGRADED for item in case.detected_issues):
            blocked.append(
                "filesystem.repair requires its own verified full-filesystem backup and authorization"
            )
        if any(item.code is RecoveryIssueCode.BOOT_DEGRADED for item in case.detected_issues):
            blocked.append(
                "boot.repair.grub remains an independent child recovery with its own BootRepairPlan/checkpoint/authorization"
            )
        package_kind = detect_package_manager(root)
        if package_kind not in {PackageManagerKind.APT, PackageManagerKind.UNKNOWN} and any(
            item.layer is RecoveryLayer.PACKAGES for item in case.detected_issues
        ):
            blocked.append(f"automatic package repair not implemented for {package_kind.value}")

        operations = self._link_dependencies(operations)
        plan = SystemRecoveryPlan(
            case_id=case.case_id,
            mode=request.mode,
            problem=self._problem(case),
            evidence_ids=tuple(item.id for item in case.evidence),
            root_cause_hypothesis_ids=tuple(item.id for item in case.hypotheses),
            operations=tuple(operations),
            dependencies=tuple(
                RecoveryDependency(
                    upstream=dependency,
                    downstream=operation.operation_id,
                    relation="must_complete_before",
                )
                for operation in operations
                for dependency in operation.depends_on
            ),
            minimum_change_rationale=(
                "Operations are derived only from observed root-cause evidence; lower-layer causes "
                "precede dependent service symptoms, unsupported repairs remain blocked, and no "
                "generic upgrade/reinstall action is emitted."
            ),
            executable=bool(operations),
            blocked_reasons=tuple(blocked),
            fingerprint_sha256="0" * 64,
        )
        plan = plan.model_copy(update={"fingerprint_sha256": recovery_plan_fingerprint(plan)})
        updated = case.model_copy(
            update={
                "mode": request.mode,
                "recovery_plan": plan,
                "status": RecoveryStatus.PLANNED,
                "updated_at": utc_now(),
            }
        )
        await self.store.put_case(updated)
        await self._event(
            "recovery.plan.created",
            updated,
            {"plan_id": plan.id, "operation_count": len(plan.operations), "blocked": list(blocked)},
        )
        await self._audit(
            "recovery.plan.created",
            updated,
            {"plan_id": plan.id, "plan_fingerprint": plan.fingerprint_sha256},
        )
        return plan

    async def authorize(self, case_id: str) -> RecoveryCase:
        case = await self._case(case_id)
        plan = self._plan(case)
        self.safety.validate_plan(plan)
        root = self._root(case.target_root)
        operations = list(plan.operations)
        for index, operation in enumerate(operations):
            if operation.status is not RecoveryOperationStatus.PLANNED:
                continue
            try:
                checkpoint = await self.executor.protect(
                    operation, root=root, session_id=case.session_id
                )
            except RecoveryExecutorError as exc:
                raise RecoveryOrchestratorError(exc.code) from exc
            operation = operation.model_copy(
                update={
                    "protection_checkpoint": checkpoint,
                    "status": RecoveryOperationStatus.PROTECTED,
                }
            )
            operations[index] = operation
            case = await self._replace_operations(case, tuple(operations), RecoveryStatus.PROTECTED)
            await self._event(
                "recovery.protection.created",
                case,
                {
                    "operation_id": operation.operation_id,
                    "checkpoint_id": checkpoint.id,
                },
            )

            async def on_challenge(challenge_id: str, *, position: int = index) -> None:
                current = operations[position]
                operations[position] = current.model_copy(
                    update={
                        "authorization_challenge_id": challenge_id,
                        "status": RecoveryOperationStatus.AUTHORIZATION_PENDING,
                    }
                )
                current_case = await self._replace_operations(
                    case, tuple(operations), RecoveryStatus.PROTECTED
                )
                await self._event(
                    "recovery.authorization.requested",
                    current_case,
                    {
                        "operation_id": current.operation_id,
                        "challenge_id": challenge_id,
                    },
                    EventSeverity.WARNING,
                )

            try:
                grant = await self.executor.authorize(
                    operation,
                    session_id=case.session_id,
                    on_challenge=on_challenge,
                )
            except RecoveryExecutorError as exc:
                raise RecoveryOrchestratorError(exc.code) from exc
            async with self._lock:
                self._grants[operation.operation_id] = grant
            operations[index] = operations[index].model_copy(
                update={"status": RecoveryOperationStatus.AUTHORIZED}
            )
            case = await self._replace_operations(case, tuple(operations), RecoveryStatus.PROTECTED)
        case = await self._replace_operations(case, tuple(operations), RecoveryStatus.AUTHORIZED)
        return case

    async def execute(self, case_id: str) -> RecoveryCase:
        case = await self._case(case_id)
        plan = self._plan(case)
        self.safety.validate_plan(plan)
        if case.status is not RecoveryStatus.AUTHORIZED:
            raise RecoveryOrchestratorError("RECOVERY_CASE_NOT_AUTHORIZED")
        root = self._root(case.target_root)
        operations = list(plan.operations)
        completed: set[str] = set()
        changes: list[str] = []
        executions: list[RecoveryExecutionSummary] = []
        for index, operation in enumerate(operations):
            try:
                self.safety.validate_operation(
                    operation,
                    mode=case.mode,
                    completed_operation_ids=frozenset(completed),
                )
            except RecoverySafetyError as exc:
                operations[index] = operation.model_copy(
                    update={"status": RecoveryOperationStatus.BLOCKED, "error_code": exc.code}
                )
                case = await self._terminal_partial(case, tuple(operations), executions, exc.code)
                return case
            async with self._lock:
                grant = self._grants.pop(operation.operation_id, None)
            if grant is None:
                operations[index] = operation.model_copy(
                    update={
                        "status": RecoveryOperationStatus.UNKNOWN,
                        "error_code": "RECOVERY_AUTHORIZATION_GRANT_UNAVAILABLE",
                    }
                )
                return await self._terminal_unknown(case, tuple(operations), executions)
            started = datetime.now(UTC)
            running = operation.model_copy(
                update={"status": RecoveryOperationStatus.EXECUTING, "started_at": started}
            )
            operations[index] = running
            case = await self._replace_operations(case, tuple(operations), RecoveryStatus.RECOVERING)
            await self._event(
                "recovery.operation.started",
                case,
                {"operation_id": running.operation_id, "capability_id": running.capability_id},
                EventSeverity.WARNING,
            )
            try:
                execution = await self.capabilities.execute(
                    running.capability_id,
                    {
                        "case_id": case.case_id,
                        "operation": running.model_dump(mode="json"),
                        "target_root": str(root),
                        "grant": grant.model_dump(mode="json"),
                        "session_id": case.session_id,
                    },
                    protection_checkpoint=running.protection_checkpoint,
                )
                if execution.status is not ExecutionStatus.SUCCEEDED or execution.result is None:
                    raise RecoveryOrchestratorError(
                        execution.error_code or "RECOVERY_CHILD_CAPABILITY_FAILED"
                    )
                child_changes = execution.result.get("changes", [])
                if isinstance(child_changes, list):
                    changes.extend(str(item) for item in child_changes)
            except (RecoveryOrchestratorError, Exception) as exc:
                code = (
                    exc.code
                    if isinstance(exc, RecoveryOrchestratorError)
                    else "RECOVERY_CHILD_CAPABILITY_FAILED"
                )
                failed = running.model_copy(
                    update={
                        "status": RecoveryOperationStatus.FAILED,
                        "finished_at": datetime.now(UTC),
                        "error_code": code,
                    }
                )
                operations[index] = failed
                executions.append(self._summary(failed))
                case = await self._terminal_partial(case, tuple(operations), executions, code)
                await self._event(
                    "recovery.operation.failed",
                    case,
                    {"operation_id": failed.operation_id, "error_code": code},
                    EventSeverity.ERROR,
                )
                return case
            completed_op = running.model_copy(
                update={
                    "status": RecoveryOperationStatus.COMPLETED,
                    "finished_at": datetime.now(UTC),
                }
            )
            operations[index] = completed_op
            completed.add(completed_op.operation_id)
            executions.append(self._summary(completed_op))
            case = await self._replace_operations(case, tuple(operations), RecoveryStatus.RECOVERING)
            await self._event(
                "recovery.operation.completed",
                case,
                {"operation_id": completed_op.operation_id},
            )

        verification = await self.verify(case.case_id)
        final_status = (
            RecoveryStatus.RECOVERED
            if verification.status is RecoveryVerificationStatus.RECOVERED
            else RecoveryStatus.PARTIAL
            if verification.status is RecoveryVerificationStatus.PARTIALLY_RECOVERED
            else RecoveryStatus.FAILED
            if verification.status is RecoveryVerificationStatus.NOT_RECOVERED
            else RecoveryStatus.UNKNOWN
        )
        case = (await self._case(case.case_id)).model_copy(
            update={
                "executions": tuple(executions),
                "verification": verification,
                "status": final_status,
                "final_state": verification.message,
                "updated_at": utc_now(),
            }
        )
        report = self._report(case, changes)
        case = case.model_copy(update={"report": report})
        await self.store.put_case(case)
        await self._event(
            _case_event(final_status), case, {"verification_status": verification.status.value}
        )
        return case

    async def verify(self, case_id: str) -> SystemRecoveryVerification:
        case = await self._case(case_id)
        root = self._root(case.target_root)
        await self._event("recovery.verification.started", case, {})
        systemd, packages = await asyncio.gather(
            self.systemd.inspect(root), self.packages.diagnose(root)
        )
        kernel = self.kernel.inspect(root)
        remaining = evidence_to_issues((*systemd, *packages, *kernel))
        status = (
            RecoveryVerificationStatus.RECOVERED
            if not remaining
            else RecoveryVerificationStatus.PARTIALLY_RECOVERED
            if case.executions or (
                case.recovery_plan
                and any(
                    item.status is RecoveryOperationStatus.COMPLETED
                    for item in case.recovery_plan.operations
                )
            )
            else RecoveryVerificationStatus.NOT_RECOVERED
        )
        verification = SystemRecoveryVerification(
            case_id=case.case_id,
            status=status,
            kernel_verified=not any(item.layer is RecoveryLayer.KERNEL for item in remaining),
            initramfs_verified=not any(item.layer is RecoveryLayer.INITRAMFS for item in remaining),
            systemd_verified=not any(item.layer is RecoveryLayer.SYSTEMD for item in remaining),
            critical_services_verified=not any(
                item.layer is RecoveryLayer.SERVICES for item in remaining
            ),
            evidence_ids=tuple(item.id for item in (*systemd, *packages, *kernel)),
            remaining_issue_ids=tuple(item.id for item in remaining),
            limitations=(
                "verification is offline/static unless the recovered OS has actually booted",
                "hardware/storage/filesystem/boot child diagnostics retain their own verification semantics",
            ),
            message=(
                "No remaining userspace/package/kernel issues were detected by the bounded postchecks."
                if not remaining
                else f"{len(remaining)} recovery issue(s) remain after executed operations."
            ),
        )
        await self.store.put_verification(verification)
        await self._event(
            "recovery.verification.completed",
            case,
            {"verification_id": verification.id, "status": verification.status.value},
        )
        return verification

    async def abort(self, case_id: str) -> RecoveryCase:
        case = await self._case(case_id)
        if case.status in {
            RecoveryStatus.RECOVERED,
            RecoveryStatus.PARTIAL,
            RecoveryStatus.FAILED,
            RecoveryStatus.UNKNOWN,
            RecoveryStatus.ABORTED,
        }:
            return case
        async with self._lock:
            for operation in self._plan(case).operations if case.recovery_plan else ():
                self._grants.pop(operation.operation_id, None)
        updated = case.model_copy(
            update={
                "status": RecoveryStatus.ABORTED,
                "final_state": "Recovery case aborted before remaining operations executed.",
                "updated_at": utc_now(),
            }
        )
        await self.store.put_case(updated)
        await self._event("recovery.case.failed", updated, {"reason": "aborted"})
        return updated

    def _configuration_operation(self, case: RecoveryCase, root: Path) -> RecoveryOperation | None:
        if not any(
            item.code is RecoveryIssueCode.FSTAB_INVALID_REFERENCE for item in case.detected_issues
        ):
            return None
        proposed = self._propose_fstab_fix(case, root)
        if proposed is None:
            return None
        change = self.configuration.build_diff(root, "etc/fstab", proposed)
        return RecoveryOperation(
            capability_id="configuration.recover",
            strategy=RecoveryStrategyKind.CONFIGURATION,
            description="Correct the single evidence-backed invalid root filesystem identity in fstab.",
            risk=RiskLevel.HIGH,
            target_resources=("/etc/fstab",),
            preconditions=("current fstab hash matches plan", "replacement UUID exists in boot/storage evidence"),
            rollback_supported=True,
            rollback_strategy="Restore the exact checkpointed original fstab if post-write validation fails.",
            rollback_limitations=("rollback cannot repair unrelated filesystem damage",),
            verification_requirements=("fstab syntax valid", "planned SHA changed", "mount identity resolvable"),
            payload={"configuration_diff": change.model_dump(mode="json")},
        )

    def _initramfs_operation(self, case: RecoveryCase) -> RecoveryOperation | None:
        evidence = next((item for item in case.evidence if item.event == "initramfs-missing"), None)
        if evidence is None:
            return None
        kernel = str(evidence.metadata.get("kernel", ""))
        if not kernel:
            return None
        return RecoveryOperation(
            capability_id="initramfs.rebuild",
            strategy=RecoveryStrategyKind.INITRAMFS,
            description=f"Rebuild the missing initramfs for the observed kernel {kernel}.",
            risk=RiskLevel.HIGH,
            target_resources=(f"/boot/initrd.img-{kernel}",),
            preconditions=("kernel artifact exists", "Debian-family update-initramfs support detected"),
            rollback_supported=True,
            rollback_strategy="Restore a checkpointed prior initramfs when one existed.",
            rollback_limitations=("no prior initramfs exists for a genuinely missing artifact",),
            verification_requirements=("initramfs artifact exists after rebuild",),
            payload={"kernel_version": kernel},
        )

    @staticmethod
    def _package_operations(case: RecoveryCase) -> list[RecoveryOperation]:
        operations: list[RecoveryOperation] = []
        codes = {item.code for item in case.detected_issues}
        if RecoveryIssueCode.PACKAGE_INTERRUPTED in codes or RecoveryIssueCode.PACKAGE_PENDING_CONFIGURATION in codes:
            operations.append(
                RecoveryOperation(
                    capability_id="package.repair",
                    strategy=RecoveryStrategyKind.PACKAGE,
                    description="Configure packages left pending by an interrupted dpkg transaction.",
                    risk=RiskLevel.HIGH,
                    target_resources=("/var/lib/dpkg/status",),
                    preconditions=("APT/dpkg Debian-family target detected",),
                    rollback_supported=False,
                    rollback_strategy="No generic rollback for dpkg maintainer scripts.",
                    rollback_limitations=("package maintainer scripts can have non-reversible side effects",),
                    verification_requirements=("dpkg audit no longer reports pending configuration",),
                    payload={"package_operation": "configure-pending"},
                )
            )
        if RecoveryIssueCode.PACKAGE_BROKEN_DEPENDENCIES in codes:
            operations.append(
                RecoveryOperation(
                    capability_id="package.repair",
                    strategy=RecoveryStrategyKind.PACKAGE,
                    description="Repair broken APT dependencies using only already available local packages.",
                    risk=RiskLevel.HIGH,
                    target_resources=("/var/lib/dpkg/status",),
                    preconditions=("APT target detected", "no network/download required"),
                    rollback_supported=False,
                    rollback_strategy="No generic rollback for package scripts or dependency configuration.",
                    rollback_limitations=("external package downloads are explicitly blocked",),
                    verification_requirements=("dpkg audit/dependency state consistent",),
                    payload={"package_operation": "fix-broken-offline"},
                )
            )
        return operations

    @staticmethod
    def _link_dependencies(operations: list[RecoveryOperation]) -> list[RecoveryOperation]:
        config_ids = [item.operation_id for item in operations if item.strategy is RecoveryStrategyKind.CONFIGURATION]
        result: list[RecoveryOperation] = []
        for operation in operations:
            depends = operation.depends_on
            if operation.strategy in {RecoveryStrategyKind.INITRAMFS, RecoveryStrategyKind.PACKAGE} and config_ids:
                depends = tuple(dict.fromkeys((*depends, *config_ids)))
            result.append(operation.model_copy(update={"depends_on": depends}))
        return result

    @staticmethod
    def _propose_fstab_fix(case: RecoveryCase, root: Path) -> str | None:
        fstab = root / "etc/fstab"
        if not fstab.is_file():
            return None
        root_uuid = None
        for item in case.evidence:
            value = item.metadata.get("root_uuid")
            if isinstance(value, str) and value:
                root_uuid = value
                break
        if root_uuid is None:
            return None
        lines = fstab.read_text(encoding="utf-8").splitlines()
        changed = False
        output: list[str] = []
        for line in lines:
            stripped = line.strip()
            fields = stripped.split()
            if (
                not changed
                and fields
                and len(fields) >= 4
                and fields[1] == "/"
                and fields[0].startswith(("UUID=", "PARTUUID="))
            ):
                fields[0] = f"UUID={root_uuid}"
                output.append("\t".join(fields))
                changed = True
            else:
                output.append(line)
        return "\n".join(output) + "\n" if changed else None

    async def _replace_operations(
        self,
        case: RecoveryCase,
        operations: tuple[RecoveryOperation, ...],
        status: RecoveryStatus,
    ) -> RecoveryCase:
        plan = self._plan(case).model_copy(update={"operations": operations})
        plan = plan.model_copy(update={"fingerprint_sha256": recovery_plan_fingerprint(plan)})
        updated = case.model_copy(
            update={"recovery_plan": plan, "status": status, "updated_at": utc_now()}
        )
        await self.store.put_case(updated)
        return updated

    async def _terminal_partial(
        self,
        case: RecoveryCase,
        operations: tuple[RecoveryOperation, ...],
        executions: list[RecoveryExecutionSummary],
        code: str,
    ) -> RecoveryCase:
        updated = await self._replace_operations(case, operations, RecoveryStatus.PARTIAL)
        updated = updated.model_copy(
            update={
                "executions": tuple(executions),
                "final_state": f"Recovery stopped after child operation failure: {code}",
            }
        )
        await self.store.put_case(updated)
        await self._event("recovery.case.partial", updated, {"error_code": code})
        return updated

    async def _terminal_unknown(
        self,
        case: RecoveryCase,
        operations: tuple[RecoveryOperation, ...],
        executions: list[RecoveryExecutionSummary],
    ) -> RecoveryCase:
        updated = await self._replace_operations(case, operations, RecoveryStatus.UNKNOWN)
        updated = updated.model_copy(
            update={
                "executions": tuple(executions),
                "final_state": "Recovery operation state is uncertain; no automatic retry is allowed.",
            }
        )
        await self.store.put_case(updated)
        await self._event("recovery.case.unknown", updated, {})
        return updated

    async def _case(self, case_id: str) -> RecoveryCase:
        case = await self.store.get_case(case_id)
        if case is None:
            raise RecoveryOrchestratorError("RECOVERY_CASE_NOT_FOUND")
        return case

    @staticmethod
    def _plan(case: RecoveryCase) -> SystemRecoveryPlan:
        if case.recovery_plan is None:
            raise RecoveryOrchestratorError("RECOVERY_PLAN_NOT_FOUND")
        return case.recovery_plan

    @staticmethod
    def _root(value: str | None) -> Path:
        if value is None:
            raise RecoveryOrchestratorError("RECOVERY_TARGET_ROOT_REQUIRED")
        root = Path(value)
        if not root.is_absolute() or root.is_symlink() or not root.is_dir():
            raise RecoveryOrchestratorError("RECOVERY_TARGET_ROOT_INVALID")
        return root.resolve()

    @staticmethod
    def _summary(operation: RecoveryOperation) -> RecoveryExecutionSummary:
        return RecoveryExecutionSummary(
            operation_id=operation.operation_id,
            capability_id=operation.capability_id,
            status=operation.status,
            started_at=operation.started_at,
            finished_at=operation.finished_at,
            error_code=operation.error_code,
        )

    @staticmethod
    def _problem(case: RecoveryCase) -> str:
        if not case.detected_issues:
            return "No evidence-backed recoverable system issue was detected."
        return case.detected_issues[0].summary

    @staticmethod
    def _report(case: RecoveryCase, changes: list[str]) -> RecoveryReport:
        verification = case.verification
        return RecoveryReport(
            initial_state=case.initial_state,
            detected_problems=tuple(item.summary for item in case.detected_issues),
            evidence_summary=tuple(item.normalized_message for item in case.evidence[:32]),
            root_cause=tuple(item.hypothesis for item in case.hypotheses[:5]),
            actions_planned=tuple(
                item.description for item in case.recovery_plan.operations
            ) if case.recovery_plan else (),
            actions_executed=tuple(
                item.capability_id for item in case.executions if item.status is RecoveryOperationStatus.COMPLETED
            ),
            protection=tuple(
                item.protection_checkpoint.id
                for item in case.recovery_plan.operations
                if item.protection_checkpoint is not None
            ) if case.recovery_plan else (),
            changes=tuple(changes),
            verification=verification.status.value if verification else "UNKNOWN",
            remaining_problems=tuple(
                item.id for item in case.detected_issues
                if verification and item.id in verification.remaining_issue_ids
            ),
            recommendations=tuple(case.recovery_plan.blocked_reasons) if case.recovery_plan else (),
            user_summary=(
                "ARES completed the evidence-backed recovery plan. Review verification and remaining limitations."
                if case.status is RecoveryStatus.RECOVERED
                else "ARES stopped with unresolved or uncertain evidence. No unsupported repair was attempted."
            ),
        )

    async def _event(
        self,
        name: str,
        case: RecoveryCase,
        payload: dict[str, Any],
        severity: EventSeverity = EventSeverity.INFO,
    ) -> None:
        await self.event_bus.publish(
            AresEvent(
                event_type=name,
                source="recovery.orchestrator",
                correlation_id=case.case_id,
                session_id=case.session_id,
                severity=severity,
                payload={"case_id": case.case_id, **payload},
            )
        )

    async def _audit(self, name: str, case: RecoveryCase, payload: dict[str, Any]) -> None:
        try:
            await self.audit.append(
                event_type=name,
                source="recovery.orchestrator",
                correlation_id=case.case_id,
                session_id=case.session_id,
                payload={"case_id": case.case_id, **payload},
            )
        except AuditLedgerError as exc:
            raise RecoveryOrchestratorError("RECOVERY_AUDIT_UNAVAILABLE") from exc


def _severity(value: str) -> EvidenceSeverity:
    return {
        "info": EvidenceSeverity.INFO,
        "warning": EvidenceSeverity.WARNING,
        "high": EvidenceSeverity.ERROR,
        "critical": EvidenceSeverity.CRITICAL,
    }.get(value.lower(), EvidenceSeverity.WARNING)


def _case_event(status: RecoveryStatus) -> str:
    return {
        RecoveryStatus.RECOVERED: "recovery.case.completed",
        RecoveryStatus.PARTIAL: "recovery.case.partial",
        RecoveryStatus.FAILED: "recovery.case.failed",
        RecoveryStatus.UNKNOWN: "recovery.case.unknown",
    }.get(status, "recovery.case.completed")
