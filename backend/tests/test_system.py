"""Read-only system overview and embedded interface tests."""

from __future__ import annotations

import json
from pathlib import Path

from httpx import ASGITransport, AsyncClient

from ares.api.routes.system import _read_json
from ares.config import Environment, LogFormat, Settings
from ares.main import create_app


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


async def test_overview_reads_only_expected_runtime_files(tmp_path: Path) -> None:
    runtime = tmp_path / "run"
    _write_json(runtime / "mode.json", {"mode": "forensic"})
    _write_json(runtime / "state/status.json", {"effective": "ephemeral"})
    _write_json(runtime / "state/network.json", {"effective": "offline"})
    _write_json(runtime / "integrity.json", {"trust": "ENFORCED_PARTIAL"})
    _write_json(
        runtime / "hardware/public/inventory-v1.json",
        {"schema_version": 1, "view": "redacted"},
    )
    application = create_app(
        Settings(
            environment=Environment.TEST,
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'system.db'}",
            runtime_state_dir=runtime,
            log_level="CRITICAL",
            log_format=LogFormat.TEXT,
        )
    )

    async with application.router.lifespan_context(application):
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/api/v1/system/overview")

    assert response.status_code == 200
    assert response.json() == {
        "mode": {"mode": "forensic"},
        "retention": {"effective": "ephemeral"},
        "network": {"effective": "offline"},
        "integrity": {"trust": "ENFORCED_PARTIAL"},
        "hardware": {"schema_version": 1, "view": "redacted"},
    }


def test_state_reader_rejects_missing_large_invalid_and_non_object_files(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    malformed = tmp_path / "malformed.json"
    array = tmp_path / "array.json"
    oversized = tmp_path / "oversized.json"
    malformed.write_text("{", encoding="utf-8")
    array.write_text("[]", encoding="utf-8")
    oversized.write_bytes(b"x" * 2_000_001)

    assert _read_json(missing) is None
    assert _read_json(malformed) is None
    assert _read_json(array) is None
    assert _read_json(oversized) is None


async def test_embedded_interface_is_served_after_api_routes(tmp_path: Path) -> None:
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<h1>ARES interface</h1>", encoding="utf-8")
    application = create_app(
        Settings(
            environment=Environment.TEST,
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'static.db'}",
            static_dir=static_dir,
            log_level="CRITICAL",
            log_format=LogFormat.TEXT,
        )
    )

    async with application.router.lifespan_context(application):
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            index = await client.get("/")
            health = await client.get("/api/v1/health/live")

    assert index.status_code == 200
    assert "ARES interface" in index.text
    assert health.status_code == 200
