"""Append-first event bus with bounded local JSONL persistence."""

from __future__ import annotations

import asyncio
import json
import os
from collections import defaultdict
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

from ares.events.models import AresEvent

EventHandler = Callable[[AresEvent], Awaitable[None]]
_MAX_EVENT_BYTES = 256_000


class EventSink(Protocol):
    """Storage boundary used by the event bus."""

    def prepare(self) -> None:
        """Create private storage before the service starts accepting work."""

    async def append(self, event: AresEvent) -> None:
        """Durably append one event or raise."""


class MemoryEventSink:
    """Deterministic sink for isolated tests."""

    def __init__(self) -> None:
        self.events: list[AresEvent] = []

    def prepare(self) -> None:
        """No storage is required."""

    async def append(self, event: AresEvent) -> None:
        self.events.append(event)


class JsonlEventSink:
    """Append-only, private audit trail for one local ARES instance."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()

    def prepare(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        if self.path.exists():
            if self.path.is_symlink() or not self.path.is_file():
                raise OSError("capability event log is not a regular file")
            self.path.chmod(0o600)

    async def append(self, event: AresEvent) -> None:
        encoded = (
            json.dumps(
                event.journal_record(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
        if len(encoded) > _MAX_EVENT_BYTES:
            raise ValueError("event exceeds the durable journal limit")
        async with self._lock:
            await asyncio.to_thread(self._append_sync, encoded)

    def _append_sync(self, encoded: bytes) -> None:
        flags = os.O_APPEND | os.O_CLOEXEC | os.O_CREAT | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self.path, flags, 0o600)
        try:
            remaining = memoryview(encoded)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError("capability event journal write failed")
                remaining = remaining[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class EventBus:
    """Persist every event before notifying in-process consumers."""

    def __init__(self, sink: EventSink) -> None:
        self.sink = sink
        self._subscribers: dict[str, list[EventHandler]] = defaultdict(list)

    def prepare(self) -> None:
        self.sink.prepare()

    def subscribe(self, event_name: str, handler: EventHandler) -> None:
        """Subscribe to an exact event name or to ``*``."""

        self._subscribers[event_name].append(handler)

    async def publish(self, event: AresEvent) -> None:
        """Append first; a persistence failure prevents an unaudited action."""

        await self.sink.append(event)
        handlers = [*self._subscribers.get(event.name, ()), *self._subscribers.get("*", ())]
        for handler in handlers:
            await handler(event)
