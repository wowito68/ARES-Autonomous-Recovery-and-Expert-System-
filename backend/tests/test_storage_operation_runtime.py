from __future__ import annotations

import asyncio
import json
import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from ares.audit import MemoryAuditLedger
from ares.protection import ProtectionCheckpointStore
from ares.runtime.storage_broker import StorageBroker
from ares.storage_operations import (
    DeclarativeStorageOperationRequest,
    LocalTestStorageExecutor,
    PartitionTableType,
    ProductionStorageWriteGate,
    StorageAuthorizationGrant,
    StorageCheckpointBundle,
    StorageOperationEngine,
    StorageOperationStore,
    StorageTransactionStatus,
    UnixBrokerStorageExecutor,
)
from ares.storage_operations.executor import StorageExecutorError
from ares.storage_operations.models import StorageLayout
from ares.tools.partition import PartitionToolError, StoragePartitionToolSuite

pytestmark = pytest.mark.skipif(shutil.which("sfdisk") is None, reason="sfdisk unavailable")


def _image(path: Path, size: int = 128 * 1024 * 1024) -> None:
    with path.open("wb") as handle:
        handle.truncate(size)


class _Consent:
    def __init__(self, decision: str = "approved") -> None:
        self.decision = decision
        self.requested = 0
        self.waited = 0

    async def request_storage(self, plan: Any) -> dict[str, Any]:
        self.requested += 1
        return {"challenge_id": "challenge-storage-1234"}

    async def wait(
        self, challenge_id: str, timeout_seconds: float = 600.0
    ) -> dict[str, Any]:
        del challenge_id, timeout_seconds
        self.waited += 1
        return {"decision": self.decision, "operator_uid": 1000}


async def _engine(
    tmp_path: Path, image: Path
) -> tuple[
    StorageOperationEngine,
    StorageOperationStore,
    ProtectionCheckpointStore,
    StoragePartitionToolSuite,
    ProductionStorageWriteGate,
]:
    gate = ProductionStorageWriteGate(test_mode=True)
    tools = StoragePartitionToolSuite(
        allow_regular_file_targets=True,
        write_gate=gate,
    )
    store = StorageOperationStore(tmp_path / "operations")
    store.prepare()
    checkpoints = ProtectionCheckpointStore(tmp_path / "checkpoints")
    checkpoints.prepare()
    engine = StorageOperationEngine(
        executor=LocalTestStorageExecutor(tools),
        store=store,
        checkpoints=checkpoints,
        write_gate=gate,
    )
    return engine, store, checkpoints, tools, gate


async def _validated_create(
    tmp_path: Path, image: Path
) -> tuple[
    StorageOperationEngine,
    StorageOperationStore,
    ProtectionCheckpointStore,
    StoragePartitionToolSuite,
    ProductionStorageWriteGate,
    Any,
]:
    engine, store, checkpoints, tools, gate = await _engine(tmp_path, image)
    plan = await engine.plan(
        DeclarativeStorageOperationRequest(
            operation="create",
            target_disk=str(image),
            size_bytes=16 * 1024 * 1024,
            table_type=PartitionTableType.GPT,
            partition_name="ARES RUNTIME TEST",
        ),
        session_id="runtime-session-1234",
        created_by="test",
    )
    validated = await engine.validate(plan.operation_id, session_id=plan.session_id)
    assert validated.executable is True
    assert validated.protection_checkpoint is not None
    return engine, store, checkpoints, tools, gate, validated


