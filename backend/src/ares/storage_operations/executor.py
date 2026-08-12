"""Execution port for partition inspection, dry-run, checkpoint and broker-gated writes."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, TypeVar
from uuid import uuid4

from pydantic import BaseModel

from ares.protection import ProtectionCheckpoint, ProtectionCheckpointStatus
from ares.storage_operations.integrity import canonical_sha256
from ares.storage_operations.models import (
    StorageAuthorizationGrant,
    StorageCheckpointBundle,
    StorageDryRunResult,
    StorageLayout,
    StorageOperationOutcome,
    StorageOperationPlan,
)
from ares.tools.partition import PartitionToolError, StoragePartitionToolSuite

AuthorizationCallback = Callable[[str], Awaitable[None]]
StageCallback = Callable[[str, dict[str, object]], Awaitable[None]]
_MAX_REQUEST_BYTES = 12_000_000
_MAX_RESPONSE_BYTES = 24_000_000
ModelT = TypeVar("ModelT", bound=BaseModel)


class StorageExecutorError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class StorageOperationExecutor(Protocol):
    async def inspect(self, target_disk: str) -> StorageLayout: ...

    async def dry_run(self, plan: StorageOperationPlan) -> StorageDryRunResult: ...

    async def create_checkpoint(self, plan: StorageOperationPlan) -> StorageCheckpointBundle: ...

    async def request_authorization(
        self,
        plan: StorageOperationPlan,
        *,
        on_challenge: AuthorizationCallback,
    ) -> StorageAuthorizationGrant: ...

    async def execute(
        self,
        plan: StorageOperationPlan,
        grant: StorageAuthorizationGrant,
        *,
        on_stage: StageCallback,
    ) -> StorageOperationOutcome: ...

    async def verify(self, plan: StorageOperationPlan) -> StorageLayout: ...


class LocalTestStorageExecutor:
    """Test-only execution adapter for disk-image fixtures and controlled loop devices."""

    def __init__(self, tools: StoragePartitionToolSuite, *, authorize: bool = True) -> None:
        self.tools = tools
        self.authorize = authorize
        self._grants: dict[str, StorageAuthorizationGrant] = {}

    async def inspect(self, target_disk: str) -> StorageLayout:
        try:
            return await self.tools.inspect(target_disk)
        except PartitionToolError as exc:
            raise StorageExecutorError(exc.code) from exc

    async def dry_run(self, plan: StorageOperationPlan) -> StorageDryRunResult:
        try:
            return await self.tools.dry_run(plan)
        except PartitionToolError as exc:
            raise StorageExecutorError(exc.code) from exc

    async def create_checkpoint(self, plan: StorageOperationPlan) -> StorageCheckpointBundle:
        checkpoint_id = uuid4().hex
        try:
            artifact = await self.tools.create_checkpoint(plan, checkpoint_id)
        except PartitionToolError as exc:
            raise StorageExecutorError(exc.code) from exc
        evidence = canonical_sha256(
            {
                "checkpoint_id": checkpoint_id,
                "operation_id": plan.operation_id,
                "target_fingerprint": plan.target_disk.fingerprint_sha256,
                "partition_table_fingerprint": artifact.partition_table_fingerprint_sha256,
                "dump_sha256": artifact.dump_sha256,
            }
        )
        checkpoint = ProtectionCheckpoint(
            id=checkpoint_id,
            status=ProtectionCheckpointStatus.READY,
            protected_resources=(plan.protected_resource_id,),
            resource_fingerprints={plan.protected_resource_id: plan.target_disk.fingerprint_sha256},
            provider_capability_id="storage.partition.inspect",
            protection_kind="snapshot",
            verification_id=f"partition-table:{artifact.dump_sha256[:24]}",
            session_id=plan.session_id,
            evidence_sha256=evidence,
            limitations=(
                "Checkpoint protects partition-table metadata; it is not a filesystem/data backup.",
                (
                    "Create/delete are executable only when DataImpactAssessment allows "
                    "table-only recovery."
                ),
            ),
        )
        return StorageCheckpointBundle(checkpoint=checkpoint, artifact=artifact)

    async def request_authorization(
        self,
        plan: StorageOperationPlan,
        *,
        on_challenge: AuthorizationCallback,
    ) -> StorageAuthorizationGrant:
        challenge_id = f"test-{uuid4().hex}"
        await on_challenge(challenge_id)
        if not self.authorize:
            raise StorageExecutorError("STORAGE_AUTHORIZATION_DENIED")
        grant = StorageAuthorizationGrant(
            id=uuid4().hex,
            challenge_id=challenge_id,
            operation_id=plan.operation_id,
            plan_id=plan.id,
            session_id=plan.session_id,
            target_fingerprint_sha256=plan.target_disk.fingerprint_sha256,
            plan_fingerprint_sha256=plan.fingerprint_sha256,
            operator_uid=1000,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        self._grants[grant.id] = grant
        return grant

    async def execute(
        self,
        plan: StorageOperationPlan,
        grant: StorageAuthorizationGrant,
        *,
        on_stage: StageCallback,
    ) -> StorageOperationOutcome:
        stored = self._grants.pop(grant.id, None)
        if not _grant_matches(plan, grant, stored):
            raise StorageExecutorError("STORAGE_AUTHORIZATION_INVALID")
        try:
            return await self.tools.execute(plan, on_stage=on_stage)
        except PartitionToolError as exc:
            raise StorageExecutorError(exc.code) from exc

    async def verify(self, plan: StorageOperationPlan) -> StorageLayout:
        try:
            return await self.tools.verify_tool.verify(plan)
        except PartitionToolError as exc:
            raise StorageExecutorError(exc.code) from exc


class UnixBrokerStorageExecutor:
    """Semantic Unix-socket client; no command or argv crosses from API to broker."""

    def __init__(self, socket_path: Path, *, timeout_seconds: float = 3_600) -> None:
        self.socket_path = socket_path
        self.timeout_seconds = timeout_seconds

    async def inspect(self, target_disk: str) -> StorageLayout:
        result = await self._request(
            {"action": "storage.inspect", "target_disk": target_disk}, _ignore_message
        )
        return _validate(StorageLayout, result, "STORAGE_BROKER_RESPONSE_INVALID")

    async def dry_run(self, plan: StorageOperationPlan) -> StorageDryRunResult:
        result = await self._request(
            {"action": "storage.dry-run", "plan": plan.model_dump(mode="json")},
            _ignore_message,
        )
        return _validate(StorageDryRunResult, result, "STORAGE_BROKER_RESPONSE_INVALID")

    async def create_checkpoint(self, plan: StorageOperationPlan) -> StorageCheckpointBundle:
        result = await self._request(
            {"action": "storage.checkpoint", "plan": plan.model_dump(mode="json")},
            _ignore_message,
        )
        return _validate(StorageCheckpointBundle, result, "STORAGE_BROKER_RESPONSE_INVALID")

    async def request_authorization(
        self,
        plan: StorageOperationPlan,
        *,
        on_challenge: AuthorizationCallback,
    ) -> StorageAuthorizationGrant:
        async def handle(message: dict[str, object]) -> None:
            if message.get("type") != "authorization_requested":
                return
            challenge_id = message.get("challenge_id")
            if isinstance(challenge_id, str):
                await on_challenge(challenge_id)

        result = await self._request(
            {"action": "storage.authorize", "plan": plan.model_dump(mode="json")}, handle
        )
        return _validate(StorageAuthorizationGrant, result, "STORAGE_BROKER_RESPONSE_INVALID")

    async def execute(
        self,
        plan: StorageOperationPlan,
        grant: StorageAuthorizationGrant,
        *,
        on_stage: StageCallback,
    ) -> StorageOperationOutcome:
        async def handle(message: dict[str, object]) -> None:
            if message.get("type") != "stage":
                return
            name = message.get("name")
            payload = message.get("payload")
            if isinstance(name, str) and isinstance(payload, dict):
                await on_stage(name, payload)

        result = await self._request(
            {
                "action": "storage.execute",
                "plan": plan.model_dump(mode="json"),
                "grant": grant.model_dump(mode="json"),
            },
            handle,
        )
        return _validate(StorageOperationOutcome, result, "STORAGE_BROKER_RESPONSE_INVALID")

    async def verify(self, plan: StorageOperationPlan) -> StorageLayout:
        result = await self._request(
            {"action": "storage.verify", "plan": plan.model_dump(mode="json")},
            _ignore_message,
        )
        return _validate(StorageLayout, result, "STORAGE_BROKER_RESPONSE_INVALID")

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
            raise StorageExecutorError("STORAGE_BROKER_UNAVAILABLE") from exc
        try:
            encoded = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(encoded) > _MAX_REQUEST_BYTES:
                raise StorageExecutorError("STORAGE_BROKER_REQUEST_TOO_LARGE")
            writer.write(encoded + b"\n")
            await writer.drain()
            while True:
                try:
                    line = await asyncio.wait_for(reader.readline(), timeout=self.timeout_seconds)
                except TimeoutError as exc:
                    raise StorageExecutorError("STORAGE_BROKER_TIMEOUT") from exc
                if not line:
                    raise StorageExecutorError("STORAGE_BROKER_DISCONNECTED")
                if len(line) > _MAX_RESPONSE_BYTES:
                    raise StorageExecutorError("STORAGE_BROKER_RESPONSE_TOO_LARGE")
                try:
                    message = json.loads(line)
                except ValueError as exc:
                    raise StorageExecutorError("STORAGE_BROKER_RESPONSE_INVALID") from exc
                if not isinstance(message, dict):
                    raise StorageExecutorError("STORAGE_BROKER_RESPONSE_INVALID")
                if message.get("type") == "error":
                    code = message.get("code")
                    raise StorageExecutorError(
                        code if isinstance(code, str) else "STORAGE_BROKER_FAILED"
                    )
                if message.get("type") == "result":
                    payload = message.get("payload")
                    if not isinstance(payload, dict):
                        raise StorageExecutorError("STORAGE_BROKER_RESPONSE_INVALID")
                    return payload
                await handler(message)
        finally:
            writer.close()
            await writer.wait_closed()


def _grant_matches(
    plan: StorageOperationPlan,
    grant: StorageAuthorizationGrant,
    stored: StorageAuthorizationGrant | None,
) -> bool:
    return bool(
        stored is not None
        and stored == grant
        and grant.expires_at > datetime.now(UTC)
        and grant.operation_id == plan.operation_id
        and grant.plan_id == plan.id
        and grant.session_id == plan.session_id
        and grant.target_fingerprint_sha256 == plan.target_disk.fingerprint_sha256
        and grant.plan_fingerprint_sha256 == plan.fingerprint_sha256
    )


def _validate[ModelT: BaseModel](
    model: type[ModelT], payload: dict[str, object], code: str
) -> ModelT:
    try:
        return model.model_validate(payload)
    except ValueError as exc:
        raise StorageExecutorError(code) from exc


async def _ignore_message(message: dict[str, object]) -> None:
    del message
