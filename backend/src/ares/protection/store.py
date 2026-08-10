"""Durable private store for protection checkpoints."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from contextlib import suppress
from pathlib import Path

from ares.protection.models import ProtectionCheckpoint

_MAX_CHECKPOINT_BYTES = 256_000


class ProtectionCheckpointStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._lock = asyncio.Lock()

    def prepare(self) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory.chmod(0o700)

    async def put(self, checkpoint: ProtectionCheckpoint) -> None:
        async with self._lock:
            await asyncio.to_thread(self._write, checkpoint)

    async def get(self, checkpoint_id: str) -> ProtectionCheckpoint | None:
        if not _safe_id(checkpoint_id):
            return None
        return await asyncio.to_thread(self._read, self.directory / f"{checkpoint_id}.json")

    def _write(self, checkpoint: ProtectionCheckpoint) -> None:
        encoded = (
            json.dumps(
                checkpoint.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
        if len(encoded) > _MAX_CHECKPOINT_BYTES:
            raise ValueError("protection checkpoint exceeds its safety limit")
        path = self.directory / f"{checkpoint.id}.json"
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=self.directory)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
            directory = os.open(self.directory, os.O_DIRECTORY | os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary)

    @staticmethod
    def _read(path: Path) -> ProtectionCheckpoint | None:
        try:
            if (
                path.is_symlink()
                or not path.is_file()
                or path.stat().st_size > _MAX_CHECKPOINT_BYTES
            ):
                return None
            return ProtectionCheckpoint.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None


def _safe_id(value: str) -> bool:
    return 8 <= len(value) <= 128 and all(
        character.isalnum() or character in "-_" for character in value
    )
