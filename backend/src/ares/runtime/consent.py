"""Independent local consent authority for exact one-use mutation plans."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import struct
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from ares.audit.ledger import AuditLedger, AuditLedgerError, UnixAuditLedgerClient
from ares.backup.models import BackupPlan
from ares.filesystems.models import FilesystemRepairPlan
from ares.storage_operations.models import StorageOperationPlan

_MAX_MESSAGE_BYTES = 512_000
UnixHandler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]
_FILESYSTEM_CONFIRMATION = "I understand that this operation modifies the filesystem."


@dataclass(slots=True)
class _Challenge:
    id: str
    kind: str
    plan_id: str
    fingerprint: str
    session_id: str
    correlation_id: str
    capability_id: str
    risk: str
    confirmation_phrase: str
    public_payload: dict[str, Any]
    expires_at: datetime
    decision: str = "pending"
    operator_uid: int | None = None
    event: asyncio.Event = field(default_factory=asyncio.Event)

    def public(self) -> dict[str, Any]:
        return {
            "challenge_id": self.id,
            "kind": self.kind,
            "capability_id": self.capability_id,
            "plan_id": self.plan_id,
            "plan_fingerprint_sha256": self.fingerprint,
            "risk": self.risk,
            "expires_at": self.expires_at.isoformat(),
            "decision": self.decision,
            "operator_uid": self.operator_uid,
            "confirmation_phrase": self.confirmation_phrase,
            **self.public_payload,
        }


class ConsentAuthority:
    def __init__(
        self,
        audit: AuditLedger,
        *,
        broker_uid: int = 0,
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
            return await self._create_backup(request)
        if action == "filesystem.create":
            self._require_peer(peer_uid, self.broker_uid)
            return await self._create_filesystem(request)
        if action == "storage.create":
            self._require_peer(peer_uid, self.broker_uid)
            return await self._create_storage(request)
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

    async def _create_backup(self, request: dict[str, Any]) -> dict[str, Any]:
        plan = BackupPlan.model_validate(request.get("plan"))
        session_id = request.get("session_id")
        if not isinstance(session_id, str) or len(session_id) < 8:
            raise ValueError("invalid session")
        challenge = _Challenge(
            id=uuid4().hex,
            kind="backup",
            plan_id=plan.id,
            fingerprint=plan.fingerprint_sha256,
            session_id=session_id,
            correlation_id=plan.backup_id,
            capability_id="backup.create",
            risk="medium",
            confirmation_phrase=f"APPROVE {plan.fingerprint_sha256[:12]}",
            public_payload={
                "source": plan.source.path,
                "destination": plan.destination.backup_path,
                "estimated_bytes": plan.source.estimated_size_bytes,
                "required_bytes": plan.required_bytes,
                "available_bytes": plan.destination.available_bytes,
                "file_count": plan.included_file_count,
                "exclusions": [
                    {
                        "relative_path": item.relative_path,
                        "reason": item.reason,
                    }
                    for item in plan.exclusions
                ],
                "excluded_count": len(plan.exclusions),
                "overwrite": False,
                "verification": "sha256",
            },
            expires_at=min(plan.expires_at, datetime.now(UTC) + timedelta(minutes=10)),
        )
        await self._store_and_audit_requested(
            challenge,
            {
                "estimated_bytes": plan.source.estimated_size_bytes,
                "required_bytes": plan.required_bytes,
                "available_bytes": plan.destination.available_bytes,
                "file_count": plan.included_file_count,
                "excluded_count": len(plan.exclusions),
            },
        )
        return challenge.public()

    async def _create_filesystem(self, request: dict[str, Any]) -> dict[str, Any]:
        plan = FilesystemRepairPlan.model_validate(request.get("plan"))
        challenge = _Challenge(
            id=uuid4().hex,
            kind="filesystem_repair",
            plan_id=plan.id,
            fingerprint=plan.fingerprint_sha256,
            session_id=plan.session_id,
            correlation_id=plan.repair_id,
            capability_id=plan.capability_id,
            risk="high",
            confirmation_phrase=(
                f"{_FILESYSTEM_CONFIRMATION} APPROVE {plan.fingerprint_sha256[:12]}"
            ),
            public_payload={
                "repair_id": plan.repair_id,
                "target": plan.target.canonical_path,
                "target_fingerprint": plan.target.fingerprint_sha256,
                "major_minor": plan.target.major_minor,
                "filesystem_uuid": plan.target.filesystem_uuid,
                "partuuid": plan.target.partuuid,
                "filesystem": plan.filesystem.value,
                "mounted": plan.mount.mounted,
                "checkpoint_id": (
                    plan.protection_checkpoint.id
                    if plan.protection_checkpoint is not None
                    else None
                ),
                "repair_actions": [item.id for item in plan.repair_actions],
                "limitations": list(plan.limitations),
            },
            expires_at=min(plan.expires_at, datetime.now(UTC) + timedelta(minutes=10)),
        )
        await self._store_and_audit_requested(
            challenge,
            {
                "repair_id": plan.repair_id,
                "target_fingerprint": plan.target.fingerprint_sha256,
                "major_minor": plan.target.major_minor,
                "filesystem": plan.filesystem.value,
                "checkpoint_id": (
                    plan.protection_checkpoint.id
                    if plan.protection_checkpoint is not None
                    else None
                ),
            },
        )
        return challenge.public()

    async def _create_storage(self, request: dict[str, Any]) -> dict[str, Any]:
        plan = StorageOperationPlan.model_validate(request.get("plan"))
        checkpoint = plan.protection_checkpoint
        challenge = _Challenge(
            id=uuid4().hex,
            kind="storage_operation",
            plan_id=plan.id,
            fingerprint=plan.fingerprint_sha256,
            session_id=plan.session_id,
            correlation_id=plan.operation_id,
            capability_id=plan.capability,
            risk=plan.risk,
            confirmation_phrase=(
                "I understand that this operation modifies the partition table. "
                f"APPROVE {plan.fingerprint_sha256[:12]}"
            ),
            public_payload={
                "operation_id": plan.operation_id,
                "operation": plan.operation.value,
                "target": plan.target_disk.canonical_path,
                "target_fingerprint": plan.target_disk.fingerprint_sha256,
                "device_kind": plan.target_disk.device_kind,
                "original_layout": plan.original_layout.partition_table.fingerprint_sha256,
                "proposed_layout": plan.proposed_layout.partition_table.fingerprint_sha256,
                "data_impact": plan.data_impact.level.value,
                "boot_impact": plan.boot_impact.level.value,
                "data_loss_possible": plan.data_loss_possible,
                "affected_resources": list(plan.affected_resources),
                "checkpoint_id": checkpoint.id if checkpoint else None,
                "dry_run_script_sha256": plan.dry_run.script_sha256 if plan.dry_run else None,
                "physical_disk_writes_enabled": False,
            },
            expires_at=min(plan.expires_at, datetime.now(UTC) + timedelta(minutes=10)),
        )
        await self._store_and_audit_requested(
            challenge,
            {
                "operation_id": plan.operation_id,
                "operation": plan.operation.value,
                "target_fingerprint": plan.target_disk.fingerprint_sha256,
                "original_layout": plan.original_layout.partition_table.fingerprint_sha256,
                "proposed_layout": plan.proposed_layout.partition_table.fingerprint_sha256,
                "data_impact": plan.data_impact.level.value,
                "boot_impact": plan.boot_impact.level.value,
                "checkpoint_id": checkpoint.id if checkpoint else None,
            },
        )
        return challenge.public()

    async def _store_and_audit_requested(
        self, challenge: _Challenge, details: dict[str, Any]
    ) -> None:
        async with self._lock:
            self._challenges[challenge.id] = challenge
        await self.audit.append(
            event_type=f"{_event_prefix(challenge)}.authorization.requested",
            source="ares-consent-agent",
            correlation_id=challenge.correlation_id,
            session_id=challenge.session_id,
            payload={
                "challenge_id": challenge.id,
                "capability_id": challenge.capability_id,
                "plan_id": challenge.plan_id,
                "plan_fingerprint": challenge.fingerprint,
                "risk": challenge.risk,
                **details,
            },
        )

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
        if decision == "approved" and confirmation != challenge.confirmation_phrase:
            raise ValueError("exact confirmation phrase required")
        try:
            await self.audit.append(
                event_type=(
                    f"{_event_prefix(challenge)}.authorization.approved"
                    if decision == "approved"
                    else f"{_event_prefix(challenge)}.authorization.denied"
                ),
                source="ares-consent-agent",
                correlation_id=challenge.correlation_id,
                session_id=challenge.session_id,
                payload={
                    "challenge_id": challenge.id,
                    "capability_id": challenge.capability_id,
                    "plan_id": challenge.plan_id,
                    "plan_fingerprint": challenge.fingerprint,
                    "operator_uid": peer_uid,
                    "decision": decision,
                },
            )
        except AuditLedgerError as exc:
            raise ValueError("audit unavailable") from exc
        challenge.operator_uid = peer_uid
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

    async def request_filesystem(self, plan: FilesystemRepairPlan) -> dict[str, Any]:
        return await _request(
            self.socket_path,
            {"action": "filesystem.create", "plan": plan.model_dump(mode="json")},
            timeout_seconds=3.0,
        )

    async def request_storage(self, plan: StorageOperationPlan) -> dict[str, Any]:
        return await _request(
            self.socket_path,
            {"action": "storage.create", "plan": plan.model_dump(mode="json")},
            timeout_seconds=3.0,
        )

    async def wait(self, challenge_id: str, timeout_seconds: float = 600.0) -> dict[str, Any]:
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
        encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
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
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            writer.write(encoded + b"\n")
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout=timeout_seconds)
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
    return int(uid)


async def _unix_server(handler: UnixHandler, socket_path: Path) -> asyncio.AbstractServer:
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


def _event_prefix(challenge: _Challenge) -> str:
    if challenge.kind == "backup":
        return "backup"
    if challenge.kind == "storage_operation":
        return "storage"
    return "repair"


def _safe_error(exc: Exception) -> str:
    text = str(exc)
    allowed = {
        "authorization expired": "AUTHORIZATION_EXPIRED",
        "challenge not found": "AUTHORIZATION_NOT_FOUND",
        "challenge is not pending": "AUTHORIZATION_NOT_PENDING",
        "exact confirmation phrase required": "AUTHORIZATION_CONFIRMATION_INVALID",
        "audit unavailable": "AUDIT_LEDGER_UNAVAILABLE",
    }
    return allowed.get(text, "AUTHORIZATION_FAILED")
