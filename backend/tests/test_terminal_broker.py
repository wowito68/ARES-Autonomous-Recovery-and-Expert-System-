"""Terminal broker boundary tests."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from ares.resources.models import MountState, ResourceKind
from ares.runtime.terminal_broker import TerminalBroker, TerminalBrokerError
from ares.terminal.models import (
    TerminalContextKind,
    TerminalPlan,
    TerminalSessionStatus,
    TerminalTarget,
)


class _Audit:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def append(self, **kwargs: Any) -> None:
        self.events.append(kwargs)


async def _send(message: dict[str, Any]) -> None:
    del message


def _plan(source: Path) -> TerminalPlan:
    target = TerminalTarget(
        resource_id="res:os-fixture",
        human_name="Fixture Linux",
        kind=ResourceKind.OPERATING_SYSTEM,
        operating_system="Fixture Linux",
        version="13",
        filesystem="ext4",
        size_bytes=1024,
        current_mount_state=MountState.MOUNTED,
        technical_path=str(source),
        technical_details={"mountpoint": str(source)},
        stable_identity="fixture-os-identity",
        fingerprint="sha256:" + "2" * 64,
        confidence=0.95,
    )
    return TerminalPlan.build(
        session_id="terminalbroker123",
        context_kind=TerminalContextKind.INSTALLED_SYSTEM_READ_ONLY,
        target=target,
    )


async def test_terminal_broker_rejects_forbidden_fields(tmp_path: Path) -> None:
    broker = TerminalBroker(
        _Audit(),
        runtime_root=tmp_path / "sessions",
        gui_request_root=tmp_path / "requests",
        allowed_client_uids=frozenset({os.getuid()}),
    )

    with pytest.raises(TerminalBrokerError) as exc:
        await broker.dispatch(
            {"action": "terminal.status", "session_id": "abc12345", "command": "id"},
            os.getuid(),
            _send,
        )

    assert exc.value.code == "TERMINAL_BROKER_FORBIDDEN_FIELD"


async def test_terminal_broker_authorize_start_close_and_cleanup_are_bounded(
    tmp_path: Path,
) -> None:
    source = tmp_path / "installed"
    source.mkdir()
    audit = _Audit()
    broker = TerminalBroker(
        audit,
        runtime_root=tmp_path / "sessions",
        gui_request_root=tmp_path / "requests",
        allowed_client_uids=frozenset({os.getuid()}),
    )
    plan = _plan(source)

    grant_payload = await broker.dispatch(
        {
            "action": "terminal.authorize",
            "plan": plan.model_dump(mode="json"),
            "operator_uid": 1000,
        },
        os.getuid(),
        _send,
    )
    session_payload = await broker.dispatch(
        {
            "action": "terminal.start",
            "plan": plan.model_dump(mode="json"),
            "grant": grant_payload,
        },
        os.getuid(),
        _send,
    )
    request_file = tmp_path / "requests" / f"{plan.session_id}.json"
    assert request_file.is_file()
    closed_payload = await broker.dispatch(
        {"action": "terminal.close", "session_id": plan.session_id},
        os.getuid(),
        _send,
    )
    evidence = await broker.dispatch(
        {"action": "terminal.cleanup", "session_id": plan.session_id},
        os.getuid(),
        _send,
    )

    assert session_payload["status"] == TerminalSessionStatus.ACTIVE.value
    assert closed_payload["status"] == TerminalSessionStatus.CLOSED.value
    assert evidence["cleanup_verified"] is True
    assert not request_file.exists()
    assert not (tmp_path / "sessions" / plan.session_id).exists()
    assert any(item["event_type"] == "terminal.authorization.granted" for item in audit.events)
