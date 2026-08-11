from pathlib import Path


def replace(path: str, old: str, new: str) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    if old not in text:
        raise SystemExit(f"expected block not found: {path}")
    file.write_text(text.replace(old, new), encoding="utf-8")


replace(
    "backend/tests/test_capabilities_v2.py",
    '    assert catalog.json()["count"] == filtered.json()["count"] == 1\n',
    '    assert catalog.json()["count"] == 1\n'
    '    filtered_payload = filtered.json()\n'
    '    filtered_ids = {item["id"] for item in filtered_payload["capabilities"]}\n'
    '    assert filtered_payload["count"] >= 6\n'
    '    assert {\n'
    '        "storage.disk-analysis",\n'
    '        "storage.partition.inspect",\n'
    '        "storage.partition.create",\n'
    '        "storage.partition.delete",\n'
    '        "storage.partition.resize",\n'
    '        "storage.partition.move",\n'
    '    } <= filtered_ids\n',
)
replace(
    "backend/tests/test_filesystem_vertical_slice.py",
    '    assert result.status is ReasoningStatus.CAPABILITY_SELECTED\n'
    '    assert result.selected_capability_id == "filesystem.repair"\n',
    '    assert result.status is ReasoningStatus.NEEDS_EVIDENCE\n'
    '    assert result.selected_capability_id is None\n'
    '    assert result.hypotheses[0].capability_id == "filesystem.repair"\n'
    '    assert "verified-protection-checkpoint" in result.requested_evidence\n',
)

real = Path("backend/tests/test_filesystem_real_image.py")
text = real.read_text(encoding="utf-8")
text = text.replace(
    '("debugfs", "-w", "-R", "set_super_value free_blocks_count 1", str(image)),',
    '("debugfs", "-w", "-R", "freei <11>", str(image)),',
)
marker = "        stderr=subprocess.DEVNULL,\n    )\n\n    async def scenario() -> None:\n"
probe = (
    "        stderr=subprocess.DEVNULL,\n"
    "    )\n"
    "    raw_check = subprocess.run(  # noqa: S603\n"
    '        ("e2fsck", "-f", "-n", str(image)),  # noqa: S607\n'
    "        check=False,\n"
    "        stdout=subprocess.DEVNULL,\n"
    "        stderr=subprocess.DEVNULL,\n"
    "    )\n"
    '    assert raw_check.returncode != 0, "debugfs fixture did not create an inconsistency"\n\n'
    "    async def scenario() -> None:\n"
)
if marker not in text:
    raise SystemExit("real image marker not found")
real.write_text(text.replace(marker, probe, 1), encoding="utf-8")

actions = Path("backend/src/ares/actions/filesystem_repair.py")
text = actions.read_text(encoding="utf-8")
old_verify = '''        if verification_status is RepairVerificationStatus.SUCCESS:
            execution_status = RepairExecutionStatus.COMPLETED
            error_code = None
            event_name = "repair.completed"
            severity = EventSeverity.INFO
        elif verification_status in {
            RepairVerificationStatus.PARTIAL,
            RepairVerificationStatus.UNKNOWN,
        }:
            execution_status = RepairExecutionStatus.PARTIAL
            error_code = "FILESYSTEM_REPAIR_PARTIAL"
            event_name = "repair.failed"
            severity = EventSeverity.WARNING
        else:
            execution_status = RepairExecutionStatus.FAILED
            error_code = "FILESYSTEM_VERIFICATION_FAILED"
            event_name = "repair.failed"
            severity = EventSeverity.ERROR
        execution = record.execution.model_copy(
            update={
                "status": execution_status,
                "finished_at": datetime.now(UTC),
                "tool": outcome.repair_tool,
                "error_code": error_code,
            }
        )
        record = record.model_copy(update={"execution": execution, "verification": verification})
        await self.store.put_repair(record)
        await _event(
            context,
            event_name,
            {
                "repair_id": plan.repair_id,
                "verification_id": verification.id,
                "verification_status": verification.status.value,
                "before": outcome.before.health.value,
                "after": outcome.after.health.value,
                "remounted": outcome.remounted,
            },
            severity,
            session_id=request.session_id,
        )
'''
new_verify = '''        execution = record.execution.model_copy(
            update={
                "status": RepairExecutionStatus.VERIFYING,
                "tool": outcome.repair_tool,
                "error_code": None,
            }
        )
        record = record.model_copy(update={"execution": execution, "verification": verification})
        await self.store.put_repair(record)
        await _event(
            context,
            "repair.verification.completed",
            {
                "repair_id": plan.repair_id,
                "verification_id": verification.id,
                "verification_status": verification.status.value,
                "before": outcome.before.health.value,
                "after": outcome.after.health.value,
                "remounted": outcome.remounted,
            },
            session_id=request.session_id,
        )
'''
if old_verify not in text:
    raise SystemExit("verification terminal block not found")
