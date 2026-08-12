"""Durable, crash-reconcilable state for storage operation plans and transactions."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from ares.storage_operations.models import (
    PartitionTableCheckpointArtifact,
    StorageOperationPlan,
    StorageOperationRecord,
    StorageTransaction,
    StorageTransactionStatus,
    StorageVerification,
)

_MAX_RECORD_BYTES = 12_000_000


class StorageOperationStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.plans = directory / "plans"
        self.transactions = directory / "transactions"
        self.verifications = directory / "verifications"
        self.checkpoints = directory / "checkpoints"
        self._lock = asyncio.Lock()

    def prepare(self) -> None:
        for directory in (
            self.directory,
            self.plans,
            self.transactions,
            self.verifications,
            self.checkpoints,
        ):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory.chmod(0o700)
        self._reconcile_interrupted()

    async def put_plan(self, plan: StorageOperationPlan) -> None:
        await self._put(self.plans / f"{plan.id}.json", plan.model_dump(mode="json"))

    async def get_plan(self, plan_id: str) -> StorageOperationPlan | None:
        payload = await self._get(self.plans / f"{_safe_id(plan_id)}.json")
        if payload is None:
            return None
        try:
            return StorageOperationPlan.model_validate(payload)
        except ValueError:
            return None

    async def put_transaction(self, transaction: StorageTransaction) -> None:
        await self._put(
            self.transactions / f"{transaction.operation_id}.json",
            transaction.model_dump(mode="json"),
        )

    async def get_transaction(self, operation_id: str) -> StorageTransaction | None:
        payload = await self._get(self.transactions / f"{_safe_id(operation_id)}.json")
        if payload is None:
            return None
        try:
            return StorageTransaction.model_validate(payload)
        except ValueError:
            return None

    async def put_verification(self, verification: StorageVerification) -> None:
        await self._put(
            self.verifications / f"{verification.operation_id}.json",
            verification.model_dump(mode="json"),
        )

    async def get_verification(self, operation_id: str) -> StorageVerification | None:
        payload = await self._get(self.verifications / f"{_safe_id(operation_id)}.json")
        if payload is None:
            return None
        try:
            return StorageVerification.model_validate(payload)
        except ValueError:
            return None

    async def put_checkpoint(self, artifact: PartitionTableCheckpointArtifact) -> None:
        await self._put(
            self.checkpoints / f"{artifact.checkpoint_id}.json",
            artifact.model_dump(mode="json"),
        )

    async def get_checkpoint(self, checkpoint_id: str) -> PartitionTableCheckpointArtifact | None:
        payload = await self._get(self.checkpoints / f"{_safe_id(checkpoint_id)}.json")
        if payload is None:
            return None
        try:
            return PartitionTableCheckpointArtifact.model_validate(payload)
        except ValueError:
            return None

    async def get_record(self, operation_id: str) -> StorageOperationRecord | None:
        transaction = await self.get_transaction(operation_id)
        if transaction is None:
            return None
        plan = await self.get_plan(transaction.plan_id)
        if plan is None:
            return None
        verification = await self.get_verification(operation_id)
        return StorageOperationRecord(
            plan=plan,
            transaction=transaction,
            verification=verification,
        )

    async def list_records(self) -> tuple[StorageOperationRecord, ...]:
        operation_ids = await asyncio.to_thread(
            lambda: tuple(path.stem for path in self.transactions.glob("*.json"))
        )
        records: list[StorageOperationRecord] = []
        for operation_id in operation_ids:
            record = await self.get_record(operation_id)
            if record is not None:
                records.append(record)
        return tuple(sorted(records, key=lambda item: item.transaction.created_at, reverse=True))

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
            raise ValueError("storage operation record exceeds safety limit")
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
        for path in self.transactions.glob("*.json"):
            payload = self._read(path)
            if payload is None:
                continue
            try:
                transaction = StorageTransaction.model_validate(payload)
            except ValueError:
                continue
            if transaction.status in {
                StorageTransactionStatus.EXECUTING,
                StorageTransactionStatus.VERIFYING,
            }:
                updated = transaction.model_copy(
                    update={
                        "status": StorageTransactionStatus.UNKNOWN,
                        "finished_at": None,
                        "reconciliation_required": True,
                        "error_code": "STORAGE_OPERATION_INTERRUPTED",
                        "last_known_stage": transaction.last_known_stage or "process-restart",
                    }
                )
                self._write(path, updated.model_dump(mode="json"))
            elif transaction.status is StorageTransactionStatus.AUTHORIZED:
                updated = transaction.model_copy(
                    update={
                        "status": StorageTransactionStatus.ABORTED,
                        "finished_at": datetime.now(UTC),
                        "error_code": "STORAGE_AUTHORIZATION_LOST_ON_RESTART",
                        "last_known_stage": "authorization-not-executed",
                    }
                )
                self._write(path, updated.model_dump(mode="json"))


def _safe_id(value: str) -> str:
    if not 8 <= len(value) <= 128 or not all(
        character.isalnum() or character in "-_" for character in value
    ):
        return "invalid"
    return value
