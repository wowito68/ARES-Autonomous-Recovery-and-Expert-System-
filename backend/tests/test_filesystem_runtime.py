from __future__ import annotations

from collections import defaultdict, deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ares.audit import MemoryAuditLedger
from ares.backup.models import (
    Backup,
    BackupDestination,
    BackupDestinationKind,
    BackupExecution,
    BackupManifest,
    BackupProgress,
    BackupSource,
    BackupStatus,
    BackupVerification,
    BackupVerificationStatus,
)
from ares.backup.store import BackupStore
from ares.filesystems.integrity import repair_plan_fingerprint
from ares.filesystems.models import (
    FilesystemRepairPlan,
    FilesystemType,
    MountSafetyReport,
    RepairAction,
)
from ares.protection import (
    ProtectionCheckpoint,
    ProtectionCheckpointError,
    ProtectionCheckpointService,
    ProtectionCheckpointStatus,
    ProtectionCheckpointStore,
)
from ares.runtime.consent import ConsentAuthority
from ares.runtime.filesystem_broker import FilesystemBroker
from ares.tools.filesystem import FilesystemToolError, FilesystemToolSuite, MountSafetyChecker
from ares.tools.storage import ProcessResult, ToolAvailability


class FakeRunner:
    def __init__(self) -> None:
        self.available: dict[str, bool] = defaultdict(lambda: True)
        self.results: dict[str, deque[ProcessResult | BaseException]] = defaultdict(deque)

    def inspect(self, tool: str) -> ToolAvailability:
        available = self.available[tool]
        return ToolAvailability(
            tool=tool,
            available=available,
            reason=None if available else "tool_not_installed",
        )

    async def run(
        self,
        tool: str,
        args: tuple[str, ...],
        *,
        timeout_seconds: float,
    ) -> ProcessResult:
        del args, timeout_seconds
        if not self.available[tool]:
            raise FileNotFoundError(tool)
        if self.results[tool]:
            value = self.results[tool].popleft()
            if isinstance(value, BaseException):
                raise value
            return value
        if tool == "blkid":
            return _result(
                "blkid",
                0,
                "TYPE=ext4\nUUID=11111111-2222-3333-4444-555555555555\n",
            )
        return _result(tool, 0)


def _result(tool: str, exit_code: int, stdout: str = "") -> ProcessResult:
    return ProcessResult(
        tool=tool,
        exit_code=exit_code,
        stdout=stdout,
        stderr="",
        duration_ms=10.0,
    )


async def _identity_and_plan(
    tmp_path: Path,
    checkpoint_store: ProtectionCheckpointStore,
    *,
    runner: FakeRunner | None = None,
) -> tuple[FilesystemToolSuite, FilesystemRepairPlan, ProtectionCheckpoint]:
    image = tmp_path / "repair.img"
    image.write_bytes(b"ARES filesystem repair fixture" * 1024)
    selected_runner = runner or FakeRunner()
    tools = FilesystemToolSuite(
        runner=selected_runner,
        mount_checker=MountSafetyChecker(scan_processes=False),
        allow_regular_file_targets=True,
    )
    identity = await tools.identify(str(image))
    resource_id = f"filesystem:{identity.fingerprint_sha256}"
    checkpoint = ProtectionCheckpoint(
        status=ProtectionCheckpointStatus.READY,
        protected_resources=(resource_id,),
        resource_fingerprints={resource_id: identity.fingerprint_sha256},
        provider_capability_id="backup.create",
        backup_id="backup-12345678",
        verification_id="verification-12345678",
        session_id="filesystem-session-123",
        evidence_sha256="b" * 64,
    )
    await checkpoint_store.put(checkpoint)
    mount = MountSafetyReport(
        mounted=False,
        busy=False,
        swap=False,
        safe_to_unmount=False,
        safe_to_remount=False,
    )
    draft = FilesystemRepairPlan(
        session_id="filesystem-session-123",
        target=identity,
        filesystem=FilesystemType.EXT4,
        mount=mount,
        detected_problems=("filesystem_errors_detected",),
        required_tools=(),
        required_permissions=("block-device.readwrite",),
        protection_checkpoint=checkpoint,
        repair_actions=(
            RepairAction(
                id="filesystem.repair",
                description="Repair isolated filesystem image",
                mutates_target=True,
            ),
        ),
        verification_steps=("Read-only post-repair check",),
        rollback_strategy="Verified file-level checkpoint only.",
        executable=True,
        fingerprint_sha256="0" * 64,
    )
    plan = draft.model_copy(update={"fingerprint_sha256": repair_plan_fingerprint(draft)})
    return tools, plan, checkpoint


