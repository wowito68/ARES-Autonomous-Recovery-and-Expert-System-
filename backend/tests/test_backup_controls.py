from __future__ import annotations

import argparse
import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from pydantic import BaseModel, ConfigDict

import ares.cli as cli_module
from ares.backup import (
    BackupCreateRequest,
    BackupPlan,
    BackupPlanRequest,
    BackupService,
    BackupStatus,
    LocalTestBackupExecutor,
    UnixBrokerBackupExecutor,
)
from ares.backup.executor import BackupExecutorError
from ares.capabilities import CapabilityManager
from ares.protection import ProtectionCheckpoint, ProtectionCheckpointStatus
from ares.tools import BackupFilesystemTools


def _configure_mounts(app: FastAPI, tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    device = os.stat(source).st_dev
    source_device = f"{os.major(device)}:{os.minor(device)}"
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        f"36 25 {source_device} / {source} rw - ext4 /dev/sda1 rw\n"
        f"37 25 99:99 / {destination} rw - ext4 /dev/sdb1 rw\n",
        encoding="utf-8",
    )
    cast(BackupFilesystemTools, app.state.backup_tools).mountinfo_path = mountinfo
    return source, destination


async def test_api_cancel_stops_running_backup(
    client: AsyncClient,
    app: FastAPI,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, destination = _configure_mounts(app, tmp_path)
    (source / "file.bin").write_bytes(b"x" * 1024)
    executor = cast(LocalTestBackupExecutor, app.state.backup_executor)
    entered = asyncio.Event()

    async def blocked_create(
        plan: BackupPlan,
        grant: Any,
        *,
        on_progress: Any,
        on_entry: Any,
    ) -> Any:
        del plan, grant, on_progress, on_entry
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(executor, "create", blocked_create)
    plan = cast(
        dict[str, Any],
        (
            await client.post(
                "/api/v1/backups/plan",
                json={"source": str(source), "destination": str(destination)},
            )
        ).json(),
    )
    accepted = cast(
        dict[str, Any],
        (
            await client.post(
                "/api/v1/backups",
                json={"plan_id": plan["id"], "request_authorization": True},
            )
        ).json(),
    )
    backup_id = cast(str, cast(dict[str, Any], accepted["backup"])["id"])
    await asyncio.wait_for(entered.wait(), timeout=2)

    response = await client.post(f"/api/v1/backups/{backup_id}/cancel")
    assert response.status_code == 200
    for _ in range(50):
        current = await client.get(f"/api/v1/backups/{backup_id}")
        payload = cast(dict[str, Any], current.json())
        if payload["status"] == "CANCELLED":
            break
        await asyncio.sleep(0.02)
    assert payload["status"] == "CANCELLED"
    assert cast(dict[str, Any], payload["execution"])["error_code"] == "BACKUP_CANCELLED"


class _SessionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str


def test_protection_checkpoint_gate_is_ready_for_future_mutations(app: FastAPI) -> None:
    manager = cast(CapabilityManager, app.state.capability_manager)
    metadata = manager.get("backup.create")
    assert metadata is not None
    protected = metadata.model_copy(update={"requires_protection_checkpoint": True})
    payload = _SessionPayload(session_id="checkpoint-session-123")

    with pytest.raises(PermissionError, match="verified protection checkpoint"):
        manager._validate_protection_checkpoint(protected, payload, None)

    wrong_session = ProtectionCheckpoint(
        status=ProtectionCheckpointStatus.READY,
        protected_resources=("resource:documents",),
        provider_capability_id="backup.create",
        backup_id="backup-12345678",
        verification_id="verification-12345678",
        session_id="another-session-123",
        evidence_sha256="a" * 64,
    )
    with pytest.raises(PermissionError, match="another session"):
        manager._validate_protection_checkpoint(protected, payload, wrong_session)

    ready = wrong_session.model_copy(update={"session_id": payload.session_id})
    manager._validate_protection_checkpoint(protected, payload, ready)


async def test_unix_broker_executor_reports_response_timeout(tmp_path: Path) -> None:
    socket_path = tmp_path / "slow.sock"
    tools = BackupFilesystemTools(tmp_path / "mountinfo")
    source = tmp_path / "src"
    destination = tmp_path / "dst"
    source.mkdir()
    destination.mkdir()
    (source / "file.txt").write_text("data", encoding="utf-8")
    device = os.stat(source).st_dev
    source_device = f"{os.major(device)}:{os.minor(device)}"
    tools.mountinfo_path.write_text(
        f"36 25 {source_device} / {source} rw - ext4 /dev/sda1 rw\n"
        f"37 25 99:99 / {destination} rw - ext4 /dev/sdb1 rw\n",
        encoding="utf-8",
    )
    plan = tools.build_plan(str(source), str(destination), __import__("ares.backup.models", fromlist=["BackupPolicy"]).BackupPolicy())

    async def slow(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readline()
        await asyncio.sleep(1)
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(slow, path=str(socket_path))
    executor = UnixBrokerBackupExecutor(socket_path, timeout_seconds=0.02)

    async def challenge(_: str) -> None:
        return None

    try:
        with pytest.raises(BackupExecutorError, match="BACKUP_BROKER_TIMEOUT"):
            await executor.request_authorization(
                plan, session_id="timeout-session-123", on_challenge=challenge
            )
    finally:
        server.close()
        await server.wait_closed()


def test_cli_parser_exposes_required_backup_commands() -> None:
    parser = cli_module.build_parser()
    plan = parser.parse_args(["backup", "plan", "/source", "/destination"])
    create = parser.parse_args(["backup", "create", "/source", "/destination"])
    listing = parser.parse_args(["backup", "list"])
    verify = parser.parse_args(["backup", "verify", "backup-12345678"])
    consent = parser.parse_args(["consent", "approve", "challenge-12345678"])

    assert (plan.backup_command, plan.source, plan.destination) == (
        "plan",
        "/source",
        "/destination",
    )
    assert create.backup_command == "create"
    assert listing.backup_command == "list"
    assert verify.backup_id == "backup-12345678"
    assert consent.consent_command == "approve"


async def test_cli_backup_plan_uses_same_service(
    client: AsyncClient,
    app: FastAPI,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    del client
    source, destination = _configure_mounts(app, tmp_path)
    (source / "file.txt").write_text("data", encoding="utf-8")
    args = argparse.Namespace(
        command="backup",
        backup_command="plan",
        source=str(source),
        destination=str(destination),
    )

    result = await cli_module._backup_command(args, app)

    assert result == 0
    output = capsys.readouterr().out
    assert '"authorization_required": true' in output
    service = cast(BackupService, app.state.backup_service)
    assert await service.list() == ()
