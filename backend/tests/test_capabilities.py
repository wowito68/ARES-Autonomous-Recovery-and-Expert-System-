"""End-to-end contracts for the first ARES v2 capability."""

from __future__ import annotations

import json
from pathlib import Path

from httpx import ASGITransport, AsyncClient

from ares.config import Environment, LogFormat, Settings
from ares.main import create_app


def _inventory() -> dict[str, object]:
    return {
        "schema_version": 1,
        "generation": 2,
        "smart_health": {"status": "skipped_policy"},
        "probes": {
            "storage": {
                "status": "ok",
                "data": {
                    "blockdevices": [
                        {
                            "name": "sda",
                            "type": "disk",
                            "size": 1_000_000,
                            "ro": False,
                            "rm": False,
                            "vendor": "ARES",
                            "model": "Test Disk",
                            "children": [
                                {
                                    "name": "sda1",
                                    "type": "part",
                                    "size": 900_000,
                                    "ro": 0,
                                    "rm": 0,
                                    "fstype": "ext4",
                                    "uuid": "fixture-uuid",
                                    "mountpoints": ["/mnt/test"],
                                }
                            ],
                        },
                        {
                            "name": "sdb",
                            "type": "disk",
                            "size": 500_000,
                            "ro": "1",
                            "rm": "true",
                        },
                    ]
                },
            }
        },
    }


def _write_inventory(runtime: Path, payload: object | None = None) -> Path:
    path = runtime / "hardware/public/inventory-v1.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload if payload is not None else _inventory()), encoding="utf-8")
    return path


def _settings(tmp_path: Path, runtime: Path) -> Settings:
    return Settings(
        environment=Environment.TEST,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'capabilities.db'}",
        runtime_state_dir=runtime,
        capability_state_dir=tmp_path / "capability-state",
        storage_process_probes_enabled=False,
        log_level="CRITICAL",
        log_format=LogFormat.TEXT,
    )


async def test_disk_analysis_runs_full_audited_vertical(tmp_path: Path) -> None:
    runtime = tmp_path / "run"
    _write_inventory(runtime)
    application = create_app(_settings(tmp_path, runtime))

    async with application.router.lifespan_context(application):
        transport = ASGITransport(app=application, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            catalog = await client.get(
                "/api/v1/capabilities",
                params={"query": "disco", "category": "storage"},
            )
            detail = await client.get("/api/v1/capabilities/storage.disk-analysis")
            response = await client.post(
                "/api/v1/capabilities/storage.disk-analysis/executions",
                json={"scope": "all_detected"},
            )
            execution_id = response.json()["id"]
            execution = await client.get(f"/api/v1/capabilities/executions/{execution_id}")
            graph = await client.get("/api/v1/knowledge/graph")
            needs_evidence = await client.post(
                "/api/v1/reasoning/assess",
                json={"goal": "Analizar los discos del equipo"},
            )
            selected = await client.post(
                "/api/v1/reasoning/assess",
                json={
                    "goal": "Analizar los discos del equipo",
                    "evidence": [{"id": "hardware.block-devices", "confidence": 1}],
                },
            )

    assert catalog.status_code == 200
    assert catalog.json()["count"] == 1
    public_capability = detail.json()
    assert public_capability["id"] == "storage.disk-analysis"
    assert public_capability["risk_level"] == "low"
    assert public_capability["mode"] == "read_only"
    assert public_capability["output_schema"]["title"] == "StorageCapabilityResult"
    assert "internal_actions" not in public_capability

    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "succeeded"
    snapshot = result["result"]["snapshot"]
    assert snapshot["summary"] == {
        "disk_count": 2,
        "partition_count": 1,
        "filesystem_count": 1,
        "mounted_filesystem_count": 0,
        "total_capacity_bytes": 1_500_000,
    }
    assert result["result"]["knowledge_graph"]["revision"] == 1
    assert len(result["steps"]) == 4
    assert execution.json() == result

    graph_payload = graph.json()
    assert graph_payload["revision"] == 1
    kinds = {node["kind"] for node in graph_payload["nodes"]}
    assert {"system", "disk", "partition", "filesystem", "smart_status"} <= kinds
    assert needs_evidence.json()["status"] == "needs_evidence"
    assert needs_evidence.json()["requested_evidence"] == ["hardware.block-devices"]
    assert selected.json()["status"] == "capability_selected"
    assert selected.json()["selected_capability_id"] == "storage.disk-analysis"

    events = [
        json.loads(line)
        for line in (tmp_path / "capability-state/events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    names = [event["event_type"] for event in events]
    assert names[0:2] == ["workflow.started", "capability.started"]
    assert "storage.snapshot.created" in names
    assert "knowledge.graph.updated" in names
    assert names[-2:] == ["workflow.completed", "capability.completed"]
    assert all(event["correlation_id"] == execution_id for event in events)
    assert all(event["session_id"] for event in events)
    assert all(event["event_id"] for event in events)
    assert all(event["timestamp"] for event in events)
    assert not any("wipefs" in json.dumps(event).casefold() for event in events)
    receipt = events[-1]["payload"]["result_receipt"]
    assert len(receipt["sha256"]) == 64
    assert receipt["bytes"] > 0
    assert set(receipt["top_level_fields"]) == {"knowledge_graph", "snapshot"}


async def test_capability_http_boundary_rejects_unknown_and_command_fields(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "run"
    _write_inventory(runtime)
    application = create_app(_settings(tmp_path, runtime))

    async with application.router.lifespan_context(application):
        transport = ASGITransport(app=application, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            unknown = await client.get("/api/v1/capabilities/storage.unknown")
            unknown_execution = await client.post(
                "/api/v1/capabilities/storage.unknown/executions",
                json={},
            )
            invalid = await client.post(
                "/api/v1/capabilities/storage.disk-analysis/executions",
                json={"scope": "all_detected", "command": "wipefs --all /dev/sda"},
            )
            missing_execution = await client.get("/api/v1/capabilities/executions/not-an-execution")

    assert unknown.status_code == 404
    assert unknown.json()["code"] == "CAPABILITY_NOT_FOUND"
    assert unknown_execution.status_code == 404
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "REQUEST_VALIDATION_FAILED"
    assert "wipefs" not in invalid.text
    assert missing_execution.status_code == 404
    assert missing_execution.json()["code"] == "CAPABILITY_EXECUTION_NOT_FOUND"


async def test_disk_analysis_failure_is_safe_and_fully_recorded(tmp_path: Path) -> None:
    runtime = tmp_path / "run"
    _write_inventory(runtime, {"probes": {}})
    application = create_app(_settings(tmp_path, runtime))

    async with application.router.lifespan_context(application):
        transport = ASGITransport(app=application, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post(
                "/api/v1/capabilities/storage.disk-analysis/executions",
                json={},
            )

    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert response.json()["error_code"] == "STORAGE_EVIDENCE_UNAVAILABLE"
    assert response.json()["result"] is None
    journal = (tmp_path / "capability-state/events.jsonl").read_text(encoding="utf-8")
    assert "action.failed" in journal
    assert "workflow.failed" in journal
    assert "STORAGE_EVIDENCE_UNAVAILABLE" in journal