async def test_storage_broker_full_lifecycle_and_one_use_grant(tmp_path: Path) -> None:
    image = tmp_path / "broker.img"
    _image(image)
    _, store, checkpoints, tools, gate, plan = await _validated_create(tmp_path, image)
    audit = MemoryAuditLedger()
    consent = _Consent()
    broker = StorageBroker(
        tools,
        audit,
        consent,
        checkpoints,
        store,
        write_gate=gate,
        allowed_client_uids=frozenset({os.getuid()}),
        emergency_journal=tmp_path / "emergency.jsonl",
    )
    messages: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    inspected = await broker.dispatch(
        {"action": "storage.inspect", "target_disk": str(image)},
        os.getuid(),
        send,
    )
    assert StorageLayout.model_validate(inspected).disk.identity.controlled_test_target is True

    dry_run = await broker.dispatch(
        {"action": "storage.dry-run", "plan": plan.model_dump(mode="json")},
        os.getuid(),
        send,
    )
    assert dry_run["valid"] is True

    granted_payload = await broker.dispatch(
        {"action": "storage.authorize", "plan": plan.model_dump(mode="json")},
        os.getuid(),
        send,
    )
    grant = StorageAuthorizationGrant.model_validate(granted_payload)
    assert consent.requested == 1 and consent.waited == 1
    assert any(item.get("type") == "authorization_requested" for item in messages)

    outcome = await broker.dispatch(
        {
            "action": "storage.execute",
            "plan": plan.model_dump(mode="json"),
            "grant": grant.model_dump(mode="json"),
        },
        os.getuid(),
        send,
    )
    assert outcome["exit_code"] == 0
    assert any(item.get("type") == "stage" for item in messages)

    verified = StorageLayout.model_validate(
        await broker.dispatch(
            {"action": "storage.verify", "plan": plan.model_dump(mode="json")},
            os.getuid(),
            send,
        )
    )
    assert verified.partition_table.type is PartitionTableType.GPT
    assert len(verified.partition_table.partitions) == 1
    assert any(
        item["event_type"] == "storage.verification.completed" for item in audit.records
    )

    with pytest.raises(PartitionToolError, match="STORAGE_AUTHORIZATION_INVALID"):
        await broker.dispatch(
            {
                "action": "storage.execute",
                "plan": plan.model_dump(mode="json"),
                "grant": grant.model_dump(mode="json"),
            },
            os.getuid(),
            send,
        )

    with pytest.raises(PermissionError):
        await broker.dispatch(
            {"action": "storage.inspect", "target_disk": str(image)},
            os.getuid() + 1,
            send,
        )


async def test_storage_broker_checkpoint_denial_and_invalid_actions(tmp_path: Path) -> None:
    image = tmp_path / "checkpoint.img"
    _image(image)
    engine, store, checkpoints, tools, gate = await _engine(tmp_path, image)
    plan = await engine.plan(
        DeclarativeStorageOperationRequest(
            operation="create",
            target_disk=str(image),
            size_bytes=8 * 1024 * 1024,
            table_type=PartitionTableType.MBR,
        ),
        session_id="runtime-session-5678",
        created_by="test",
    )
    audit = MemoryAuditLedger()
    broker = StorageBroker(
        tools,
        audit,
        _Consent("denied"),
        checkpoints,
        store,
        write_gate=gate,
        allowed_client_uids=frozenset({os.getuid()}),
    )

    async def send(message: dict[str, Any]) -> None:
        del message

    checkpoint_payload = await broker.dispatch(
        {"action": "storage.checkpoint", "plan": plan.model_dump(mode="json")},
        os.getuid(),
        send,
    )
    bundle = StorageCheckpointBundle.model_validate(checkpoint_payload)
    assert bundle.artifact.operation_id == plan.operation_id
    assert bundle.checkpoint.status.value == "ready"

    validated = await engine.validate(plan.operation_id, session_id=plan.session_id)
    with pytest.raises(PartitionToolError, match="STORAGE_AUTHORIZATION_DENIED"):
        await broker.dispatch(
            {"action": "storage.authorize", "plan": validated.model_dump(mode="json")},
            os.getuid(),
            send,
        )

    with pytest.raises(PartitionToolError, match="STORAGE_TARGET_INVALID"):
        await broker.dispatch({"action": "storage.inspect"}, os.getuid(), send)
    with pytest.raises(PartitionToolError, match="STORAGE_BROKER_ACTION_REJECTED"):
        await broker.dispatch({"action": "storage.nope"}, os.getuid(), send)


