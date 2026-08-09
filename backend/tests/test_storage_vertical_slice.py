"""Integration tests for the storage API, reasoning, stores and CLI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from ares import cli
from ares.config import Environment, LogFormat, Settings
from ares.diagnostics import DiagnosticStore
from ares.main import create_app
from ares.storage import StorageSnapshotStore, build_storage_snapshot
from ares.tools import (
    BlockDeviceProbe,
    MountProbe,
    SmartProbe,
    StorageEvidence,
    ToolAvailability,
    UsageProbe,
)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        environment=Environment.TEST,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'storage.db'}",
        runtime_state_dir=tmp_path / "run",
        capability_state_dir=tmp_path / "state",
        storage_process_probes_enabled=False,
        log_level="CRITICAL",
        log_format=LogFormat.TEXT,
    )


def _write_inventory(settings: Settings) -> None:
    path = settings.runtime_state_dir / "hardware/public/inventory-v1.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "probes": {
                    "storage": {
                        "status": "ok",
                        "data": {
                            "blockdevices": [
                                {
                                    "name": "sda",
                                    "type": "disk",
                                    "size": 2_000,
                                    "model": "Fixture SSD",
                                    "children": [
                                        {
                                            "name": "sda1",
                                            "type": "part",
                                            "size": 1_500,
                                            "fstype": "ext4",
                                        }
                                    ],
                                }
                            ]
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )


async def test_storage_api_runs_vertical_slice_and_retrieves_artifacts(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_inventory(settings)
    application = create_app(settings)

    async with application.router.lifespan_context(application):
        transport = ASGITransport(app=application, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            analysis = await client.post(
                "/api/v1/storage/analyze",
                headers={"X-Request-ID": "storage-api-test-123"},
            )
            payload = analysis.json()
            disks = await client.get("/api/v1/storage/disks")
            snapshot = await client.get(f"/api/v1/storage/snapshots/{payload['snapshot_id']}")
            diagnostic = await client.get(f"/api/v1/diagnostics/{payload['diagnostic_id']}")
            missing_snapshot = await client.get("/api/v1/storage/snapshots/does-not-exist")
            missing_diagnostic = await client.get("/api/v1/diagnostics/does-not-exist")

    assert analysis.status_code == 200
    assert payload["diagnostic"]["snapshot_id"] == payload["snapshot_id"]
    assert payload["diagnostic"]["confidence"] >= 0.5
    assert "Estado:" in payload["message"]
    assert disks.status_code == 200
    assert disks.json()["count"] == 1
    assert disks.json()["disks"][0]["model"] == "Fixture SSD"
    assert snapshot.status_code == diagnostic.status_code == 200
    assert snapshot.json()["id"] == payload["snapshot_id"]
    assert diagnostic.json()["id"] == payload["diagnostic_id"]
    assert missing_snapshot.status_code == missing_diagnostic.status_code == 404
    assert missing_snapshot.json()["code"] == "STORAGE_SNAPSHOT_NOT_FOUND"
    assert missing_diagnostic.json()["code"] == "DIAGNOSTIC_NOT_FOUND"

    state_dir = settings.capability_state_dir
    assert state_dir is not None
    events = [
        json.loads(line)
        for line in (state_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    diagnostic_events = [item for item in events if item["event_type"] == "diagnostic.generated"]
    assert len(diagnostic_events) == 1
    assert diagnostic_events[0]["session_id"] == "storage-api-test-123"
    assert diagnostic_events[0]["payload"]["decision"] == "diagnostic_generated"


async def test_storage_api_returns_problem_details_without_evidence(tmp_path: Path) -> None:
    application = create_app(_settings(tmp_path))
    async with application.router.lifespan_context(application):
        transport = ASGITransport(app=application, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post("/api/v1/storage/analyze")
            disks = await client.get("/api/v1/storage/disks")

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "STORAGE_EVIDENCE_UNAVAILABLE"
    assert disks.json() == {"disks": [], "count": 0}


def _diagnostic_snapshot(*, smart_passed: bool | None, used_percent: float) -> StorageEvidence:
    return StorageEvidence(
        devices=(
            BlockDeviceProbe(
                name="sda",
                path="/dev/sda",
                device_type="disk",
                size_bytes=10_000,
                hardware_identity="fixture-disk",
            ),
            BlockDeviceProbe(
                name="sda1",
                path="/dev/sda1",
                device_type="part",
                size_bytes=9_000,
                parent_path="/dev/sda",
                filesystem_type="ext4" if smart_passed is not None else None,
                mountpoints=("/data",),
            ),
        ),
        mounts=(
            MountProbe(
                source="/dev/sda1",
                target="/data",
                filesystem_type="ext4",
                options=("rw",),
            ),
        ),
        usage=(
            UsageProbe(
                source="/dev/sda1",
                target="/data",
                total_bytes=10_000,
                used_bytes=int(used_percent * 100),
                available_bytes=max(0, 10_000 - int(used_percent * 100)),
                used_percent=used_percent,
            ),
        ),
        smart=(
            SmartProbe(
                device="/dev/sda",
                status=(
                    "passed"
                    if smart_passed is True
                    else "failed"
                    if smart_passed is False
                    else "unavailable"
                ),
                passed=smart_passed,
                reason=None if smart_passed is not None else "tool_not_installed",
            ),
        ),
        tool_availability=(
            ToolAvailability(tool="lsblk", available=True),
            ToolAvailability(
                tool="smartctl",
                available=smart_passed is not None,
                reason=None if smart_passed is not None else "tool_not_installed",
            ),
        ),
    )


async def test_reasoning_reports_capacity_and_real_smart_failure(tmp_path: Path) -> None:
    application = create_app(_settings(tmp_path))
    snapshot = build_storage_snapshot(
        _diagnostic_snapshot(smart_passed=False, used_percent=97),
        "diagnostic-session",
    )
    diagnostic = application.state.reasoning_engine.diagnose_storage(snapshot)

    assert diagnostic.severity.value == "error"
    assert {finding.type for finding in diagnostic.findings} >= {
        "storage_capacity",
        "storage_health",
    }
    assert diagnostic.affected_resources
    assert diagnostic.recommendations
    assert diagnostic.needs_more_evidence is False


async def test_missing_smart_is_limitation_not_disk_failure(tmp_path: Path) -> None:
    application = create_app(_settings(tmp_path))
    snapshot = build_storage_snapshot(
        _diagnostic_snapshot(smart_passed=None, used_percent=20),
        "diagnostic-session",
    )
    diagnostic = application.state.reasoning_engine.diagnose_storage(snapshot)

    assert not any(finding.type == "storage_health" for finding in diagnostic.findings)
    assert any("SMART no disponible" in item for item in diagnostic.limitations)
    assert any("filesystem desconocido" in item for item in diagnostic.limitations)
    assert diagnostic.needs_more_evidence is True


async def test_snapshot_and_diagnostic_stores_round_trip(tmp_path: Path) -> None:
    snapshot_store = StorageSnapshotStore(tmp_path / "snapshots")
    diagnostic_store = DiagnosticStore(tmp_path / "diagnostics")
    snapshot_store.prepare()
    diagnostic_store.prepare()
    snapshot = build_storage_snapshot(
        _diagnostic_snapshot(smart_passed=True, used_percent=10),
        "store-session",
    )
    application = create_app(_settings(tmp_path / "app"))
    diagnostic = application.state.reasoning_engine.diagnose_storage(snapshot)

    await snapshot_store.put(snapshot)
    await diagnostic_store.put(diagnostic)

    assert await snapshot_store.get(snapshot.id) == snapshot
    assert await snapshot_store.latest() == snapshot
    assert await snapshot_store.get("../bad") is None
    assert await diagnostic_store.get(diagnostic.id) == diagnostic
    assert await diagnostic_store.get("../bad") is None

    reloaded = StorageSnapshotStore(tmp_path / "snapshots")
    reloaded.prepare()
    assert await reloaded.latest() == snapshot


async def test_cli_analyze_and_snapshot_use_same_application_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = _settings(tmp_path)
    _write_inventory(settings)

    def application() -> FastAPI:
        return create_app(settings)

    monkeypatch.setattr(cli, "create_app", application)
    analyze_args = argparse.Namespace(command="storage", storage_command="analyze")
    assert await cli._main(analyze_args) == 0
    analyzed = json.loads(capsys.readouterr().out)
    assert analyzed["snapshot_id"]

    snapshot_args = argparse.Namespace(
        command="storage",
        storage_command="snapshot",
        snapshot_id=analyzed["snapshot_id"],
    )
    assert await cli._main(snapshot_args) == 0
    loaded = json.loads(capsys.readouterr().out)
    assert loaded["id"] == analyzed["snapshot_id"]

    missing_args = argparse.Namespace(
        command="storage",
        storage_command="snapshot",
        snapshot_id="missing-snapshot",
    )
    assert await cli._main(missing_args) == 3
    assert "not found" in capsys.readouterr().err


def test_cli_parser_exposes_storage_commands() -> None:
    parser = cli.build_parser()
    analyze = parser.parse_args(["storage", "analyze"])
    snapshot = parser.parse_args(["storage", "snapshot", "snapshot-id"])
    assert analyze.storage_command == "analyze"
    assert snapshot.snapshot_id == "snapshot-id"
