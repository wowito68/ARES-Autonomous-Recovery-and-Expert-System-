"""Strict loopback-only adapter for the local Ollama API."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx

from ares.config import Settings

RuntimeState = Literal["ready", "model_missing", "runtime_unavailable"]


@dataclass(frozen=True, slots=True)
class AIRuntimeStatus:
    """Observed state of the configured local model runtime."""

    state: RuntimeState
    model: str
    installed_models: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AIChatResult:
    """Bounded assistant result returned by a local model."""

    content: str
    model: str
    done_reason: str | None
    prompt_tokens: int | None
    response_tokens: int | None


class AIRuntimeError(Exception):
    """Safe internal error carrying a stable public reason."""

    def __init__(self, reason: Literal["runtime_unavailable", "model_missing", "invalid_response"]):
        super().__init__(reason)
        self.reason = reason


class AIRuntime(Protocol):
    """Port implemented by a local and unprivileged model runtime."""

    async def status(self) -> AIRuntimeStatus: ...

    async def chat(self, messages: list[dict[str, str]]) -> AIChatResult: ...


class OllamaRuntime:
    """Call Ollama over loopback without proxy, cloud, or tool access."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = settings.ai_base_url.rstrip("/")
        self._model = settings.ai_model
        self._connect_timeout = settings.ai_connect_timeout_seconds
        self._response_timeout = settings.ai_response_timeout_seconds
        self._max_response_chars = settings.ai_max_response_chars
        self._max_predict_tokens = settings.ai_max_predict_tokens
        self._transport = transport

    async def status(self) -> AIRuntimeStatus:
        try:
            async with self._client(response_timeout=self._connect_timeout) as client:
                response = await client.get("/api/tags")
                response.raise_for_status()
        except (httpx.HTTPError, OSError):
            return AIRuntimeStatus(state="runtime_unavailable", model=self._model)

        try:
            payload = _json_object(response)
        except AIRuntimeError:
            return AIRuntimeStatus(state="runtime_unavailable", model=self._model)
        models = payload.get("models")
        if not isinstance(models, list):
            return AIRuntimeStatus(state="runtime_unavailable", model=self._model)
        installed = tuple(
            sorted(
                {
                    name
                    for item in models
                    if isinstance(item, dict)
                    and isinstance((name := item.get("name")), str)
                    and 0 < len(name) <= 128
                }
            )
        )
        state: RuntimeState = "ready" if self._model in installed else "model_missing"
        return AIRuntimeStatus(state=state, model=self._model, installed_models=installed)

    async def chat(self, messages: list[dict[str, str]]) -> AIChatResult:
        request = {
            "model": self._model,
            "messages": messages,
            "stream": False,
            "think": False,
            "keep_alive": "5m",
            "options": {
                "temperature": 0.2,
                "num_predict": self._max_predict_tokens,
            },
        }
        try:
            async with self._client(response_timeout=self._response_timeout) as client:
                response = await client.post("/api/chat", json=request)
        except (httpx.HTTPError, OSError) as exc:
            raise AIRuntimeError("runtime_unavailable") from exc

        if response.status_code == 404:
            raise AIRuntimeError("model_missing")
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise AIRuntimeError("runtime_unavailable") from exc

        payload = _json_object(response)
        message = payload.get("message")
        if not isinstance(message, dict) or message.get("tool_calls"):
            raise AIRuntimeError("invalid_response")
        content = message.get("content")
        model = payload.get("model")
        if (
            not isinstance(content, str)
            or not content.strip()
            or len(content) > self._max_response_chars
            or not isinstance(model, str)
            or model != self._model
        ):
            raise AIRuntimeError("invalid_response")
        return AIChatResult(
            content=content.strip(),
            model=model,
            done_reason=_optional_string(payload.get("done_reason")),
            prompt_tokens=_optional_nonnegative_int(payload.get("prompt_eval_count")),
            response_tokens=_optional_nonnegative_int(payload.get("eval_count")),
        )

    def _client(self, *, response_timeout: float) -> httpx.AsyncClient:
        timeout = httpx.Timeout(
            connect=self._connect_timeout,
            read=response_timeout,
            write=10.0,
            pool=self._connect_timeout,
        )
        return httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            trust_env=False,
            follow_redirects=False,
            headers={"User-Agent": "ARES-OS/local-ai"},
            transport=self._transport,
        )


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise AIRuntimeError("invalid_response") from exc
    if not isinstance(payload, dict):
        raise AIRuntimeError("invalid_response")
    return payload


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and len(value) <= 64 else None


def _optional_nonnegative_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
