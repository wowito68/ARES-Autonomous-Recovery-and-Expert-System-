from __future__ import annotations

import argparse
import builtins
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

import pytest

from ares import cli
from ares.actions.base import ActionContext, ActionError
from ares.actions.disk import (
    AnalyzeDiskInventoryAction,
    ReadDiskInventoryAction,
    UpdateStorageGraphAction,
    _as_bool,
    _nested_string,
    _safe_optional_string,
)
from ares.backup.models import BackupStatus
from ares.events import EventBus, MemoryEventSink
from ares.filesystems.models import RepairExecutionStatus
from ares.knowledge import GraphKind, KnowledgeGraph
from ares.runtime import entrypoints


@dataclass
class _Dump:
    payload: dict[str, Any] = field(default_factory=lambda: {"ok": True})
    id: str = "fixture-id"
    executable: bool = True

    def model_dump_json(self, *, indent: int | None = None) -> str:
        return json.dumps(self.payload, indent=indent)

    def model_dump(self, *, mode: str = "python") -> dict[str, Any]:
        del mode
        return dict(self.payload)


@dataclass
class _Progress:
    percent: float = 100.0

    def model_dump(self, *, mode: str = "python") -> dict[str, Any]:
        del mode
        return {"percent": self.percent}


@dataclass
class _BackupExecution:
    authorization_challenge_id: str | None = "backup-challenge-1234"
    progress: _Progress = field(default_factory=_Progress)


@dataclass
class _BackupRecord(_Dump):
    id: str = "backup-record-1234"
    status: BackupStatus = BackupStatus.COMPLETED
    execution: _BackupExecution = field(default_factory=_BackupExecution)


@dataclass
class _AcceptedBackup(_Dump):
    backup: _BackupRecord = field(default_factory=_BackupRecord)


class _BackupService:
    records: ClassVar[list[_BackupRecord]] = [_BackupRecord()]

    async def plan(self, request: object, *, session_id: str) -> _Dump:
        del request, session_id
        return _Dump(payload={"kind": "backup-plan"}, id="backup-plan-1234")

    async def create(
        self,
        request: object,
        *,
        session_id: str,
        created_by: str,
    ) -> _AcceptedBackup:
        del request, session_id, created_by
        return _AcceptedBackup(payload={"accepted": True})

    async def get(self, backup_id: str) -> _BackupRecord | None:
        if backup_id == "missing":
            return None
        return _BackupRecord(id=backup_id)

    async def list(self) -> list[_BackupRecord]:
        return list(self.records)

    async def verify(self, backup_id: str, *, session_id: str) -> _Dump:
        del session_id
        return _Dump(payload={"verified": backup_id})


@dataclass
class _RepairExecution:
    authorization_challenge_id: str | None = "repair-challenge-1234"
    status: RepairExecutionStatus = RepairExecutionStatus.COMPLETED
    error_code: str | None = None


@dataclass
class _RepairRecord(_Dump):
    id: str = "repair-record-1234"
    execution: _RepairExecution = field(default_factory=_RepairExecution)


@dataclass
class _RepairPlan(_Dump):
    id: str = "repair-plan-1234"
    executable: bool = True


class _FilesystemService:
    plan_executable: ClassVar[bool] = True
    missing_record: ClassVar[bool] = False
    missing_plan: ClassVar[bool] = False

    async def inspect(self, request: object, *, session_id: str) -> _Dump:
        del request, session_id
        return _Dump(payload={"filesystem": "ext4"})

    async def plan(self, request: object, *, session_id: str) -> _RepairPlan:
        del request, session_id
        return _RepairPlan(executable=self.plan_executable)

    async def get(self, repair_id: str) -> _RepairRecord | None:
        if self.missing_record or repair_id == "missing":
            return None
        return _RepairRecord(id=repair_id)

    async def get_plan(self, plan_id: str) -> _RepairPlan | None:
        if self.missing_plan or plan_id == "missing-plan":
            return None
        return _RepairPlan(id=plan_id, executable=self.plan_executable)

    async def start(
        self,
        request: object,
        *,
        session_id: str,
        created_by: str,
    ) -> _RepairRecord:
        del request, session_id, created_by
        return _RepairRecord()


def _context(tmp_path: Path) -> tuple[ActionContext, MemoryEventSink, KnowledgeGraph]:
    sink = MemoryEventSink()
    bus = EventBus(sink)
    bus.prepare()
    graph = KnowledgeGraph(tmp_path / "graph.json")
    graph.prepare()
    return ActionContext("disk-execution-1234", bus, graph), sink, graph


