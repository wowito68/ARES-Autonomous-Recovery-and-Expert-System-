from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import cast

from fastapi import FastAPI
from httpx import AsyncClient

from ares.audit import MemoryAuditLedger
from ares.backup import BackupService, BackupStatus, BackupVerificationStatus
from ares.capabilities import CapabilityManager, CapabilityMode, RiskLevel
from ares.events import MemoryEventSink
from ares.knowledge import GraphKind, KnowledgeGraph
from ares.reasoning import ReasoningEngine, ReasoningRequest, ReasoningStatus
from ares.tools import BackupFilesystemTools


def _configure_mounts(app: FastAPI, tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "api-source"
    destination = tmp_path / "api-destination"
    source.mkdir()
    destination.mkdir()
    device = os.stat(source).st_dev
    source_device = f"{os.major(device)}:{os.minor(device)}"
    mountinfo = tmp_path / "api-mountinfo"
    mountinfo.write_text(
        f"36 25 {source_device} / {source} rw,relatime - ext4 /dev/sda1 rw\n"
        f"37 25 99:99 / {destination} rw,relatime - ext4 /dev/sdb1 rw\n",
        encoding="utf-8",
    )
    tools = cast(BackupFilesystemTools, app.state.backup_tools)
    tools.mountinfo_path = mountinfo
    return source, destination


async def _wait_terminal(client: AsyncClient, backup_id: str) -> dict[str, object]:
    for _ in range(100):
        response = await client.get(f"/api/v1/backups/{backup_id}")
        assert response.status_code == 200
        payload = cast(dict[str, object], response.json())
        if payload["status"] in {
            "COMPLETED",
            "FAILED",
            "CANCELLED",
            "CORRUPTED",
        }:
            return payload
        await asyncio.sleep(0.02)
    raise AssertionError("backup did not reach a terminal state")


async def test_backup_capabilities_are_registered_with_expected_policy(app: FastAPI) -> None:
    manager = cast(CapabilityManager, app.state.capability_manager)
    create = manager.get("backup.create")
    verify = manager.get("backup.verify")
    listing = manager.get("backup.list")

    assert create is not None
    assert create.mode is CapabilityMode.MUTATING
    assert create.risk is RiskLevel.MEDIUM
    assert create.requires_protection_checkpoint is False
    assert verify is not None and verify.mode is CapabilityMode.READ_ONLY
    assert listing is not None and listing.mode is CapabilityMode.READ_ONLY
    descriptor = manager.descriptor("backup.create")
    assert descriptor is not None
    assert "plan_id" in descriptor.input_schema["properties"]
    assert "manifest" in descriptor.output_schema["properties"]


async def test_reasoning_selects_backup_create_for_protection_goal(app: FastAPI) -> None:
    engine = cast(ReasoningEngine, app.state.reasoning_engine)

    result = engine.assess(
        ReasoningRequest(goal="Haz un respaldo de mis documentos antes de reparar el sistema")
    )

    assert result.status is ReasoningStatus.CAPABILITY_SELECTED
    assert result.selected_capability_id == "backup.create"


async def test_api_backup_plan_create_manifest_verification_list_and_graph(
    client: AsyncClient,
    app: FastAPI,
    tmp_path: Path,
) -> None:
    source, destination = _configure_mounts(app, tmp_path)
    (source / "document.txt").write_text("important", encoding="utf-8")
    (source / ".hidden").write_text("hidden", encoding="utf-8")

    plan_response = await client.post(
        "/api/v1/backups/plan",
        json={"source": str(source), "destination": str(destination)},
    )
    assert plan_response.status_code == 200
    plan = plan_response.json()
    assert plan["authorization_required"] is True
    assert plan["risk"] == "medium"
    assert plan["included_file_count"] == 2
    assert plan["destination"]["available_bytes"] >= plan["required_bytes"]

    denied_by_contract = await client.post(
        "/api/v1/backups",
        json={"plan_id": plan["id"], "request_authorization": False},
    )
    assert denied_by_contract.status_code == 409
    assert denied_by_contract.json()["code"] == "BACKUP_EXPLICIT_AUTHORIZATION_REQUEST_REQUIRED"

    create_response = await client.post(
        "/api/v1/backups",
        json={"plan_id": plan["id"], "request_authorization": True},
    )
    assert create_response.status_code == 202
    accepted = create_response.json()
    backup_id = accepted["backup"]["id"]

    backup = await _wait_terminal(client, backup_id)
    assert backup["status"] == "COMPLETED"
    assert backup["verification_status"] == "VERIFIED"
    assert backup["file_count"] == 2
    assert backup["checksum"]
    assert backup["encryption_status"] == "none"
    assert backup["compression_status"] == "none"

    manifest_response = await client.get(f"/api/v1/backups/{backup_id}/manifest")
    verification_response = await client.get(f"/api/v1/backups/{backup_id}/verification")
    list_response = await client.get("/api/v1/backups")
    assert manifest_response.status_code == 200
    assert manifest_response.json()["file_count"] == 2
    assert verification_response.status_code == 200
    assert verification_response.json()["status"] == "VERIFIED"
    assert list_response.status_code == 200
    assert any(item["id"] == backup_id for item in list_response.json()["backups"])

    graph = cast(KnowledgeGraph, app.state.knowledge_graph)
    snapshot = await graph.snapshot()
    kinds = {node.kind for node in snapshot.nodes}
    relations = {edge.relation for edge in snapshot.edges}
    assert GraphKind.BACKUP in kinds
    assert GraphKind.BACKUP_SOURCE in kinds
    assert GraphKind.BACKUP_DESTINATION in kinds
    assert GraphKind.BACKUP_VERIFICATION in kinds
    assert {"backed_up_to", "stored_on", "verified_by", "protects", "contains"} <= relations

    event_sink = cast(MemoryEventSink | object, app.state.event_bus.sink)
    # Production uses JSONL, but the test app still exposes events through the persisted journal.
    assert event_sink is not None
    audit = cast(MemoryAuditLedger, app.state.audit_ledger)
    assert any(record["event_type"] == "backup.planned" for record in audit.records)


async def test_api_verify_detects_corruption(
    client: AsyncClient, app: FastAPI, tmp_path: Path
) -> None:
    source, destination = _configure_mounts(app, tmp_path)
    (source / "document.txt").write_text("original", encoding="utf-8")
    plan = (
        await client.post(
            "/api/v1/backups/plan",
            json={"source": str(source), "destination": str(destination)},
        )
    ).json()
    accepted = (
        await client.post(
            "/api/v1/backups",
            json={"plan_id": plan["id"], "request_authorization": True},
        )
    ).json()
    backup_id = accepted["backup"]["id"]
    backup = await _wait_terminal(client, backup_id)
    assert backup["status"] == "COMPLETED"

    target = Path(cast(dict[str, str], backup["destination"])["backup_path"])
    (target / "document.txt").write_text("corrupted", encoding="utf-8")
    service = cast(BackupService, app.state.backup_service)
    verified = await service.verify(backup_id, session_id="verify-session-123")

    assert verified.backup.status is BackupStatus.CORRUPTED
    assert verified.verification.status is BackupVerificationStatus.CORRUPTED
    refreshed = await client.get(f"/api/v1/backups/{backup_id}")
    assert refreshed.json()["status"] == "CORRUPTED"


async def test_backup_api_returns_problem_details_for_missing_resources(
    client: AsyncClient,
) -> None:
    missing = "a" * 32
    for suffix in ("", "/manifest", "/verification"):
        response = await client.get(f"/api/v1/backups/{missing}{suffix}")
        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/problem+json")


async def test_backup_store_and_service_listing_are_shared(
    client: AsyncClient, app: FastAPI, tmp_path: Path
) -> None:
    del client
    source, destination = _configure_mounts(app, tmp_path)
    (source / "file.txt").write_text("data", encoding="utf-8")
    service = cast(BackupService, app.state.backup_service)
    plan = await service.plan(
        __import__("ares.backup", fromlist=["BackupPlanRequest"]).BackupPlanRequest(
            source=str(source), destination=str(destination)
        ),
        session_id="service-session-123",
    )
    accepted = await service.create(
        __import__("ares.backup", fromlist=["BackupCreateRequest"]).BackupCreateRequest(
            plan_id=plan.id, request_authorization=True
        ),
        session_id="service-session-123",
        created_by="test",
    )
    for _ in range(100):
        current = await service.get(accepted.backup.id)
        assert current is not None
        if current.status in {BackupStatus.COMPLETED, BackupStatus.FAILED}:
            break
        await asyncio.sleep(0.02)
    listed = await service.list()
    assert any(item.id == accepted.backup.id for item in listed)
