"""Durable JSON store for terminal plans and sessions."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ares.terminal.models import TerminalPlan, TerminalSession


class TerminalStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.plans = root / "plans"
        self.sessions = root / "sessions"

    def prepare(self) -> None:
        self.plans.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.sessions.mkdir(mode=0o700, parents=True, exist_ok=True)

    async def put_plan(self, plan: TerminalPlan) -> None:
        await self._put(self.plans / f"{_safe_id(plan.id)}.json", plan.model_dump(mode="json"))

    async def get_plan(self, plan_id: str) -> TerminalPlan | None:
        payload = await self._get(self.plans / f"{_safe_id(plan_id)}.json")
        return TerminalPlan.model_validate(payload) if payload is not None else None

    async def put_session(self, session: TerminalSession) -> None:
        await self._put(
            self.sessions / f"{_safe_id(session.id)}.json", session.model_dump(mode="json")
        )

    async def get_session(self, session_id: str) -> TerminalSession | None:
        payload = await self._get(self.sessions / f"{_safe_id(session_id)}.json")
        return TerminalSession.model_validate(payload) if payload is not None else None

    async def list_sessions(self) -> tuple[TerminalSession, ...]:
        if not self.sessions.exists():
            return ()
        output: list[TerminalSession] = []
        for path in sorted(self.sessions.glob("*.json")):
            payload = await self._get(path)
            if payload is not None:
                output.append(TerminalSession.model_validate(payload))
        return tuple(output)

    async def _put(self, path: Path, payload: dict[str, object]) -> None:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"

        def write() -> None:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_text(encoded, encoding="utf-8")

        await asyncio.to_thread(write)

    async def _get(self, path: Path) -> dict[str, object] | None:
        def read() -> dict[str, object] | None:
            if not path.is_file() or path.stat().st_size > 2_000_000:
                return None
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return None
            return payload

        return await asyncio.to_thread(read)


def _safe_id(value: str) -> str:
    if not value or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for ch in value):
        raise ValueError("unsafe terminal id")
    return value
