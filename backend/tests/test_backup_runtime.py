from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ares.audit import AuditLedgerError, AuditWriter, MemoryAuditLedger, UnixAuditLedgerClient
from ares.backup import (
    Backup,
    BackupExecution,
    BackupProgress,
    BackupStatus,
    BackupStore,
    BackupVerificationStatus,
    LocalTestBackupExecutor,
    UnixBrokerBackupExecutor,
)
from ares.backup.executor import BackupExecutorError
from ares.backup.models import BackupPolicy
from ares.runtime.broker import BackupBroker
from ares.runtime.consent import ConsentAuthority
from ares.tools.backup import BackupFilesystemTools, BackupToolError


def _fixture_plan(tmp_path: Path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    (source / "file.txt").write_text("backup data", encoding="utf-8")
    device = os.stat(source).st_dev
    source_device = f"{os.major(device)}:{os.minor(device)}"
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        f"36 25 {source_device} / {source} rw - ext4 /dev/sda1 rw\n"
        f"37 25 99:99 / {destination} rw - ext4 /dev/sdb1 rw\n",
        encoding="utf-8",
    )
    tools = BackupFilesystemTools(mountinfo)
    return tools, tools.build_plan(str(source), str(destination), BackupPolicy())


def _backup(plan) -> Backup:
    progress = BackupProgress(
        files_completed=plan.included_file_count,
        files_total=plan.included_file_count,
        bytes_completed=plan.source.estimated_size_bytes,
        bytes_total=plan.source.estimated_size_bytes,
        percent=100,
        eta_seconds=0,
    )
    return Backup(
        id=plan.backup_id,
        created_at=datetime.now(UTC),
        source=plan.source,
        destination=plan.destination,
        size=plan.source.estimated_size_bytes,
        file_count=plan.included_file_count,
        status=BackupStatus.VERIFYING,
        created_by="test",
        session_id="runtime-session-123",
        plan_id=plan.id,
        execution=BackupExecution(
            backup_id=plan.backup_id,
            status=BackupStatus.VERIFYING,
            progress=progress,
        ),
    )


async def test_local_executor_requires_and_consumes_exact_one_use_grant(tmp_path: Path) -> None:
    tools, plan = _fixture_plan(tmp_path)
    executor = LocalTestBackupExecutor(tools)
    challenges: list[str] = []

    async def challenge(value: str) -> None:
        challenges.append(value)

    grant = await executor.request_authorization(
        plan, session_id="runtime-session-123", on_challenge=challenge
    )

    async def noop(_) -> None:
        return None

    manifest = await executor.create(plan, grant, on_progress=noop, on_entry=noop)
    assert challenges and grant.session_id == "runtime-session-123"
    assert manifest.file_count == 1
    with pytest.raises(BackupExecutorError, match="BACKUP_AUTHORIZATION_INVALID"):
        await executor.create(plan, grant, on_progress=noop, on_entry=noop)


async def test_local_executor_can_deny_authorization(tmp_path: Path) -> None:
    tools, plan = _fixture_plan(tmp_path)
    executor = LocalTestBackupExecutor(tools, authorize=False)

    async def noop(_: str) -> None:
        return None

    with pytest.raises(BackupExecutorError, match="BACKUP_AUTHORIZATION_DENIED"):
        await executor.request_authorization(
            plan, session_id="runtime-session-123", on_challenge=noop
        )


