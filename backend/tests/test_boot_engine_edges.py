from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ares.boot.engine import (
    BootRecoveryEngine,
    BootRecoveryEngineError,
    _dependencies,
    _diagnostic_confidence,
    _issues,
    _max_severity,
    _recommended_action,
    _repair_operations,
    _select_os,
)
from ares.boot.executor import LocalTestBootExecutor
from ares.boot.models import (
    BootConfiguration,
    BootDiagnoseInput,
    BootEntry,
    BootEvidence,
    BootIssue,
    BootIssueCode,
    BootIssueSeverity,
    Bootloader,
    BootloaderKind,
    BootOperationKind,
    BootPartition,
    BootRepairPlanInput,
    BootRepairStatus,
    BootTargetOS,
    DistributionFamily,
    FirmwareMode,
)
from ares.boot.store import BootRecoveryStore
from ares.protection import ProtectionCheckpointStore
from ares.storage_operations import StorageOperationEngine, StorageOperationEngineError
from ares.storage_operations.models import StorageLayout
from ares.tools.boot import BootRepairToolSuite
from tests.test_boot_engine import _diagnostic, _engine
from tests.test_boot_tools import _root, _Runner


class _FailingStorage(StorageOperationEngine):
    def __init__(self) -> None:
        pass

    async def inspect(self, target_disk: str) -> StorageLayout:
        del target_disk
        raise StorageOperationEngineError("STORAGE_FIXTURE_FAILURE")


class _NoStorage(StorageOperationEngine):
    def __init__(self) -> None:
        pass

    async def inspect(self, target_disk: str) -> StorageLayout:
        raise AssertionError(f"unexpected storage inspection: {target_disk}")


def _diagnostic_engine(tmp_path: Path, sys_root: Path) -> BootRecoveryEngine:
    store = BootRecoveryStore(tmp_path / "diagnose-store")
    store.prepare()
    checkpoints = ProtectionCheckpointStore(tmp_path / "diagnose-protection")
    checkpoints.prepare()
    tools = BootRepairToolSuite(
        runner=_Runner(),
        sys_root=sys_root,
        test_mode=True,
        runtime_root=tmp_path / "diagnose-runtime",
    )
    executor = LocalTestBootExecutor(tools, tmp_path / "diagnose-checkpoints")
    return BootRecoveryEngine(
        tools=tools,
        storage=_NoStorage(),
        store=store,
        checkpoints=checkpoints,
        executor=executor,
    )


async def test_boot_diagnose_bios_uefi_and_storage_failure(tmp_path: Path) -> None:
    root = _root(tmp_path / "bios-root")
    bios_sys = tmp_path / "bios-sys"
    (bios_sys / "firmware").mkdir(parents=True)
    bios_engine = _diagnostic_engine(tmp_path / "bios-engine", bios_sys)
    bios = await bios_engine.diagnose(
        BootDiagnoseInput(root_path=str(root)),
        session_id="boot-diagnose-bios",
    )
    assert bios.environment.firmware.mode is FirmwareMode.BIOS
    assert bios.environment.bootloader.kind is BootloaderKind.GRUB
    assert bios.environment.operating_systems[0].family is DistributionFamily.DEBIAN
    assert bios.environment.configuration.kernels
    assert bios.confidence > 0

    uefi_root = _root(tmp_path / "uefi-root")
    uefi_sys = tmp_path / "uefi-sys"
    (uefi_sys / "firmware/efi/efivars").mkdir(parents=True)
    uefi_engine = _diagnostic_engine(tmp_path / "uefi-engine", uefi_sys)
    uefi = await uefi_engine.diagnose(
        BootDiagnoseInput(root_path=str(uefi_root)),
        session_id="boot-diagnose-uefi",
    )
    assert uefi.environment.firmware.mode is FirmwareMode.UEFI
    assert any(item.code is BootIssueCode.ESP_NOT_MOUNTED for item in uefi.issues)

    store = BootRecoveryStore(tmp_path / "storage-failure-store")
    store.prepare()
    checkpoints = ProtectionCheckpointStore(tmp_path / "storage-failure-protection")
    checkpoints.prepare()
    tools = BootRepairToolSuite(
        runner=_Runner(),
        sys_root=bios_sys,
        test_mode=True,
        runtime_root=tmp_path / "storage-failure-runtime",
    )
    failure_engine = BootRecoveryEngine(
        tools=tools,
        storage=_FailingStorage(),
        store=store,
        checkpoints=checkpoints,
        executor=LocalTestBootExecutor(tools, tmp_path / "storage-failure-checkpoints"),
    )
    with pytest.raises(BootRecoveryEngineError, match="BOOT_STORAGE_INSPECTION_FAILED"):
        await failure_engine.diagnose(
            BootDiagnoseInput(target_disk="/fixture.img", root_path=str(root)),
            session_id="boot-storage-failure",
        )


