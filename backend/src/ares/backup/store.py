"""Durable private metadata store for backup plans, records and verification evidence."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from ares.backup.models import (
    Backup,
    BackupManifest,
    BackupPlan,
    BackupStatus,
    BackupVerification,
    BackupVerificationStatus,
    utc_now,
)

TModel = TypeVar("TModel", bound=BaseModel)
_MAX_RECORD_BYTES = 4_000_000
_MAX_MANIFEST_BYTES = 128_000_000


class BackupStore:
    """Persist immutable plans/manifests and current backup execution records atomically."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.plans_dir = directory / "plans"
        self.backups_dir = directory / "records"
        self.manifests_dir = directory / "manifests"
        self.verifications_dir = directory / "verifications"
        self._lock = asyncio.Lock()

    def prepare(self) -> None:
        for path in (
            self.directory,
            self.plans_dir,
            self.backups_dir,
            self.manifests_dir,
            self.verifications_dir,
        ):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.chmod(0o700)
        self._reconcile_interrupted_records()

    async def put_plan(self, plan: BackupPlan) -> None:
        await self._put(self.plans_dir / f"{plan.id}.json", plan, _MAX_RECORD_BYTES)

    async def get_plan(self, plan_id: str) -> BackupPlan | None:
        return await self._get(
            self.plans_dir / f"{plan_id}.json", BackupPlan, _MAX_RECORD_BYTES, plan_id
        )

    async def put_backup(self, backup: Backup) -> None:
        await self._put(self.backups_dir / f"{backup.id}.json", backup, _MAX_RECORD_BYTES)

    async def get_backup(self, backup_id: str) -> Backup | None:
        return await self._get(
            self.backups_dir / f"{backup_id}.json", Backup, _MAX_RECORD_BYTES, backup_id
        )

    async def list_backups(self, limit: int = 100) -> tuple[Backup, ...]:
        def read() -> tuple[Backup, ...]:
            records: list[Backup] = []
            candidates = sorted(
                self.backups_dir.glob("*.json"),
                key=lambda item: item.stat().st_mtime_ns if item.is_file() else 0,
                reverse=True,
            )
            for path in candidates[:limit]:
                record = self._read_model(path, Backup, _MAX_RECORD_BYTES)
                if record is not None:
                    records.append(record)
            return tuple(records)

        return await asyncio.to_thread(read)

    async def put_manifest(self, manifest: BackupManifest) -> None:
        await self._put(
            self.manifests_dir / f"{manifest.backup_id}.json", manifest, _MAX_MANIFEST_BYTES
        )

    async def get_manifest(self, backup_id: str) -> BackupManifest | None:
        return await self._get(
            self.manifests_dir / f"{backup_id}.json",
            BackupManifest,
            _MAX_MANIFEST_BYTES,
            backup_id,
        )

    async def put_verification(self, verification: BackupVerification) -> None:
        await self._put(
            self.verifications_dir / f"{verification.backup_id}.json",
            verification,
            _MAX_RECORD_BYTES,
        )

    async def get_verification(self, backup_id: str) -> BackupVerification | None:
        return await self._get(
            self.verifications_dir / f"{backup_id}.json",
            BackupVerification,
            _MAX_RECORD_BYTES,
            backup_id,
        )

    async def _put(self, path: Path, value: BaseModel, maximum: int) -> None:
        async with self._lock:
            await asyncio.to_thread(self._write_model, path, value, maximum)

    async def _get(
        self,
        path: Path,
        model: type[TModel],
        maximum: int,
        identifier: str,
    ) -> TModel | None:
        if not _safe_id(identifier):
            return None
        return await asyncio.to_thread(self._read_model, path, model, maximum)

    @staticmethod
    def _read_model(path: Path, model: type[TModel], maximum: int) -> TModel | None:
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
                return None
            return model.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    @staticmethod
    def _write_model(path: Path, value: BaseModel, maximum: int) -> None:
        encoded = (
            json.dumps(
                value.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
        if len(encoded) > maximum:
            raise ValueError("backup metadata exceeds its safety limit")
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

    def _reconcile_interrupted_records(self) -> None:
        for path in self.backups_dir.glob("*.json"):
            backup = self._read_model(path, Backup, _MAX_RECORD_BYTES)
            if backup is None or backup.status not in {
                BackupStatus.VALIDATING,
                BackupStatus.RUNNING,
                BackupStatus.VERIFYING,
            }:
                continue
            execution = backup.execution.model_copy(
                update={
                    "status": BackupStatus.FAILED,
                    "finished_at": utc_now(),
                    "error_code": "BACKUP_INTERRUPTED",
                }
            )
            reconciled = backup.model_copy(
                update={
                    "status": BackupStatus.FAILED,
                    "verification_status": BackupVerificationStatus.FAILED,
                    "execution": execution,
                    "metadata": {**backup.metadata, "reconciled_after_restart": True},
                }
            )
            self._write_model(path, reconciled, _MAX_RECORD_BYTES)


def _safe_id(value: str) -> bool:
    return 8 <= len(value) <= 128 and all(
        character.isalnum() or character in "-_" for character in value
    )
