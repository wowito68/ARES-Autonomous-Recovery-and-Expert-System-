from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from ares.config import Environment, LogFormat, Settings
from ares.main import create_app

pytestmark = pytest.mark.skipif(shutil.which("sfdisk") is None, reason="sfdisk unavailable")


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        environment=Environment.TEST,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'storage-operations.db'}",
        runtime_state_dir=tmp_path / "run",
        capability_state_dir=tmp_path / "state",
        storage_process_probes_enabled=False,
        log_level="CRITICAL",
        log_format=LogFormat.TEXT,
    )


def _image(path: Path) -> None:
    with path.open("wb") as handle:
        handle.truncate(128 * 1024 * 1024)


async def _poll(
    client: AsyncClient,
    operation_id: str,
    expected: set[str],
    headers: dict[str, str],
) -> dict[str, object]:
    for _ in range(100):
        response = await client.get(f"/api/v1/storage/operations/{operation_id}", headers=headers)
        assert response.status_code == 200
        payload = cast(dict[str, object], response.json())
        transaction = cast(dict[str, object], payload["transaction"])
        if transaction["status"] in expected:
            return payload
        await asyncio.sleep(0.02)
    raise AssertionError("storage operation did not reach expected state")


async def test_storage_operation_api_full_test_image_lifecycle(tmp_path: Path) -> None:
    image = tmp_path / "api-storage-fixture.img"
    _image(image)
    app: FastAPI = create_app(_settings(tmp_path))
    headers = {"X-ARES-Session-ID": "storage-api-session-1234"}

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            layout = await client.get(
                "/api/v1/storage/layout", params={"target_disk": str(image)}, headers=headers
            )
            assert layout.status_code == 200

            planned = await client.post(
                "/api/v1/storage/operations/plan",
                headers=headers,
                json={
                    "operation": "create",
                    "target_disk": str(image),
                    "size_bytes": 16 * 1024 * 1024,
                    "table_type": "GPT",
                    "partition_name": "ARES API TEST",
                },
            )
            assert planned.status_code == 200
            plan = cast(dict[str, object], planned.json())
            operation_id = cast(str, plan["operation_id"])
            assert plan["executable"] is False

            validated = await client.post(
                f"/api/v1/storage/operations/{operation_id}/validate", headers=headers
            )
            assert validated.status_code == 200
            validated_plan = cast(dict[str, object], validated.json())
            assert validated_plan["executable"] is True
            assert validated_plan["dry_run"] is not None
            assert validated_plan["protection_checkpoint"] is not None

            authorized = await client.post(
                f"/api/v1/storage/operations/{operation_id}/authorize",
                headers=headers,
                json={"request_authorization": True},
            )
            assert authorized.status_code == 202
            authorized_record = await _poll(client, operation_id, {"AUTHORIZED"}, headers)
            transaction = cast(dict[str, object], authorized_record["transaction"])
            assert transaction["authorization_challenge_id"]

            execute = await client.post(
                f"/api/v1/storage/operations/{operation_id}/execute", headers=headers
            )
            assert execute.status_code == 202
            committed = await _poll(
                client, operation_id, {"COMMITTED", "FAILED", "UNKNOWN"}, headers
            )
            committed_tx = cast(dict[str, object], committed["transaction"])
            assert committed_tx["status"] == "COMMITTED"

            verification = await client.get(
                f"/api/v1/storage/operations/{operation_id}/verification", headers=headers
            )
            assert verification.status_code == 200
            assert cast(dict[str, object], verification.json())["status"] == "VERIFIED"

            graph_kinds: set[object] = set()
            for _ in range(100):
                graph = await client.get("/api/v1/knowledge/graph", headers=headers)
                assert graph.status_code == 200
                nodes = cast(
                    list[dict[str, object]], cast(dict[str, object], graph.json())["nodes"]
                )
                graph_kinds = {node["kind"] for node in nodes}
                if "storage_verification" in graph_kinds:
                    break
                await asyncio.sleep(0.02)
            assert {"partition_table", "storage_transaction", "storage_verification"} <= graph_kinds

            missing = await client.get("/api/v1/storage/operations/missing-operation")
            assert missing.status_code == 404
            missing_verification = await client.get(
                "/api/v1/storage/operations/missing-operation/verification"
            )
            assert missing_verification.status_code == 404


async def test_storage_operation_api_rejects_session_and_pre_auth_execution(
    tmp_path: Path,
) -> None:
    image = tmp_path / "api-storage-errors.img"
    _image(image)
    app = create_app(_settings(tmp_path))
    owner = {"X-ARES-Session-ID": "storage-owner-session-1234"}
    other = {"X-ARES-Session-ID": "storage-other-session-1234"}

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            planned = await client.post(
                "/api/v1/storage/operations/plan",
                headers=owner,
                json={
                    "operation": "create",
                    "target_disk": str(image),
                    "size_bytes": 8 * 1024 * 1024,
                    "table_type": "MBR",
                },
            )
            assert planned.status_code == 200
            operation_id = cast(str, cast(dict[str, object], planned.json())["operation_id"])

            wrong_session = await client.post(
                f"/api/v1/storage/operations/{operation_id}/validate", headers=other
            )
            assert wrong_session.status_code == 409
            assert cast(dict[str, object], wrong_session.json())["code"] == (
                "STORAGE_OPERATION_SESSION_MISMATCH"
            )

            unauthorized = await client.post(
                f"/api/v1/storage/operations/{operation_id}/execute", headers=owner
            )
            assert unauthorized.status_code == 409
            assert cast(dict[str, object], unauthorized.json())["code"] == (
                "STORAGE_OPERATION_NOT_AUTHORIZED"
            )

            reconcile = await client.post(
                f"/api/v1/storage/operations/{operation_id}/reconcile", headers=owner
            )
            assert reconcile.status_code == 409