def _inventory_payload() -> dict[str, Any]:
    return {
        "generation": 7,
        "smart_health": {"status": "degraded"},
        "probes": {
            "storage": {
                "status": "partial",
                "data": {
                    "blockdevices": [
                        {
                            "name": "sda",
                            "type": "disk",
                            "size": 1000,
                            "ro": "true",
                            "rm": 0,
                            "tran": " sata ",
                            "model": "Model\x00X",
                            "vendor": "Vendor",
                            "mountpoints": ["/", 7, "/very/long"],
                            "children": [
                                {
                                    "name": "sda1",
                                    "type": "part",
                                    "size": 500,
                                    "ro": False,
                                    "rm": False,
                                },
                                {"name": "bad name", "type": "part"},
                            ],
                        },
                        {
                            "name": "usb0",
                            "type": "weird",
                            "size": True,
                            "ro": 0,
                            "rm": "1",
                        },
                        "invalid",
                    ]
                },
            }
        },
    }


async def test_disk_inventory_actions_cover_read_analyze_and_graph(tmp_path: Path) -> None:
    context, sink, graph = _context(tmp_path)
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps(_inventory_payload()), encoding="utf-8")
    reader = ReadDiskInventoryAction(inventory)
    observed = await reader.run({}, context)
    assert observed["generation"] == 7
    assert observed["inventory_status"] == "partial"
    assert len(observed["devices"]) == 3
    assert observed["devices"][0]["model"] == "ModelX"
    assert observed["devices"][1]["parent"] == "disk:sda"
    assert observed["devices"][2]["type"] == "disk"
    assert observed["devices"][2]["size_bytes"] == 0

    analyzed = await AnalyzeDiskInventoryAction().run(observed, context)
    codes = {item["code"] for item in analyzed["findings"]}
    assert {"READ_ONLY_DISK_OBSERVED", "INVENTORY_PARTIAL", "SMART_NOT_EVALUATED"} <= codes
    assert analyzed["summary"]["disk_count"] == 2

    projected = await UpdateStorageGraphAction().run(analyzed, context)
    assert projected["disk_node_count"] == 2
    assert projected["partition_node_count"] == 1
    snapshot = await graph.snapshot()
    assert {GraphKind.SYSTEM, GraphKind.DISK, GraphKind.PARTITION} <= {
        node.kind for node in snapshot.nodes
    }
    assert sink.events[-1].name == "knowledge.graph.updated"

    await reader.compensate({}, context)
    analyzer = AnalyzeDiskInventoryAction()
    await analyzer.compensate({}, context)
    projector = UpdateStorageGraphAction()
    await projector.compensate({}, context)