async def test_consent_authority_requires_exact_phrase_and_independent_uids(tmp_path: Path) -> None:
    _, plan = _fixture_plan(tmp_path)
    audit = MemoryAuditLedger()
    authority = ConsentAuthority(audit, broker_uid=41, operator_uid=42)
    created = await authority.dispatch(
        {"action": "create", "plan": plan.model_dump(mode="json"), "session_id": "consent-session-123"},
        41,
    )
    challenge_id = created["challenge_id"]
    assert created["source"] == plan.source.path
    assert created["destination"] == plan.destination.backup_path
    assert created["required_bytes"] == plan.required_bytes
    assert created["available_bytes"] == plan.destination.available_bytes
    assert created["risk"] == "medium"

    with pytest.raises(PermissionError):
        await authority.dispatch({"action": "get", "challenge_id": challenge_id}, 41)
    with pytest.raises(ValueError, match="exact confirmation"):
        await authority.dispatch(
            {"action": "approve", "challenge_id": challenge_id, "confirmation": "wrong"}, 42
        )

    phrase = created["confirmation_phrase"]
    approved = await authority.dispatch(
        {"action": "approve", "challenge_id": challenge_id, "confirmation": phrase}, 42
    )
    waited = await authority.dispatch({"action": "wait", "challenge_id": challenge_id}, 41)
    assert approved["decision"] == "approved"
    assert waited["decision"] == "approved"
    assert {record["event_type"] for record in audit.records} >= {
        "backup.authorization.requested",
        "backup.authorization.approved",
    }


async def test_consent_authority_can_deny(tmp_path: Path) -> None:
    _, plan = _fixture_plan(tmp_path)
    authority = ConsentAuthority(MemoryAuditLedger(), broker_uid=1, operator_uid=2)
    created = await authority.dispatch(
        {"action": "create", "plan": plan.model_dump(mode="json"), "session_id": "consent-session-456"},
        1,
    )
    denied = await authority.dispatch(
        {"action": "deny", "challenge_id": created["challenge_id"]}, 2
    )
    assert denied["decision"] == "denied"


