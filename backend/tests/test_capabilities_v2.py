"""End-to-end contracts for ARES v2 and the Disk Analysis reference capability."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from ares.actions import ActionContext, ActionError, AnalyzeDiskInventoryAction
from ares.config import Environment, LogFormat, Settings
from ares.events import EventBus, MemoryEventSink
from ares.knowledge import KnowledgeGraph
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
                            "size": 512_000_000_000,
                            "ro": False,
                            "rm": False,
                            "model": " ARES Test SSD\x00 ",
                            "vendor": "Test",
                            "tran": "sata",
                            "serial": "must-never-escape",
                            "children": [
                                {
                                    "name": "sda1",
                                    "type": "part",
                                    "size": 1_000_000,
                                    "mountpoints": ["/boot", 4],
                                }
                            ],
                        },
                        {
                            "name": "sdb",
                            "type": "disk",
                            "size": 8_000_000_000,
                            "ro": "true",
                            "rm": 1,
                            "tran": "usb",
                        },
                        {"name": "../../bad", "type": "disk", "size": 4},
                    ]
                },
            }
        },
    }


def _write_inventory(runtime: Path, payload: object | None = None) -> Path:
    path = runtime / "hardware/public/inventory-v1.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_inventory() if payload is None else payload), encoding="utf-8")
    return path


def _settings(tmp_path: Path, runtime: Path) -> Settings:
    return Settings(
        environment=Environment.TEST,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'ares-v2.db'}",
        runtime_state_dir=runtime,
        log_level="CRITICAL",
        log_format=LogFormat.TEXT,
    )


async def test_disk_analysis_flows_through_catalog_workflow_events_and_graph(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "run"
    _write_inventory(runtime)
    application = create_app(_settings(tmp_path, runtime))

    async with application.router.lifespan_context(application):
        transport = ASGITransport(app=application, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            catalog = await client.get("/api/v1/capabilities", params={"query": "disco"})
            filtered = await client.get("/api/v1/capabilities", params={"category": "storage"})
            missing_filter = await client.get(
                "/api/v1/capabilities", params={"query": "inexistente"}
            )
            detail = await client.get("/api/v1/capabilities/storage.disk-analysis")
            executed = await client.post(
                "/api/v1/capabilities/storage.disk-analysis/executions",
                json={"scope": "all_detected"},
            )
            record = await client.get(f"/api/v1/capabilities/executions/{executed.json()['id']}")
            graph = await client.get("/api/v1/knowledge/graph")

    assert catalog.status_code == filtered.status_code == detail.status_code == 200
    assert catalog.json()["count"] == filtered.json()["count"] == 1
    assert missing_filter.json() == {"capabilities": [], "count": 0}
    public_metadata = detail.json()
    assert public_metadata["risk_level"] == "low"
    assert public_metadata["operation_class"] == "observe"
    assert "internal_actions" not in public_metadata

    assert executed.status_code == 200
    execution = executed.json()
    assert execution["status"] == "succeeded"
    assert len(execution["steps"]) == 3
    assert all("action_id" not in step for step in execution["steps"])
    assert record.json() == execution
    result = execution["result"]
    assert result["summary"] == {
        "disk_count": 2,
        "fixed_disk_count": 1,
        "removable_disk_count": 1,
        "read_only_disk_count": 1,
        "total_capacity_bytes": 520_000_000_000,
    }
    assert {finding["code"] for finding in result["findings"]} == {
        "READ_ONLY_DISK_OBSERVED",
        "SMART_NOT_EVALUATED",
    }
    assert "must-never-escape" not in json.dumps(result)
    assert result["devices"][0]["model"] == "ARES Test SSD"
    assert graph.json()["revision"] == 1
    assert {node["id"] for node in graph.json()["nodes"]} == {
        "system:local",
        "disk:sda",
        "disk:sda1",
        "disk:sdb",
    }

    event_path = tmp_path / "capabilities/events.jsonl"
    events = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()]
    event_names = [event["name"] for event in events]
    assert event_names[0:2] == ["workflow.started", "capability.started"]
    assert "knowledge.graph.updated" in event_names
    assert event_names[-2:] == ["workflow.completed", "capability.completed"]
    assert {event["correlation_id"] for event in events} == {execution["id"]}


async def test_capability_api_rejects_unknown_ids_inputs_and_missing_inventory(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "run"
    application = create_app(_settings(tmp_path, runtime))

    async with application.router.lifespan_context(application):
        transport = ASGITransport(app=application, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            missing = await client.get("/api/v1/capabilities/storage.missing")
            missing_execution = await client.get("/api/v1/capabilities/executions/does-not-exist")
            invalid = await client.post(
                "/api/v1/capabilities/storage.disk-analysis/executions",
                json={"scope": "all_detected", "command": "rm"},
            )
            failed = await client.post(
                "/api/v1/capabilities/storage.disk-analysis/executions",
                json={"scope": "all_detected"},
            )

    assert missing.status_code == missing_execution.status_code == 404
    assert missing.json()["code"] == "CAPABILITY_NOT_FOUND"
    assert missing_execution.json()["code"] == "CAPABILITY_EXECUTION_NOT_FOUND"
    assert invalid.status_code == 422
    assert failed.status_code == 200
    assert failed.json()["status"] == "failed"
    assert failed.json()["error_code"] == "DISK_INVENTORY_UNAVAILABLE"


async def test_reasoning_requests_evidence_selects_only_a_capability_and_stops(
    tmp_path: Path,
) -> None:
    application = create_app(_settings(tmp_path, tmp_path / "run"))

    async with application.router.lifespan_context(application):
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            stopped = await client.post(
                "/api/v1/reasoning/assess",
                json={"goal": "traducir una novela"},
            )
            evidence = await client.post(
                "/api/v1/reasoning/assess",
                json={"goal": "analizar capacidad del disco"},
            )
            selected = await client.post(
                "/api/v1/reasoning/assess",
                json={
                    "goal": "analizar capacidad del disco",
                    "evidence": [{"id": "hardware.block-devices", "confidence": 0.9}],
                },
            )

    assert stopped.json()["status"] == "stopped"
    assert evidence.json()["status"] == "needs_evidence"
    assert evidence.json()["requested_evidence"] == ["hardware.block-devices"]
    decision = selected.json()
    assert decision["status"] == "capability_selected"
    assert decision["selected_capability_id"] == "storage.disk-analysis"
    assert "command" not in json.dumps(decision).casefold()
    assert "action" not in json.dumps(decision).casefold()


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ([], "DISK_INVENTORY_INVALID"),
        ({"probes": {}}, "DISK_INVENTORY_INVALID"),
        ("not-json", "DISK_INVENTORY_UNAVAILABLE"),
    ],
)
def test_inventory_reader_fails_closed_for_invalid_evidence(
    tmp_path: Path,
    payload: object,
    code: str,
) -> None:
    from ares.actions import ReadDiskInventoryAction

    path = tmp_path / "inventory.json"
    if payload == "not-json":
        path.write_text("{", encoding="utf-8")
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ActionError, match=code):
        ReadDiskInventoryAction(path)._read()


async def test_disk_analyzer_reports_partial_empty_inventory(tmp_path: Path) -> None:
    sink = MemoryEventSink()
    context = ActionContext(
        execution_id="execution-test",
        event_bus=EventBus(sink),
        graph=KnowledgeGraph(tmp_path / "graph.json"),
    )
    result = await AnalyzeDiskInventoryAction().run(
        {
            "devices": [],
            "inventory_status": "partial",
            "smart_status": "unavailable",
        },
        context,
    )

    assert result["summary"]["disk_count"] == 0
    assert {finding["code"] for finding in result["findings"]} == {
        "NO_FIXED_DISK_OBSERVED",
        "INVENTORY_PARTIAL",
        "SMART_NOT_EVALUATED",
    }

    with pytest.raises(ActionError, match="DISK_ANALYSIS_INPUT_INVALID"):
        await AnalyzeDiskInventoryAction().run({}, context)
