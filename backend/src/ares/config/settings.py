"""Validated settings with secure local defaults."""

from __future__ import annotations

import re
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Self
from urllib.parse import urlsplit

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ares import __version__

_ENTRY_POINT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class Environment(StrEnum):
    """Supported runtime environments."""

    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class LogFormat(StrEnum):
    """Supported log encodings."""

    JSON = "json"
    TEXT = "text"


class Settings(BaseSettings):
    """ARES settings loaded from environment variables prefixed with ``ARES_``."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="ARES_",
        extra="forbid",
        case_sensitive=False,
        frozen=True,
    )

    service_name: str = "ares-api"
    version: str = __version__
    environment: Environment = Environment.DEVELOPMENT
    api_prefix: str = "/api/v1"
    database_url: str = "sqlite+aiosqlite:///./data/ares.db"
    database_echo: bool = False
    database_busy_timeout_ms: Annotated[int, Field(ge=100, le=60_000)] = 5_000
    readiness_timeout_seconds: Annotated[float, Field(gt=0, le=30)] = 2.0
    runtime_state_dir: Path = Path("/run/ares")
    capability_state_dir: Path | None = None
    capability_plugin_entrypoint_group: Annotated[
        str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{2,127}$")
    ] = "ares.capabilities"
    capability_plugin_allowlist: tuple[str, ...] = ()
    static_dir: Path | None = None
    ai_base_url: str = "http://127.0.0.1:11434"
    ai_model: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")] = (
        "qwen2.5:1.5b-instruct-q4_K_M"
    )
    ai_connect_timeout_seconds: Annotated[float, Field(gt=0, le=10)] = 1.0
    ai_response_timeout_seconds: Annotated[float, Field(gt=0, le=600)] = 120.0
    ai_max_context_chars: Annotated[int, Field(ge=1_000, le=100_000)] = 16_000
    ai_max_response_chars: Annotated[int, Field(ge=1_000, le=100_000)] = 16_000
    ai_max_predict_tokens: Annotated[int, Field(ge=64, le=4_096)] = 768
    log_level: str = "INFO"
    log_format: LogFormat = LogFormat.JSON

    @model_validator(mode="after")
    def reject_unsafe_production_diagnostics(self) -> Self:
        """Prevent unsafe diagnostics and untrusted plugin discovery policy."""

        if self.environment is Environment.PRODUCTION and self.database_echo:
            raise ValueError("database_echo must be disabled in production")
        endpoint = urlsplit(self.ai_base_url)
        if endpoint.scheme != "http" or endpoint.hostname not in {
            "127.0.0.1",
            "::1",
            "localhost",
        }:
            raise ValueError("ai_base_url must be an HTTP loopback endpoint")
        if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
            raise ValueError("ai_base_url must not contain credentials, query, or fragment")
        if len(set(self.capability_plugin_allowlist)) != len(self.capability_plugin_allowlist):
            raise ValueError("capability_plugin_allowlist contains duplicates")
        if any(
            _ENTRY_POINT_NAME.fullmatch(name) is None
            for name in self.capability_plugin_allowlist
        ):
            raise ValueError("capability_plugin_allowlist contains an invalid entry-point name")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide immutable settings instance."""

    return Settings()
