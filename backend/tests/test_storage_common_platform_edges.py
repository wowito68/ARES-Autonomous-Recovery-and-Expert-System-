from __future__ import annotations

import argparse
import asyncio
import builtins
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI

from ares import cli
from ares.actions.storage_operations import _layout_graph
from ares.audit.ledger import (
    AuditLedgerError,
    AuditReceipt,
    AuditWriter,
    UnixAuditLedgerClient,
    _bounded_string,
    _prepare_socket_path as prepare_audit_socket,
    _redact_payload,
    _safe_audit_value,
    _unix_server as audit_unix_server,
)
from ares.backup.executor import BackupExecutorError, UnixBrokerBackupExecutor, _ignore_message as backup_ignore
from ares.events import EventBus
from ares.knowledge import GraphKind
from ares.main import create_app
from ares.runtime import broker as root_broker
from ares.storage_operations.models import (
    BootDependency,
    FilesystemResource,
    MountPointResource,
    OperatingSystemResource,
    StorageTransactionStatus,
    VolumeKind,
    VolumeResource,
)
from ares.filesystems.executor import (
    FilesystemExecutorError,
    UnixBrokerFilesystemExecutor,
    _ignore_message as filesystem_ignore,
)
from tests.test_storage_operation_api import _image, _settings
from tests.test_storage_partition_edges import _identity, _layout, _partition


async def _serve_once(path: Path, response: bytes, *, delay: float = 0.0) -> asyncio.AbstractServer:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readline()
        if delay:
            await asyncio.sleep(delay)
        if response:
            writer.write(response)
            await writer.drain()
        writer.close()
        await writer.wait_closed()

    return await asyncio.start_unix_server(handle, path=str(path))


async def test_backup_broker_client_fail_closed_protocol_edges(tmp_path: Path) -> None:
    async def run_case(name: str, response: bytes, code: str, *, timeout: float = 1.0) -> None:
        path = tmp_path / f"backup-{name}.sock"
        server = await _serve_once(path, response, delay=0.05 if name == "timeout" else 0.0)
        async with server:
            executor = UnixBrokerBackupExecutor(path, timeout_seconds=timeout)
            with pytest.raises(BackupExecutorError, match=code):
                await executor._request({"action": "fixture"}, backup_ignore)

    await run_case("disconnect", b"", "BACKUP_BROKER_DISCONNECTED")
    await run_case("json", b"not-json\n", "BACKUP_BROKER_RESPONSE_INVALID")
    await run_case("list", b"[]\n", "BACKUP_BROKER_RESPONSE_INVALID")
    await run_case(
        "error",
        json.dumps({"type": "error", "code": "BACKUP_FIXTURE_ERROR"}).encode() + b"\n",
        "BACKUP_FIXTURE_ERROR",
    )
    await run_case(
        "error-no-code",
        json.dumps({"type": "error", "code": 7}).encode() + b"\n",
        "BACKUP_BROKER_FAILED",
    )
    await run_case(
        "bad-payload",
        json.dumps({"type": "result", "payload": "bad"}).encode() + b"\n",
        "BACKUP_BROKER_RESPONSE_INVALID",
    )
    await run_case("timeout", b"", "BACKUP_BROKER_TIMEOUT", timeout=0.01)

    path = tmp_path / "backup-large.sock"
    server = await _serve_once(path, b"")
    async with server:
        executor = UnixBrokerBackupExecutor(path)
        with pytest.raises(BackupExecutorError, match="BACKUP_BROKER_REQUEST_TOO_LARGE"):
            await executor._request({"payload": "x" * 4_000_100}, backup_ignore)