def _backup_objects(
    tmp_path: Path, *, source_path: Path, mount_point: Path, device_id: str
) -> tuple[Backup, BackupManifest, BackupVerification]:
    source = BackupSource(
        path=str(source_path),
        device_id=device_id,
        mount_point=str(mount_point),
        filesystem_type="ext4",
        estimated_size_bytes=4,
        estimated_file_count=1,
        estimated_directory_count=0,
    )
    destination = BackupDestination(
        root_path=str(tmp_path / "destination"),
        backup_path=str(tmp_path / "destination" / "ARES" / "backup-12345678"),
        device_id="99:99",
        mount_point=str(tmp_path / "destination"),
        filesystem_type="ext4",
        kind=BackupDestinationKind.MOUNTED_EXTERNAL_DISK,
        available_bytes=1_000_000,
    )
    progress = BackupProgress(
        files_completed=1,
        files_total=1,
        bytes_completed=4,
        bytes_total=4,
        percent=100,
        speed_bytes_per_second=1,
        eta_seconds=0,
    )
    backup = Backup(
        id="backup-12345678",
        created_at=datetime.now(UTC),
        source=source,
        destination=destination,
        size=4,
        file_count=1,
        status=BackupStatus.COMPLETED,
        checksum="c" * 64,
        verification_status=BackupVerificationStatus.VERIFIED,
        created_by="test",
        session_id="filesystem-session-123",
        plan_id="plan-12345678",
        execution=BackupExecution(
            backup_id="backup-12345678",
            status=BackupStatus.COMPLETED,
            progress=progress,
        ),
    )
    manifest = BackupManifest(
        backup_id=backup.id,
        source=source,
        entries=(),
        file_count=1,
        directory_count=0,
        total_size_bytes=4,
        manifest_checksum_sha256="d" * 64,
    )
    verification = BackupVerification(
        backup_id=backup.id,
        status=BackupVerificationStatus.VERIFIED,
        expected_file_count=1,
        verified_file_count=1,
        expected_size_bytes=4,
        verified_size_bytes=4,
        manifest_valid=True,
        message="verified test backup",
    )
    return backup, manifest, verification


async def test_checkpoint_service_requires_verified_full_filesystem_backup(tmp_path: Path) -> None:
    backup_store = BackupStore(tmp_path / "backups")
    checkpoint_store = ProtectionCheckpointStore(tmp_path / "checkpoints")
    backup_store.prepare()
    checkpoint_store.prepare()
    source = tmp_path / "source"
    source.mkdir()
    backup, manifest, verification = _backup_objects(
        tmp_path,
        source_path=source,
        mount_point=source,
        device_id="8:1",
    )
    await backup_store.put_backup(backup)
    await backup_store.put_manifest(manifest)
    await backup_store.put_verification(verification)
    service = ProtectionCheckpointService(backup_store, checkpoint_store)

    checkpoint = await service.from_backup(
        backup_id=backup.id,
        resource_id="filesystem:" + "a" * 64,
        resource_fingerprint_sha256="a" * 64,
        expected_device_id="8:1",
        session_id="filesystem-session-123",
    )

    assert checkpoint.status is ProtectionCheckpointStatus.READY
    assert checkpoint.backup_id == backup.id
    assert checkpoint.verification_id == verification.id
    assert await checkpoint_store.get(checkpoint.id) == checkpoint


