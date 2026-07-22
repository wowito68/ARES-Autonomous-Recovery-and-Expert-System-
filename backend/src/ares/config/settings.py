"""Validated settings with secure local defaults."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ares import __version__


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
    log_level: str = "INFO"
    log_format: LogFormat = LogFormat.JSON

    @model_validator(mode="after")
    def reject_unsafe_production_diagnostics(self) -> Self:
        """Prevent SQL values from being logged by a production process."""

        if self.environment is Environment.PRODUCTION and self.database_echo:
            raise ValueError("database_echo must be disabled in production")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide immutable settings instance."""

    return Settings()
