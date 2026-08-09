"""Tamper-evident local audit ledger client and writer defined by ADR-0006."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import socket
import struct
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

_MAX_AUDIT_MESSAGE_BYTES = 256_000


class AuditReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: int = Field(ge=1)
    mac: str = Field(pattern=r"^[a-f0-9]{64}$")


class AuditLedger(Protocol):
    async def append(
        self,
        *,
        event_type: str,
        source: str,
        correlation_id: str,
        session_id: str,
        payload: dict[str, Any],
    ) -> AuditReceipt: ...


class AuditLedgerError(Exception):
    pass


class MemoryAuditLedger:
    """Test ledger with the same fail/ack semantics but no persistent key."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    async def append(
        self,
        *,
        event_type: str,
        source: str,
        correlation_id: str,
        session_id: str,
        payload: dict[str, Any],
    ) -> AuditReceipt:
        record = {
            "event_type": event_type,
            "source": source,
            "correlation_id": correlation_id,
            "session_id": session_id,
            "payload": payload,
        }
        self.records.append(record)
        digest = hashlib.sha256(
            json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return AuditReceipt(sequence=len(self.records), mac=digest)


class UnixAuditLedgerClient:
    """Append through the audit-writer Unix socket and require durable ACK."""

    def __init__(self, socket_path: Path, *, timeout_seconds: float = 3.0) -> None:
        self.socket_path = socket_path
        self.timeout_seconds = timeout_seconds

    async def append(
        self,
        *,
        event_type: str,
        source: str,
        correlation_id: str,
        session_id: str,
        payload: dict[str, Any],
    ) -> AuditReceipt:
        request = {
            "event_type": event_type,
            "source": source,
            "correlation_id": correlation_id,
            "session_id": session_id,
            "payload": payload,
        }
        encoded = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > _MAX_AUDIT_MESSAGE_BYTES:
            raise AuditLedgerError("audit message too large")
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(self.socket_path)), self.timeout_seconds
            )
            try:
                writer.write(encoded + b"\n")
                await writer.drain()
                line = await asyncio.wait_for(reader.readline(), self.timeout_seconds)
            finally:
                writer.close()
                await writer.wait_closed()
        except (OSError, TimeoutError) as exc:
            raise AuditLedgerError("audit writer unavailable") from exc
        if not line or len(line) > 4096:
            raise AuditLedgerError("audit writer returned an invalid acknowledgement")
        try:
            response = json.loads(line)
            if not isinstance(response, dict) or response.get("ok") is not True:
                raise ValueError
            return AuditReceipt.model_validate(response.get("receipt"))
        except (ValueError, TypeError) as exc:
            raise AuditLedgerError("audit writer returned an invalid acknowledgement") from exc


