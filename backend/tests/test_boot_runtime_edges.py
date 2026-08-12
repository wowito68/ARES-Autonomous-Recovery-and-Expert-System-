from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from ares.audit import AuditLedgerError, AuditReceipt, MemoryAuditLedger
from ares.boot.engine import BootRecoveryEngine
from ares.boot.executor import BootExecutorError, UnixBrokerBootExecutor
from ares.boot.models import BootRepairPlan, BootRepairPlanInput
from ares.boot.store import BootRecoveryStore
from ares.runtime.boot_broker import BootBroker
from ares.storage_operations.models import StorageDeviceIdentity
from ares.tools.boot import BootToolError
from ares.tools.partition import DiskIdentityTool
from tests.test_boot_engine import _diagnostic, _engine
from tests.test_boot_tools import _root
from tests.test_storage_partition_edges import _identity


class _IdentityFixture(DiskIdentityTool):
    def __init__(self, identity: StorageDeviceIdentity) -> None:
        self.identity = identity
        self.calls: list[str] = []

    def identify(self, path: str) -> StorageDeviceIdentity:
        self.calls.append(path)
        return self.identity


class _ConsentFixture:
    def __init__(self, *, decision: str = "approved", operator_uid: int = 1000) -> None:
        self.decision = decision
        self.operator_uid = operator_uid
        self.requested: list[str] = []

    async def request_boot(self, plan: BootRepairPlan) -> dict[str, Any]:
        self.requested.append(plan.id)
        return {"challenge_id": "boot-challenge-1234"}

    async def wait(self, challenge_id: str, timeout_seconds: float = 600.0) -> dict[str, Any]:
        del timeout_seconds
        assert challenge_id == "boot-challenge-1234"
        return {"decision": self.decision, "operator_uid": self.operator_uid}


class _CompletionFailAudit:
    def __init__(self) -> None:
        self.base = MemoryAuditLedger()

    async def append(
        self,
        *,
        event_type: str,
        source: str,
        correlation_id: str,
        session_id: str,
        payload: dict[str, Any],
    ) -> AuditReceipt:
        if event_type == "boot.repair.execution-completed":
            raise AuditLedgerError("fixture completion failure")
        return await self.base.append(
            event_type=event_type,
            source=source,
            correlation_id=correlation_id,
            session_id=session_id,
            payload=payload,
        )


async def _plan_and_protect(
    tmp_path: Path,
) -> tuple[BootRecoveryEngine, BootRecoveryStore, BootRepairPlan]:
    root = _root(tmp_path, grub=False, initramfs=False)
    engine, store, _ = _engine(tmp_path)
    diagnostic = _diagnostic(root)
    await store.put_diagnostic(diagnostic)
    plan = await engine.plan(
        BootRepairPlanInput(diagnostic_id=diagnostic.id),
        session_id="boot-broker-session",
    )
    protected = await engine.protect(plan.id, session_id="boot-broker-session")
    return engine, store, protected


