"""Async SQLAlchemy engine configured for local SQLite."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool


class Database:
    """Own the database engine lifecycle and readiness check."""

    def __init__(
        self,
        url: str,
        *,
        echo: bool,
        busy_timeout_ms: int,
        require_existing_file: bool = False,
    ) -> None:
        self.url = url
        self.busy_timeout_ms = busy_timeout_ms
        self.require_existing_file = require_existing_file
        self.database_path = _sqlite_file_path(url)
        engine_url = _read_write_engine_url(url, self.database_path)
        options: dict[str, Any] = {
            "echo": echo,
            "hide_parameters": True,
            "pool_pre_ping": True,
        }
        if _is_sqlite_memory(url):
            options["poolclass"] = StaticPool
        self.engine: AsyncEngine = create_async_engine(engine_url, **options)
        if self.engine.url.get_backend_name() == "sqlite":
            self._configure_sqlite()

    def prepare_storage(self) -> None:
        """Initialize development storage explicitly, never from readiness."""

        if self.database_path is not None:
            self.database_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not self.database_path.exists() and not self.require_existing_file:
                with sqlite3.connect(self.database_path) as connection:
                    connection.execute("PRAGMA schema_version")
                self.database_path.chmod(0o600)

    async def check(self) -> None:
        """Raise if the database cannot serve a minimal query."""

        if self.database_path is not None and not self.database_path.is_file():
            raise FileNotFoundError("The configured SQLite database does not exist")
        async with self.engine.connect() as connection:
            result = await connection.execute(text("PRAGMA schema_version"))
            if result.scalar_one_or_none() is None:
                raise OSError("SQLite did not return its schema version")

    async def dispose(self) -> None:
        """Release pooled connections during application shutdown."""

        await self.engine.dispose()

    def _configure_sqlite(self) -> None:
        timeout = self.busy_timeout_ms

        @event.listens_for(self.engine.sync_engine, "connect")
        def set_sqlite_pragmas(dbapi_connection: Any, _: Any) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute(f"PRAGMA busy_timeout={timeout:d}")
            finally:
                cursor.close()


def _is_sqlite_memory(url: str) -> bool:
    parsed = make_url(url)
    return parsed.get_backend_name() == "sqlite" and parsed.database in {None, "", ":memory:"}


def _sqlite_file_path(url: str) -> Path | None:
    parsed = make_url(url)
    database = parsed.database
    if parsed.get_backend_name() != "sqlite" or database in {None, "", ":memory:"}:
        return None
    assert database is not None
    return Path(database).expanduser().resolve()


def _read_write_engine_url(url: str, database_path: Path | None) -> URL:
    """Use SQLite URI ``mode=rw`` so a health query can never create storage."""

    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite" or database_path is None:
        return parsed
    return parsed.set(
        database=f"file:{database_path}",
        query={**parsed.query, "mode": "rw", "uri": "true"},
    )