class AuditWriter:
    """Single-writer HMAC chain; ACK is emitted only after fsync/fdatasync."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.key_path = directory / "ledger.key"
        self.ledger_path = directory / "ledger.jsonl"
        self._lock = asyncio.Lock()
        self._key = b""
        self._sequence = 0
        self._previous_mac = "0" * 64

    def prepare(self) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        if self.key_path.exists():
            if self.key_path.is_symlink() or not self.key_path.is_file():
                raise OSError("audit key is unsafe")
            self._key = self.key_path.read_bytes()
        else:
            self._key = os.urandom(32)
            descriptor = os.open(
                self.key_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            try:
                os.write(descriptor, self._key)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        if len(self._key) != 32:
            raise OSError("audit key has invalid length")
        self.key_path.chmod(0o600)
        self._load_tail()

    async def append(self, request: dict[str, Any], peer_uid: int) -> AuditReceipt:
        event_type = _bounded_string(request.get("event_type"), 96)
        source = _bounded_string(request.get("source"), 96)
        correlation_id = _bounded_string(request.get("correlation_id"), 128)
        session_id = _bounded_string(request.get("session_id"), 128)
        payload = request.get("payload")
        if not all((event_type, source, correlation_id, session_id)) or not isinstance(payload, dict):
            raise ValueError("invalid audit request")
        redacted = _redact_payload(payload)
        async with self._lock:
            sequence = self._sequence + 1
            body = {
                "sequence": sequence,
                "previous_mac": self._previous_mac,
                "peer_uid": peer_uid,
                "source": source,
                "timestamp": datetime.now(UTC).isoformat(),
                "correlation_id": correlation_id,
                "session_id": session_id,
                "event_type": event_type,
                "payload": redacted,
            }
            body_bytes = json.dumps(
                body,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            mac = hmac.new(self._key, body_bytes, hashlib.sha256).hexdigest()
            record = {**body, "mac": mac}
            encoded = (
                json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                + "\n"
            ).encode("utf-8")
            await asyncio.to_thread(self._append_sync, encoded)
            self._sequence = sequence
            self._previous_mac = mac
            return AuditReceipt(sequence=sequence, mac=mac)

    def _append_sync(self, encoded: bytes) -> None:
        flags = os.O_APPEND | os.O_CLOEXEC | os.O_CREAT | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self.ledger_path, flags, 0o600)
        try:
            view = memoryview(encoded)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("audit ledger write failed")
                view = view[written:]
            if hasattr(os, "fdatasync"):
                os.fdatasync(descriptor)
            else:
                os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _load_tail(self) -> None:
        if not self.ledger_path.exists():
            return
        if self.ledger_path.is_symlink() or not self.ledger_path.is_file():
            raise OSError("audit ledger is unsafe")
        self.ledger_path.chmod(0o600)
        previous = "0" * 64
        sequence = 0
        with self.ledger_path.open("rb") as handle:
            for raw in handle:
                if len(raw) > _MAX_AUDIT_MESSAGE_BYTES * 2:
                    raise OSError("audit ledger record too large")
                record = json.loads(raw)
                if not isinstance(record, dict):
                    raise OSError("audit ledger record invalid")
                mac = record.pop("mac", None)
                if not isinstance(mac, str) or record.get("previous_mac") != previous:
                    raise OSError("audit ledger chain invalid")
                body = json.dumps(
                    record,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
                expected = hmac.new(self._key, body, hashlib.sha256).hexdigest()
                if not hmac.compare_digest(mac, expected):
                    raise OSError("audit ledger MAC invalid")
                current_sequence = record.get("sequence")
                if not isinstance(current_sequence, int) or current_sequence != sequence + 1:
                    raise OSError("audit ledger sequence invalid")
                sequence = current_sequence
                previous = mac
        self._sequence = sequence
        self._previous_mac = previous


async def serve_audit_writer(
    *,
    directory: Path = Path("/var/lib/ares/audit"),
    socket_path: Path = Path("/run/ares/sockets/audit.sock"),
) -> None:
    writer = AuditWriter(directory)
    writer.prepare()

    async def handle(reader: asyncio.StreamReader, stream: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), 3.0)
            if not line or len(line) > _MAX_AUDIT_MESSAGE_BYTES:
                raise ValueError("invalid audit message")
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("invalid audit message")
            peer_uid = _peer_uid(stream)
            receipt = await writer.append(request, peer_uid)
            response = {"ok": True, "receipt": receipt.model_dump(mode="json")}
        except Exception:
            response = {"ok": False}
        stream.write(json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n")
        try:
            await stream.drain()
        finally:
            stream.close()
            await stream.wait_closed()

    server = await _unix_server(handle, socket_path)
    async with server:
        await server.serve_forever()


def _peer_uid(stream: asyncio.StreamWriter) -> int:
    peer = stream.get_extra_info("socket")
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


def _bounded_string(value: object, maximum: int) -> str | None:
    if not isinstance(value, str) or not value or len(value) > maximum or "\x00" in value:
        return None
    return value


def _redact_payload(value: dict[str, Any]) -> dict[str, Any]:
    sensitive = {"password", "secret", "token", "key", "authorization"}
    result: dict[str, Any] = {}
    for key, item in value.items():
        if key.casefold() in sensitive:
            result[key] = "[REDACTED]"
        elif isinstance(item, str):
            result[key] = item[:1024]
        elif isinstance(item, (bool, int, float)) or item is None:
            result[key] = item
        elif isinstance(item, list):
            result[key] = item[:64]
        elif isinstance(item, dict):
            result[key] = _redact_payload(item)
    return result