async def test_checkpoint_service_rejects_partial_backup_and_other_device(tmp_path: Path) -> None:
    backup_store = BackupStore(tmp_path / "backups")
    checkpoint_store = ProtectionCheckpointStore(tmp_path / "checkpoints")
    backup_store.prepare()
    checkpoint_store.prepare()
    mount = tmp_path / "source"
    partial = mount / "Documents"
    partial.mkdir(parents=True)
    backup, manifest, verification = _backup_objects(
        tmp_path,
        source_path=partial,
        mount_point=mount,
        device_id="8:1",
    )
    await backup_store.put_backup(backup)
    await backup_store.put_manifest(manifest)
    await backup_store.put_verification(verification)
    service = ProtectionCheckpointService(backup_store, checkpoint_store)

    with pytest.raises(ProtectionCheckpointError, match="PROTECTION_BACKUP_NOT_FULL_FILESYSTEM"):
        await service.from_backup(
            backup_id=backup.id,
            resource_id="filesystem:" + "a" * 64,
            resource_fingerprint_sha256="a" * 64,
            expected_device_id="8:1",
            session_id="filesystem-session-123",
        )

    full_backup, full_manifest, full_verification = _backup_objects(
        tmp_path,
        source_path=mount,
        mount_point=mount,
        device_id="8:1",
    )
    await backup_store.put_backup(full_backup)
    await backup_store.put_manifest(full_manifest)
    await backup_store.put_verification(full_verification)
    with pytest.raises(ProtectionCheckpointError, match="PROTECTION_BACKUP_TARGET_MISMATCH"):
        await service.from_backup(
            backup_id=full_backup.id,
            resource_id="filesystem:" + "a" * 64,
            resource_fingerprint_sha256="a" * 64,
            expected_device_id="8:2",
            session_id="filesystem-session-123",
        )


async def test_consent_authority_requires_exact_filesystem_phrase_and_operator_uid(
    tmp_path: Path,
) -> None:
    checkpoint_store = ProtectionCheckpointStore(tmp_path / "checkpoints")
    checkpoint_store.prepare()
    _, plan, _ = await _identity_and_plan(tmp_path, checkpoint_store)
    audit = MemoryAuditLedger()
    authority = ConsentAuthority(audit, broker_uid=41, operator_uid=42)

    challenge = await authority.dispatch(
        {"action": "filesystem.create", "plan": plan.model_dump(mode="json")},
        41,
    )

    phrase = challenge["confirmation_phrase"]
    assert phrase.startswith("I understand that this operation modifies the filesystem. APPROVE ")
    assert challenge["target_fingerprint"] == plan.target.fingerprint_sha256
    with pytest.raises(ValueError, match="exact confirmation phrase required"):
        await authority.dispatch(
            {
                "action": "approve",
                "challenge_id": challenge["challenge_id"],
                "confirmation": "APPROVE",
            },
            42,
        )
    approved = await authority.dispatch(
        {
            "action": "approve",
            "challenge_id": challenge["challenge_id"],
            "confirmation": phrase,
        },
        42,
    )
    waited = await authority.dispatch(
        {"action": "wait", "challenge_id": challenge["challenge_id"]}, 41
    )
    assert approved["decision"] == "approved"
    assert waited["operator_uid"] == 42
    assert {record["event_type"] for record in audit.records} >= {
        "repair.authorization.requested",
        "repair.authorization.approved",
    }


class ApprovedConsent:
    async def request_filesystem(self, plan: FilesystemRepairPlan) -> dict[str, Any]:
        return {"challenge_id": f"challenge-{plan.id[:8]}"}

    async def wait(self, challenge_id: str, timeout_seconds: float = 600.0) -> dict[str, Any]:
        del timeout_seconds
        return {
            "challenge_id": challenge_id,
            "decision": "approved",
            "operator_uid": 42,
        }


