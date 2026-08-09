"""Independent local consent authority for exact one-use backup plans."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import struct
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from ares.audit.ledger import AuditLedger, AuditLedgerError, UnixAuditLedgerClient
from ares.backup.models import BackupPlan

_MAX_MESSAGE_BYTES = 512_000


@dataclass(slots=True)
class _Challenge:
    id: str
    plan_id: str
    fingerprint: str
    session_id: str
    source: str
    destination: str
    estimated_bytes: int
    required_bytes: int
    available_bytes: int
    file_count: int
    exclusions: tuple[dict[str, str], ...]
    expires_at: datetime
    decision: str = "pending"
    event: asyncio.Event = field(default_factory=asyncio.Event)

    def public(self) -> dict[str, Any]:
        return {
            "challenge_id": self.id,
            "plan_id": self.plan_id,
            "plan_fingerprint_sha256": self.fingerprint,
            "source": self.source,
            "destination": self.destination,
            "estimated_bytes": self.estimated_bytes,
            "required_bytes": self.required_bytes,
            "available_bytes": self.available_bytes,
            "file_count": self.file_count,
            "exclusions": list(self.exclusions),
            "excluded_count": len(self.exclusions),
            "risk": "medium",
            "overwrite": False,
            "verification": "sha256",
            "expires_at": self.expires_at.isoformat(),
            "decision": self.decision,
            "confirmation_phrase": f"APPROVE {self.fingerprint[:12]}",
        }


class ConsentAuthority:
    def __init__(
        self,
        audit: AuditLedger,
        *,
        broker_uid: int = 979,
        operator_uid: int = 1000,
    ) -> None:
        self.audit = audit
        self.broker_uid = broker_uid
        self.operator_uid = operator_uid
        self._challenges: dict[str, _Challenge] = {}
        self._lock = asyncio.Lock()

    async def dispatch(self, request: dict[str, Any], peer_uid: int) -> dict[str, Any]:
        action = request.get("action")
        if action == "create":
            self._require_peer(peer_uid, self.broker_uid)
            return await self._create(request)
        if action == "wait":
            self._require_peer(peer_uid, self.broker_uid)
            return await self._wait(request)
        if action == "get":
            self._require_peer(peer_uid, self.operator_uid)
            return await self._get(request)
        if action == "approve":
            self._require_peer(peer_uid, self.operator_uid)
            return await self._decide(request, "approved", peer_uid)
        if action == "deny":
            self._require_peer(peer_uid, self.operator_uid)
            return await self._decide(request, "denied", peer_uid)
        raise ValueError("unsupported consent action")

    async def _create(self, request: dict[str, Any]) -> dict[str, Any]:
        plan = BackupPlan.model_validate(request.get("plan"))
        session_id = request.get("session_id")
        if not isinstance(session_id, str) or len(session_id) < 8:
            raise ValueError("invalid session")
        challenge = _Challenge(
            id=uuid4().hex,
            plan_id=plan.id,
            fingerprint=plan.fingerprint_sha256,
            session_id=session_id,
            source=plan.source.path,
            destination=plan.destination.backup_path,
            estimated_bytes=plan.source.estimated_size_bytes,
            required_bytes=plan.required_bytes,
            available_bytes=plan.destination.available_bytes,
            file_count=plan.included_file_count,
            exclusions=tuple(
                {
                    "relative_path": item.relative_path,
                    "reason": item.reason,
                }
                for item in plan.exclusions
            ),
            expires_at=min(plan.expires_at, datetime.now(UTC) + timedelta(minutes=10)),
        )
        async with self._lock:
            self._challenges[challenge.id] = challenge
        await self.audit.append(
            event_type="backup.authorization.requested",
            source="ares-consent-agent",
            correlation_id=plan.backup_id,
            session_id=session_id,
            payload={
                "challenge_id": challenge.id,
                "plan_id": plan.id,
                "plan_fingerprint": plan.fingerprint_sha256,
                "estimated_bytes": plan.source.estimated_size_bytes,
                "required_bytes": plan.required_bytes,
                "available_bytes": plan.destination.available_bytes,
                "file_count": plan.included_file_count,
                "excluded_count": len(plan.exclusions),
                "risk": "medium",
            },
        )
        return challenge.public()

    async def _wait(self, request: dict[str, Any]) -> dict[str, Any]:
        challenge = await self._lookup(request)
        remaining = (challenge.expires_at - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            challenge.decision = "expired"
            raise ValueError("authorization expired")
        try:
            await asyncio.wait_for(challenge.event.wait(), timeout=remaining)
        except TimeoutError as exc:
            challenge.decision = "expired"
            raise ValueError("authorization expired") from exc
        return challenge.public()

    async def _get(self, request: dict[str, Any]) -> dict[str, Any]:
        challenge = await self._lookup(request)
        return challenge.public()

    async def _decide(
        self,
        request: dict[str, Any],
        decision: str,
        peer_uid: int,
    ) -> dict[str, Any]:
        challenge = await self._lookup(request)
        if challenge.decision != "pending" or challenge.expires_at <= datetime.now(UTC):
            raise ValueError("challenge is not pending")
        confirmation = request.get("confirmation")
        expected = f"APPROVE {challenge.fingerprint[:12]}"
        if decision == "approved" and confirmation != expected:
            raise ValueError("exact confirmation phrase required")
        try:
            await self.audit.append(
                event_type=(
                    "backup.authorization.approved"
                    if decision == "approved"
                    else "backup.authorization.denied"
                ),
                source="ares-consent-agent",
                correlation_id=challenge.id,
                session_id=challenge.session_id,
                payload={
                    "challenge_id": challenge.id,
                    "plan_id": challenge.plan_id,
                    "plan_fingerprint": challenge.fingerprint,
                    "operator_uid": peer_uid,
                    "decision": decision,
                },
            )
        except AuditLedgerError as exc:
            raise ValueError("audit unavailable") from exc
        challenge.decision = decision
        challenge.event.set()
        return challenge.public()

    async def _lookup(self, request: dict[str, Any]) -> _Challenge:
        challenge_id = request.get("challenge_id")
        if not isinstance(challenge_id, str):
            raise ValueError("invalid challenge")
        async with self._lock:
            challenge = self._challenges.get(challenge_id)
        if challenge is None:
            raise ValueError("challenge not found")
        return challenge

    @staticmethod
    def _require_peer(actual: int, expected: int) -> None:
        if actual != expected:
            raise PermissionError("consent peer is not authorized")


class UnixConsentClient:
    """Broker-side client for the independent consent process."""

    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path

    async def request(self, plan: BackupPlan, session_id: str) -> dict[str, Any]:
        return await _request(
            self.socket_path,
            {
                "action": "create",
                "plan": plan.model_dump(mode="json"),
                "session_id": session_id,
            },
            timeout_seconds=3.0,
        )

    async def wait(
        self, challenge_id: str, timeout_seconds: float = 600.0
    ) -> dict[str, Any]:
        return await _request(
            self.socket_path,
            {"action": "wait", "challenge_id": challenge_id},
            timeout_seconds=timeout_seconds,
        )


class UnixConsentOperatorClient:
    """Operator CLI client; it cannot create challenges, only inspect/decide them."""

    def __init__(self, socket_path: Path = Path("/run/ares/sockets/consent.sock")) -> None:
        self.socket_path = socket_path

    async def get(self, challenge_id: str) -> dict[str, Any]:
        return await _request(
            self.socket_path,
            {"action": "get", "challenge_id": challenge_id},
            timeout_seconds=3.0,
        )

    async def approve(self, challenge_id: str, confirmation: str) -> dict[str, Any]:
        return await _request(
            self.socket_path,
            {
                "action": "approve",
                "challenge_id": challenge_id,
                "confirmation": confirmation,
            },
            timeout_seconds=3.0,
        )

    async def deny(self, challenge_id: str) -> dict[str, Any]:
        return await _request(
            self.socket_path,
            {"action": "deny", "challenge_id": challenge_id},
            timeout_seconds=3.0,
        )


async def serve_consent_agent(
    *,
    socket_path: Path = Path("/run/ares/sockets/consent.sock"),
    audit_socket: Path = Path("/run/ares/sockets/audit.sock"),
) -> None:
    authority = ConsentAuthority(UnixAuditLedgerClient(audit_socket))

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=3.0)
            if not line or len(line) > _MAX_MESSAGE_BYTES:
                raise ValueError("invalid consent request")
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("invalid consent request")
            payload = await authority.dispatch(request, _peer_uid(writer))
            response = {"ok": True, "payload": payload}
        except PermissionError:
            response = {"ok": False, "code": "CONSENT_FORBIDDEN"}
        except Exception as exc:
            response = {"ok": False, "code": _safe_error(exc)}
        encoded = json.dumps(
            response, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        writer.write(encoded + b"\n")
        try:
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await _unix_server(handle, socket_path)
    async with server:
        await server.serve_forever()


async def _request(
    socket_path: Path,
    payload: dict[str, Any],
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(socket_path)),
            timeout=min(timeout_seconds, 3.0),
        )
        try:
            encoded = json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            writer.write(encoded + b"\n")
            await writer.drain()
            line = await asyncio.wait_for(
                reader.readline(), timeout=timeout_seconds
            )
        finally:
            writer.close()
            await writer.wait_closed()
    except (OSError, TimeoutError) as exc:
        raise RuntimeError("consent agent unavailable") from exc
    if not line or len(line) > _MAX_MESSAGE_BYTES:
        raise RuntimeError("invalid consent response")
    response = json.loads(line)
    if not isinstance(response, dict) or response.get("ok") is not True:
        code = response.get("code") if isinstance(response, dict) else None
        raise RuntimeError(code if isinstance(code, str) else "consent failed")
    result = response.get("payload")
    if not isinstance(result, dict):
        raise RuntimeError("invalid consent response")
    return result


def _peer_uid(writer: asyncio.StreamWriter) -> int:
    peer = writer.get_extra_info("socket")
    if peer is None or not hasattr(socket, "SO_PEERCRED"):
        return -1
    size = struct.calcsize("3i")
    credentials = peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, size)
    _, uid, _ = struct.unpack("3i", credentials)
    return uid


async def _unix_server(handler, socket_path: Path) -> asyncio.AbstractServer:
    listen_fds = int(os.environ.get("LISTEN_FDS", "0") or "0")
    listen_pid = int(os.environ.get("LISTEN_PID", "0") or "0")
    if listen_fds >= 1 and listen_pid == os.getpid():
        inherited = socket.socket(fileno=3)
        inherited.setblocking(False)
        return await asyncio.start_unix_server(handler, sock=inherited)
    await asyncio.to_thread(_prepare_socket_path, socket_path)
    server = await asyncio.start_unix_server(handler, path=str(socket_path))
    os.chmod(socket_path, 0o660)
    return server


def _prepare_socket_path(socket_path: Path) -> None:
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    with suppress(FileNotFoundError):
        socket_path.unlink()


def _safe_error(exc: Exception) -> str:
    text = str(exc)
    allowed = {
        "authorization expired": "BACKUP_AUTHORIZATION_EXPIRED",
        "challenge not found": "BACKUP_AUTHORIZATION_NOT_FOUND",
        "challenge is not pending": "BACKUP_AUTHORIZATION_NOT_PENDING",
        "exact confirmation phrase required": "BACKUP_AUTHORIZATION_CONFIRMATION_INVALID",
        "audit unavailable": "AUDIT_LEDGER_UNAVAILABLE",
    }
    return allowed.get(text, "BACKUP_AUTHORIZATION_FAILED")