async def test_filesystem_broker_client_fail_closed_protocol_edges(tmp_path: Path) -> None:
    async def run_case(name: str, response: bytes, code: str, *, timeout: float = 1.0) -> None:
        path = tmp_path / f"filesystem-{name}.sock"
        server = await _serve_once(path, response, delay=0.05 if name == "timeout" else 0.0)
        async with server:
            executor = UnixBrokerFilesystemExecutor(path, timeout_seconds=timeout)
            with pytest.raises(FilesystemExecutorError, match=code):
                await executor._request({"action": "fixture"}, filesystem_ignore)

    await run_case("disconnect", b"", "FILESYSTEM_BROKER_DISCONNECTED")
    await run_case("json", b"not-json\n", "FILESYSTEM_BROKER_RESPONSE_INVALID")
    await run_case("list", b"[]\n", "FILESYSTEM_BROKER_RESPONSE_INVALID")
    await run_case(
        "error",
        json.dumps({"type": "error", "code": "FILESYSTEM_FIXTURE_ERROR"}).encode() + b"\n",
        "FILESYSTEM_FIXTURE_ERROR",
    )
    await run_case(
        "error-no-code",
        json.dumps({"type": "error", "code": 7}).encode() + b"\n",
        "FILESYSTEM_BROKER_FAILED",
    )
    await run_case(
        "bad-payload",
        json.dumps({"type": "result", "payload": 9}).encode() + b"\n",
        "FILESYSTEM_BROKER_RESPONSE_INVALID",
    )
    await run_case("timeout", b"", "FILESYSTEM_BROKER_TIMEOUT", timeout=0.01)

    path = tmp_path / "filesystem-large.sock"
    server = await _serve_once(path, b"")
    async with server:
        executor = UnixBrokerFilesystemExecutor(path)
        with pytest.raises(FilesystemExecutorError, match="FILESYSTEM_BROKER_REQUEST_TOO_LARGE"):
            await executor._request({"payload": "x" * 2_000_100}, filesystem_ignore)


async def test_audit_client_writer_helpers_and_unix_ack_protocol(tmp_path: Path) -> None:
    directory = tmp_path / "audit"
    writer = AuditWriter(directory)
    writer.prepare()
    with pytest.raises(ValueError, match="invalid audit request"):
        await writer.append({"event_type": "bad", "payload": []}, 1000)

    receipt = await writer.append(
        {
            "event_type": "storage.test",
            "source": "test-suite",
            "correlation_id": "correlation-1234",
            "session_id": "session-1234",
            "payload": {
                "secret": "hide-me",
                "nested": {"token": "hide-too", "ok": [1, "value"]},
                "unsupported": object(),
            },
        },
        1000,
    )
    assert receipt.sequence == 1
    record = json.loads((directory / "ledger.jsonl").read_text(encoding="utf-8"))
    assert record["payload"]["secret"] == "[REDACTED]"
    assert record["payload"]["nested"]["token"] == "[REDACTED]"
    assert record["payload"]["unsupported"] is None

    assert _bounded_string("ok", 4) == "ok"
    assert _bounded_string("", 4) is None
    assert _bounded_string("toolong", 3) is None
    assert _bounded_string("bad\x00value", 20) is None
    assert _bounded_string(1, 4) is None
    assert _redact_payload({"authorization": "x"})["authorization"] == "[REDACTED]"
    assert _safe_audit_value(tuple()) is None
    assert _safe_audit_value([1] * 100) == [1] * 64
    assert len(cast(str, _safe_audit_value("x" * 2000))) == 1024

    socket_path = tmp_path / "audit-client.sock"

    async def handle(reader: asyncio.StreamReader, stream: asyncio.StreamWriter) -> None:
        request = json.loads(await reader.readline())
        assert request["event_type"] == "storage.audit"
        response = {
            "ok": True,
            "receipt": AuditReceipt(sequence=3, mac="a" * 64).model_dump(mode="json"),
        }
        stream.write(json.dumps(response).encode() + b"\n")
        await stream.drain()
        stream.close()
        await stream.wait_closed()

    server = await asyncio.start_unix_server(handle, path=str(socket_path))
    async with server:
        client = UnixAuditLedgerClient(socket_path)
        ack = await client.append(
            event_type="storage.audit",
            source="test-suite",
            correlation_id="correlation-audit",
            session_id="session-audit",
            payload={"ok": True},
        )
        assert ack.sequence == 3

    for name, response in (
        ("empty", b""),
        ("bad-json", b"no-json\n"),
        ("negative", json.dumps({"ok": False}).encode() + b"\n"),
        ("bad-receipt", json.dumps({"ok": True, "receipt": {}}).encode() + b"\n"),
    ):
        path = tmp_path / f"audit-{name}.sock"
        one = await _serve_once(path, response)
        async with one:
            client = UnixAuditLedgerClient(path)
            with pytest.raises(AuditLedgerError, match="invalid acknowledgement"):
                await client.append(
                    event_type="storage.audit",
                    source="test-suite",
                    correlation_id="correlation-audit",
                    session_id="session-audit",
                    payload={},
                )

    too_large = UnixAuditLedgerClient(tmp_path / "unused.sock")
    with pytest.raises(AuditLedgerError, match="message too large"):
        await too_large.append(
            event_type="storage.audit",
            source="test-suite",
            correlation_id="correlation-audit",
            session_id="session-audit",
            payload={"value": "x" * 300_000},
        )


