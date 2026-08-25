"""Privileged broker policy for contextual terminal sessions.

This policy intentionally accepts only terminal.* actions and typed plans.  It
does not accept commands, argv, shell names, executable paths, arbitrary mount
points, flags or environment from clients.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import subprocess
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from shutil import rmtree
from typing import Any

from ares.audit import AuditLedger, AuditLedgerError
from ares.terminal.models import (
    TerminalAuthorizationGrant,
    TerminalCleanupEvidence,
    TerminalCleanupStatus,
    TerminalPlan,
    TerminalSession,
    TerminalSessionStatus,
    terminal_plan_fingerprint,
)

TerminalBrokerSend = Callable[[dict[str, Any]], Awaitable[None]]
_FORBIDDEN_REQUEST_FIELDS = {
    "command",
    "argv",
    "shell",
    "executable",
    "script",
    "device",
    "mountpoint",
    "flags",
    "environment",
}


class TerminalBrokerError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class TerminalBroker:
    def __init__(
        self,
        audit: AuditLedger,
        *,
        runtime_root: Path = Path("/run/ares/terminals"),
        gui_request_root: Path = Path("/run/ares/terminal"),
        allowed_client_uids: frozenset[int] = frozenset({971, 1000}),
    ) -> None:
        self.audit = audit
        self.runtime_root = runtime_root
        self.gui_request_root = gui_request_root
        self.allowed_client_uids = allowed_client_uids
        self._grants: dict[str, TerminalAuthorizationGrant] = {}
        self._sessions: dict[str, TerminalSession] = {}
        self._lock = asyncio.Lock()

    async def dispatch(
        self, request: dict[str, Any], peer_uid: int, send: TerminalBrokerSend
    ) -> dict[str, Any]:
        del send
        if peer_uid not in self.allowed_client_uids:
            raise PermissionError("broker client not authorized")
        if _FORBIDDEN_REQUEST_FIELDS.intersection(request):
            raise TerminalBrokerError("TERMINAL_BROKER_FORBIDDEN_FIELD")
        action = request.get("action")
        if action == "terminal.authorize":
            return await self._authorize(request)
        if action == "terminal.start":
            return await self._start(request)
        if action == "terminal.status":
            return await self._status(request)
        if action == "terminal.close":
            return await self._close(request)
        if action == "terminal.cleanup":
            return await self._cleanup(request)
        raise TerminalBrokerError("TERMINAL_BROKER_ACTION_REJECTED")

    async def _authorize(self, request: dict[str, Any]) -> dict[str, Any]:
        plan = TerminalPlan.model_validate(request.get("plan"))
        operator_uid = request.get("operator_uid")
        if not isinstance(operator_uid, int) or operator_uid < 0:
            raise TerminalBrokerError("TERMINAL_OPERATOR_UID_INVALID")
        self._validate_plan(plan)
        grant = TerminalAuthorizationGrant(
            plan_id=plan.id,
            session_id=plan.session_id,
            context_kind=plan.context_kind,
            target_fingerprint=plan.target_fingerprint,
            plan_fingerprint_sha256=plan.fingerprint_sha256,
            operator_uid=operator_uid,
            expires_at=plan.expires_at,
        )
        async with self._lock:
            self._grants[grant.id] = grant
        await self._audit(
            "terminal.authorization.granted",
            plan.session_id,
            {
                "plan_id": plan.id,
                "plan_fingerprint": plan.fingerprint_sha256,
                "target_fingerprint": plan.target_fingerprint,
                "grant_id_hash": _token(grant.id),
                "operator_uid": operator_uid,
                "one_use": True,
            },
        )
        return grant.model_dump(mode="json")

    async def _start(self, request: dict[str, Any]) -> dict[str, Any]:
        plan = TerminalPlan.model_validate(request.get("plan"))
        grant = TerminalAuthorizationGrant.model_validate(request.get("grant"))
        async with self._lock:
            stored = self._grants.pop(grant.id, None)
        if stored != grant or grant.consumed:
            raise TerminalBrokerError("TERMINAL_AUTHORIZATION_INVALID")
        if grant.expires_at <= datetime.now(UTC):
            raise TerminalBrokerError("TERMINAL_AUTHORIZATION_EXPIRED")
        if grant.plan_id != plan.id or grant.plan_fingerprint_sha256 != plan.fingerprint_sha256:
            raise TerminalBrokerError("TERMINAL_AUTHORIZATION_INVALID")
        self._validate_plan(plan)
        session_dir = await asyncio.to_thread(self._prepare_session_dir, plan)
        await self._audit(
            "terminal.session.starting",
            plan.session_id,
            {
                "plan_id": plan.id,
                "plan_fingerprint": plan.fingerprint_sha256,
                "target_fingerprint": plan.target_fingerprint,
                "runtime_dir_token": _token(str(session_dir)),
            },
        )
        session = TerminalSession(
            id=plan.session_id,
            plan_id=plan.id,
            context_kind=plan.context_kind,
            status=TerminalSessionStatus.ACTIVE,
            authorized_at=datetime.now(UTC),
            started_at=datetime.now(UTC),
            expires_at=plan.expires_at,
            cleanup_status=TerminalCleanupStatus.REQUIRED,
            technical_details={
                "runtime_dir_token": _token(str(session_dir)),
                "mode": "read-only",
            },
        )
        async with self._lock:
            self._sessions[session.id] = session
        return session.model_dump(mode="json")

    async def _status(self, request: dict[str, Any]) -> dict[str, Any]:
        session_id = _session_id(request)
        session = self._sessions.get(session_id)
        if session is None:
            raise TerminalBrokerError("TERMINAL_SESSION_NOT_FOUND")
        return session.model_dump(mode="json")

    async def _close(self, request: dict[str, Any]) -> dict[str, Any]:
        session_id = _session_id(request)
        session = self._sessions.get(session_id)
        if session is None:
            raise TerminalBrokerError("TERMINAL_SESSION_NOT_FOUND")
        closing = session.model_copy(update={"status": TerminalSessionStatus.CLOSING})
        self._sessions[session_id] = closing
        evidence = await asyncio.to_thread(self._cleanup_session_dir, session_id)
        status = (
            TerminalSessionStatus.CLOSED
            if evidence.cleanup_verified
            else TerminalSessionStatus.CLEANUP_REQUIRED
        )
        closed = closing.model_copy(
            update={
                "status": status,
                "closed_at": datetime.now(UTC),
                "cleanup_status": evidence.status,
            }
        )
        self._sessions[session_id] = closed
        await self._audit(
            "terminal.session.closed",
            session_id,
            {
                "status": closed.status.value,
                "cleanup_status": closed.cleanup_status.value,
                "cleanup_verified": evidence.cleanup_verified,
            },
        )
        return closed.model_dump(mode="json")

    async def _cleanup(self, request: dict[str, Any]) -> dict[str, Any]:
        session_id = _session_id(request)
        evidence = await asyncio.to_thread(self._cleanup_session_dir, session_id)
        return evidence.model_dump(mode="json")

    def _validate_plan(self, plan: TerminalPlan) -> None:
        if plan.fingerprint_sha256 != terminal_plan_fingerprint(plan):
            raise TerminalBrokerError("TERMINAL_PLAN_FINGERPRINT_INVALID")
        if not plan.read_only or plan.execute_target_binaries:
            raise TerminalBrokerError("TERMINAL_PLAN_POLICY_REJECTED")
        if plan.network_policy != "disabled" or plan.device_policy != "minimal":
            raise TerminalBrokerError("TERMINAL_PLAN_POLICY_REJECTED")
        if plan.target is None or not plan.target.technical_path:
            raise TerminalBrokerError("TERMINAL_TARGET_INVALID")
        if plan.target.technical_path == "/":
            raise TerminalBrokerError("TERMINAL_TARGET_UNSAFE")
        source = Path(plan.target.technical_details.get("mountpoint") or plan.target.technical_path)
        if str(source) == "/":
            raise TerminalBrokerError("TERMINAL_TARGET_UNSAFE")
        if not (str(source).startswith("/dev/") or source.is_dir()):
            raise TerminalBrokerError("TERMINAL_TARGET_SOURCE_UNSUPPORTED")
        if plan.target.fingerprint != plan.target_fingerprint:
            raise TerminalBrokerError("TERMINAL_TARGET_FINGERPRINT_CHANGED")

    def _prepare_session_dir(self, plan: TerminalPlan) -> Path:
        session_dir = self.runtime_root / plan.session_id
        session_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
        control_dir = session_dir / "control"
        control_dir.mkdir(mode=0o770, exist_ok=False)
        with suppress(Exception):
            import grp

            os.chown(control_dir, 0, grp.getgrnam("ares").gr_gid)
        source_path, mounted_by_broker = self._prepare_read_only_source(plan, session_dir)
        public = {
            "session_id": plan.session_id,
            "plan_id": plan.id,
            "context_kind": plan.context_kind.value,
            "title": f"ARES — Terminal de solo lectura — {plan.target.human_name if plan.target else 'sistema instalado'}",
            "system": plan.target.operating_system if plan.target else None,
            "target": plan.target.human_name if plan.target else None,
            "mode": "Solo lectura",
            "network": "Deshabilitada",
            "history": "disabled",
            "plan_fingerprint": plan.fingerprint_sha256,
            "source_token": _token(str(source_path)),
            "mounted_by_broker": mounted_by_broker,
        }
        (session_dir / "public.json").write_text(
            json.dumps(public, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        os.chmod(session_dir / "public.json", 0o644)
        (session_dir / "internal.json").write_text(
            json.dumps(
                {
                    "source_path": str(source_path),
                    "mounted_by_broker": mounted_by_broker,
                    "control_dir": str(control_dir),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        os.chmod(session_dir / "internal.json", 0o600)
        self._write_gui_request(plan, source_path, control_dir)
        return session_dir

    def _prepare_read_only_source(self, plan: TerminalPlan, session_dir: Path) -> tuple[Path, bool]:
        if plan.target is None or plan.target.technical_path is None:
            raise TerminalBrokerError("TERMINAL_TARGET_INVALID")
        existing_mount = plan.target.technical_details.get("mountpoint") or ""
        if existing_mount and existing_mount != "/" and Path(existing_mount).is_dir():
            return Path(existing_mount), False
        source = Path(plan.target.technical_path)
        if source.is_dir() and str(source) != "/":
            return source, False
        if not str(source).startswith("/dev/"):
            raise TerminalBrokerError("TERMINAL_TARGET_SOURCE_UNSUPPORTED")
        root = session_dir / "root"
        root.mkdir(mode=0o700, exist_ok=False)
        result = subprocess.run(
            ["/usr/bin/mount", "-o", "ro,nosuid,nodev,noexec", "--", str(source), str(root)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
        )
        if result.returncode != 0:
            raise TerminalBrokerError("TERMINAL_TARGET_READ_ONLY_MOUNT_FAILED")
        return root, True

    def _write_gui_request(
        self, plan: TerminalPlan, source_path: Path, control_dir: Path
    ) -> None:
        request_dir = self.gui_request_root
        request_dir.mkdir(mode=0o770, parents=True, exist_ok=True)
        request = {
            "request_id": plan.session_id,
            "context": plan.context_kind.value,
            "label": f"Sistema instalado: {plan.target.human_name if plan.target else 'detectado'}",
            "privilege": "solo lectura; namespace privado; sin binarios del sistema instalado",
            "plan_id": plan.id,
            "source_path": str(source_path),
            "control_dir": str(control_dir),
        }
        tmp = request_dir / f".{plan.session_id}.json.tmp"
        final = request_dir / f"{plan.session_id}.json"
        tmp.write_text(json.dumps(request, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        os.chmod(tmp, 0o660)
        tmp.replace(final)

    def _cleanup_session_dir(self, session_id: str) -> TerminalCleanupEvidence:
        session_dir = self.runtime_root / session_id
        evidence: list[str] = []
        mount_active = False
        process_active = False
        if session_dir.exists():
            control_dir = session_dir / "control"
            pid_file = control_dir / "launcher.pid"
            if pid_file.is_file():
                process_active = self._terminate_launcher(pid_file, evidence)
            root = session_dir / "root"
            if root.exists():
                mount_active = self._is_mount(root)
                if mount_active:
                    result = subprocess.run(
                        ["/usr/bin/umount", "-R", "--", str(root)],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=20,
                    )
                    if result.returncode == 0:
                        evidence.append("unmounted:root")
                    mount_active = self._is_mount(root)
            with suppress(FileNotFoundError):
                (self.gui_request_root / f"{session_id}.json").unlink()
                evidence.append("removed:gui-request")
            with suppress(OSError):
                rmtree(session_dir)
                evidence.append("removed:session-dir")
        active = session_dir.exists()
        return TerminalCleanupEvidence(
            session_id=session_id,
            status=TerminalCleanupStatus.VERIFIED if not active else TerminalCleanupStatus.REQUIRED,
            process_active=process_active,
            mount_active=mount_active,
            socket_active=False,
            cleanup_verified=not active and not process_active and not mount_active,
            evidence=tuple(evidence),
        )

    def _terminate_launcher(self, pid_file: Path, evidence: list[str]) -> bool:
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            evidence.append("launcher-pid-invalid")
            return False
        if pid <= 1:
            evidence.append("launcher-pid-rejected")
            return False
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, signal.SIGTERM)
            evidence.append("terminal-processgroup-sigterm")
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if not self._pid_alive(pid):
                return False
            time.sleep(0.1)
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, signal.SIGKILL)
            evidence.append("terminal-processgroup-sigkill")
        return self._pid_alive(pid)

    def _pid_alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _is_mount(self, path: Path) -> bool:
        return (
            subprocess.run(
                ["/usr/bin/findmnt", "-rn", "--target", str(path)],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            ).returncode
            == 0
        )

    async def _audit(self, event_type: str, session_id: str, payload: dict[str, Any]) -> None:
        try:
            await self.audit.append(
                event_type=event_type,
                source="ares-tool-broker",
                correlation_id=session_id,
                session_id=session_id,
                payload=payload,
            )
        except AuditLedgerError:
            raise


def _session_id(request: dict[str, Any]) -> str:
    session_id = request.get("session_id")
    if not isinstance(session_id, str) or len(session_id) < 8:
        raise TerminalBrokerError("TERMINAL_SESSION_INVALID")
    return session_id


def _token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
