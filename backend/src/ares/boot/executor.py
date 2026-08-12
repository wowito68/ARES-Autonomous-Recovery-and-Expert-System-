"""Boot repair executor protocol and TEST/Unix broker adapters."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from ares.boot.contracts import BootCheckpointBundle, BootRepairOutcome
from ares.boot.integrity import boot_checkpoint_artifact_sha256, canonical_sha256
from ares.boot.models import (
    BootAuthorizationGrant,
    BootCheckpointArtifact,
    BootOperationKind,
    BootRepairPlan,
    BootVerification,
)
from ares.protection import ProtectionCheckpoint, ProtectionCheckpointStatus
from ares.tools.boot import BootRepairToolSuite, BootToolError, checkpoint_evidence_hash, hash_file

_MAX_REQUEST = 2 * 1024 * 1024
_MAX_RESPONSE = 8 * 1024 * 1024
ChallengeCallback = Callable[[str], Awaitable[None]]
StageCallback = Callable[[str, dict[str, object]], Awaitable[None]]


class BootExecutorError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class BootRepairExecutor(Protocol):
    async def create_checkpoint(self, plan: BootRepairPlan) -> BootCheckpointBundle: ...

    async def request_authorization(
        self, plan: BootRepairPlan, *, on_challenge: ChallengeCallback
    ) -> BootAuthorizationGrant: ...

    async def execute(
        self,
        plan: BootRepairPlan,
        grant: BootAuthorizationGrant,
        *,
        on_stage: StageCallback,
    ) -> BootRepairOutcome: ...

    async def verify(self, plan: BootRepairPlan) -> BootVerification: ...


class LocalTestBootExecutor:
    """TEST-only executor. Production composition never selects this adapter."""

    def __init__(self, tools: BootRepairToolSuite, checkpoint_root: Path) -> None:
        self.tools = tools
        self.checkpoint_root = checkpoint_root
        self._grants: dict[str, BootAuthorizationGrant] = {}

    async def create_checkpoint(self, plan: BootRepairPlan) -> BootCheckpointBundle:
        try:
            entries, efi_entries = await _checkpoint_state(plan, self.tools, self.checkpoint_root)
        except BootToolError as exc:
            raise BootExecutorError(exc.code) from exc
        checkpoint_id = uuid4().hex
        artifact_draft = BootCheckpointArtifact(
            checkpoint_id=checkpoint_id,
            repair_id=plan.repair_id,
            root_path=plan.current_configuration.root_path or "",
            target_disk_fingerprint_sha256=plan.target_disk.fingerprint_sha256,
            file_hashes=entries,
            missing_paths=tuple(path for path in _checkpoint_paths(plan) if path not in entries),
            efi_entries_sha256=checkpoint_evidence_hash({}, efi_entries),
            artifact_sha256="0" * 64,
        )
        artifact = artifact_draft.model_copy(
            update={"artifact_sha256": boot_checkpoint_artifact_sha256(artifact_draft)}
        )
        checkpoint = ProtectionCheckpoint(
            id=checkpoint_id,
            status=ProtectionCheckpointStatus.READY,
            protected_resources=(plan.target_disk.resource_id,),
            resource_fingerprints={
                plan.target_disk.resource_id: plan.target_disk.fingerprint_sha256
            },
            provider_capability_id="boot.diagnose",
            protection_kind="snapshot",
            verification_id=f"boot-state:{artifact.artifact_sha256[:24]}",
            session_id=plan.session_id,
            evidence_sha256=canonical_sha256(
                {
                    "artifact": artifact.artifact_sha256,
                    "files": artifact.file_hashes,
                    "efi": artifact.efi_entries_sha256,
                }
            ),
            limitations=(
                "Boot checkpoint protects selected EFI/GRUB/system boot state only.",
                "It is not a full filesystem or user-data backup.",
            ),
        )
        return BootCheckpointBundle(checkpoint=checkpoint, artifact=artifact)

    async def request_authorization(
        self, plan: BootRepairPlan, *, on_challenge: ChallengeCallback
    ) -> BootAuthorizationGrant:
        challenge_id = f"test-boot-{uuid4().hex}"
        await on_challenge(challenge_id)
        grant = BootAuthorizationGrant(
            challenge_id=challenge_id,
            repair_id=plan.repair_id,
            plan_id=plan.id,
            session_id=plan.session_id,
            plan_fingerprint_sha256=plan.fingerprint_sha256,
            target_disk_fingerprint_sha256=plan.target_disk.fingerprint_sha256,
            operator_uid=1000,
            expires_at=min(plan.expires_at, datetime.now(UTC) + timedelta(minutes=5)),
        )
        self._grants[grant.id] = grant
        return grant

    async def execute(
        self,
        plan: BootRepairPlan,
        grant: BootAuthorizationGrant,
        *,
        on_stage: StageCallback,
    ) -> BootRepairOutcome:
        stored = self._grants.pop(grant.id, None)
        if stored != grant or not _grant_matches(plan, grant):
            raise BootExecutorError("BOOT_AUTHORIZATION_INVALID")
        try:
            environment = await self.tools.environment.prepare(plan)
            await on_stage("boot.repair-environment.created", {})
            changed: list[BootOperationKind] = []
            try:
                for operation in plan.operations:
                    if not operation.enabled or operation.kind in {
                        BootOperationKind.CREATE_CHECKPOINT,
                        BootOperationKind.PREPARE_REPAIR_ENVIRONMENT,
                        BootOperationKind.VERIFY_BOOT_CHAIN,
                        BootOperationKind.CLEANUP_REPAIR_ENVIRONMENT,
                    }:
                        continue
                    if operation.kind is BootOperationKind.INSTALL_GRUB:
                        await self.tools.grub.install(plan, environment)
                        await on_stage("boot.bootloader.installed", {})
                    elif operation.kind is BootOperationKind.REGENERATE_GRUB_CONFIG:
                        await self.tools.grub.regenerate_config(plan, environment)
                        await on_stage("boot.configuration.regenerated", {})
                    elif operation.kind is BootOperationKind.REGENERATE_INITRAMFS:
                        await self.tools.grub.regenerate_initramfs(plan, environment)
                        await on_stage("boot.initramfs.regenerated", {})
                    elif operation.kind is BootOperationKind.UPDATE_EFI_ENTRY:
                        if plan.target_esp is None:
                            raise BootToolError("BOOT_ESP_REQUIRED")
                        await self.tools.entries.create_grub_entry(
                            disk_path=plan.target_disk.canonical_path,
                            partition_number=plan.target_esp.partition_number,
                            loader_path="\\EFI\\debian\\grubx64.efi",
                        )
                        await on_stage("boot.entry.updated", {})
                    changed.append(operation.kind)
                return BootRepairOutcome(
                    repair_id=plan.repair_id,
                    changed_operations=tuple(changed),
                    environment=environment,
                )
            finally:
                await self.tools.environment.cleanup(environment)
        except BootToolError as exc:
            raise BootExecutorError(exc.code) from exc

    async def verify(self, plan: BootRepairPlan) -> BootVerification:
        try:
            return await self.tools.verification.verify(plan)
        except BootToolError as exc:
            raise BootExecutorError(exc.code) from exc


class UnixBrokerBootExecutor:
    def __init__(self, socket_path: Path, *, timeout_seconds: float = 600.0) -> None:
        self.socket_path = socket_path
        self.timeout_seconds = timeout_seconds

    async def create_checkpoint(self, plan: BootRepairPlan) -> BootCheckpointBundle:
        payload = await self._request(
            {"action": "boot.checkpoint", "plan": plan.model_dump(mode="json")}, _ignore
        )
        return BootCheckpointBundle.model_validate(payload)

    async def request_authorization(
        self, plan: BootRepairPlan, *, on_challenge: ChallengeCallback
    ) -> BootAuthorizationGrant:
        payload = await self._request(
            {"action": "boot.authorize", "plan": plan.model_dump(mode="json")},
            _challenge_adapter(on_challenge),
        )
        return BootAuthorizationGrant.model_validate(payload)

    async def execute(
        self,
        plan: BootRepairPlan,
        grant: BootAuthorizationGrant,
        *,
        on_stage: StageCallback,
    ) -> BootRepairOutcome:
        payload = await self._request(
            {
                "action": "boot.execute",
                "plan": plan.model_dump(mode="json"),
                "grant": grant.model_dump(mode="json"),
            },
            on_stage,
        )
        return BootRepairOutcome.model_validate(payload)

    async def verify(self, plan: BootRepairPlan) -> BootVerification:
        payload = await self._request(
            {"action": "boot.verify", "plan": plan.model_dump(mode="json")}, _ignore
        )
        return BootVerification.model_validate(payload)

    async def _request(self, request: dict[str, Any], on_stage: StageCallback) -> dict[str, Any]:
        encoded = (json.dumps(request, separators=(",", ":")) + "\n").encode("utf-8")
        if len(encoded) > _MAX_REQUEST:
            raise BootExecutorError("BOOT_BROKER_REQUEST_TOO_LARGE")
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(self.socket_path)),
                timeout=min(self.timeout_seconds, 10.0),
            )
        except (OSError, TimeoutError) as exc:
            raise BootExecutorError("BOOT_BROKER_UNAVAILABLE") from exc
        try:
            writer.write(encoded)
            await writer.drain()
            while True:
                try:
                    line = await asyncio.wait_for(reader.readline(), timeout=self.timeout_seconds)
                except TimeoutError as exc:
                    raise BootExecutorError("BOOT_BROKER_TIMEOUT") from exc
                if not line:
                    raise BootExecutorError("BOOT_BROKER_DISCONNECTED")
                if len(line) > _MAX_RESPONSE:
                    raise BootExecutorError("BOOT_BROKER_RESPONSE_TOO_LARGE")
                try:
                    message = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise BootExecutorError("BOOT_BROKER_RESPONSE_INVALID") from exc
                if not isinstance(message, dict):
                    raise BootExecutorError("BOOT_BROKER_RESPONSE_INVALID")
                kind = message.get("type")
                if kind == "authorization_requested":
                    challenge_id = message.get("challenge_id")
                    if not isinstance(challenge_id, str):
                        raise BootExecutorError("BOOT_BROKER_RESPONSE_INVALID")
                    await on_stage("authorization_requested", {"challenge_id": challenge_id})
                    continue
                if kind == "stage":
                    name = message.get("name")
                    payload = message.get("payload", {})
                    if not isinstance(name, str) or not isinstance(payload, dict):
                        raise BootExecutorError("BOOT_BROKER_RESPONSE_INVALID")
                    await on_stage(name, payload)
                    continue
                if kind == "error":
                    code = message.get("code")
                    raise BootExecutorError(code if isinstance(code, str) else "BOOT_BROKER_FAILED")
                result_payload = message.get("payload")
                if kind != "result" or not isinstance(result_payload, dict):
                    raise BootExecutorError("BOOT_BROKER_RESPONSE_INVALID")
                if not all(isinstance(key, str) for key in result_payload):
                    raise BootExecutorError("BOOT_BROKER_RESPONSE_INVALID")
                return {str(key): value for key, value in result_payload.items()}
        finally:
            writer.close()
            await writer.wait_closed()


def _grant_matches(plan: BootRepairPlan, grant: BootAuthorizationGrant) -> bool:
    return bool(
        grant.expires_at > datetime.now(UTC)
        and grant.repair_id == plan.repair_id
        and grant.plan_id == plan.id
        and grant.session_id == plan.session_id
        and grant.plan_fingerprint_sha256 == plan.fingerprint_sha256
        and grant.target_disk_fingerprint_sha256 == plan.target_disk.fingerprint_sha256
    )


async def _checkpoint_state(
    plan: BootRepairPlan,
    tools: BootRepairToolSuite,
    root: Path,
) -> tuple[dict[str, str], tuple[Any, ...]]:
    checkpoint_dir = root / plan.repair_id
    checkpoint_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    source_root = Path(plan.current_configuration.root_path or "")
    hashes: dict[str, str] = {}
    for relative in _checkpoint_paths(plan):
        source = source_root / relative
        if not source.is_file() or source.is_symlink():
            continue
        digest = hash_file(source)
        hashes[relative] = digest
        destination = checkpoint_dir / relative
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
        os.chmod(destination, 0o600)
    efi_entries, _ = await tools.efi.inspect()
    return hashes, efi_entries


def _checkpoint_paths(plan: BootRepairPlan) -> tuple[str, ...]:
    values = ["etc/fstab", "etc/default/grub", "boot/grub/grub.cfg"]
    values.extend(plan.current_configuration.kernels)
    values.extend(plan.current_configuration.initramfs)
    return tuple(dict.fromkeys(values))


def _challenge_adapter(callback: ChallengeCallback) -> StageCallback:
    async def stage(name: str, payload: dict[str, object]) -> None:
        if name != "authorization_requested":
            return
        challenge_id = payload.get("challenge_id")
        if isinstance(challenge_id, str):
            await callback(challenge_id)

    return stage


async def _ignore(name: str, payload: dict[str, object]) -> None:
    del name, payload
