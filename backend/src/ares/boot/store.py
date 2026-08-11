"""Durable JSON stores for boot diagnostics, repair plans, executions and evidence."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from ares.boot.models import (
    BootCheckpointArtifact,
    BootDiagnosticResult,
    BootRepairExecution,
    BootRepairPlan,
    BootRepairRecord,
    BootRepairStatus,
    BootVerification,
)

T = TypeVar("T", bound=BaseModel)
_MAX_JSON_BYTES = 8 * 1024 * 1024


class BootRecoveryStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.diagnostics = root / "diagnostics"
        self.plans = root / "plans"
        self.executions = root / "executions"
        self.verifications = root / "verifications"
        self.checkpoints = root / "checkpoint-artifacts"

    def prepare(self) -> None:
        for path in (
            self.root,
            self.diagnostics,
            self.plans,
            self.executions,
            self.verifications,
            self.checkpoints,
        ):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(path, 0o700)
        self._reconcile_interrupted()

    async def put_diagnostic(self, result: BootDiagnosticResult) -> None:
        self._write(self.diagnostics / f"{_safe_id(result.id)}.json", result)

    async def get_diagnostic(self, diagnostic_id: str) -> BootDiagnosticResult | None:
        return self._read(
            self.diagnostics / f"{_safe_id(diagnostic_id)}.json", BootDiagnosticResult
        )

    async def put_plan(self, plan: BootRepairPlan) -> None:
        self._write(self.plans / f"{_safe_id(plan.id)}.json", plan)

    async def get_plan(self, plan_id: str) -> BootRepairPlan | None:
        return self._read(self.plans / f"{_safe_id(plan_id)}.json", BootRepairPlan)

    async def put_execution(self, execution: BootRepairExecution) -> None:
        self._write(self.executions / f"{_safe_id(execution.repair_id)}.json", execution)

    async def get_execution(self, repair_id: str) -> BootRepairExecution | None:
        return self._read(
            self.executions / f"{_safe_id(repair_id)}.json", BootRepairExecution
        )

    async def put_verification(self, verification: BootVerification) -> None:
        self._write(
            self.verifications / f"{_safe_id(verification.repair_id)}.json", verification
        )

    async def get_verification(self, repair_id: str) -> BootVerification | None:
        return self._read(
            self.verifications / f"{_safe_id(repair_id)}.json", BootVerification
        )

    async def put_checkpoint(self, artifact: BootCheckpointArtifact) -> None:
        self._write(self.checkpoints / f"{_safe_id(artifact.checkpoint_id)}.json", artifact)

    async def get_checkpoint(self, checkpoint_id: str) -> BootCheckpointArtifact | None:
        return self._read(
            self.checkpoints / f"{_safe_id(checkpoint_id)}.json", BootCheckpointArtifact
        )

    async def get_record(self, repair_id: str) -> BootRepairRecord | None:
        execution = await self.get_execution(repair_id)
        if execution is None:
            return None
        plan = await self.get_plan(execution.plan_id)
        if plan is None:
            return None
        return BootRepairRecord(
            plan=plan,
            execution=execution,
            verification=await self.get_verification(repair_id),
        )

    async def list_records(self) -> tuple[BootRepairRecord, ...]:
        records: list[BootRepairRecord] = []
        for path in sorted(self.executions.glob("*.json")):
            execution = self._read(path, BootRepairExecution)
            if execution is None:
                continue
            record = await self.get_record(execution.repair_id)
            if record is not None:
                records.append(record)
        return tuple(records)

    def _reconcile_interrupted(self) -> None:
        for path in self.executions.glob("*.json"):
            execution = self._read(path, BootRepairExecution)
            if execution is None:
                continue
            if execution.status in {BootRepairStatus.EXECUTING, BootRepairStatus.VERIFYING}:
                self._write(
                    path,
                    execution.model_copy(
                        update={
                            "status": BootRepairStatus.UNKNOWN,
                            "reconciliation_required": True,
                            "error_code": "BOOT_REPAIR_INTERRUPTED",
                            "last_known_stage": "process-restart-reconciliation",
                        }
                    ),
                )
            elif execution.status is BootRepairStatus.AUTHORIZED:
                self._write(
                    path,
                    execution.model_copy(
                        update={
                            "status": BootRepairStatus.ABORTED,
                            "error_code": "BOOT_AUTHORIZATION_LOST_ON_RESTART",
                            "last_known_stage": "authorization-lost-on-restart",
                        }
                    ),
                )

    @staticmethod
    def _write(path: Path, model: BaseModel) -> None:
        if path.is_symlink():
            raise OSError("boot recovery store path is symlink")
        encoded = model.model_dump_json(indent=2).encode("utf-8")
        if len(encoded) > _MAX_JSON_BYTES:
            raise OSError("boot recovery record too large")
        temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
        descriptor = os.open(
            temporary,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC,
            0o600,
        )
        try:
            os.write(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        os.chmod(path, 0o600)

    @staticmethod
    def _read(path: Path, model: type[T]) -> T | None:
        if not path.exists() or path.is_symlink():
            return None
        try:
            if path.stat().st_size > _MAX_JSON_BYTES:
                return None
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return None
            return model.model_validate(payload)
        except (OSError, json.JSONDecodeError, ValidationError):
            return None


def _safe_id(value: str) -> str:
    if len(value) < 8 or len(value) > 128:
        return "invalid"
    if not all(character.isalnum() or character in {"-", "_"} for character in value):
        return "invalid"
    return value
