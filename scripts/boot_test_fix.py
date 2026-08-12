from pathlib import Path


def replace(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    if old not in text:
        raise SystemExit(f"anchor missing in {path}: {old[:100]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


service = "backend/tests/test_boot_service_actions.py"
replace(
    service,
    "from ares.workflows import StepOutputs, WorkflowEngine",
    "from ares.workflows import WorkflowEngine",
)
replace(
    service,
    "    assert await postcheck({}, StepOutputs()) is False",
    "    assert await postcheck({}, {}) is False",
)
replace(
    service,
    "    assert await service.verification(repair_id) == terminal.verification\n\n    snapshot = await graph.snapshot()",
    "    assert await service.verification(repair_id) == terminal.verification\n"
    "    for _ in range(200):\n"
    "        if repair_id not in service._tasks:\n"
    "            break\n"
    "        await asyncio.sleep(0.01)\n"
    "    assert repair_id not in service._tasks\n\n"
    "    snapshot = await graph.snapshot()",
)
replace(
    service,
    '    assert verification.status.value == "PARTIAL"\n'
    "    reconciled = await service.get(protected.repair_id)\n"
    "    assert reconciled is not None\n"
    "    assert reconciled.execution.status is BootRepairStatus.COMPLETED",
    '    assert verification.status.value == "FAILED"\n'
    "    reconciled = await service.get(protected.repair_id)\n"
    "    assert reconciled is not None\n"
    "    assert reconciled.execution.status is BootRepairStatus.UNKNOWN\n"
    "    assert reconciled.execution.reconciliation_required is True",
)

runtime = "backend/tests/test_boot_runtime_edges.py"
replace(
    runtime,
    "from ares.tools.boot import BootToolError",
    "from ares.tools.boot import BootToolError\nfrom ares.tools.partition import DiskIdentityTool",
)
replace(runtime, "class _IdentityFixture:", "class _IdentityFixture(DiskIdentityTool):")
replace(
    runtime,
    "        correlation_id: str | None,\n        session_id: str | None,",
    "        correlation_id: str,\n        session_id: str,",
)
replace(
    runtime,
    '    assert checkpoint_payload["checkpoint"]["status"] == "READY"',
    '    assert checkpoint_payload["checkpoint"]["status"] == "ready"',
)

api = "backend/tests/test_boot_api_cli_edges.py"
replace(
    api,
    '            json={"target_disk": None, "root_path": service.diagnostic_result.environment.operating_systems[0].root_path},',
    '            json={\n'
    '                "target_disk": None,\n'
    '                "root_path": service.diagnostic_result.environment.operating_systems[0].root_path,\n'
    '            },',
)

engine = "backend/tests/test_boot_engine_edges.py"
replace(
    engine,
    '    assert {item.relation for item in dependencies} >= {"depends_on", "configured_by", "loads", "boots"}',
    '    assert {item.relation for item in dependencies} >= {\n'
    '        "depends_on",\n'
    '        "configured_by",\n'
    '        "loads",\n'
    '        "boots",\n'
    '    }',
)
replace(
    engine,
    '        BootEntry(number="0001", label="debian", loader_path="EFI/debian/grubx64.efi"),',
    '        BootEntry(\n'
    '            id="entry:0001",\n'
    '            number="0001",\n'
    '            label="debian",\n'
    '            loader_path="EFI/debian/grubx64.efi",\n'
    '        ),',
)
replace(
    engine,
    '    no_target = diagnostic.model_copy(\n'
    '        update={\n'
    '            "environment": diagnostic.environment.model_copy(update={"target_disk": None})\n'
    '        }\n'
    '    )',
    '    no_target = diagnostic.model_copy(\n'
    '        update={\n'
    '            "id": "no-target-diagnostic",\n'
    '            "environment": diagnostic.environment.model_copy(update={"target_disk": None}),\n'
    '        }\n'
    '    )',
)