async def test_broker_consumes_one_use_grant_and_audits_exact_plan(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.results["e2fsck"].extend(
        (_result("e2fsck", 4), _result("e2fsck", 1), _result("e2fsck", 0))
    )
    checkpoint_store = ProtectionCheckpointStore(tmp_path / "checkpoints")
    checkpoint_store.prepare()
    tools, plan, _ = await _identity_and_plan(tmp_path, checkpoint_store, runner=runner)
    audit = MemoryAuditLedger()
    broker = FilesystemBroker(
        tools,
        audit,
        ApprovedConsent(),
        checkpoint_store,
        allowed_client_uids=frozenset({7}),
        emergency_journal=tmp_path / "reconciliation.jsonl",
    )
    messages: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    grant_payload = await broker.dispatch(
        {"action": "filesystem.authorize", "plan": plan.model_dump(mode="json")},
        7,
        send,
    )
    result = await broker.dispatch(
        {
            "action": "filesystem.execute",
            "plan": plan.model_dump(mode="json"),
            "grant": grant_payload,
        },
        7,
        send,
    )

    assert result["after"]["health"] == "HEALTHY"
    assert any(message.get("type") == "authorization_requested" for message in messages)
    assert any(message.get("name") == "filesystem.repair-command.started" for message in messages)
    event_names = [record["event_type"] for record in audit.records]
    assert "repair.execution.intent" in event_names
    assert "repair.execution.completed" in event_names
    assert event_names.index("repair.execution.intent") < event_names.index(
        "filesystem.repair-command.started"
    )
    with pytest.raises(FilesystemToolError, match="FILESYSTEM_AUTHORIZATION_INVALID"):
        await broker.dispatch(
            {
                "action": "filesystem.execute",
                "plan": plan.model_dump(mode="json"),
                "grant": grant_payload,
            },
            7,
            send,
        )


async def test_broker_rejects_tampered_plan_and_fabricated_checkpoint(tmp_path: Path) -> None:
    checkpoint_store = ProtectionCheckpointStore(tmp_path / "checkpoints")
    checkpoint_store.prepare()
    tools, plan, checkpoint = await _identity_and_plan(tmp_path, checkpoint_store)
    broker = FilesystemBroker(
        tools,
        MemoryAuditLedger(),
        ApprovedConsent(),
        checkpoint_store,
        allowed_client_uids=frozenset({7}),
    )

    async def send(message: dict[str, Any]) -> None:
        del message

    tampered = plan.model_copy(update={"limitations": ("tampered",)})
    with pytest.raises(FilesystemToolError, match="FILESYSTEM_PROTECTION_CHECKPOINT_INVALID"):
        await broker.dispatch(
            {"action": "filesystem.authorize", "plan": tampered.model_dump(mode="json")},
            7,
            send,
        )

    fake_checkpoint = checkpoint.model_copy(update={"id": "fake-checkpoint-12345678"})
    fake_draft = plan.model_copy(
        update={
            "protection_checkpoint": fake_checkpoint,
            "fingerprint_sha256": "0" * 64,
        }
    )
    fake = fake_draft.model_copy(update={"fingerprint_sha256": repair_plan_fingerprint(fake_draft)})
    with pytest.raises(FilesystemToolError, match="FILESYSTEM_PROTECTION_CHECKPOINT_INVALID"):
        await broker.dispatch(
            {"action": "filesystem.authorize", "plan": fake.model_dump(mode="json")},
            7,
            send,
        )


async def test_broker_rejects_wrong_peer_before_any_repair(tmp_path: Path) -> None:
    checkpoint_store = ProtectionCheckpointStore(tmp_path / "checkpoints")
    checkpoint_store.prepare()
    tools, plan, _ = await _identity_and_plan(tmp_path, checkpoint_store)
    broker = FilesystemBroker(
        tools,
        MemoryAuditLedger(),
        ApprovedConsent(),
        checkpoint_store,
        allowed_client_uids=frozenset({7}),
    )

    async def send(message: dict[str, Any]) -> None:
        del message

    with pytest.raises(PermissionError):
        await broker.dispatch(
            {"action": "filesystem.authorize", "plan": plan.model_dump(mode="json")},
            8,
            send,
        )
