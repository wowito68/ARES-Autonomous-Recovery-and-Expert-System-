from __future__ import annotations

from pathlib import Path

import pytest

from ares.boot.engine import BootRecoveryEngine, BootRecoveryEngineError
from ares.boot.executor import BootExecutorError, LocalTestBootExecutor
from ares.boot.models import (
    BootConfiguration,
    BootDiagnosticResult,
    BootEnvironment,
    BootEvidence,
    BootIssue,
    BootIssueCode,
    BootIssueSeverity,
    Bootloader,
    BootloaderKind,
    BootPartition,
    BootRepairExecution,
    BootRepairPlanInput,
    BootRepairStatus,
    BootTargetOS,
    BootVerificationConfidence,
    BootVerificationStatus,
    DistributionFamily,
    FirmwareEnvironment,
    FirmwareMode,
)
from ares.boot.store import BootRecoveryStore
from ares.protection import ProtectionCheckpointStore
from ares.storage_operations import StorageOperationEngine
from ares.storage_operations.models import StorageLayout
from ares.tools.boot import BootRepairToolSuite
from tests.test_boot_tools import _root, _Runner
from tests.test_storage_partition_edges import _identity


class _UnusedStorage(StorageOperationEngine):
    def __init__(self) -> None:
        pass

    async def inspect(self, target_disk: str) -> StorageLayout:
        raise AssertionError(f"unexpected storage inspection: {target_disk}")


def _diagnostic(root: Path) -> BootDiagnosticResult:
    evidence = BootEvidence(
        source="fixture",
        observation="Debian root, kernel and GRUB loader evidence were detected.",
        confidence=0.96,
    )
    target_os = BootTargetOS(
        id="os:debian-fixture",
        name="Debian GNU/Linux 13",
        version="13",
        family=DistributionFamily.DEBIAN,
        root_path=str(root),
        boot_path=str(root / "boot"),
    )
    config = BootConfiguration(
        root_path=str(root),
        boot_path=str(root / "boot"),
        grub_default_path=str(root / "etc/default/grub"),
        fstab_path=str(root / "etc/fstab"),
        kernels=("boot/vmlinuz-6.12-test",),
        initramfs=(),
    )
    issues = (
        BootIssue(
            code=BootIssueCode.GRUB_CONFIG_MISSING,
            severity=BootIssueSeverity.HIGH,
            summary="GRUB configuration is missing.",
            evidence_ids=(evidence.id,),
            confidence=0.94,
            recommended_action="Regenerate GRUB configuration.",
            repairable_automatically=True,
        ),
        BootIssue(
            code=BootIssueCode.INITRAMFS_MISSING,
            severity=BootIssueSeverity.HIGH,
            summary="initramfs is missing.",
            evidence_ids=(evidence.id,),
            confidence=0.92,
            recommended_action="Regenerate initramfs explicitly.",
            repairable_automatically=True,
        ),
    )
    return BootDiagnosticResult(
        environment=BootEnvironment(
            firmware=FirmwareEnvironment(
                mode=FirmwareMode.BIOS,
                efivars_available=False,
                boot_entries_available=False,
                evidence_ids=(evidence.id,),
            ),
            bootloader=Bootloader(
                kind=BootloaderKind.GRUB,
                distribution_family=DistributionFamily.DEBIAN,
                repair_supported=True,
                evidence_ids=(evidence.id,),
            ),
            target_disk=_identity(),
            boot_disk_id="disk:fixture",
            root_partition=BootPartition(
                resource_id="partition:root",
                device_path="image:fixture:1",
                partition_number=1,
                role="root",
                filesystem_type="ext4",
                mount_point=str(root),
            ),
            operating_systems=(target_os,),
            configuration=config,
            evidence=(evidence,),
        ),
        issues=issues,
        evidence=(evidence,),
        confidence=0.93,
        severity=BootIssueSeverity.HIGH,
        recommended_action="Create a repair plan.",
    )


def _engine(tmp_path: Path) -> tuple[BootRecoveryEngine, BootRecoveryStore, LocalTestBootExecutor]:
    store = BootRecoveryStore(tmp_path / "boot-state")
    store.prepare()
    checkpoints = ProtectionCheckpointStore(tmp_path / "protection")
    checkpoints.prepare()
    tools = BootRepairToolSuite(
        runner=_Runner(),
        test_mode=True,
        runtime_root=tmp_path / "runtime",
    )
    executor = LocalTestBootExecutor(tools, tmp_path / "checkpoint-files")
    return (
        BootRecoveryEngine(
            tools=tools,
            storage=_UnusedStorage(),
            store=store,
            checkpoints=checkpoints,
            executor=executor,
        ),
        store,
        executor,
    )


