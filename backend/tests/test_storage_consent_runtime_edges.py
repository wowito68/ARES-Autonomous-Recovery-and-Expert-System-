from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ares.audit import AuditLedgerError, AuditReceipt, MemoryAuditLedger
from ares.runtime.broker import _prepare_socket_path as prepare_broker_socket
from ares.runtime.broker import _safe_code as broker_safe_code
from ares.runtime.broker import _token as broker_token
from ares.runtime.consent import (
    ConsentAuthority,
    UnixConsentClient,
    UnixConsentOperatorClient,
    _event_prefix,
    _prepare_socket_path,
    _request,
    _safe_error,
)
from ares.storage_operations.executor import StorageExecutorError
from ares.tools.backup import BackupToolError
from ares.tools.filesystem import FilesystemToolError
from ares.tools.partition import PartitionToolError
from tests.test_storage_operation_runtime import _validated_create


class _FailingAudit:
    async def append(
        self,
        *,
        event_type: str,
        source: str,
        correlation_id: str,
        session_id: str,
        payload: dict[str, Any],
    ) -> AuditReceipt:
        del event_type, source, correlation_id, session_id, payload
        raise AuditLedgerError("fixture audit unavailable")


async def test_storage_consent_requires_independent_peer_exact_phrase_and_one_decision(
    tmp_path: Path,
) -> None:
    image = tmp_path / "consent-storage.img"
    with image.open("wb") as handle:
        handle.truncate(128 * 1024 * 1024)
    _, _, _, _, _, plan = await _validated_create(tmp_path / "state", image)
    audit = MemoryAuditLedger()
    authority = ConsentAuthority(audit, broker_uid=41, operator_uid=42)

    with pytest.raises(PermissionError):
        await authority.dispatch(
            {"action": "storage.create", "plan": plan.model_dump(mode="json")},
            42,
        )

    created = await authority.dispatch(
        {"action": "storage.create", "plan": plan.model_dump(mode="json")},
        41,
    )
    challenge_id = created["challenge_id"]
    assert created["kind"] == "storage_operation"
    assert created["operation_id"] == plan.operation_id
    assert created["target_fingerprint"] == plan.target_disk.fingerprint_sha256
    assert created["checkpoint_id"] == plan.protection_checkpoint.id
    assert created["physical_disk_writes_enabled"] is False
    assert _event_prefix(authority._challenges[challenge_id]) == "storage"

    with pytest.raises(PermissionError):
        await authority.dispatch({"action": "get", "challenge_id": challenge_id}, 41)
    visible = await authority.dispatch({"action": "get", "challenge_id": challenge_id}, 42)
    assert visible["decision"] == "pending"

    with pytest.raises(ValueError, match="exact confirmation phrase required"):
        await authority.dispatch(
            {
                "action": "approve",
                "challenge_id": challenge_id,
                "confirmation": "not the exact phrase",
            },
            42,
        )
    approved = await authority.dispatch(
        {
            "action": "approve",
            "challenge_id": challenge_id,
            "confirmation": created["confirmation_phrase"],
        },
        42,
    )
    assert approved["decision"] == "approved"
    assert approved["operator_uid"] == 42
    waited = await authority.dispatch({"action": "wait", "challenge_id": challenge_id}, 41)
    assert waited["decision"] == "approved"

    with pytest.raises(ValueError, match="challenge is not pending"):
        await authority.dispatch({"action": "deny", "challenge_id": challenge_id}, 42)
    with pytest.raises(ValueError, match="invalid challenge"):
        await authority.dispatch({"action": "get", "challenge_id": 3}, 42)
    with pytest.raises(ValueError, match="challenge not found"):
        await authority.dispatch({"action": "get", "challenge_id": "missing-challenge"}, 42)
    with pytest.raises(ValueError, match="unsupported consent action"):
        await authority.dispatch({"action": "unsupported"}, 42)

    events = {item["event_type"] for item in audit.records}
    assert "storage.authorization.requested" in events
    assert "storage.authorization.approved" in events


async def test_storage_consent_denial_expiration_and_audit_failure(tmp_path: Path) -> None:
    image = tmp_path / "consent-errors.img"
    with image.open("wb") as handle:
        handle.truncate(128 * 1024 * 1024)
    _, _, _, _, _, plan = await _validated_create(tmp_path / "state", image)
    authority = ConsentAuthority(MemoryAuditLedger(), broker_uid=1, operator_uid=2)

    denied_challenge = await authority.dispatch(
        {"action": "storage.create", "plan": plan.model_dump(mode="json")}, 1
    )
    denied = await authority.dispatch(
        {"action": "deny", "challenge_id": denied_challenge["challenge_id"]}, 2
    )
    assert denied["decision"] == "denied"
    assert _event_prefix(authority._challenges[denied_challenge["challenge_id"]]) == "storage"

    expired_challenge = await authority.dispatch(
        {"action": "storage.create", "plan": plan.model_dump(mode="json")}, 1
    )
    challenge = authority._challenges[expired_challenge["challenge_id"]]
    challenge.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(ValueError, match="authorization expired"):
        await authority.dispatch({"action": "wait", "challenge_id": challenge.id}, 1)
    assert challenge.decision == "expired"

    audit_failure = await authority.dispatch(
        {"action": "storage.create", "plan": plan.model_dump(mode="json")}, 1
    )
    authority.audit = _FailingAudit()
    with pytest.raises(ValueError, match="audit unavailable"):
        await authority.dispatch(
            {
                "action": "approve",
                "challenge_id": audit_failure["challenge_id"],
                "confirmation": audit_failure["confirmation_phrase"],
            },
            2,
        )


