"""Privileged ARES Tool Broker implementation for the initial backup mutation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import struct
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from ares.audit.ledger import AuditLedgerError, UnixAuditLedgerClient
from ares.backup.models import AuthorizationGrant, Backup, BackupManifest, BackupPlan
from ares.runtime.consent import UnixConsentClient
from ares.tools.backup import BackupFilesystemTools, BackupToolError

_MAX_REQUEST_BYTES = 4_000_000
_MAX_RESPONSE_BYTES = 128_000_000


class BackupBroker:
    """Revalidate exact plans, consume one-use grants and perform bounded backup Tools."""

    def __init__(
        self,
        tools: BackupFilesystemTools,
        audit: UnixAuditLedgerClient,
        consent: UnixConsentClient,
        *,
        allowed_client_uids: frozenset[int] = frozenset({971, 1000}),
        emergency_journal: Path = Path("/var/lib/ares/broker/reconciliation.jsonl"),
    ) -> None:
        self.tools = tools
        self.audit = audit
        self.consent = consent
        self.allowed_client_uids = allowed_client_uids
        self.emergency_journal = emergency_journal
        self._grants: dict[str, AuthorizationGrant] = {}
        self._lock = asyncio.Lock()

    async def dispatch(
        self,
        request: dict[str, Any],
        peer_uid: int,
        send,
    ) -> dict[str, Any]:
        if peer_uid not in self.allowed_client_uids:
            raise PermissionError("broker client not authorized")
        action = request.get("action")
        if action == "authorize":
            return await self._authorize(request, send)
        if action == "create":
            return await self._create(request, send)
        if action == "verify":
            return await self._verify(request)
        raise BackupToolError("BACKUP_BROKER_ACTION_REJECTED")

    async def _authorize(self, request: dict[str, Any], send) -> dict[str, Any]:
        plan = BackupPlan.model_validate(request.get("plan"))
        session_id = request.get("session_id")
        if not isinstance(session_id, str) or len(session_id) < 8:
            raise BackupToolError("BACKUP_SESSION_INVALID")
        self.tools.revalidate_plan(plan)
        await self.audit.append(
            event_type="backup.authorization.intent",
            source="ares-tool-broker",
            correlation_id=plan.backup_id,
            session_id=session_id,
            payload={
                "plan_id": plan.id,
                "plan_fingerprint": plan.fingerprint_sha256,
                "source_device": plan.source.device_id,
                "destination_device": plan.destination.device_id,
                "estimated_bytes": plan.source.estimated_size_bytes,
                "file_count": plan.included_file_count,
                "excluded_count": len(plan.exclusions),
                "risk": "medium",
            },
        )
        challenge = await self.consent.request(plan, session_id)
        challenge_id = challenge.get("challenge_id")
        if not isinstance(challenge_id, str):
            raise BackupToolError("BACKUP_AUTHORIZATION_FAILED")
        await send({"type": "authorization_requested", "challenge_id": challenge_id})
        decision = await self.consent.wait(challenge_id)
        if decision.get("decision") != "approved":
            raise BackupToolError("BACKUP_AUTHORIZATION_DENIED")
        self.tools.revalidate_plan(plan)
        grant = AuthorizationGrant(
            id=uuid4().hex,
            challenge_id=challenge_id,
            plan_id=plan.id,
            plan_fingerprint_sha256=plan.fingerprint_sha256,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        async with self._lock:
            self._grants[grant.id] = grant
        await self.audit.append(
            event_type="backup.authorization.granted",
            source="ares-tool-broker",
            correlation_id=plan.backup_id,
            session_id=session_id,
            payload={
                "grant_id_hash": _token(grant.id),
                "challenge_id": challenge_id,
                "plan_id": plan.id,
                "plan_fingerprint": plan.fingerprint_sha256,
                "one_use": True,
            },
        )
        return grant.model_dump(mode="json")

    async def _create(self, request: dict[str, Any], send) -> dict[str, Any]:
        plan = BackupPlan.model_validate(request.get("plan"))
        grant = AuthorizationGrant.model_validate(request.get("grant"))
        self.tools.revalidate_plan(plan)
        async with self._lock:
            stored = self._grants.pop(grant.id, None)
        if (
            stored is None
            or stored != grant
            or grant.expires_at <= datetime.now(UTC)
            or grant.plan_id != plan.id
            or grant.plan_fingerprint_sha256 != plan.fingerprint_sha256
        ):
            raise BackupToolError("BACKUP_AUTHORIZATION_INVALID")
        session_id = f"grant-{grant.challenge_id}"
        await self.audit.append(
            event_type="backup.execution.intent",
            source="ares-tool-broker",
            correlation_id=plan.backup_id,
            session_id=session_id,
            payload={
                "plan_id": plan.id,
                "plan_fingerprint": plan.fingerprint_sha256,
                "source_device": plan.source.device_id,
                "destination_device": plan.destination.device_id,
                "destination_kind": plan.destination.kind.value,
                "required_bytes": plan.required_bytes,
                "overwrite": False,
            },
        )

        async def progress(value) -> None:
            await send({"type": "progress", "payload": value.model_dump(mode="json")})

        async def entry(value) -> None:
            await send(
                {
                    "type": "entry",
                    "payload": value.model_dump(mode="json"),
                    "audit_path_token": _token(value.relative_path),
                }
            )

        try:
            manifest = await self.tools.create_backup(plan, progress, entry)
        except BaseException as exc:
            try:
                await self.audit.append(
                    event_type="backup.execution.failed",
                    source="ares-tool-broker",
                    correlation_id=plan.backup_id,
                    session_id=session_id,
                    payload={"plan_id": plan.id, "error_code": _safe_code(exc)},
                )
            except AuditLedgerError:
                await asyncio.to_thread(
                    self._emergency,
                    {
                        "event": "backup.execution.failed",
                        "backup_id": plan.backup_id,
                        "error_code": _safe_code(exc),
                    },
                )
            raise
        try:
            await self.audit.append(
                event_type="backup.execution.completed",
                source="ares-tool-broker",
                correlation_id=plan.backup_id,
                session_id=session_id,
                payload={
                    "plan_id": plan.id,
                    "file_count": manifest.file_count,
                    "size_bytes": manifest.total_size_bytes,
                    "manifest_checksum": manifest.manifest_checksum_sha256,
                },
            )
        except AuditLedgerError as exc:
            await asyncio.to_thread(
                self._emergency,
                {
                    "event": "backup.execution.completed",
                    "backup_id": plan.backup_id,
                    "manifest_checksum": manifest.manifest_checksum_sha256,
                    "reconciliation_required": True,
                },
            )
            raise BackupToolError("BACKUP_RECONCILIATION_REQUIRED") from exc
        return manifest.model_dump(mode="json")

    async def _verify(self, request: dict[str, Any]) -> dict[str, Any]:
        backup = Backup.model_validate(request.get("backup"))
        manifest = BackupManifest.model_validate(request.get("manifest"))
        verification = await self.tools.verify_backup(backup, manifest)
        await self.audit.append(
            event_type="backup.verification.completed",
            source="ares-tool-broker",
            correlation_id=backup.id,
            session_id=backup.session_id,
            payload={
                "backup_id": backup.id,
                "status": verification.status.value,
                "verified_file_count": verification.verified_file_count,
                "verified_size_bytes": verification.verified_size_bytes,
                "mismatch_count": len(verification.checksum_mismatches),
                "missing_count": len(verification.missing_entries),
            },
        )
        return verification.model_dump(mode="json")

    def _emergency(self, record: dict[str, Any]) -> None:
        self.emergency_journal.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        encoded = (
            json.dumps(
                {"timestamp": datetime.now(UTC).isoformat(), **record},
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        descriptor = os.open(
            self.emergency_journal,
            os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_CLOEXEC,
            0o600,
        )
        try:
            os.write(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


async def serve_tool_broker(
    *,
    socket_path: Path = Path("/run/ares/sockets/broker.sock"),
    audit_socket: Path = Path("/run/ares/sockets/audit.sock"),
    consent_socket: Path = Path("/run/ares/sockets/consent.sock"),
) -> None:
    broker = BackupBroker(
        BackupFilesystemTools(),
        UnixAuditLedgerClient(audit_socket),
        UnixConsentClient(consent_socket),
    )

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        async def send(message: dict[str, Any]) -> None:
            encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(encoded) > _MAX_RESPONSE_BYTES:
                raise BackupToolError("BACKUP_BROKER_RESPONSE_TOO_LARGE")
            writer.write(encoded + b"\n")
            await writer.drain()

        try:
            line = await asyncio.wait_for(reader.readline(), timeout=3.0)
            if not line or len(line) > _MAX_REQUEST_BYTES:
                raise BackupToolError("BACKUP_BROKER_REQUEST_INVALID")
            request = json.loads(line)
            if not isinstance(request, dict):
                raise BackupToolError("BACKUP_BROKER_REQUEST_INVALID")
            result = await broker.dispatch(request, _peer_uid(writer), send)
            await send({"type": "result", "payload": result})
        except asyncio.CancelledError:
            raise
        except PermissionError:
            await send({"type": "error", "code": "BACKUP_BROKER_FORBIDDEN"})
        except Exception as exc:
            await send({"type": "error", "code": _safe_code(exc)})
        finally:
            writer.close()
            await writer.wait_closed()

    server = await _unix_server(handle, socket_path)
    async with server:
        await server.serve_forever()


def _peer_uid(writer: asyncio.StreamWriter) -> int:
    peer = writer.get_extra_info("socket")
    if peer is None or not hasattr(socket, "SO_PEERCRED"):
        return -1
    credentials = peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _, uid, _ = struct.unpack("3i", credentials)
    return uid


async def _unix_server(handler, socket_path: Path) -> asyncio.AbstractServer:
    listen_fds = int(os.environ.get("LISTEN_FDS", "0") or "0")
    listen_pid = int(os.environ.get("LISTEN_PID", "0") or "0")
    if listen_fds >= 1 and listen_pid == os.getpid():
        inherited = socket.socket(fileno=3)
        inherited.setblocking(False)
        return await asyncio.start_unix_server(handler, sock=inherited)
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        socket_path.unlink()
    except FileNotFoundError:
        pass
    server = await asyncio.start_unix_server(handler, path=str(socket_path))
    os.chmod(socket_path, 0o660)
    return server


def _token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _safe_code(exc: BaseException) -> str:
    if isinstance(exc, BackupToolError):
        return exc.code
    if isinstance(exc, AuditLedgerError):
        return "AUDIT_LEDGER_UNAVAILABLE"
    if isinstance(exc, asyncio.CancelledError):
        return "BACKUP_CANCELLED"
    return "BACKUP_BROKER_FAILED"
