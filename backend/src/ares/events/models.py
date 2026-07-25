"""Event contracts shared by the ARES v2 core."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    """Return an aware UTC timestamp."""

    return datetime.now(UTC)


class AresEvent(BaseModel):
    """Immutable, serializable event written before it is dispatched."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: uuid4().hex)
    name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{2,95}$")]
    source: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{2,95}$")]
    correlation_id: Annotated[str, Field(min_length=8, max_length=128)]
    occurred_at: datetime = Field(default_factory=utc_now)
    payload: dict[str, Any] = Field(default_factory=dict)
