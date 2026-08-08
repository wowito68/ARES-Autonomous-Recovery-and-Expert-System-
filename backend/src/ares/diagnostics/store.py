"""Private durable store for structured diagnoses."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

from ares.diagnostics.models import DiagnosticResult

_MAX_DIAGNOSTIC_BYTES = 2_000_000


class DiagnosticStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._lock = asyncio.Lock()

    def prepare(self) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory.chmod(0o700)

    async def put(self, diagnostic: DiagnosticResult) -> None:
        async with self._lock:
            await asyncio.to_thread(self._write, diagnostic)

    async def get(self, diagnostic_id: str) -> DiagnosticResult | None:
        if not _safe_id(diagnostic_id):
            return None
        path = self.directory / f"{diagnostic_id}.json"
        try:
            if (
                path.is_symlink()
                or not path.is_file()
                or path.stat().st_size > _MAX_DIAGNOSTIC_BYTES
            ):
                return None
            text = await asyncio.to_thread(path.read_text, encoding="utf-8")
            return DiagnosticResult.model_validate_json(text)
        except (OSError, ValueError):
            return None

    def _write(self, diagnostic: DiagnosticResult) -> None:
        encoded = json.dumps(
            diagnostic.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(encoded) > _MAX_DIAGNOSTIC_BYTES:
            raise ValueError("diagnostic exceeds its safety limit")
        descriptor, temporary = tempfile.mkstemp(prefix=".diagnostic-", dir=self.directory)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.directory / f"{diagnostic.id}.json")
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _safe_id(value: str) -> bool:
    return 8 <= len(value) <= 128 and all(
        character.isalnum() or character in "-_" for character in value
    )