async def test_boot_engine_plan_protection_authorization_and_execution_rejections(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path, grub=False, initramfs=False)
    engine, store, _ = _engine(tmp_path)
    diagnostic = _diagnostic(root)
    await store.put_diagnostic(diagnostic)

    with pytest.raises(BootRecoveryEngineError, match="BOOT_DIAGNOSTIC_NOT_FOUND"):
        await engine.plan(
            BootRepairPlanInput(diagnostic_id="missing-diagnostic"),
            session_id="boot-edge-session",
        )

    no_target = diagnostic.model_copy(
        update={
            "environment": diagnostic.environment.model_copy(update={"target_disk": None})
        }
    )
    await store.put_diagnostic(no_target)
    with pytest.raises(BootRecoveryEngineError, match="BOOT_TARGET_DISK_REQUIRED"):
        await engine.plan(
            BootRepairPlanInput(diagnostic_id=no_target.id),
            session_id="boot-edge-session",
        )

    plan = await engine.plan(
        BootRepairPlanInput(diagnostic_id=diagnostic.id),
        session_id="boot-edge-session",
    )
    with pytest.raises(BootRecoveryEngineError, match="BOOT_REPAIR_SESSION_MISMATCH"):
        await engine.protect(plan.id, session_id="another-session")
    with pytest.raises(BootRecoveryEngineError, match="BOOT_REPAIR_NOT_PROTECTED"):
        await engine.request_authorization(
            plan.id,
            session_id="boot-edge-session",
            on_challenge=_ignore_challenge,
        )
    with pytest.raises(BootRecoveryEngineError, match="BOOT_REPAIR_NOT_AUTHORIZED"):
        await engine.execute_authorized(
            plan.repair_id,
            session_id="boot-edge-session",
            on_stage=_ignore_stage,
        )

    expired = plan.model_copy(update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)})
    await store.put_plan(expired)
    with pytest.raises(BootRecoveryEngineError, match="BOOT_REPAIR_PLAN_EXPIRED"):
        await engine.protect(expired.id, session_id="boot-edge-session")

    await store.put_plan(plan)
    protected = await engine.protect(plan.id, session_id="boot-edge-session")
    execution = await store.get_execution(protected.repair_id)
    assert execution is not None
    await store.put_execution(execution.model_copy(update={"status": BootRepairStatus.AUTHORIZED}))
    with pytest.raises(BootRecoveryEngineError, match="BOOT_AUTHORIZATION_GRANT_UNAVAILABLE"):
        await engine.execute_authorized(
            protected.repair_id,
            session_id="boot-edge-session",
            on_stage=_ignore_stage,
        )

    await engine.mark_failed_or_unknown(protected.repair_id, "BOOT_BROKER_TIMEOUT")
    unknown = await store.get_execution(protected.repair_id)
    assert unknown is not None
    assert unknown.status is BootRepairStatus.UNKNOWN
    assert unknown.reconciliation_required is True

    await engine.mark_failed_or_unknown(protected.repair_id, "BOOT_FIXTURE_FAILURE")
    failed = await store.get_execution(protected.repair_id)
    assert failed is not None
    assert failed.status is BootRepairStatus.REPAIR_FAILED
    assert failed.reconciliation_required is False

    await engine.mark_aborted("missing-repair", code="BOOT_NOTHING_TO_ABORT")
    with pytest.raises(BootRecoveryEngineError, match="BOOT_REPAIR_NOT_FOUND"):
        await engine.reconcile_unknown("missing-repair", session_id="boot-edge-session")


