"""Contracts for the bounded, local-only assistant API."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from ares.config import Environment, LogFormat, Settings
from ares.llm import AIChatResult, AIRuntimeError, AIRuntimeStatus
from ares.main import create_app


@dataclass
class FakeRuntime:
    """Deterministic runtime double that records the server-owned prompt."""

    state: str = "ready"
    error: str | None = None
    received: list[list[dict[str, str]]] = field(default_factory=list)

    async def status(self) -> AIRuntimeStatus:
        return AIRuntimeStatus(
            state=self.state,  # type: ignore[arg-type]
            model="ares-test-model",
            installed_models=("ares-test-model",) if self.state == "ready" else (),
        )

    async def chat(self, messages: list[dict[str, str]]) -> AIChatResult:
        self.received.append(messages)
        if self.error is not None:
            raise AIRuntimeError(self.error)  # type: ignore[arg-type]
        return AIChatResult(
            content="Diagnóstico local de prueba.",
            model="ares-test-model",
            done_reason="stop",
            prompt_tokens=24,
            response_tokens=8,
        )


def test_runtime_error_rejects_unknown_public_reason() -> None:
    with pytest.raises(TypeError):
        AIRuntimeError()  # type: ignore[call-arg]


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": Environment.TEST,
        "database_url": f"sqlite+aiosqlite:///{tmp_path / 'ai-test.db'}",
        "log_level": "CRITICAL",
        "log_format": LogFormat.TEXT,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


async def _client(
    tmp_path: Path,
    runtime: FakeRuntime,
    **settings_overrides: object,
) -> AsyncIterator[AsyncClient]:
    application = create_app(_settings(tmp_path, **settings_overrides), ai_runtime=runtime)
    async with application.router.lifespan_context(application):
        transport = ASGITransport(app=application, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client


async def test_ai_status_reports_ready_without_exposing_installed_inventory(tmp_path: Path) -> None:
    runtime = FakeRuntime()

    async for client in _client(tmp_path, runtime):
        response = await client.get("/api/v1/ai/status")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "runtime": "ollama",
        "model": "ares-test-model",
        "installed": True,
        "local_only": True,
        "tools_enabled": False,
    }


async def test_chat_injects_server_prompt_and_never_defines_tools(tmp_path: Path) -> None:
    runtime = FakeRuntime()

    async for client in _client(tmp_path, runtime):
        response = await client.post(
            "/api/v1/assistant/chat",
            json={"messages": [{"role": "user", "content": "¿Qué observas?"}]},
        )

    assert response.status_code == 200
    assert response.json() == {
        "content": "Diagnóstico local de prueba.",
        "model": "ares-test-model",
        "runtime": "ollama",
        "tools_enabled": False,
        "done_reason": "stop",
        "prompt_tokens": 24,
        "response_tokens": 8,
    }
    assert runtime.received[0][0]["role"] == "system"
    assert "no tienes herramientas" in runtime.received[0][0]["content"]
    assert runtime.received[0][-1] == {"role": "user", "content": "¿Qué observas?"}


@pytest.mark.parametrize(
    ("reason", "code"),
    [
        ("runtime_unavailable", "AI_RUNTIME_UNAVAILABLE"),
        ("model_missing", "AI_MODEL_MISSING"),
        ("invalid_response", "AI_INVALID_RESPONSE"),
    ],
)
async def test_chat_maps_runtime_failures_to_stable_problem(
    tmp_path: Path,
    reason: str,
    code: str,
) -> None:
    runtime = FakeRuntime(error=reason)

    async for client in _client(tmp_path, runtime):
        response = await client.post(
            "/api/v1/assistant/chat",
            json={"messages": [{"role": "user", "content": "Prueba"}]},
        )

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == code


async def test_chat_rejects_non_user_final_turn(tmp_path: Path) -> None:
    runtime = FakeRuntime()

    async for client in _client(tmp_path, runtime):
        response = await client.post(
            "/api/v1/assistant/chat",
            json={"messages": [{"role": "assistant", "content": "texto"}]},
        )

    assert response.status_code == 422
    assert response.json()["code"] == "REQUEST_VALIDATION_FAILED"
    assert runtime.received == []


async def test_chat_rejects_context_over_server_limit(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    messages = [
        {"role": "user", "content": "a" * 700},
        {"role": "assistant", "content": "b" * 700},
        {"role": "user", "content": "c" * 700},
    ]

    async for client in _client(tmp_path, runtime, ai_max_context_chars=2_000):
        response = await client.post("/api/v1/assistant/chat", json={"messages": messages})

    assert response.status_code == 422
    assert response.json()["code"] == "AI_CONTEXT_TOO_LARGE"
    assert runtime.received == []
