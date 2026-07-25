"""The Ollama adapter must stay on loopback and reject unsafe responses."""

from __future__ import annotations

import json

import httpx
import pytest

from ares.config import Settings
from ares.llm import AIRuntimeError, OllamaRuntime


def _runtime(handler: httpx.SyncByteStream | object) -> OllamaRuntime:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return OllamaRuntime(Settings(), transport=transport)


async def test_status_detects_configured_model_and_sorts_inventory() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "http://127.0.0.1:11434/api/tags"
        return httpx.Response(
            200,
            json={
                "models": [
                    {"name": "z-model"},
                    {"name": "qwen2.5:1.5b-instruct-q4_K_M"},
                    {"name": 4},
                ]
            },
        )

    status = await _runtime(handler).status()

    assert status.state == "ready"
    assert status.installed_models == ("qwen2.5:1.5b-instruct-q4_K_M", "z-model")


async def test_status_distinguishes_missing_model() -> None:
    runtime = _runtime(lambda _: httpx.Response(200, json={"models": [{"name": "other"}]}))

    status = await runtime.status()

    assert status.state == "model_missing"


@pytest.mark.parametrize(
    "handler",
    [
        lambda _: httpx.Response(500),
        lambda _: httpx.Response(200, text="not-json"),
        lambda _: httpx.Response(200, json=[]),
        lambda _: httpx.Response(200, json={"models": "invalid"}),
        lambda request: (_ for _ in ()).throw(httpx.ConnectError("offline", request=request)),
    ],
)
async def test_status_fails_closed_for_unavailable_or_invalid_runtime(handler: object) -> None:
    status = await _runtime(handler).status()

    assert status.state == "runtime_unavailable"
    assert status.installed_models == ()


async def test_chat_sends_bounded_generation_without_tools() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert request.url == "http://127.0.0.1:11434/api/chat"
        assert payload["stream"] is False
        assert payload["think"] is False
        assert payload["keep_alive"] == "5m"
        assert payload["options"]["num_predict"] == 768
        assert "tools" not in payload
        return httpx.Response(
            200,
            json={
                "model": "qwen2.5:1.5b-instruct-q4_K_M",
                "message": {"role": "assistant", "content": " Respuesta local. "},
                "done_reason": "stop",
                "prompt_eval_count": 10,
                "eval_count": 5,
            },
        )

    result = await _runtime(handler).chat([{"role": "user", "content": "hola"}])

    assert result.content == "Respuesta local."
    assert result.done_reason == "stop"
    assert result.prompt_tokens == 10
    assert result.response_tokens == 5


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (httpx.Response(404), "model_missing"),
        (httpx.Response(500), "runtime_unavailable"),
        (
            httpx.Response(
                200,
                json={
                    "model": "qwen2.5:1.5b-instruct-q4_K_M",
                    "message": {"content": "texto", "tool_calls": [{"function": {}}]},
                },
            ),
            "invalid_response",
        ),
        (
            httpx.Response(
                200,
                json={"model": "unexpected", "message": {"content": "texto"}},
            ),
            "invalid_response",
        ),
        (httpx.Response(200, text="not-json"), "invalid_response"),
    ],
)
async def test_chat_maps_invalid_or_failed_responses(
    response: httpx.Response,
    reason: str,
) -> None:
    runtime = _runtime(lambda _: response)

    with pytest.raises(AIRuntimeError) as raised:
        await runtime.chat([{"role": "user", "content": "hola"}])

    assert raised.value.reason == reason


async def test_chat_maps_connection_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    with pytest.raises(AIRuntimeError, match="runtime_unavailable"):
        await _runtime(handler).chat([{"role": "user", "content": "hola"}])


async def test_optional_generation_metadata_rejects_wrong_types() -> None:
    response = httpx.Response(
        200,
        json={
            "model": "qwen2.5:1.5b-instruct-q4_K_M",
            "message": {"content": "texto"},
            "done_reason": 8,
            "prompt_eval_count": True,
            "eval_count": -1,
        },
    )

    result = await _runtime(lambda _: response).chat([{"role": "user", "content": "hola"}])

    assert result.done_reason is None
    assert result.prompt_tokens is None
    assert result.response_tokens is None