text = text.replace(old_verify, new_verify)
old_class = '''class ProjectFilesystemRepairGraphAction:
    id = "knowledge.project-filesystem-repair"
    idempotent = True

    async def run'''
new_class = '''class ProjectFilesystemRepairGraphAction:
    id = "knowledge.project-filesystem-repair"
    idempotent = True

    def __init__(self, store: FilesystemRepairStore) -> None:
        self.store = store

    async def run'''
if old_class not in text:
    raise SystemExit("project class block not found")
text = text.replace(old_class, new_class)
old_result = '''        snapshot = await context.graph.apply(tuple(nodes), tuple(edges))
        result = FilesystemRepairResult(
            repair=record,
            verification=verification,
            knowledge_graph_revision=snapshot.revision,
        )
        await _event(
            context,
            "knowledge.graph.updated",
            {"repair_id": record.id, "revision": snapshot.revision},
            session_id=record.execution.session_id,
        )
        return result.model_dump(mode="json")
'''
new_result = '''        snapshot = await context.graph.apply(tuple(nodes), tuple(edges))
        if verification.status is RepairVerificationStatus.SUCCESS:
            terminal_status = RepairExecutionStatus.COMPLETED
            error_code = None
            event_name = "repair.completed"
            severity = EventSeverity.INFO
        elif verification.status in {
            RepairVerificationStatus.PARTIAL,
            RepairVerificationStatus.UNKNOWN,
        }:
            terminal_status = RepairExecutionStatus.PARTIAL
            error_code = "FILESYSTEM_REPAIR_PARTIAL"
            event_name = "repair.failed"
            severity = EventSeverity.WARNING
        else:
            terminal_status = RepairExecutionStatus.FAILED
            error_code = "FILESYSTEM_VERIFICATION_FAILED"
            event_name = "repair.failed"
            severity = EventSeverity.ERROR
        execution = record.execution.model_copy(
            update={
                "status": terminal_status,
                "finished_at": datetime.now(UTC),
                "error_code": error_code,
            }
        )
        record = record.model_copy(update={"execution": execution})
        await self.store.put_repair(record)
        await _event(
            context,
            "knowledge.graph.updated",
            {"repair_id": record.id, "revision": snapshot.revision},
            session_id=record.execution.session_id,
        )
        await _event(
            context,
            event_name,
            {
                "repair_id": record.id,
                "verification_id": verification.id,
                "verification_status": verification.status.value,
            },
            severity,
            session_id=record.execution.session_id,
        )
        result = FilesystemRepairResult(
            repair=record,
            verification=verification,
            knowledge_graph_revision=snapshot.revision,
        )
        return result.model_dump(mode="json")
'''
if old_result not in text:
    raise SystemExit("graph result block not found")
actions.write_text(text.replace(old_result, new_result), encoding="utf-8")

replace(
    "backend/src/ares/capabilities/plugins/filesystem_repair.py",
    "        self.project = ProjectFilesystemRepairGraphAction()\n",
    "        self.project = ProjectFilesystemRepairGraphAction(store)\n",
)
