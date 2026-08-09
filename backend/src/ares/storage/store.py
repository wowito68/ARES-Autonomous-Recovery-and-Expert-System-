"""Private durable store for storage snapshots."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from contextlib import suppress
from pathlib import Path

from ares.storage.models import SystemStorageSnapshot

_MAX_SNAPSHOT_BYTES = 8_000_000


class StorageSnapshotStore:
    """Persist immutable storage snapshots as private atomic JSON documents."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._lock = asyncio.Lock()
        self._latest_id: str | None = None

    def prepare(self) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        candidates = sorted(
            (item for item in self.directory.glob("*.json") if item.is_file()),
            key=lambda item: item.stat().st_mtime_ns,
            reverse=True,
        )
        self._latest_id = candidates[0].stem if candidates else None

    async def put(self, snapshot: SystemStorageSnapshot) -> None:
        async with self._lock:
            await asyncio.to_thread(self._write, snapshot)
            self._latest_id = snapshot.id

    async def get(self, snapshot_id: str) -> SystemStorageSnapshot | None:
        if not _safe_id(snapshot_id):
            return None
        path = self.directory / f"{snapshot_id}.json"
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_SNAPSHOT_BYTES:
                return None
            text = await asyncio.to_thread(path.read_text, encoding="utf-8")
            return SystemStorageSnapshot.model_validate_json(text)
        except (OSError, ValueError):
            return None

    async def latest(self) -> SystemStorageSnapshot | None:
        latest_id = self._latest_id
        return await self.get(latest_id) if latest_id is not None else None

    def _write(self, snapshot: SystemStorageSnapshot) -> None:
        encoded = json.dumps(
            snapshot.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(encoded) > _MAX_SNAPSHOT_BYTES:
            raise ValueError("storage snapshot exceeds its safety limit")
        descriptor, temporary = tempfile.mkstemp(prefix=".snapshot-", dir=self.directory)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.directory / f"{snapshot.id}.json")
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary)


def _safe_id(value: str) -> bool:
    return 8 <= len(value) <= 128 and all(
        character.isalnum() or character in "-_" for character in value
    )
