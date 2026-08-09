"""Backup execution port and broker/local adapters."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from ares.backup.models import (
    AuthorizationGrant,
    Backup,
    BackupEntry,
    BackupManifest,
    BackupPlan,
    BackupProgress,
    BackupVerification,
)
from ares.tools.backup import BackupFilesystemTools, BackupToolError

AuthorizationCallback = Callable[[str], Awaitable[None]]
ProgressCallback = Callable[[BackupProgress], Awaitable[None]]
EntryCallback = Callable[[BackupEntry], Awaitable[None]]


class BackupExecutorError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class BackupExecutor(Protocol):
    async def request_authorization(
        self,
        plan: BackupPlan,
        *,
        session_id: str,
        on_challenge: AuthorizationCallback,
    ) -> AuthorizationGrant: ...

    async def create(
        self,
        plan: BackupPlan,
        grant: AuthorizationGrant,
        *,
        on_progress: ProgressCallback,
        on_entry: EntryCallback,
    ) -> BackupManifest: ...

    async def verify(self, backup: Backup, manifest: BackupManifest) -> BackupVerification: ...


class LocalTestBackupExecutor:
    """Deterministic test-only executor; never selected outside Environment.TEST."""

    def __init__(self, tools: BackupFilesystemTools, *, authorize: bool = True) -> None:
        self.tools = tools
        self.authorize = authorize
        self._grants: dict[str, str] = {}

    async def request_authorization(
        self,
        plan: BackupPlan,
        *,
        session_id: str,
        on_challenge: AuthorizationCallback,
    ) -> AuthorizationGrant:
        del session_id
        challenge_id = f"test-{uuid4().hex}"
        await on_challenge(challenge_id)
        if not self.authorize:
            raise BackupExecutorError("BACKUP_AUTHORIZATION_DENIED")
        grant_id = uuid4().hex
        self._grants[grant_id] = plan.fingerprint_sha256
        return AuthorizationGrant(
            id=grant_id,
            challenge_id=challenge_id,
            plan_id=plan.id,
            plan_fingerprint_sha256=plan.fingerprint_sha256,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )

    async def create(
        self,
        plan: BackupPlan,
        grant: AuthorizationGrant,
        *,
        on_progress: ProgressCallback,
        on_entry: EntryCallback,
    ) -> BackupManifest:
        fingerprint = self._grants.pop(grant.id, None)
        if fingerprint != plan.fingerprint_sha256 or grant.expires_at <= datetime.now(UTC):
            raise BackupExecutorError("BACKUP_AUTHORIZATION_INVALID")
        try:
            return await self.tools.create_backup(plan, on_progress, on_entry)
        except BackupToolError as exc:
            raise BackupExecutorError(exc.code) from exc

    async def verify(self, backup: Backup, manifest: BackupManifest) -> BackupVerification:
        return await self.tools.verify_backup(backup, manifest)


class UnixBrokerBackupExecutor:
    """Client for the privileged broker using semantic JSON-lines messages only."""

    def __init__(self, socket_path: Path, *, timeout_seconds: float = 86_400) -> None:
        self.socket_path = socket_path
        self.timeout_seconds = timeout_seconds

    async def request_authorization(
        self,
        plan: BackupPlan,
        *,
        session_id: str,
        on_challenge: AuthorizationCallback,
    ) -> AuthorizationGrant:
        async def handle(message: dict[str, object]) -> None:
            if message.get("type") == "authorization_requested":
                challenge_id = message.get("challenge_id")
                if isinstance(challenge_id, str):
                    await on_challenge(challenge_id)

        result = await self._request(
            {"action": "authorize", "plan": plan.model_dump(mode="json"), "session_id": session_id},
            handle,
        )
        try:
            return AuthorizationGrant.model_validate(result)
        except ValueError as exc:
            raise BackupExecutorError("BACKUP_BROKER_RESPONSE_INVALID") from exc

    async def create(
        self,
        plan: BackupPlan,
        grant: AuthorizationGrant,
        *,
        on_progress: ProgressCallback,
        on_entry: EntryCallback,
    ) -> BackupManifest:
        async def handle(message: dict[str, object]) -> None:
            kind = message.get("type")
            payload = message.get("payload")
            if kind == "progress" and isinstance(payload, dict):
                await on_progress(BackupProgress.model_validate(payload))
            elif kind == "entry" and isinstance(payload, dict):
                await on_entry(BackupEntry.model_validate(payload))

        result = await self._request(
            {
                "action": "create",
                "plan": plan.model_dump(mode="json"),
                "grant": grant.model_dump(mode="json"),
            },
            handle,
        )
        try:
            return BackupManifest.model_validate(result)
        except ValueError as exc:
            raise BackupExecutorError("BACKUP_BROKER_RESPONSE_INVALID") from exc

    async def verify(self, backup: Backup, manifest: BackupManifest) -> BackupVerification:
        result = await self._request(
            {
                "action": "verify",
                "backup": backup.model_dump(mode="json"),
                "manifest": manifest.model_dump(mode="json"),
            },
            _ignore_message,
        )
        try:
            return BackupVerification.model_validate(result)
        except ValueError as exc:
            raise BackupExecutorError("BACKUP_BROKER_RESPONSE_INVALID") from exc

    async def _request(
        self,
        request: dict[str, object],
        handler: Callable[[dict[str, object]], Awaitable[None]],
    ) -> dict[str, object]:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(self.socket_path)), timeout=3.0
            )
        except (OSError, TimeoutError) as exc:
            raise BackupExecutorError("BACKUP_BROKER_UNAVAILABLE") from exc
        try:
            encoded = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(encoded) > 4_000_000:
                raise BackupExecutorError("BACKUP_BROKER_REQUEST_TOO_LARGE")
            writer.write(encoded + b"\n")
            await writer.drain()
            while True:
                try:
                    line = await asyncio.wait_for(reader.readline(), timeout=self.timeout_seconds)
                except TimeoutError as exc:
                    raise BackupExecutorError("BACKUP_BROKER_TIMEOUT") from exc
                if not line:
                    raise BackupExecutorError("BACKUP_BROKER_DISCONNECTED")
                if len(line) > 4_000_000:
                    raise BackupExecutorError("BACKUP_BROKER_RESPONSE_TOO_LARGE")
                try:
                    message = json.loads(line)
                except ValueError as exc:
                    raise BackupExecutorError("BACKUP_BROKER_RESPONSE_INVALID") from exc
                if not isinstance(message, dict):
                    raise BackupExecutorError("BACKUP_BROKER_RESPONSE_INVALID")
                if message.get("type") == "error":
                    code = message.get("code")
                    raise BackupExecutorError(
                        code if isinstance(code, str) else "BACKUP_BROKER_FAILED"
                    )
                if message.get("type") == "result":
                    result = message.get("payload")
                    if not isinstance(result, dict):
                        raise BackupExecutorError("BACKUP_BROKER_RESPONSE_INVALID")
                    return result
                await handler(message)
        finally:
            writer.close()
            await writer.wait_closed()


async def _ignore_message(message: dict[str, object]) -> None:
    del message