async def test_boot_broker_checkpoint_authorize_execute_verify_and_one_use(tmp_path: Path) -> None:
    engine, store, protected = await _plan_and_protect(tmp_path)
    audit = MemoryAuditLedger()
    consent = _ConsentFixture()
    identity = _IdentityFixture(protected.target_disk)
    broker = BootBroker(
        tools=engine.tools,
        identity=identity,
        audit=audit,
        consent=consent,
        checkpoints=engine.checkpoints,
        store=store,
        checkpoint_root=tmp_path / "broker-checkpoints",
        allowed_client_uids=frozenset({1000}),
        emergency_journal=tmp_path / "boot-emergency.jsonl",
    )

    unprotected = protected.model_copy(update={"protection_checkpoint": None})
    from ares.boot.integrity import boot_plan_fingerprint

    unprotected = unprotected.model_copy(update={"fingerprint_sha256": "0" * 64})
    unprotected = unprotected.model_copy(
        update={"fingerprint_sha256": boot_plan_fingerprint(unprotected)}
    )
    checkpoint_payload = await broker.dispatch(
        {"action": "boot.checkpoint", "plan": unprotected.model_dump(mode="json")},
        1000,
        _ignore_send,
    )
    assert checkpoint_payload["checkpoint"]["status"] == "ready"
    assert checkpoint_payload["artifact"]["file_hashes"]

    messages: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    grant_payload = await broker.dispatch(
        {"action": "boot.authorize", "plan": protected.model_dump(mode="json")},
        1000,
        send,
    )
    assert messages == [{"type": "authorization_requested", "challenge_id": "boot-challenge-1234"}]
    assert grant_payload["operator_uid"] == 1000

    async def lose_observer(message: dict[str, Any]) -> None:
        del message
        raise OSError("fixture observer disconnected")

    outcome = await broker.dispatch(
        {
            "action": "boot.execute",
            "plan": protected.model_dump(mode="json"),
            "grant": grant_payload,
        },
        1000,
        lose_observer,
    )
    changed = set(outcome["changed_operations"])
    assert {"INSTALL_GRUB", "REGENERATE_GRUB_CONFIG", "REGENERATE_INITRAMFS"} <= changed

    verification = await broker.dispatch(
        {"action": "boot.verify", "plan": protected.model_dump(mode="json")},
        1000,
        _ignore_send,
    )
    assert verification["status"] == "PARTIAL"
    assert identity.calls
    events = {record["event_type"] for record in audit.records}
    assert "boot.authorization.intent" in events
    assert "boot.authorization.granted" in events
    assert "boot.repair.intent" in events
    assert "boot.repair.execution-completed" in events
    assert "boot.verification.completed" in events

    with pytest.raises(BootToolError, match="BOOT_AUTHORIZATION_INVALID"):
        await broker.dispatch(
            {
                "action": "boot.execute",
                "plan": protected.model_dump(mode="json"),
                "grant": grant_payload,
            },
            1000,
            _ignore_send,
        )

    with pytest.raises(PermissionError, match="not authorized"):
        await broker.dispatch({"action": "boot.verify"}, 9999, _ignore_send)
    with pytest.raises(BootToolError, match="BOOT_BROKER_ACTION_REJECTED"):
        await broker.dispatch({"action": "boot.unknown"}, 1000, _ignore_send)


async def test_boot_broker_denial_identity_change_and_post_effect_audit_failure(
    tmp_path: Path,
) -> None:
    engine, store, protected = await _plan_and_protect(tmp_path)
    denied = BootBroker(
        tools=engine.tools,
        identity=_IdentityFixture(protected.target_disk),
        audit=MemoryAuditLedger(),
        consent=_ConsentFixture(decision="denied"),
        checkpoints=engine.checkpoints,
        store=store,
        checkpoint_root=tmp_path / "denied-checkpoints",
    )
    with pytest.raises(BootToolError, match="BOOT_AUTHORIZATION_DENIED"):
        await denied.dispatch(
            {"action": "boot.authorize", "plan": protected.model_dump(mode="json")},
            1000,
            _ignore_send,
        )

    changed_identity = BootBroker(
        tools=engine.tools,
        identity=_IdentityFixture(_identity(path="/changed-fixture.img")),
        audit=MemoryAuditLedger(),
        consent=_ConsentFixture(),
        checkpoints=engine.checkpoints,
        store=store,
        checkpoint_root=tmp_path / "identity-checkpoints",
    )
    with pytest.raises(BootToolError, match="BOOT_DEVICE_IDENTITY_CHANGED"):
        await changed_identity.dispatch(
            {"action": "boot.authorize", "plan": protected.model_dump(mode="json")},
            1000,
            _ignore_send,
        )

    failing_audit = _CompletionFailAudit()
    fail_broker = BootBroker(
        tools=engine.tools,
        identity=_IdentityFixture(protected.target_disk),
        audit=failing_audit,
        consent=_ConsentFixture(),
        checkpoints=engine.checkpoints,
        store=store,
        checkpoint_root=tmp_path / "failure-checkpoints",
        emergency_journal=tmp_path / "failure-emergency.jsonl",
    )
    grant = await fail_broker.dispatch(
        {"action": "boot.authorize", "plan": protected.model_dump(mode="json")},
        1000,
        _ignore_send,
    )
    with pytest.raises(BootToolError, match="BOOT_RECONCILIATION_REQUIRED"):
        await fail_broker.dispatch(
            {
                "action": "boot.execute",
                "plan": protected.model_dump(mode="json"),
                "grant": grant,
            },
            1000,
            _ignore_send,
        )
    emergency = (tmp_path / "failure-emergency.jsonl").read_text(encoding="utf-8")
    assert "boot.repair.execution-completed" in emergency


