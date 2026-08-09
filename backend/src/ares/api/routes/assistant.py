"""Bounded, read-only conversational API for the local model."""

from __future__ import annotations

from typing import Annotated, Literal, cast

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ares.config import Settings
from ares.core.problems import AresProblem, ProblemDetail
from ares.llm import AIRuntime, AIRuntimeError

router = APIRouter()

_SYSTEM_PROMPT = """\
Eres ARES, un asistente local de diagnóstico y recuperación para Debian y hardware de PC.
Responde en el idioma del usuario, con pasos breves, verificables y conservadores.
No tienes terminal, privilegios ni acceso directo a Tools o Actions.
Conoces las Capabilities publicadas por ARES y puedes proponer un backup cuando el usuario quiera proteger datos antes de una reparación, pero nunca afirmes que lo ejecutaste desde el chat.
Para un backup debes identificar el recurso y un destino explícito; si el destino seguro no está determinado, pregunta en vez de adivinar. Explica que ARES genera primero un BackupPlan estructurado con tamaño, espacio, exclusiones y riesgo, y que backup.create requiere consentimiento local independiente antes de escribir.
No afirmes que ejecutaste comandos, reparaste algo o verificaste hardware si no existe evidencia estructurada proporcionada por ARES.
Separa hechos, hipótesis y próximos pasos. Si falta evidencia, dilo claramente.
No solicites contraseñas, claves, tokens ni datos personales.
"""


class ChatMessage(BaseModel):
    """One untrusted user-visible conversation message."""

    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: Annotated[str, Field(min_length=1, max_length=4_000)]


class ChatRequest(BaseModel):
    """Bounded chat history; system instructions always remain server-owned."""

    model_config = ConfigDict(extra="forbid")

    messages: Annotated[list[ChatMessage], Field(min_length=1, max_length=12)]

    @model_validator(mode="after")
    def require_user_turn(self) -> ChatRequest:
        if self.messages[-1].role != "user":
            raise ValueError("the final message must have role=user")
        return self


class ChatResponse(BaseModel):
    """Completed local-model response."""

    model_config = ConfigDict(extra="forbid")

    content: str
    model: str
    runtime: Literal["ollama"] = "ollama"
    tools_enabled: Literal[False] = False
    done_reason: str | None
    prompt_tokens: int | None
    response_tokens: int | None


@router.post(
    "/chat",
    response_model=ChatResponse,
    responses={
        503: {
            "description": "The local AI runtime or configured model is unavailable",
            "content": {"application/problem+json": {"schema": ProblemDetail.model_json_schema()}},
        }
    },
    summary="Ask the local read-only assistant",
)
async def chat(payload: ChatRequest, request: Request) -> ChatResponse:
    """Send bounded context to a loopback-only model with no tool definitions."""

    settings = cast(Settings, request.app.state.settings)
    context_chars = sum(len(message.content) for message in payload.messages)
    if context_chars > settings.ai_max_context_chars:
        raise AresProblem(
            status=422,
            code="AI_CONTEXT_TOO_LARGE",
            title="AI context too large",
            detail="The conversation exceeds the local context safety limit.",
        )
    messages = [{"role": "system", "content": _SYSTEM_PROMPT}]
    messages.extend(message.model_dump(mode="json") for message in payload.messages)
    runtime = _runtime(request)
    try:
        result = await runtime.chat(messages)
    except AIRuntimeError as exc:
        if exc.reason == "model_missing":
            code = "AI_MODEL_MISSING"
            detail = "The configured offline model is not installed."
        elif exc.reason == "invalid_response":
            code = "AI_INVALID_RESPONSE"
            detail = "The local model returned an invalid or unsafe response."
        else:
            code = "AI_RUNTIME_UNAVAILABLE"
            detail = "The local AI runtime is not available."
        raise AresProblem(
            status=503,
            code=code,
            title="Local AI unavailable",
            detail=detail,
        ) from exc
    return ChatResponse(
        content=result.content,
        model=result.model,
        done_reason=result.done_reason,
        prompt_tokens=result.prompt_tokens,
        response_tokens=result.response_tokens,
    )


def _runtime(request: Request) -> AIRuntime:
    return cast(AIRuntime, request.app.state.ai_runtime)
