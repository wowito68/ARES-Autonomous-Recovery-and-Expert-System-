"""Read-only platform state assembled from root-owned runtime files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from ares.config import Settings

router = APIRouter()
_MAX_STATE_FILE_BYTES = 2_000_000


class SystemOverview(BaseModel):
    """Small snapshot used by the local interface."""

    model_config = ConfigDict(extra="forbid")

    mode: dict[str, Any] | None
    retention: dict[str, Any] | None
    network: dict[str, Any] | None
    integrity: dict[str, Any] | None
    hardware: dict[str, Any] | None


@router.get("/overview", response_model=SystemOverview, summary="Read ARES platform state")
async def overview(request: Request) -> SystemOverview:
    """Read only allowlisted, bounded JSON files from the volatile ARES state."""

    settings = cast(Settings, request.app.state.settings)
    root = settings.runtime_state_dir
    return SystemOverview(
        mode=_read_json(root / "mode.json"),
        retention=_read_json(root / "state/status.json"),
        network=_read_json(root / "state/network.json"),
        integrity=_read_json(root / "integrity.json"),
        hardware=_read_json(root / "hardware/public/inventory-v1.json"),
    )


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        if not path.is_file() or path.stat().st_size > _MAX_STATE_FILE_BYTES:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None
