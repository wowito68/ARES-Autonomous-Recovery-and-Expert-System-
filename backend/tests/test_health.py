"""Contract tests for the foundational health API."""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from ares import __version__
from ares.config import Environment, LogFormat, Settings
from ares.main import create_app


async def test_liveness_reports_service_and_version(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health/live")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "ares-api",
        "version": __version__,
    }
    assert re.fullmatch(r"[0-9a-f]{32}", response.headers["x-request-id"])


async def test_valid_request_id_is_preserved(client: AsyncClient) -> None:
    request_id = "diagnostic-request-0001"

    response = await client.get(
        "/api/v1/health/live",
        headers={"X-Request-ID": request_id},
    )

    assert response.headers["x-request-id"] == request_id


async def test_unsafe_request_id_is_replaced(client: AsyncClient) -> None:
    response = await client.get(
        "/api/v1/health/live",
        headers={"X-Request-ID": "short"},
    )

    assert response.headers["x-request-id"] != "short"
    assert re.fullmatch(r"[0-9a-f]{32}", response.headers["x-request-id"])


async def test_readiness_queries_database(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "ares-api",
        "version": __version__,
        "checks": {"database": "ok"},
    }


async def test_readiness_returns_problem_when_database_fails(
    client: AsyncClient, app: FastAPI
) -> None:
    database = app.state.database
    database.check = AsyncMock(side_effect=OSError("database offline"))

    response = await client.get("/api/v1/health/ready")

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json() == {
        "type": "urn:ares:error:dependency-unavailable",
        "title": "Service unavailable",
        "status": 503,
        "code": "DEPENDENCY_UNAVAILABLE",
        "detail": "The local database is not ready.",
        "instance": "/api/v1/health/ready",
        "request_id": response.headers["x-request-id"],
    }


async def test_unknown_route_uses_problem_details(client: AsyncClient) -> None:
    response = await client.get("/api/v1/not-found")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "HTTP_404"
    assert response.json()["request_id"] == response.headers["x-request-id"]


async def test_http_problem_preserves_protocol_headers(client: AsyncClient) -> None:
    response = await client.post("/api/v1/health/live")

    assert response.status_code == 405
    assert response.headers["allow"] == "GET"
    assert response.json()["code"] == "HTTP_405"


async def test_unexpected_error_is_redacted_and_correlated(
    client: AsyncClient, app: FastAPI
) -> None:
    async def fail() -> None:
        raise RuntimeError("sensitive internal detail")

    app.add_api_route("/api/v1/fail", fail)

    response = await client.get("/api/v1/fail")

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "INTERNAL_ERROR"
    assert response.json()["detail"] == "An unexpected local error occurred."
    assert response.json()["request_id"] == response.headers["x-request-id"]
    assert "sensitive internal detail" not in response.text


async def test_openapi_contains_both_health_checks(client: AsyncClient) -> None:
    response = await client.get("/openapi.json")

    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/api/v1/health/live" in paths
    assert "/api/v1/health/ready" in paths
    readiness_error = paths["/api/v1/health/ready"]["get"]["responses"]["503"]
    assert set(readiness_error["content"]) == {"application/problem+json"}


async def test_swagger_html_is_disabled_offline(client: AsyncClient) -> None:
    response = await client.get("/docs")

    assert response.status_code == 404


async def test_validation_runtime_and_openapi_use_problem_details(
    client: AsyncClient, app: FastAPI
) -> None:
    async def typed_route(item_id: int) -> dict[str, int]:
        return {"item_id": item_id}

    app.add_api_route("/api/v1/items/{item_id}", typed_route, methods=["GET"])

    response = await client.get("/api/v1/items/not-an-integer")
    schema_response = await client.get("/openapi.json")

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "REQUEST_VALIDATION_FAILED"
    validation_error = schema_response.json()["paths"]["/api/v1/items/{item_id}"]["get"][
        "responses"
    ]["422"]
    assert set(validation_error["content"]) == {"application/problem+json"}


async def test_readiness_rejects_corrupt_database(tmp_path: Path) -> None:
    database_path = tmp_path / "corrupt.db"
    database_path.write_bytes(b"not-a-sqlite-database" * 32)
    application = create_app(
        Settings(
            environment=Environment.TEST,
            database_url=f"sqlite+aiosqlite:///{database_path}",
            log_level="CRITICAL",
            log_format=LogFormat.TEXT,
        )
    )

    async with application.router.lifespan_context(application):
        transport = ASGITransport(app=application, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
            response = await http_client.get("/api/v1/health/ready")

    assert response.status_code == 503
    assert response.json()["code"] == "DEPENDENCY_UNAVAILABLE"


async def test_production_readiness_does_not_recreate_missing_database(tmp_path: Path) -> None:
    database_path = tmp_path / "missing.db"
    application = create_app(
        Settings(
            environment=Environment.PRODUCTION,
            database_url=f"sqlite+aiosqlite:///{database_path}",
            log_level="CRITICAL",
            log_format=LogFormat.TEXT,
        )
    )

    async with application.router.lifespan_context(application):
        transport = ASGITransport(app=application, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
            response = await http_client.get("/api/v1/health/ready")

    assert response.status_code == 503
    assert not database_path.exists()


async def test_readiness_never_recreates_deleted_development_database(tmp_path: Path) -> None:
    database_path = tmp_path / "deleted.db"
    application = create_app(
        Settings(
            environment=Environment.TEST,
            database_url=f"sqlite+aiosqlite:///{database_path}",
            log_level="CRITICAL",
            log_format=LogFormat.TEXT,
        )
    )

    async with application.router.lifespan_context(application):
        assert database_path.exists()
        database_path.unlink()
        transport = ASGITransport(app=application, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
            response = await http_client.get("/api/v1/health/ready")

    assert response.status_code == 503
    assert not database_path.exists()
