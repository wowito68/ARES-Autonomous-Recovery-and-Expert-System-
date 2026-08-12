"""Durable atomic store for recovery cases and compound-operation state."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from ares.recovery.models import (
    RecoveryCase,
    RecoveryOperationStatus,
    RecoveryStatus,
    SystemRecoveryVerification,
)

_MAX_RECORD_BYTES = 8_000_000


class RecoveryStore:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.cases_dir = state_dir / "cases"
        self.verifications_dir = state_dir / "verifications"
        self._lock = asyncio.Lock()

    def prepare(self) -> None:
        for directory in (self.state_dir, self.cases_dir, self.verifications_dir):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(directory, 0o700)
        for path in self.cases_dir.glob("*.json"):
            case = self._read(path, RecoveryCase)
            if case is None:
                continue
            reconciled = self._reconcile_interrupted(case)
            if reconciled != case:
                self._write(path, reconciled)

    async def put_case(self, case: RecoveryCase) -> None:
        async with self._lock:
            await asyncio.to_thread(self._write, self._case_path(case.case_id), case)

    async def get_case(self, case_id: str) -> RecoveryCase | None:
        return await asyncio.to_thread(self._read, self._case_path(case_id), RecoveryCase)

    async def list_cases(self, limit: int = 100) -> tuple[RecoveryCase, ...]:
        paths = sorted(
            self.cases_dir.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True
        )[:limit]
        values = await asyncio.gather(
            *(asyncio.to_thread(self._read, path, RecoveryCase) for path in paths)
        )
        return tuple(value for value in values if value is not None)

    async def put_verification(self, verification: SystemRecoveryVerification) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._write,
                self.verifications_dir / f"{verification.case_id}.json",
                verification,
            )

    async def get_verification(self, case_id: str) -> SystemRecoveryVerification | None:
        return await asyncio.to_thread(
            self._read,
            self.verifications_dir / f"{case_id}.json",
            SystemRecoveryVerification,
        )

    def _case_path(self, case_id: str) -> Path:
        if not case_id or "/" in case_id or "\\" in case_id or ".." in case_id:
            raise ValueError("invalid recovery case id")
        return self.cases_dir / f"{case_id}.json"

    @staticmethod
    def _reconcile_interrupted(case: RecoveryCase) -> RecoveryCase:
        if case.status not in {RecoveryStatus.RECOVERING, RecoveryStatus.VERIFYING}:
            return case
        executions = tuple(
            item.model_copy(
                update={
                    "status": (
                        RecoveryOperationStatus.UNKNOWN
                        if item.status
                        in {RecoveryOperationStatus.EXECUTING, RecoveryOperationStatus.VERIFYING}
                        else item.status
                    ),
                    "error_code": (
                        "RECOVERY_PROCESS_INTERRUPTED"
                        if item.status
                        in {RecoveryOperationStatus.EXECUTING, RecoveryOperationStatus.VERIFYING}
                        else item.error_code
                    ),
                    "finished_at": (
                        datetime.now(UTC)
                        if item.status
                        in {RecoveryOperationStatus.EXECUTING, RecoveryOperationStatus.VERIFYING}
                        else item.finished_at
                    ),
                }
            )
            for item in case.recovery_plan.operations
        ) if case.recovery_plan is not None else ()
        plan = case.recovery_plan.model_copy(update={"operations": executions}) if case.recovery_plan else None
        return case.model_copy(
            update={
                "status": RecoveryStatus.UNKNOWN,
                "updated_at": datetime.now(UTC),
                "recovery_plan": plan,
                "final_state": "Recovery process interrupted; explicit reconciliation required.",
            }
        )

    @staticmethod
    def _read(path: Path, model_type: type[RecoveryCase] | type[SystemRecoveryVerification]):
        try:
            stat = path.lstat()
        except FileNotFoundError:
            return None
        if path.is_symlink() or stat.st_size > _MAX_RECORD_BYTES:
            raise ValueError("unsafe recovery state file")
        data = json.loads(path.read_text(encoding="utf-8"))
        return model_type.model_validate(data)

    @staticmethod
    def _write(path: Path, value: RecoveryCase | SystemRecoveryVerification) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        encoded = value.model_dump_json(indent=2).encode("utf-8")
        if len(encoded) > _MAX_RECORD_BYTES:
            raise ValueError("recovery state record too large")
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
