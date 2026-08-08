"""Deterministic tests for the read-only storage Tool Layer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ares.events import EventBus, MemoryEventSink
from ares.tools import (
    ProcessResult,
    ReadOnlyStorageProcessRunner,
    StorageToolSuite,
    ToolAvailability,
    parse_blkid_export,
    parse_df_output,
    parse_findmnt_json,
    parse_lsblk_json,
    parse_smartctl_json,
)
from ares.tools.storage import detect_operating_systems


class FakeRunner:
    def __init__(
        self,
        results: dict[str, ProcessResult] | None = None,
        *,
        unavailable: dict[str, str] | None = None,
        failures: dict[str, Exception] | None = None,
    ) -> None:
        self.results = results or {}
        self.unavailable = unavailable or {}
        self.failures = failures or {}
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def inspect(self, tool: str) -> ToolAvailability:
        if tool in self.unavailable:
            return ToolAvailability(tool=tool, available=False, reason=self.unavailable[tool])
        if tool in self.results or tool in self.failures or tool in {"blkid", "smartctl"}:
            return ToolAvailability(tool=tool, available=True)
        return ToolAvailability(tool=tool, available=False, reason="tool_not_installed")

    async def run(self, tool: str, args: tuple[str, ...], *, timeout: float) -> ProcessResult:
        del timeout
        self.calls.append((tool, args))
        failure = self.failures.get(tool)
        if failure is not None:
            raise failure
        return self.results[tool]


def _result(tool: str, stdout: str, exit_code: int = 0) -> ProcessResult:
    return ProcessResult(
        tool=tool,
        exit_code=exit_code,
        stdout=stdout,
        stderr="",
        duration_ms=1.5,
    )


def _inventory(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "probes": {
                    "storage": {
                        "data": {
                            "blockdevices": [
                                {
                                    "name": "vda",
                                    "type": "disk",
                                    "size": 1000,
                                    "children": [
                                        {
                                            "name": "vda1",
                                            "type": "part",
                                            "size": 900,
                                            "fstype": "ext4",
                                            "mountpoints": ["/fixture"],
                                        }
                                    ],
                                }
                            ]
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )


def test_parse_lsblk_handles_multiple_partitions_and_redacts_identity() -> None:
    payload = {
        "blockdevices": [
            {
                "name": "sda",
                "path": "/dev/sda",
                "type": "disk",
                "size": 1000,
                "ro": False,
                "rm": False,
                "model": "SSD",
                "serial": "secret-serial",
                "wwn": "secret-wwn",
                "children": [
                    {
                        "name": "sda1",
                        "path": "/dev/sda1",
                        "type": "part",
                        "size": 400,
                        "pkname": "sda",
                        "fstype": "ext4",
                        "uuid": "uuid-a",
                        "mountpoints": ["/fixture", None],
                    },
                    {
                        "name": "sda2",
                        "path": "/dev/sda2",
                        "type": "part",
                        "size": 600,
                        "pkname": "sda",
                        "fstype": None,
                        "mountpoints": [],
                    },
                ],
            }
        ]
    }
    devices = parse_lsblk_json(json.dumps(payload))
    assert [item.path for item in devices] == ["/dev/sda", "/dev/sda1", "/dev/sda2"]
    assert devices[0].hardware_identity is not None
    assert "secret" not in devices[0].hardware_identity
    assert devices[1].parent_path == "/dev/sda"
    assert devices[1].filesystem_type == "ext4"
    assert devices[2].filesystem_type is None


@pytest.mark.parametrize("payload", ["{", "{}", "[]"])
def test_parse_lsblk_rejects_invalid_shapes(payload: str) -> None:
    with pytest.raises(ValueError):
        parse_lsblk_json(payload)


def test_parse_blkid_findmnt_df_and_smart() -> None:
    blkid = parse_blkid_export(
        "DEVNAME=/dev/sda1\nUUID=abc\nTYPE=ext4\nLABEL=ROOT\nVERSION=1.0\n\n"
        "DEVNAME=../../bad\nTYPE=xfs\n"
    )
    assert len(blkid) == 1
    assert blkid[0].filesystem_type == "ext4"
    assert blkid[0].uuid == "abc"

    mounts = parse_findmnt_json(
        json.dumps(
            {
                "filesystems": [
                    {
                        "source": "/dev/sda1",
                        "target": "/fixture",
                        "fstype": "ext4",
                        "options": "rw,relatime",
                    },
                    {"source": "tmpfs", "target": "relative"},
                ]
            }
        )
    )
    assert mounts[0].target == "/fixture"
    assert mounts[0].options == ("rw", "relatime")

    usage = parse_df_output(
        "Filesystem 1B-blocks Used Available Use% Mounted on\n"
        "/dev/sda1 1000 900 100 90% /fixture\n"
        "broken line\n"
    )
    assert usage[0].used_percent == 90
    assert usage[0].available_bytes == 100

    smart = parse_smartctl_json(
        json.dumps({"smart_status": {"passed": True}, "temperature": {"current": 37}}),
        "/dev/sda",
    )
    assert smart.status == "passed"
    assert smart.passed is True
    assert smart.temperature_celsius == 37


@pytest.mark.parametrize(
    ("text", "device"),
    [("{", "/dev/sda"), ("{}", "../../bad")],
)
def test_smart_parser_rejects_invalid_input(text: str, device: str) -> None:
    with pytest.raises(ValueError):
        parse_smartctl_json(text, device)


async def test_tool_suite_collects_structured_results_and_broker_gates_device_tools(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(
        {
            "lsblk": _result(
                "lsblk",
                json.dumps(
                    {
                        "blockdevices": [
                            {
                                "name": "sda",
                                "path": "/dev/sda",
                                "type": "disk",
                                "size": 1000,
                                "ro": False,
                                "rm": False,
                                "children": [
                                    {
                                        "name": "sda1",
                                        "path": "/dev/sda1",
                                        "type": "part",
                                        "size": 900,
                                        "pkname": "sda",
                                        "fstype": "ext4",
                                        "mountpoints": ["/fixture"],
                                    }
                                ],
                            }
                        ]
                    }
                ),
            ),
            "findmnt": _result(
                "findmnt",
                json.dumps(
                    {
                        "filesystems": [
                            {
                                "source": "/dev/sda1",
                                "target": "/fixture",
                                "fstype": "ext4",
                                "options": "rw",
                            }
                        ]
                    }
                ),
            ),
            "df": _result(
                "df",
                "Filesystem 1B-blocks Used Available Use% Mounted on\n"
                "/dev/sda1 1000 910 90 91% /fixture\n",
            ),
        }
    )
    sink = MemoryEventSink()
    evidence = await StorageToolSuite(
        tmp_path / "missing.json",
        runner=runner,
    ).collect(EventBus(sink), "correlation-123")

    assert len(evidence.devices) == 2
    assert evidence.mounts[0].target == "/fixture"
    assert evidence.usage[0].used_percent == 91
    availability = {item.tool: item for item in evidence.tool_availability}
    assert availability["lsblk"].available is True
    assert availability["smartctl"].available is False
    assert availability["smartctl"].reason == "privileged_broker_required"
    assert evidence.smart[0].status == "unavailable"
    assert evidence.smart[0].reason == "privileged_broker_required"
    assert {event.name for event in sink.events} >= {
        "tool.execution.started",
        "tool.execution.completed",
    }
    assert {call[0] for call in runner.calls} == {"lsblk", "findmnt", "df"}


@pytest.mark.parametrize(
    ("failure", "expected_reason"),
    [
        (TimeoutError(), "timeout"),
        (PermissionError("permission_denied"), "permission_denied"),
        (OSError("command_failed"), "command_failed"),
    ],
)
async def test_tool_suite_degrades_on_timeout_permission_and_command_failure(
    tmp_path: Path,
    failure: Exception,
    expected_reason: str,
) -> None:
    inventory = tmp_path / "inventory.json"
    _inventory(inventory)
    runner = FakeRunner(
        {
            "findmnt": _result("findmnt", '{"filesystems": []}'),
            "df": _result("df", "Filesystem 1B-blocks Used Available Use% Mounted on\n"),
        },
        failures={"lsblk": failure},
        unavailable={"smartctl": "tool_not_installed", "blkid": "tool_not_installed"},
    )
    evidence = await StorageToolSuite(inventory, runner=runner).collect(
        EventBus(MemoryEventSink()),
        "correlation-456",
    )
    availability = {item.tool: item for item in evidence.tool_availability}
    assert availability["lsblk"].reason == expected_reason
    assert evidence.devices[0].path == "/dev/vda"
    assert "using_boot_inventory_fallback" in evidence.warnings
    assert evidence.smart[0].reason == "tool_not_installed"


async def test_disabled_process_probes_use_fixture_inventory_only(tmp_path: Path) -> None:
    inventory = tmp_path / "inventory.json"
    _inventory(inventory)
    runner = FakeRunner()
    evidence = await StorageToolSuite(
        inventory,
        runner=runner,
        process_probes_enabled=False,
    ).collect(EventBus(MemoryEventSink()), "correlation-789")
    assert evidence.devices[0].name == "vda"
    assert runner.calls == []
    assert {item.reason for item in evidence.tool_availability if item.tool in {"lsblk", "df"}} == {
        "process_probes_disabled"
    }


async def test_read_only_runner_rejects_device_and_argument_mutation_paths() -> None:
    delegate = FakeRunner(
        {
            "lsblk": _result("lsblk", '{"blockdevices": []}'),
            "smartctl": _result("smartctl", "{}"),
        }
    )
    runner = ReadOnlyStorageProcessRunner(delegate)

    with pytest.raises(PermissionError, match="read_only_policy_rejected"):
        await runner.run("smartctl", ("--all", "/dev/sda"), timeout=1)
    with pytest.raises(PermissionError, match="read_only_policy_rejected"):
        await runner.run("lsblk", ("--discard",), timeout=1)
    with pytest.raises(PermissionError, match="read_only_policy_rejected"):
        await runner.run("wipefs", ("--all", "/dev/sda"), timeout=1)
    assert delegate.calls == []
    assert runner.inspect("wipefs").reason == "read_only_policy_rejected"


def test_detect_operating_systems_reads_only_existing_os_release(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "etc").mkdir(parents=True)
    (root / "etc/os-release").write_text(
        'NAME="Fixture Linux"\nVERSION_ID="1"\nID=fixture\n',
        encoding="utf-8",
    )
    mounts = parse_findmnt_json(
        json.dumps(
            {
                "filesystems": [
                    {
                        "source": "/dev/sda1",
                        "target": str(root),
                        "fstype": "ext4",
                        "options": "ro",
                    }
                ]
            }
        )
    )
    detected = detect_operating_systems(mounts)
    assert detected[0].name == "Fixture Linux"
    assert detected[0].version == "1"
    assert detected[0].os_id == "fixture"
