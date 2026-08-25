"""Durable JSON store for operational agent runs."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

from ares.agent.models import AgentRun


class AgentRunStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def prepare(self) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    async def put(self, run: AgentRun) -> None:
        await asyncio.to_thread(self._write, run)

    async def get(self, run_id: str) -> AgentRun | None:
        if not _safe_id(run_id):
            return None
        path = self.directory / f"{run_id}.json"
        try:
            text = await asyncio.to_thread(path.read_text, encoding="utf-8")
        except OSError:
            return None
        return AgentRun.model_validate_json(text)

    async def list(self, *, limit: int = 50) -> tuple[AgentRun, ...]:
        def read() -> tuple[AgentRun, ...]:
            if not self.directory.exists():
                return ()
            runs: list[AgentRun] = []
            for path in sorted(
                self.directory.glob("*.json"),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )[:limit]:
                try:
                    runs.append(AgentRun.model_validate_json(path.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    continue
            return tuple(runs)

        return await asyncio.to_thread(read)

    def _write(self, run: AgentRun) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        encoded = json.dumps(
            run.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        descriptor, temporary = tempfile.mkstemp(prefix=".agent-run-", dir=self.directory)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.directory / f"{run.id}.json")
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _safe_id(value: str) -> bool:
    return bool(value) and len(value) <= 64 and all(
        char.isalnum() or char in {"-", "_"} for char in value
    )