async def test_unix_consent_clients_cover_storage_wait_and_operator_protocol(
    tmp_path: Path,
) -> None:
    socket_path = tmp_path / "consent.sock"
    actions: list[str] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = json.loads(await reader.readline())
        action = request["action"]
        actions.append(action)
        payload: dict[str, Any]
        if action == "storage.create":
            payload = {"challenge_id": "unix-storage-challenge", "decision": "pending"}
        elif action == "wait":
            payload = {"challenge_id": request["challenge_id"], "decision": "approved"}
        elif action == "get":
            payload = {
                "challenge_id": request["challenge_id"],
                "decision": "pending",
                "confirmation_phrase": "APPROVE fixture",
            }
        elif action == "approve":
            payload = {"challenge_id": request["challenge_id"], "decision": "approved"}
        elif action == "deny":
            payload = {"challenge_id": request["challenge_id"], "decision": "denied"}
        else:
            payload = {}
        writer.write(json.dumps({"ok": True, "payload": payload}).encode() + b"\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_unix_server(handle, path=str(socket_path))
    async with server:
        image = tmp_path / "unix-consent.img"
        with image.open("wb") as handle_file:
            handle_file.truncate(128 * 1024 * 1024)
        _, _, _, _, _, plan = await _validated_create(tmp_path / "state", image)
        broker_client = UnixConsentClient(socket_path)
        created = await broker_client.request_storage(plan)
        assert created["challenge_id"] == "unix-storage-challenge"
        waited = await broker_client.wait(created["challenge_id"], timeout_seconds=1)
        assert waited["decision"] == "approved"

        operator = UnixConsentOperatorClient(socket_path)
        visible = await operator.get(created["challenge_id"])
        assert visible["decision"] == "pending"
        approved = await operator.approve(created["challenge_id"], "APPROVE fixture")
        denied = await operator.deny(created["challenge_id"])
        assert approved["decision"] == "approved"
        assert denied["decision"] == "denied"

    assert actions == ["storage.create", "wait", "get", "approve", "deny"]


async def test_consent_request_rejects_socket_error_bad_response_error_and_payload(
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeError, match="consent agent unavailable"):
        await _request(
            tmp_path / "missing.sock",
            {"action": "get"},
            timeout_seconds=0.1,
        )

    async def exercise_response(name: str, response: bytes, match: str) -> None:
        socket_path = tmp_path / name

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await reader.readline()
            writer.write(response)
            await writer.drain()
            writer.close()

        server = await asyncio.start_unix_server(handle, path=str(socket_path))
        async with server:
            with pytest.raises(RuntimeError, match=match):
                await _request(
                    socket_path,
                    {"action": "get", "challenge_id": "fixture"},
                    timeout_seconds=1,
                )

    await exercise_response("empty.sock", b"", "invalid consent response")
    await exercise_response(
        "error.sock",
        json.dumps({"ok": False, "code": "CONSENT_FORBIDDEN"}).encode() + b"\n",
        "CONSENT_FORBIDDEN",
    )
    await exercise_response(
        "payload.sock",
        json.dumps({"ok": True, "payload": "bad"}).encode() + b"\n",
        "invalid consent response",
    )


async def test_runtime_socket_helpers_and_safe_error_mappings(tmp_path: Path) -> None:
    consent_path = tmp_path / "consent" / "agent.sock"
    consent_path.parent.mkdir()
    consent_path.write_text("old", encoding="utf-8")
    _prepare_socket_path(consent_path)
    assert consent_path.parent.is_dir() and not consent_path.exists()

    broker_path = tmp_path / "broker" / "broker.sock"
    prepare_broker_socket(broker_path)
    assert broker_path.parent.is_dir() and not broker_path.exists()

    assert _safe_error(ValueError("authorization expired")) == "AUTHORIZATION_EXPIRED"
    assert _safe_error(ValueError("challenge not found")) == "AUTHORIZATION_NOT_FOUND"
    assert _safe_error(ValueError("challenge is not pending")) == "AUTHORIZATION_NOT_PENDING"
    assert (
        _safe_error(ValueError("exact confirmation phrase required"))
        == "AUTHORIZATION_CONFIRMATION_INVALID"
    )
    assert _safe_error(ValueError("audit unavailable")) == "AUDIT_LEDGER_UNAVAILABLE"
    assert _safe_error(ValueError("other")) == "AUTHORIZATION_FAILED"

    assert broker_safe_code(BackupToolError("BACKUP_TEST")) == "BACKUP_TEST"
    assert broker_safe_code(FilesystemToolError("FILESYSTEM_TEST")) == "FILESYSTEM_TEST"
    assert broker_safe_code(PartitionToolError("STORAGE_TEST")) == "STORAGE_TEST"
    assert broker_safe_code(AuditLedgerError("audit")) == "AUDIT_LEDGER_UNAVAILABLE"
    assert broker_safe_code(asyncio.CancelledError()) == "BROKER_CANCELLED"
    assert broker_safe_code(StorageExecutorError("not directly broker-coded")) == "BROKER_FAILED"
    assert len(broker_token("sensitive-relative-path")) == 24
    assert broker_token("same") == broker_token("same")
