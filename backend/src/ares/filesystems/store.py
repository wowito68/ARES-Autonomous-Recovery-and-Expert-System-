"""Durable state for filesystem repair plans and executions."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from ares.filesystems.models import (
    FilesystemInspection,
    FilesystemRepairPlan,
    FilesystemRepairRecord,
    RepairExecutionStatus,
)

_MAX_RECORD_BYTES = 4_000_000
_TERMINAL = {
    RepairExecutionStatus.COMPLETED,
    RepairExecutionStatus.PARTIAL,
    RepairExecutionStatus.FAILED,
    RepairExecutionStatus.CANCELLED,
    RepairExecutionStatus.ABORTED,
}


class FilesystemRepairStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.plans = directory / "plans"
        self.repairs = directory / "repairs"
        self.inspections = directory / "inspections"
        self._lock = asyncio.Lock()

    def prepare(self) -> None:
        for directory in (self.directory, self.plans, self.repairs, self.inspections):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory.chmod(0o700)
        self._reconcile_interrupted()

    async def put_plan(self, plan: FilesystemRepairPlan) -> None:
        await self._put(self.plans / f"{plan.id}.json", plan.model_dump(mode="json"))

    async def get_plan(self, plan_id: str) -> FilesystemRepairPlan | None:
        payload = await self._get(self.plans / f"{_safe_id(plan_id)}.json")
        if payload is None:
            return None
        try:
            return FilesystemRepairPlan.model_validate(payload)
        except ValueError:
            return None

    async def put_repair(self, repair: FilesystemRepairRecord) -> None:
        await self._put(self.repairs / f"{repair.id}.json", repair.model_dump(mode="json"))

    async def get_repair(self, repair_id: str) -> FilesystemRepairRecord | None:
        payload = await self._get(self.repairs / f"{_safe_id(repair_id)}.json")
        if payload is None:
            return None
        try:
            return FilesystemRepairRecord.model_validate(payload)
        except ValueError:
            return None

    async def put_inspection(self, inspection: FilesystemInspection) -> None:
        await self._put(
            self.inspections / f"{inspection.id}.json", inspection.model_dump(mode="json")
        )

    async def _put(self, path: Path, payload: dict[str, object]) -> None:
        async with self._lock:
            await asyncio.to_thread(self._write, path, payload)

    async def _get(self, path: Path) -> dict[str, object] | None:
        return await asyncio.to_thread(self._read, path)

    def _write(self, path: Path, payload: dict[str, object]) -> None:
        encoded = (
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
        if len(encoded) > _MAX_RECORD_BYTES:
            raise ValueError("filesystem repair record exceeds safety limit")
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_DIRECTORY | os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary)

    @staticmethod
    def _read(path: Path) -> dict[str, object] | None:
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_RECORD_BYTES:
                return None
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else None
        except (OSError, ValueError):
            return None

    def _reconcile_interrupted(self) -> None:
        for path in self.repairs.glob("*.json"):
            payload = self._read(path)
            if payload is None:
                continue
            try:
                record = FilesystemRepairRecord.model_validate(payload)
            except ValueError:
                continue
            if record.execution.status in _TERMINAL:
                continue
            execution = record.execution.model_copy(
                update={
                    "status": RepairExecutionStatus.ABORTED,
                    "finished_at": datetime.now(UTC),
                    "error_code": "FILESYSTEM_REPAIR_INTERRUPTED",
                }
            )
            self._write(path, record.model_copy(update={"execution": execution}).model_dump(mode="json"))


def _safe_id(value: str) -> str:
    if not 8 <= len(value) <= 128 or not all(
        character.isalnum() or character in "-_" for character in value
    ):
        return "invalid"
    return value