async def test_boot_plan_checkpoint_authorization_repair_and_verification(tmp_path: Path) -> None:
    root = _root(tmp_path, grub=False, initramfs=False)
    engine, store, executor = _engine(tmp_path)
    diagnostic = _diagnostic(root)
    await store.put_diagnostic(diagnostic)

    plan = await engine.plan(
        BootRepairPlanInput(diagnostic_id=diagnostic.id),
        session_id="boot-engine-session",
    )
    assert plan.executable is True
    assert plan.protection_checkpoint is None
    assert {item.kind.value for item in plan.operations} >= {
        "INSTALL_GRUB",
        "REGENERATE_GRUB_CONFIG",
        "REGENERATE_INITRAMFS",
    }

    protected = await engine.protect(plan.id, session_id="boot-engine-session")
    checkpoint = protected.protection_checkpoint
    assert checkpoint is not None
    artifact = await store.get_checkpoint(checkpoint.id)
    assert artifact is not None
    assert "boot/vmlinuz-6.12-test" in artifact.file_hashes

    challenges: list[str] = []

    async def challenge(challenge_id: str) -> None:
        challenges.append(challenge_id)

    grant = await engine.request_authorization(
        protected.id,
        session_id="boot-engine-session",
        on_challenge=challenge,
    )
    assert challenges == [grant.challenge_id]
    record = await store.get_record(protected.repair_id)
    assert record is not None
    assert record.execution.status is BootRepairStatus.AUTHORIZED

    stages: list[str] = []

    async def stage(name: str, payload: dict[str, object]) -> None:
        del payload
        stages.append(name)

    verification = await engine.execute_authorized(
        protected.repair_id,
        session_id="boot-engine-session",
        on_stage=stage,
    )
    assert verification.status is BootVerificationStatus.PARTIAL
    assert verification.confidence is BootVerificationConfidence.LIMITED
    assert (root / "boot/grub/grub.cfg").is_file()
    assert (root / "boot/initrd.img-6.12-test").is_file()
    assert "boot.bootloader.installed" in stages
    assert "boot.configuration.regenerated" in stages
    assert "boot.initramfs.regenerated" in stages

    record = await store.get_record(protected.repair_id)
    assert record is not None
    assert record.execution.status is BootRepairStatus.COMPLETED
    assert record.verification is not None

    with pytest.raises(BootExecutorError, match="BOOT_AUTHORIZATION_INVALID"):
        await executor.execute(protected, grant, on_stage=stage)


async def test_boot_plan_blocks_windows_and_non_debian(tmp_path: Path) -> None:
    root = _root(tmp_path)
    engine, store, _ = _engine(tmp_path)
    diagnostic = _diagnostic(root)
    environment = diagnostic.environment.model_copy(
        update={
            "bootloader": Bootloader(kind=BootloaderKind.WINDOWS_BOOT_MANAGER),
            "operating_systems": (
                diagnostic.environment.operating_systems[0].model_copy(
                    update={"family": DistributionFamily.OTHER}
                ),
            ),
        }
    )
    blocked = diagnostic.model_copy(update={"environment": environment})
    await store.put_diagnostic(blocked)
    plan = await engine.plan(
        BootRepairPlanInput(diagnostic_id=blocked.id),
        session_id="boot-blocked-session",
    )
    assert plan.executable is False
    assert "windows_boot_manager_repair_not_enabled" in plan.limitations
    assert "grub_repair_supported_only_for_debian_family" in plan.limitations
    with pytest.raises(BootRecoveryEngineError, match="BOOT_REPAIR_PLAN_NOT_EXECUTABLE"):
        await engine.protect(plan.id, session_id="boot-blocked-session")


async def test_boot_store_reconciles_interrupted_and_authorized_states(tmp_path: Path) -> None:
    root = _root(tmp_path)
    engine, store, _ = _engine(tmp_path)
    diagnostic = _diagnostic(root)
    await store.put_diagnostic(diagnostic)
    plan = await engine.plan(
        BootRepairPlanInput(diagnostic_id=diagnostic.id),
        session_id="boot-restart-session",
    )
    execution = await store.get_execution(plan.repair_id)
    assert execution is not None
    await store.put_execution(execution.model_copy(update={"status": BootRepairStatus.EXECUTING}))
    store.prepare()
    recovered = await store.get_execution(plan.repair_id)
    assert recovered is not None
    assert recovered.status is BootRepairStatus.UNKNOWN
    assert recovered.reconciliation_required is True

    second = BootRepairExecution(
        id="repair-authorized",
        repair_id="repair-authorized",
        plan_id=plan.id,
        session_id="boot-restart-session",
        status=BootRepairStatus.AUTHORIZED,
    )
    await store.put_execution(second)
    store.prepare()
    aborted = await store.get_execution("repair-authorized")
    assert aborted is not None
    assert aborted.status is BootRepairStatus.ABORTED