def test_audit_writer_rejects_unsafe_key_and_invalid_chain(tmp_path: Path) -> None:
    unsafe_dir = tmp_path / "unsafe"
    unsafe_dir.mkdir()
    target = tmp_path / "real-key"
    target.write_bytes(b"x" * 32)
    (unsafe_dir / "ledger.key").symlink_to(target)
    with pytest.raises(OSError, match="audit key is unsafe"):
        AuditWriter(unsafe_dir).prepare()

    short_dir = tmp_path / "short"
    short_dir.mkdir()
    (short_dir / "ledger.key").write_bytes(b"short")
    with pytest.raises(OSError, match="invalid length"):
        AuditWriter(short_dir).prepare()

    prepare_dir = tmp_path / "socket-parent"
    socket_path = prepare_dir / "audit.sock"
    prepare_dir.mkdir()
    socket_path.write_text("old", encoding="utf-8")
    prepare_audit_socket(socket_path)
    assert not socket_path.exists()


async def test_audit_unix_server_helper_and_peer_uid(tmp_path: Path) -> None:
    socket_path = tmp_path / "audit-helper.sock"
    peer_uids: list[int] = []

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer_uids.append(root_broker._peer_uid(writer))
        data = await reader.readline()
        writer.write(data)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await audit_unix_server(handler, socket_path)
    async with server:
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        writer.write(b"hello\n")
        await writer.drain()
        assert await reader.readline() == b"hello\n"
        writer.close()
        await writer.wait_closed()
    assert peer_uids and peer_uids[0] in {os.getuid(), -1}