async def test_boot_issue_dependency_and_minimal_operation_helpers(tmp_path: Path) -> None:
    evidence = BootEvidence(
        source="fixture",
        observation="Synthetic boot evidence for helper coverage.",
        confidence=0.9,
    )
    os_one = BootTargetOS(
        id="os:one",
        name="Debian One",
        version="13",
        family=DistributionFamily.DEBIAN,
        root_path=str(tmp_path / "one"),
    )
    os_two = os_one.model_copy(update={"id": "os:two", "name": "Debian Two"})
    broken_config = BootConfiguration(
        root_path=os_one.root_path,
        kernels=(),
        initramfs=(),
        invalid_fstab_references=("UUID=missing",),
    )
    broad = _issues(
        firmware=FirmwareMode.UEFI,
        bootloader=Bootloader(kind=BootloaderKind.UNKNOWN),
        configuration=broken_config,
        operating_systems=(os_one, os_two),
        entries=(),
        esp=None,
        evidence=(evidence,),
    )
    broad_codes = {item.code for item in broad}
    assert BootIssueCode.MULTIPLE_LINUX_INSTALLATIONS in broad_codes
    assert BootIssueCode.GRUB_MISSING in broad_codes
    assert BootIssueCode.KERNEL_MISSING in broad_codes
    assert BootIssueCode.FSTAB_REFERENCE_INVALID in broad_codes
    assert BootIssueCode.ESP_NOT_MOUNTED in broad_codes
    assert _max_severity(broad) is BootIssueSeverity.CRITICAL
    assert _diagnostic_confidence(broad, (evidence,)) > 0.8
    assert "Select which Linux" in _recommended_action(broad, [os_one, os_two])

    windows = _issues(
        firmware=FirmwareMode.BIOS,
        bootloader=Bootloader(kind=BootloaderKind.WINDOWS_BOOT_MANAGER),
        configuration=BootConfiguration(kernels=("boot/vmlinuz",), initramfs=("boot/initrd",)),
        operating_systems=(os_one,),
        entries=(),
        esp=None,
        evidence=(evidence,),
    )
    assert any(item.code is BootIssueCode.WINDOWS_BOOT_REPAIR_UNSUPPORTED for item in windows)

    systemd_boot = _issues(
        firmware=FirmwareMode.BIOS,
        bootloader=Bootloader(kind=BootloaderKind.SYSTEMD_BOOT),
        configuration=BootConfiguration(kernels=("boot/vmlinuz",), initramfs=("boot/initrd",)),
        operating_systems=(os_one,),
        entries=(),
        esp=None,
        evidence=(evidence,),
    )
    assert any(item.code is BootIssueCode.UNSUPPORTED_BOOTLOADER for item in systemd_boot)

    esp = BootPartition(
        resource_id="partition:esp",
        device_path="image:esp",
        partition_number=1,
        role="esp",
        filesystem_type="vfat",
        mount_point=str(tmp_path / "esp"),
    )
    grub = Bootloader(
        kind=BootloaderKind.GRUB,
        distribution_family=DistributionFamily.DEBIAN,
        repair_supported=True,
        efi_loader_paths=("EFI/debian/grubx64.efi",),
    )
    config = BootConfiguration(
        root_path=os_one.root_path,
        grub_config_path=str(tmp_path / "boot/grub/grub.cfg"),
        kernels=("boot/vmlinuz",),
        initramfs=("boot/initrd",),
    )
    efi_missing = _issues(
        firmware=FirmwareMode.UEFI,
        bootloader=grub,
        configuration=config,
        operating_systems=(os_one,),
        entries=(),
        esp=esp,
        evidence=(evidence,),
    )
    assert any(item.code is BootIssueCode.EFI_ENTRY_MISSING for item in efi_missing)

    diagnostic = _diagnostic(_root(tmp_path / "operation-root", grub=False, initramfs=False))
    extra_issue = BootIssue(
        code=BootIssueCode.EFI_ENTRY_MISSING,
        severity=BootIssueSeverity.HIGH,
        summary="EFI entry is missing.",
        evidence_ids=(diagnostic.evidence[0].id,),
        confidence=0.95,
        recommended_action="Create exact EFI entry.",
        repairable_automatically=True,
    )
    operations = _repair_operations(
        diagnostic.model_copy(update={"issues": (*diagnostic.issues, extra_issue)})
    )
    operation_kinds = {item.kind for item in operations}
    assert BootOperationKind.UPDATE_EFI_ENTRY in operation_kinds
    assert BootOperationKind.REGENERATE_INITRAMFS in operation_kinds
    assert BootOperationKind.VERIFY_BOOT_CHAIN in operation_kinds

    dependencies = _dependencies(
        FirmwareMode.UEFI,
        grub,
        config,
        os_one,
        esp,
        (evidence,),
    )
    assert {item.relation for item in dependencies} >= {"depends_on", "configured_by", "loads", "boots"}

    assert _select_os((os_one,), None) == os_one
    assert _select_os((os_one, os_two), "os:two") == os_two
    with pytest.raises(BootRecoveryEngineError, match="BOOT_LINUX_INSTALLATION_NOT_FOUND"):
        _select_os((), None)
    with pytest.raises(BootRecoveryEngineError, match="BOOT_TARGET_OS_REQUIRED"):
        _select_os((os_one, os_two), None)
    with pytest.raises(BootRecoveryEngineError, match="BOOT_TARGET_OS_NOT_FOUND"):
        _select_os((os_one,), "os:missing")

    assert _max_severity(()) is BootIssueSeverity.INFO
    assert _diagnostic_confidence((), ()) == 0.0
    assert "additional evidence" in _recommended_action((), [os_one])
    no_issue_entries = (
        BootEntry(number="0001", label="debian", loader_path="EFI/debian/grubx64.efi"),
    )
    no_efi_issue = _issues(
        firmware=FirmwareMode.UEFI,
        bootloader=grub,
        configuration=config,
        operating_systems=(os_one,),
        entries=no_issue_entries,
        esp=esp,
        evidence=(evidence,),
    )
    assert not any(item.code is BootIssueCode.EFI_ENTRY_MISSING for item in no_efi_issue)


async def _ignore_challenge(challenge_id: str) -> None:
    del challenge_id


async def _ignore_stage(name: str, payload: dict[str, object]) -> None:
    del name, payload
