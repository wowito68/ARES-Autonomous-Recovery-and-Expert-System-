from __future__ import annotations

import asyncio
from collections import defaultdict
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI
from httpx import AsyncClient

from ares.capabilities import CapabilityManager, CapabilityMode, RiskLevel
from ares.filesystems.executor import LocalTestFilesystemExecutor
from ares.filesystems.integrity import repair_plan_fingerprint
from ares.filesystems.models import (
    FilesystemRepairPlan,
    FilesystemType,
    MountSafetyReport,
    RepairAction,
)
from ares.knowledge import GraphKind, KnowledgeGraph
from ares.protection import (
    ProtectionCheckpoint,
    ProtectionCheckpointStatus,
    ProtectionCheckpointStore,
)
from ares.reasoning import ReasoningEngine, ReasoningRequest, ReasoningStatus
from ares.tools.storage import ProcessResult, ToolAvailability

_SESSION_HEADERS = {
    "X-ARES-Session-ID": "filesystem-api-session-123",
    "X-Request-ID": "filesystem-api-request-123",
}


class ScenarioRunner:
    def __init__(self) -> None:
        self.available: dict[str, bool] = defaultdict(lambda: True)
        self.check_count = 0
        self.repair_count = 0

    def inspect(self, tool: str) -> ToolAvailability:
        available = self.available[tool]
        return ToolAvailability(
            tool=tool,
            available=available,
            reason=None if available else "tool_not_installed",
        )

    async def run(
        self,
        tool: str,
        args: tuple[str, ...],
        *,
        timeout_seconds: float,
    ) -> ProcessResult:
        del timeout_seconds
        if tool == "blkid":
            return _result(
                tool,
                0,
                "TYPE=ext4\nUUID=api-test-uuid\n",
            )
        if tool == "e2fsck":
            if "-n" in args:
                self.check_count += 1
                exit_code = 0 if self.repair_count else 4
                return _result(tool, exit_code)
            if "-p" in args:
                self.repair_count += 1
                return _result(tool, 1)
        return _result(tool, 0)


def _result(tool: str, exit_code: int, stdout: str = "") -> ProcessResult:
    return ProcessResult(
        tool=tool,
        exit_code=exit_code,
        stdout=stdout,
        stderr="",
        duration_ms=5.0,
    )


def _image(tmp_path: Path) -> Path:
    image = tmp_path / "api-filesystem.img"
    image.write_bytes(b"ARES API filesystem fixture" * 1024)
    return image


def test_filesystem_repair_capability_metadata_is_high_risk_and_exact(app: FastAPI) -> None:
    manager = cast(CapabilityManager, app.state.capability_manager)
    metadata = manager.get("filesystem.repair")

    assert metadata is not None
    assert metadata.mode is CapabilityMode.MUTATING
    assert metadata.risk is RiskLevel.HIGH
    assert metadata.requires_authorization is True
    assert metadata.requires_protection_checkpoint is True
    assert metadata.requires_unmounted is True
    assert metadata.supports_dry_run is True
    assert metadata.supports_verification is True
    assert metadata.supports_rollback is False
    assert metadata.supported_filesystems == ("ext2", "ext3", "ext4", "xfs", "ntfs")
    descriptor = manager.descriptor("filesystem.repair")
    assert descriptor is not None
    properties = descriptor.input_schema["properties"]
    assert "plan_id" in properties
    assert "protected_resource_id" in properties
    assert "device" not in properties
    assert "command" not in properties
    assert "argv" not in properties


def test_reasoning_can_select_repair_but_cannot_supply_a_device(app: FastAPI) -> None:
    engine = cast(ReasoningEngine, app.state.reasoning_engine)

    result = engine.assess(ReasoningRequest(goal="Repara mi partición con filesystem dañado"))

    assert result.status is ReasoningStatus.NEEDS_EVIDENCE
    assert result.selected_capability_id is None
    assert result.hypotheses[0].capability_id == "filesystem.repair"
    assert "verified-protection-checkpoint" in result.requested_evidence
    manager = cast(CapabilityManager, app.state.capability_manager)
    descriptor = manager.descriptor("filesystem.repair")
    assert descriptor is not None
    assert "device" not in descriptor.input_schema["properties"]