async def test_unknown_reconciliation_proves_original_or_proposed_state(tmp_path: Path) -> None:
    original_image = tmp_path / "original.img"
    _image(original_image)
    engine, store, _, _, _ = await _engine(tmp_path / "original-case", original_image)
    plan = await engine.plan(
        DeclarativeStorageOperationRequest(
            operation="create",
            target_disk=str(original_image),
            size_bytes=8 * 1024 * 1024,
            table_type=PartitionTableType.GPT,
        ),
        session_id="reconcile-session-1111",
        created_by="test",
    )
    await engine.validate(plan.operation_id, session_id=plan.session_id)
    record = await store.get_record(plan.operation_id)
    assert record is not None
    await store.put_transaction(
        record.transaction.model_copy(
            update={"status": StorageTransactionStatus.UNKNOWN, "reconciliation_required": True}
        )
    )
    verification = await engine.reconcile_unknown(plan.operation_id, session_id=plan.session_id)
    assert verification.status.value == "PARTIAL"
    record = await store.get_record(plan.operation_id)
    assert record is not None
    assert record.transaction.status is StorageTransactionStatus.ABORTED

    proposed_image = tmp_path / "proposed.img"
    _image(proposed_image)
    engine2, store2, _, _, _ = await _engine(tmp_path / "proposed-case", proposed_image)
    plan2 = await engine2.plan(
        DeclarativeStorageOperationRequest(
            operation="create",
            target_disk=str(proposed_image),
            size_bytes=8 * 1024 * 1024,
            table_type=PartitionTableType.GPT,
        ),
        session_id="reconcile-session-2222",
        created_by="test",
    )
    validated2 = await engine2.validate(plan2.operation_id, session_id=plan2.session_id)

    async def stage(name: str, payload: dict[str, object]) -> None:
        del name, payload

    grant = await engine2.request_authorization(
        validated2.operation_id,
        session_id=validated2.session_id,
        on_challenge=lambda _: asyncio.sleep(0),
    )
    await engine2.execute(
        validated2.operation_id,
        grant,
        session_id=validated2.session_id,
        on_stage=stage,
    )
    record2 = await store2.get_record(validated2.operation_id)
    assert record2 is not None
    await store2.put_transaction(
        record2.transaction.model_copy(
            update={"status": StorageTransactionStatus.UNKNOWN, "reconciliation_required": True}
        )
    )
    verification2 = await engine2.reconcile_unknown(
        validated2.operation_id, session_id=validated2.session_id
    )
    assert verification2.status.value == "VERIFIED"
    record2 = await store2.get_record(validated2.operation_id)
    assert record2 is not None
    assert record2.transaction.status is StorageTransactionStatus.COMMITTED