async def test_disk_inventory_actions_cover_invalid_missing_symlink_large_and_empty_fixed(
    tmp_path: Path,
) -> None:
    context, _, _ = _context(tmp_path)
    missing = ReadDiskInventoryAction(tmp_path / "missing.json")
    with pytest.raises(ActionError, match="DISK_INVENTORY_UNAVAILABLE"):
        await missing.run({}, context)

    invalid_json = tmp_path / "invalid.json"
    invalid_json.write_text("not-json", encoding="utf-8")
    with pytest.raises(ActionError, match="DISK_INVENTORY_UNAVAILABLE"):
        await ReadDiskInventoryAction(invalid_json).run({}, context)

    invalid_shape = tmp_path / "shape.json"
    invalid_shape.write_text("[]", encoding="utf-8")
    with pytest.raises(ActionError, match="DISK_INVENTORY_INVALID"):
        await ReadDiskInventoryAction(invalid_shape).run({}, context)

    invalid_devices = tmp_path / "devices.json"
    invalid_devices.write_text(json.dumps({"probes": {"storage": {"data": {}}}}), encoding="utf-8")
    with pytest.raises(ActionError, match="DISK_INVENTORY_INVALID"):
        await ReadDiskInventoryAction(invalid_devices).run({}, context)

    target = tmp_path / "target.json"
    target.write_text(json.dumps(_inventory_payload()), encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(ActionError, match="DISK_INVENTORY_UNAVAILABLE"):
        await ReadDiskInventoryAction(link).run({}, context)

    large = tmp_path / "large.json"
    with large.open("wb") as handle:
        handle.truncate(4_000_001)
    with pytest.raises(ActionError, match="DISK_INVENTORY_TOO_LARGE"):
        await ReadDiskInventoryAction(large).run({}, context)

    with pytest.raises(ActionError, match="DISK_ANALYSIS_INPUT_INVALID"):
        await AnalyzeDiskInventoryAction().run({"devices": "bad"}, context)
    empty = await AnalyzeDiskInventoryAction().run(
        {"devices": [], "inventory_status": "ok", "smart_status": "ok"}, context
    )
    assert {item["code"] for item in empty["findings"]} == {"NO_FIXED_DISK_OBSERVED"}
    with pytest.raises(ActionError, match="GRAPH_INPUT_INVALID"):
        await UpdateStorageGraphAction().run({"devices": "bad"}, context)


def test_disk_action_helper_sanitizers_cover_types() -> None:
    assert _safe_optional_string("  abc\x00\n", 10) == "abc"
    assert _safe_optional_string(7, 10) is None
    assert _safe_optional_string("\x00", 10) is None
    assert _nested_string({"a": {"b": "value"}}, "a", "b") == "value"
    assert _nested_string({}, "a", "b") == "unknown"
    assert _as_bool(True) is True
    assert _as_bool(1) is True
    assert _as_bool(0) is False
    assert _as_bool("TRUE") is True
    assert _as_bool("no") is False


async def test_backup_cli_plan_create_cancel_complete_list_verify_and_unknown_command(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    application = argparse.Namespace(state=argparse.Namespace(backup_service=_BackupService()))
    parser = cli.build_parser()

    assert await cli._backup_command(parser.parse_args(["backup", "plan", "/src", "/dst"]), application) == 0
    assert await cli._backup_command(parser.parse_args(["backup", "list"]), application) == 0
    assert await cli._backup_command(parser.parse_args(["backup", "verify", "backup-1"]), application) == 0

    monkeypatch.setattr(builtins, "input", lambda _: "NO")
    assert await cli._backup_command(parser.parse_args(["backup", "create", "/src", "/dst"]), application) == 4
    monkeypatch.setattr(builtins, "input", lambda _: "REQUEST")
    assert await cli._backup_command(parser.parse_args(["backup", "create", "/src", "/dst"]), application) == 0

    assert await cli._backup_command(argparse.Namespace(backup_command="other"), application) == 1
    output = capsys.readouterr()
    assert "BACKUP PLAN" in output.out
    assert "Independent authorization required" in output.err


async def test_filesystem_cli_all_routes_and_safety_rejections(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    service = _FilesystemService()
    application = argparse.Namespace(state=argparse.Namespace(filesystem_repair_service=service))
    parser = cli.build_parser()

    assert await cli._filesystem_command(parser.parse_args(["filesystem", "inspect", "/dev/test"]), application) == 0
    assert await cli._filesystem_command(
        parser.parse_args(["filesystem", "repair", "plan", "/dev/test"]), application
    ) == 0
    _FilesystemService.plan_executable = False
    assert await cli._filesystem_command(
        parser.parse_args(["filesystem", "repair", "plan", "/dev/test"]), application
    ) == 3
    _FilesystemService.plan_executable = True

    assert await cli._filesystem_command(
        parser.parse_args(["filesystem", "repair", "status", "repair-1"]), application
    ) == 0
    assert await cli._filesystem_command(
        parser.parse_args(["filesystem", "repair", "status", "missing"]), application
    ) == 3

    _FilesystemService.missing_plan = True
    assert await cli._filesystem_command(
        parser.parse_args(["filesystem", "repair", "missing-plan"]), application
    ) == 3
    _FilesystemService.missing_plan = False

    monkeypatch.setattr(builtins, "input", lambda _: "NO")
    assert await cli._filesystem_command(
        parser.parse_args(["filesystem", "repair", "repair-plan-1234"]), application
    ) == 4
    monkeypatch.setattr(builtins, "input", lambda _: "REQUEST")
    assert await cli._filesystem_command(
        parser.parse_args(["filesystem", "repair", "repair-plan-1234"]), application
    ) == 0

    assert await cli._filesystem_command(
        argparse.Namespace(filesystem_command="repair", repair_action="plan", value=None, backup_id=None),
        application,
    ) == 2
    assert await cli._filesystem_command(
        argparse.Namespace(filesystem_command="repair", repair_action="status", value=None, backup_id=None),
        application,
    ) == 2
    assert await cli._filesystem_command(
        argparse.Namespace(
            filesystem_command="repair",
            repair_action="repair-plan-1234",
            value="extra",
            backup_id=None,
        ),
        application,
    ) == 2
    assert await cli._filesystem_command(argparse.Namespace(filesystem_command="other"), application) == 1
    assert "Independent high-risk authorization required" in capsys.readouterr().err


def test_runtime_entrypoints_route_exact_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def audit() -> None:
        calls.append("audit")

    async def broker() -> None:
        calls.append("broker")

    async def consent() -> None:
        calls.append("consent")

    monkeypatch.setattr(entrypoints, "serve_audit_writer", audit)
    monkeypatch.setattr(entrypoints, "serve_tool_broker", broker)
    monkeypatch.setattr(entrypoints, "serve_consent_agent", consent)

    for service in ("audit", "broker", "consent"):
        monkeypatch.setattr(sys, "argv", ["ares-runtime", service])
        assert entrypoints.main() == 0
    assert calls == ["audit", "broker", "consent"]
