"""Event contracts shared by the ARES v2 core."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any
from uuid import uuid4

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    model_validator,
)


def utc_now() -> datetime:
    """Return an aware UTC timestamp."""

    return datetime.now(UTC)


class EventSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class AresEvent(BaseModel):
    """Immutable event envelope written before it is dispatched.

    Canonical serialized fields follow the vertical-slice envelope while the
    legacy ``id``/``name``/``occurred_at`` fields remain as computed aliases
    during the v2 migration so existing journal readers continue to work.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    event_id: str = Field(
        default_factory=lambda: uuid4().hex,
        validation_alias=AliasChoices("event_id", "id"),
    )
    event_type: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{2,95}$")] = Field(
        validation_alias=AliasChoices("event_type", "name")
    )
    source: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{2,95}$")]
    correlation_id: Annotated[str, Field(min_length=8, max_length=128)]
    session_id: Annotated[str, Field(min_length=8, max_length=128)]
    timestamp: datetime = Field(
        default_factory=utc_now,
        validation_alias=AliasChoices("timestamp", "occurred_at"),
    )
    severity: EventSeverity | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def default_session_to_correlation(cls, value: Any) -> Any:
        if isinstance(value, dict) and "session_id" not in value:
            correlation_id = value.get("correlation_id")
            if isinstance(correlation_id, str):
                value = {**value, "session_id": correlation_id}
        return value

    @computed_field
    @property
    def id(self) -> str:
        """Legacy serialized alias for ``event_id``."""

        return self.event_id

    @computed_field
    @property
    def name(self) -> str:
        """Legacy serialized alias for ``event_type``."""

        return self.event_type

    @computed_field
    @property
    def occurred_at(self) -> datetime:
        """Legacy serialized alias for ``timestamp``."""

        return self.timestamp