async def test_audit_writer_chains_records_and_detects_tampering(tmp_path: Path) -> None:
    directory = tmp_path / "audit"
    writer = AuditWriter(directory)
    writer.prepare()
    first = await writer.append(
        {
            "event_type": "backup.test",
            "source": "test-suite",
            "correlation_id": "correlation-123",
            "session_id": "session-audit-123",
            "payload": {"password": "secret", "count": 1},
        },
        1000,
    )
    second = await writer.append(
        {
            "event_type": "backup.test2",
            "source": "test-suite",
            "correlation_id": "correlation-456",
            "session_id": "session-audit-123",
            "payload": {"result": "ok"},
        },
        1000,
    )
    assert first.sequence == 1
    assert second.sequence == 2
    lines = (directory / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["payload"]["password"] == "[REDACTED]"

    reloaded = AuditWriter(directory)
    reloaded.prepare()
    record = json.loads(lines[0])
    record["payload"]["count"] = 999
    (directory / "ledger.jsonl").write_text(
        json.dumps(record) + "\n" + lines[1] + "\n", encoding="utf-8"
    )
    with pytest.raises(OSError, match="MAC invalid"):
        AuditWriter(directory).prepare()


async def test_missing_runtime_sockets_fail_closed(tmp_path: Path) -> None:
    audit = UnixAuditLedgerClient(tmp_path / "missing-audit.sock")
    with pytest.raises(AuditLedgerError, match="unavailable"):
        await audit.append(
            event_type="backup.test",
            source="test-suite",
            correlation_id="correlation-123",
            session_id="runtime-session-123",
            payload={},
        )

    executor = UnixBrokerBackupExecutor(tmp_path / "missing-broker.sock")
    _, plan = _fixture_plan(tmp_path / "fixture")

    async def challenge(_: str) -> None:
        return None

    with pytest.raises(BackupExecutorError, match="BACKUP_BROKER_UNAVAILABLE"):
        await executor.request_authorization(
            plan, session_id="runtime-session-123", on_challenge=challenge
        )


class _ApprovedConsent:
    def __init__(self) -> None:
        self.challenge_id = "challenge-12345678"

    async def request(self, plan, session_id: str) -> dict[str, Any]:
        del plan, session_id
        return {"challenge_id": self.challenge_id}

    async def wait(
        self, challenge_id: str, timeout_seconds: float = 600.0
    ) -> dict[str, Any]:
        del timeout_seconds
        assert challenge_id == self.challenge_id
        return {"decision": "approved"}


async def test_broker_revalidates_audits_and_consumes_grant(tmp_path: Path) -> None:
    tools, plan = _fixture_plan(tmp_path)
    audit = MemoryAuditLedger()
    broker = BackupBroker(
        tools,
        audit,
        _ApprovedConsent(),
        allowed_client_uids=frozenset({77}),
        emergency_journal=tmp_path / "reconciliation.jsonl",
    )
    messages: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    grant_payload = await broker.dispatch(
        {"action": "authorize", "plan": plan.model_dump(mode="json"), "session_id": "broker-session-123"},
        77,
        send,
    )
    assert messages[0]["type"] == "authorization_requested"
    assert grant_payload["session_id"] == "broker-session-123"

    manifest_payload = await broker.dispatch(
        {"action": "create", "plan": plan.model_dump(mode="json"), "grant": grant_payload},
        77,
        send,
    )
    assert manifest_payload["file_count"] == 1
    assert any(message["type"] == "progress" for message in messages)
    assert {record["event_type"] for record in audit.records} >= {
        "backup.authorization.intent",
        "backup.authorization.granted",
        "backup.execution.intent",
        "backup.execution.completed",
    }
    with pytest.raises(BackupToolError, match="BACKUP_AUTHORIZATION_INVALID"):
        await broker.dispatch(
            {"action": "create", "plan": plan.model_dump(mode="json"), "grant": grant_payload},
            77,
            send,
        )
    with pytest.raises(PermissionError):
        await broker.dispatch({"action": "verify"}, 999, send)


async def test_broker_verifies_completed_backup(tmp_path: Path) -> None:
    tools, plan = _fixture_plan(tmp_path)
    audit = MemoryAuditLedger()
    broker = BackupBroker(
        tools,
        audit,
        _ApprovedConsent(),
        allowed_client_uids=frozenset({7}),
        emergency_journal=tmp_path / "reconciliation.jsonl",
    )

    async def send(_: dict[str, Any]) -> None:
        return None

    grant = await broker.dispatch(
        {"action": "authorize", "plan": plan.model_dump(mode="json"), "session_id": "broker-session-456"},
        7,
        send,
    )
    manifest = await broker.dispatch(
        {"action": "create", "plan": plan.model_dump(mode="json"), "grant": grant}, 7, send
    )
    verification = await broker.dispatch(
        {
            "action": "verify",
            "backup": _backup(plan).model_dump(mode="json"),
            "manifest": manifest,
        },
        7,
        send,
    )
    assert verification["status"] == "VERIFIED"


async def test_backup_store_reconciles_interrupted_record(tmp_path: Path) -> None:
    tools, plan = _fixture_plan(tmp_path)
    store = BackupStore(tmp_path / "store")
    store.prepare()
    progress = BackupProgress(
        files_completed=0,
        files_total=plan.included_file_count,
        bytes_completed=0,
        bytes_total=plan.source.estimated_size_bytes,
        percent=0,
    )
    backup = Backup(
        id=plan.backup_id,
        created_at=datetime.now(UTC),
        source=plan.source,
        destination=plan.destination,
        size=0,
        file_count=0,
        status=BackupStatus.RUNNING,
        verification_status=BackupVerificationStatus.RUNNING,
        created_by="test",
        session_id="store-session-123",
        plan_id=plan.id,
        execution=BackupExecution(
            backup_id=plan.backup_id,
            status=BackupStatus.RUNNING,
            progress=progress,
        ),
    )
    await store.put_backup(backup)

    reloaded = BackupStore(tmp_path / "store")
    reloaded.prepare()
    reconciled = await reloaded.get_backup(plan.backup_id)
    assert reconciled is not None
    assert reconciled.status is BackupStatus.FAILED
    assert reconciled.execution.error_code == "BACKUP_INTERRUPTED"