async def _serve_once(
    path: Path,
    responses: tuple[dict[str, Any] | bytes, ...],
    *,
    delay: float = 0.0,
) -> asyncio.AbstractServer:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readline()
        if delay:
            await asyncio.sleep(delay)
        for response in responses:
            encoded = response if isinstance(response, bytes) else json.dumps(response).encode()
            writer.write(encoded if encoded.endswith(b"\n") else encoded + b"\n")
            await writer.drain()
        writer.close()
        await writer.wait_closed()

    return await asyncio.start_unix_server(handle, path=str(path))


async def test_unix_boot_executor_protocol_success_and_fail_closed_edges(tmp_path: Path) -> None:
    messages: list[tuple[str, dict[str, object]]] = []

    async def capture(name: str, payload: dict[str, object]) -> None:
        messages.append((name, payload))

    success_path = tmp_path / "boot-success.sock"
    server = await _serve_once(
        success_path,
        (
            {"type": "authorization_requested", "challenge_id": "challenge-1234"},
            {"type": "stage", "name": "boot.stage", "payload": {"ok": True}},
            {"type": "result", "payload": {"ok": True}},
        ),
    )
    async with server:
        executor = UnixBrokerBootExecutor(success_path)
        result = await executor._request({"action": "fixture"}, capture)
    assert result == {"ok": True}
    assert messages == [
        ("authorization_requested", {"challenge_id": "challenge-1234"}),
        ("boot.stage", {"ok": True}),
    ]

    cases: tuple[tuple[str, tuple[dict[str, Any] | bytes, ...], str], ...] = (
        ("disconnect", (), "BOOT_BROKER_DISCONNECTED"),
        ("json", (b"not-json\n",), "BOOT_BROKER_RESPONSE_INVALID"),
        ("list", (b"[]\n",), "BOOT_BROKER_RESPONSE_INVALID"),
        (
            "error",
            ({"type": "error", "code": "BOOT_FIXTURE_ERROR"},),
            "BOOT_FIXTURE_ERROR",
        ),
        (
            "bad-result",
            ({"type": "result", "payload": "bad"},),
            "BOOT_BROKER_RESPONSE_INVALID",
        ),
        (
            "bad-auth",
            ({"type": "authorization_requested", "challenge_id": 7},),
            "BOOT_BROKER_RESPONSE_INVALID",
        ),
        (
            "bad-stage",
            ({"type": "stage", "name": 7, "payload": {}},),
            "BOOT_BROKER_RESPONSE_INVALID",
        ),
    )
    for name, responses, code in cases:
        path = tmp_path / f"boot-{name}.sock"
        one = await _serve_once(path, responses)
        async with one:
            executor = UnixBrokerBootExecutor(path, timeout_seconds=1.0)
            with pytest.raises(BootExecutorError, match=code):
                await executor._request({"action": "fixture"}, capture)

    timeout_path = tmp_path / "boot-timeout.sock"
    timeout_server = await _serve_once(timeout_path, (), delay=0.05)
    async with timeout_server:
        executor = UnixBrokerBootExecutor(timeout_path, timeout_seconds=0.01)
        with pytest.raises(BootExecutorError, match="BOOT_BROKER_TIMEOUT"):
            await executor._request({"action": "fixture"}, capture)

    unavailable = UnixBrokerBootExecutor(tmp_path / "missing.sock")
    with pytest.raises(BootExecutorError, match="BOOT_BROKER_UNAVAILABLE"):
        await unavailable._request({"action": "fixture"}, capture)

    too_large = UnixBrokerBootExecutor(tmp_path / "unused.sock")
    with pytest.raises(BootExecutorError, match="BOOT_BROKER_REQUEST_TOO_LARGE"):
        await too_large._request({"value": "x" * 2_100_000}, capture)


async def _ignore_send(message: dict[str, Any]) -> None:
    del message