async def test_unix_storage_executor_protocol_and_fail_closed_errors(tmp_path: Path) -> None:
    image = tmp_path / "unix.img"
    _image(image)
    engine, store, _, _, _ = await _engine(tmp_path / "fixture", image)
    plan = await engine.plan(
        DeclarativeStorageOperationRequest(
            operation="create",
            target_disk=str(image),
            size_bytes=8 * 1024 * 1024,
            table_type=PartitionTableType.GPT,
        ),
        session_id="unix-session-1234",
        created_by="test",
    )
    validated = await engine.validate(plan.operation_id, session_id=plan.session_id)
    checkpoint = validated.protection_checkpoint
    assert checkpoint is not None
    artifact = await store.get_checkpoint(checkpoint.id)
    assert artifact is not None
    bundle = StorageCheckpointBundle(checkpoint=checkpoint, artifact=artifact)
    grant = StorageAuthorizationGrant(
        id="grant-unix-1234",
        challenge_id="challenge-unix-1234",
        operation_id=validated.operation_id,
        plan_id=validated.id,
        session_id=validated.session_id,
        target_fingerprint_sha256=validated.target_disk.fingerprint_sha256,
        plan_fingerprint_sha256=validated.fingerprint_sha256,
        operator_uid=1000,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    socket_path = tmp_path / "broker.sock"
    seen: list[str] = []

    async def handler(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        request = json.loads(await reader.readline())
        action = cast(str, request["action"])
        seen.append(action)
        if action == "storage.inspect":
            payload = validated.original_layout.model_dump(mode="json")
        elif action == "storage.dry-run":
            assert validated.dry_run is not None
            payload = validated.dry_run.model_dump(mode="json")
        elif action == "storage.checkpoint":
            payload = bundle.model_dump(mode="json")
        elif action == "storage.authorize":
            writer.write(
                json.dumps(
                    {
                        "type": "authorization_requested",
                        "challenge_id": grant.challenge_id,
                    }
                ).encode()
                + b"\n"
            )
            await writer.drain()
            payload = grant.model_dump(mode="json")
        elif action == "storage.execute":
            payload = {
                "operation_id": validated.operation_id,
                "operation": validated.operation.value,
                "tool": "sfdisk",
                "exit_code": 0,
                "kernel_reread": None,
                "before": validated.original_layout.model_dump(mode="json"),
                "after": validated.proposed_layout.model_dump(mode="json"),
                "evidence": ["fixture"],
            }
            writer.write(
                json.dumps(
                    {"type": "stage", "name": "storage.partition-table.updated", "payload": {}}
                ).encode()
                + b"\n"
            )
            await writer.drain()
        elif action == "storage.verify":
            payload = validated.proposed_layout.model_dump(mode="json")
        else:
            writer.write(
                json.dumps({"type": "error", "code": "STORAGE_BROKER_FAILED"}).encode()
                + b"\n"
            )
            await writer.drain()
            writer.close()
            return
        writer.write(json.dumps({"type": "result", "payload": payload}).encode() + b"\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_unix_server(handler, path=str(socket_path))
    async with server:
        executor = UnixBrokerStorageExecutor(socket_path, timeout_seconds=2)
        assert (await executor.inspect(str(image))).disk.id
        assert (await executor.dry_run(validated)).valid is True
        assert (await executor.create_checkpoint(validated)).checkpoint.id == checkpoint.id
        challenges: list[str] = []

        async def on_challenge(value: str) -> None:
            challenges.append(value)

        returned_grant = await executor.request_authorization(
            validated, on_challenge=on_challenge
        )
        assert returned_grant == grant
        stages: list[str] = []

        async def on_stage(name: str, payload: dict[str, object]) -> None:
            del payload
            stages.append(name)

        outcome = await executor.execute(validated, grant, on_stage=on_stage)
        assert outcome.exit_code == 0
        assert await executor.verify(validated) == validated.proposed_layout
        assert challenges == [grant.challenge_id]
        assert stages == ["storage.partition-table.updated"]

    assert seen == [
        "storage.inspect",
        "storage.dry-run",
        "storage.checkpoint",
        "storage.authorize",
        "storage.execute",
        "storage.verify",
    ]

    missing = UnixBrokerStorageExecutor(tmp_path / "missing.sock", timeout_seconds=0.1)
    with pytest.raises(StorageExecutorError, match="STORAGE_BROKER_UNAVAILABLE"):
        await missing.inspect(str(image))


async def test_local_executor_denial_and_grant_consumption(tmp_path: Path) -> None:
    image = tmp_path / "local.img"
    _image(image)
    engine, _, _, tools, _ = await _engine(tmp_path / "local", image)
    plan = await engine.plan(
        DeclarativeStorageOperationRequest(
            operation="create",
            target_disk=str(image),
            size_bytes=8 * 1024 * 1024,
            table_type=PartitionTableType.GPT,
        ),
        session_id="local-session-1234",
        created_by="test",
    )
    validated = await engine.validate(plan.operation_id, session_id=plan.session_id)
    denied = LocalTestStorageExecutor(tools, authorize=False)

    with pytest.raises(StorageExecutorError, match="STORAGE_AUTHORIZATION_DENIED"):
        await denied.request_authorization(validated, on_challenge=lambda _: asyncio.sleep(0))

    executor = LocalTestStorageExecutor(tools)
    grant = await executor.request_authorization(
        validated, on_challenge=lambda _: asyncio.sleep(0)
    )

    async def stage(name: str, payload: dict[str, object]) -> None:
        del name, payload

    await executor.execute(validated, grant, on_stage=stage)
    with pytest.raises(StorageExecutorError, match="STORAGE_AUTHORIZATION_INVALID"):
        await executor.execute(validated, grant, on_stage=stage)