async def test_root_broker_unix_server_helper(tmp_path: Path) -> None:
    socket_path = tmp_path / "root-broker.sock"

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = json.loads(await reader.readline())
        writer.write(json.dumps({"type": "result", "payload": request}).encode() + b"\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await root_broker._unix_server(handler, socket_path)
    async with server:
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        writer.write(json.dumps({"action": "fixture"}).encode() + b"\n")
        await writer.drain()
        response = json.loads(await reader.readline())
        assert response["payload"]["action"] == "fixture"
        writer.close()
        await writer.wait_closed()
    assert socket_path.exists()


async def _poll_storage(app: FastAPI, operation_id: str, states: set[StorageTransactionStatus]) -> None:
    store = app.state.storage_operation_store
    for _ in range(100):
        record = await store.get_record(operation_id)
        if record is not None and record.transaction.status in states:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("storage CLI operation did not reach expected state")


async def test_storage_cli_uses_real_service_lifecycle_on_disposable_image(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    image = tmp_path / "cli-storage.img"
    _image(image)
    app = create_app(_settings(tmp_path))
    parser = cli.build_parser()

    async with app.router.lifespan_context(app):
        assert await cli._storage_command(parser.parse_args(["storage", "layout", str(image)]), app) == 0
        assert await cli._storage_command(
            parser.parse_args(
                [
                    "storage",
                    "plan",
                    "create",
                    str(image),
                    "--size-bytes",
                    str(8 * 1024 * 1024),
                    "--table-type",
                    "GPT",
                ]
            ),
            app,
        ) == 0
        records = await app.state.storage_operation_store.list_records()
        assert len(records) == 1
        operation_id = records[0].plan.operation_id

        assert await cli._storage_command(
            parser.parse_args(["storage", "validate", operation_id]), app
        ) == 0
        assert await cli._storage_command(
            parser.parse_args(["storage", "authorize", operation_id]), app
        ) == 0
        await _poll_storage(app, operation_id, {StorageTransactionStatus.AUTHORIZED})
        assert await cli._storage_command(
            parser.parse_args(["storage", "execute", operation_id]), app
        ) == 0
        await _poll_storage(
            app,
            operation_id,
            {StorageTransactionStatus.COMMITTED, StorageTransactionStatus.FAILED},
        )
        record = await app.state.storage_operation_store.get_record(operation_id)
        assert record is not None and record.transaction.status is StorageTransactionStatus.COMMITTED

        assert await cli._storage_command(
            parser.parse_args(["storage", "status", operation_id]), app
        ) == 0
        await app.state.storage_operation_store.put_transaction(
            record.transaction.model_copy(
                update={
                    "status": StorageTransactionStatus.UNKNOWN,
                    "reconciliation_required": True,
                }
            )
        )
        assert await cli._storage_command(
            parser.parse_args(["storage", "reconcile", operation_id]), app
        ) == 0
        assert await cli._storage_command(
            parser.parse_args(["storage", "status", "missing-operation"]), app
        ) == 3

    output = capsys.readouterr()
    assert "plan_fingerprint" in output.out or "fingerprint_sha256" in output.out
    assert "not found" in output.err


class _ConsentOperatorFixture:
    challenge: dict[str, Any] = {
        "challenge_id": "challenge-cli-1234",
        "confirmation_phrase": "APPROVE fixture-1234",
    }
    approved: list[tuple[str, str]] = []
    denied: list[str] = []
    fail = False

    def __init__(self, socket_path: Path) -> None:
        del socket_path

    async def get(self, challenge_id: str) -> dict[str, Any]:
        if self.fail:
            raise RuntimeError("fixture unavailable")
        return {**self.challenge, "challenge_id": challenge_id}

    async def approve(self, challenge_id: str, confirmation: str) -> dict[str, Any]:
        self.approved.append((challenge_id, confirmation))
        return {"decision": "approved"}

    async def deny(self, challenge_id: str) -> dict[str, Any]:
        self.denied.append(challenge_id)
        return {"decision": "denied"}


async def test_consent_cli_approve_deny_mismatch_invalid_and_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _ConsentOperatorFixture.approved.clear()
    _ConsentOperatorFixture.denied.clear()
    _ConsentOperatorFixture.fail = False
    monkeypatch.setattr(cli, "UnixConsentOperatorClient", _ConsentOperatorFixture)
    monkeypatch.setattr(cli, "get_settings", lambda: SimpleNamespace(consent_socket=tmp_path / "x.sock"))

    deny_args = argparse.Namespace(consent_command="deny", challenge_id="challenge-cli-1234")
    assert await cli._consent_command(deny_args) == 0
    assert _ConsentOperatorFixture.denied == ["challenge-cli-1234"]

    approve_args = argparse.Namespace(consent_command="approve", challenge_id="challenge-cli-1234")
    monkeypatch.setattr(builtins, "input", lambda _: "wrong")
    assert await cli._consent_command(approve_args) == 4

    monkeypatch.setattr(builtins, "input", lambda _: "APPROVE fixture-1234")
    assert await cli._consent_command(approve_args) == 0
    assert _ConsentOperatorFixture.approved[-1][1] == "APPROVE fixture-1234"

    _ConsentOperatorFixture.challenge = {"challenge_id": "challenge-cli-1234"}
    assert await cli._consent_command(approve_args) == 2
    _ConsentOperatorFixture.challenge = {
        "challenge_id": "challenge-cli-1234",
        "confirmation_phrase": "APPROVE fixture-1234",
    }
    _ConsentOperatorFixture.fail = True
    assert await cli._consent_command(approve_args) == 2
    assert "ARES consent failed" in capsys.readouterr().err


def test_storage_layout_graph_projects_boot_filesystem_mount_os_and_volume_dependencies() -> None:
    identity = _identity()
    filesystem = FilesystemResource(
        id="filesystem:graph",
        device_path="image:test:1",
        filesystem_type="ext4",
        uuid="fs-uuid",
        label="root",
    )
    mount = MountPointResource(
        id="mount:graph",
        source="image:test:1",
        path="/",
        filesystem_type="ext4",
    )
    volume = VolumeResource(id="volume:graph", kind=VolumeKind.LVM_PV, name="pv0")
    os_item = OperatingSystemResource(
        id="os:graph",
        name="Debian",
        version="13",
        source="image:test:1",
        mount_point="/",
    )
    boot = BootDependency(
        id="boot:graph",
        kind="root",
        partition_number=1,
        reason="root filesystem",
        critical=True,
    )
    partition = _partition(
        identity,
        filesystem_id=filesystem.id,
        mount_ids=(mount.id,),
        volume_ids=(volume.id,),
    ).model_copy(update={"operating_system_ids": (os_item.id,)})
    layout = _layout(
        identity,
        (partition,),
        filesystems=(filesystem,),
        mounts=(mount,),
        volumes=(volume,),
        boot=(boot,),
    ).model_copy(update={"operating_systems": (os_item,)})

    nodes, edges = _layout_graph(layout)
    kinds = {node.kind for node in nodes}
    assert {
        GraphKind.DISK,
        GraphKind.PARTITION_TABLE,
        GraphKind.PARTITION,
        GraphKind.FILESYSTEM,
        GraphKind.MOUNT_POINT,
        GraphKind.OPERATING_SYSTEM,
        GraphKind.VOLUME,
        GraphKind.BOOT_DEPENDENCY,
    } <= kinds
    relations = {edge.relation for edge in edges}
    assert {
        "has_partition_table",
        "contains_partition",
        "contains_filesystem",
        "mounted_at",
        "contains",
        "hosts_operating_system",
        "has_boot_dependency",
    } <= relations
