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
