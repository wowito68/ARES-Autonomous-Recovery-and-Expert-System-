"""Local, non-authoritative language-model runtime."""

from ares.llm.ollama import (
    AIChatResult,
    AIRuntime,
    AIRuntimeError,
    AIRuntimeStatus,
    OllamaRuntime,
)

__all__ = [
    "AIChatResult",
    "AIRuntime",
    "AIRuntimeError",
    "AIRuntimeStatus",
    "OllamaRuntime",
]
