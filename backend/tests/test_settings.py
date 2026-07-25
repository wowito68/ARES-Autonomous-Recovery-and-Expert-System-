"""Security invariants for application configuration."""

import pytest
from pydantic import ValidationError

from ares.config import Environment, Settings


def test_unknown_debug_flag_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(debug=True)  # type: ignore[call-arg]


def test_production_rejects_database_echo() -> None:
    with pytest.raises(ValidationError, match="database_echo must be disabled"):
        Settings(environment=Environment.PRODUCTION, database_echo=True)


def test_settings_are_immutable() -> None:
    settings = Settings()

    with pytest.raises(ValidationError):
        settings.database_echo = True


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://127.0.0.1:11434",
        "http://192.168.1.4:11434",
        "http://user:password@127.0.0.1:11434",
        "http://127.0.0.1:11434?remote=true",
        "http://127.0.0.1:11434/#fragment",
    ],
)
def test_ai_endpoint_must_be_plain_http_loopback_without_credentials(endpoint: str) -> None:
    with pytest.raises(ValidationError, match="ai_base_url"):
        Settings(ai_base_url=endpoint)


def test_ipv6_loopback_ai_endpoint_is_allowed() -> None:
    settings = Settings(ai_base_url="http://[::1]:11434")

    assert settings.ai_base_url == "http://[::1]:11434"