async def test_api_inspect_and_blocked_plan_without_checkpoint(
    client: AsyncClient, app: FastAPI, tmp_path: Path
) -> None:
    image = _image(tmp_path)
    executor = cast(LocalTestFilesystemExecutor, app.state.filesystem_executor)
    executor.tools.runner = ScenarioRunner()

    inspection_response = await client.post(
        "/api/v1/filesystems/inspect",
        json={"device": str(image)},
        headers=_SESSION_HEADERS,
    )
    assert inspection_response.status_code == 200
    inspection = inspection_response.json()
    assert inspection["filesystem"] == "ext4"
    assert inspection["health"] == "INCONSISTENT"
    assert inspection["identity"]["filesystem_uuid"] == "api-test-uuid"
    assert inspection["identity"]["block_device"] is False

    plan_response = await client.post(
        "/api/v1/filesystems/repair/plan",
        json={"device": str(image), "backup_id": None},
        headers=_SESSION_HEADERS,
    )
    assert plan_response.status_code == 200
    plan = plan_response.json()
    assert plan["risk"] == "high"
    assert plan["requires_authorization"] is True
    assert plan["protection_checkpoint"] is None
    assert plan["executable"] is False
    assert "verified_full_filesystem_backup_required" in plan["limitations"]

    start_response = await client.post(
        "/api/v1/filesystems/repair",
        json={"plan_id": plan["id"], "request_authorization": True},
        headers=_SESSION_HEADERS,
    )
    assert start_response.status_code == 409
    assert start_response.json()["code"] == "FILESYSTEM_PROTECTION_CHECKPOINT_REQUIRED"


async def test_api_executes_exact_protected_image_and_projects_before_after(
    client: AsyncClient, app: FastAPI, tmp_path: Path
) -> None:
    image = _image(tmp_path)
    executor = cast(LocalTestFilesystemExecutor, app.state.filesystem_executor)
    runner = ScenarioRunner()
    executor.tools.runner = runner
    identity = await executor.tools.identify(str(image))
    resource_id = f"filesystem:{identity.fingerprint_sha256}"
    checkpoint = ProtectionCheckpoint(
        status=ProtectionCheckpointStatus.READY,
        protected_resources=(resource_id,),
        resource_fingerprints={resource_id: identity.fingerprint_sha256},
        provider_capability_id="backup.create",
        backup_id="api-backup-12345678",
        verification_id="api-verification-12345678",
        session_id="filesystem-api-session-123",
        evidence_sha256="c" * 64,
    )
    checkpoint_store = cast(ProtectionCheckpointStore, app.state.protection_checkpoint_store)
    await checkpoint_store.put(checkpoint)
    draft = FilesystemRepairPlan(
        session_id="filesystem-api-session-123",
        target=identity,
        filesystem=FilesystemType.EXT4,
        mount=MountSafetyReport(
            mounted=False,
            busy=False,
            swap=False,
            safe_to_unmount=False,
            safe_to_remount=False,
        ),
        detected_problems=("filesystem_errors_detected",),
        protection_checkpoint=checkpoint,
        required_permissions=("block-device.readwrite",),
        repair_actions=(
            RepairAction(
                id="filesystem.repair",
                description="Repair isolated API filesystem image",
                mutates_target=True,
            ),
        ),
        verification_steps=("Run structured post-check",),
        rollback_strategy="Verified file-level checkpoint only.",
        executable=True,
        fingerprint_sha256="0" * 64,
    )
    plan = draft.model_copy(update={"fingerprint_sha256": repair_plan_fingerprint(draft)})
    await app.state.filesystem_repair_store.put_plan(plan)

    response = await client.post(
        "/api/v1/filesystems/repair",
        json={"plan_id": plan.id, "request_authorization": True},
        headers=_SESSION_HEADERS,
    )
    assert response.status_code == 202
    repair_id = response.json()["id"]

    record: dict[str, Any] | None = None
    for _ in range(100):
        current = await client.get(
            f"/api/v1/filesystems/repairs/{repair_id}", headers=_SESSION_HEADERS
        )
        assert current.status_code == 200
        record = current.json()
        if record["execution"]["status"] in {
            "COMPLETED",
            "PARTIAL",
            "FAILED",
            "CANCELLED",
            "ABORTED",
        }:
            break
        await asyncio.sleep(0.01)
    assert record is not None
    assert record["execution"]["status"] == "COMPLETED"
    assert record["verification"]["status"] == "SUCCESS"
    assert record["verification"]["before"]["health"] == "INCONSISTENT"
    assert record["verification"]["after"]["health"] == "HEALTHY"
    assert runner.repair_count == 1

    verification = await client.get(
        f"/api/v1/filesystems/repairs/{repair_id}/verification",
        headers=_SESSION_HEADERS,
    )
    assert verification.status_code == 200
    assert verification.json()["status"] == "SUCCESS"

    graph = cast(KnowledgeGraph, app.state.knowledge_graph)
    snapshot = await graph.snapshot()
    kinds = {node.kind for node in snapshot.nodes}
    relations = {edge.relation for edge in snapshot.edges}
    assert GraphKind.FILESYSTEM_STATUS in kinds
    assert GraphKind.REPAIR_EXECUTION in kinds
    assert GraphKind.REPAIR_VERIFICATION in kinds
    assert GraphKind.PROTECTION_CHECKPOINT in kinds
    assert {"had_status", "repaired_by", "current_status", "protected_by"} <= relations


async def test_api_missing_repair_returns_problem_details(client: AsyncClient) -> None:
    response = await client.get(
        "/api/v1/filesystems/repairs/not-found-12345678",
        headers=_SESSION_HEADERS,
    )
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
