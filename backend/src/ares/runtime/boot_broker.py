"""Privileged semantic broker for exact BootRepairPlan execution."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from ares.audit import AuditLedger, AuditLedgerError
from ares.boot.contracts import BootCheckpointBundle, BootRepairOutcome
from ares.boot.integrity import (
    boot_checkpoint_artifact_sha256,
    boot_plan_integrity_valid,
    canonical_sha256,
)
from ares.boot.models import (
    BootAuthorizationGrant,
    BootCheckpointArtifact,
    BootOperationKind,
    BootRepairPlan,
)
from ares.boot.store import BootRecoveryStore
from ares.protection import (
    ProtectionCheckpoint,
    ProtectionCheckpointStatus,
    ProtectionCheckpointStore,
)
from ares.tools.boot import BootRepairToolSuite, BootToolError, checkpoint_evidence_hash, hash_file
from ares.tools.partition import DiskIdentityTool


class BootConsentClient(Protocol):
    async def request_boot(self, plan: BootRepairPlan) -> dict[str, Any]: ...

    async def wait(self, challenge_id: str, timeout_seconds: float = 600.0) -> dict[str, Any]: ...


BrokerSend = Callable[[dict[str, Any]], Awaitable[None]]


class BootBroker:
    def __init__(
        self,
        *,
        tools: BootRepairToolSuite,
        identity: DiskIdentityTool,
        audit: AuditLedger,
        consent: BootConsentClient,
        checkpoints: ProtectionCheckpointStore,
        store: BootRecoveryStore,
        checkpoint_root: Path = Path("/var/lib/ares/boot/checkpoints"),
        allowed_client_uids: frozenset[int] = frozenset({971, 1000}),
        emergency_journal: Path = Path("/var/lib/ares/broker/boot-reconciliation.jsonl"),
    ) -> None:
        self.tools = tools
        self.identity = identity
        self.audit = audit
        self.consent = consent
        self.checkpoints = checkpoints
        self.store = store
        self.checkpoint_root = checkpoint_root
        self.allowed_client_uids = allowed_client_uids
        self.emergency_journal = emergency_journal
        self._grants: dict[str, BootAuthorizationGrant] = {}
        self._lock = asyncio.Lock()

    async def dispatch(
        self, request: dict[str, Any], peer_uid: int, send: BrokerSend
    ) -> dict[str, Any]:
        if peer_uid not in self.allowed_client_uids:
            raise PermissionError("boot broker client not authorized")
        action = request.get("action")
        if action == "boot.checkpoint":
            return await self._checkpoint(request)
        if action == "boot.authorize":
            return await self._authorize(request, send)
        if action == "boot.execute":
            return await self._execute(request, send)
        if action == "boot.verify":
            return await self._verify(request)
        raise BootToolError("BOOT_BROKER_ACTION_REJECTED")

    async def _checkpoint(self, request: dict[str, Any]) -> dict[str, Any]:
        plan = BootRepairPlan.model_validate(request.get("plan"))
        await self._validate_plan(plan, checkpoint_required=False)
        checkpoint_id = uuid4().hex
        checkpoint_dir = self.checkpoint_root / checkpoint_id
        checkpoint_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
        root = Path(plan.current_configuration.root_path or "")
        file_hashes: dict[str, str] = {}
        missing: list[str] = []
        for relative in _checkpoint_paths(plan):
            source = root / relative
            if not source.is_file() or source.is_symlink():
                missing.append(relative)
                continue
            digest = await asyncio.to_thread(hash_file, source)
            file_hashes[relative] = digest
            destination = checkpoint_dir / relative
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            data = await asyncio.to_thread(source.read_bytes)
            await asyncio.to_thread(destination.write_bytes, data)
            os.chmod(destination, 0o600)
        entries, _ = await self.tools.efi.inspect()
        artifact_draft = BootCheckpointArtifact(
            checkpoint_id=checkpoint_id,
            repair_id=plan.repair_id,
            root_path=str(root),
            target_disk_fingerprint_sha256=plan.target_disk.fingerprint_sha256,
            file_hashes=file_hashes,
            missing_paths=tuple(missing),
            efi_entries_sha256=checkpoint_evidence_hash({}, entries),
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
        await self.audit.append(
            event_type="boot.protection-checkpoint.created",
            source="ares-tool-broker",
            correlation_id=plan.repair_id,
            session_id=plan.session_id,
            payload={
                "checkpoint_id": checkpoint.id,
                "plan_fingerprint": plan.fingerprint_sha256,
                "target_disk_fingerprint": plan.target_disk.fingerprint_sha256,
                "artifact_sha256": artifact.artifact_sha256,
                "file_count": len(file_hashes),
                "missing_count": len(missing),
            },
        )
        return BootCheckpointBundle(checkpoint=checkpoint, artifact=artifact).model_dump(
            mode="json"
        )

    async def _authorize(self, request: dict[str, Any], send: BrokerSend) -> dict[str, Any]:
        plan = BootRepairPlan.model_validate(request.get("plan"))
        await self._validate_plan(plan, checkpoint_required=True)
        await self.audit.append(
            event_type="boot.authorization.intent",
            source="ares-tool-broker",
            correlation_id=plan.repair_id,
            session_id=plan.session_id,
            payload=_audit_plan(plan),
        )
        challenge = await self.consent.request_boot(plan)
        challenge_id = challenge.get("challenge_id")
        if not isinstance(challenge_id, str):
            raise BootToolError("BOOT_AUTHORIZATION_FAILED")
        await send({"type": "authorization_requested", "challenge_id": challenge_id})
        decision = await self.consent.wait(challenge_id)
        if decision.get("decision") != "approved":
            raise BootToolError("BOOT_AUTHORIZATION_DENIED")
        operator_uid = decision.get("operator_uid")
        if not isinstance(operator_uid, int) or operator_uid < 0:
            raise BootToolError("BOOT_AUTHORIZATION_INVALID")
        await self._validate_plan(plan, checkpoint_required=True)
        grant = BootAuthorizationGrant(
            challenge_id=challenge_id,
            repair_id=plan.repair_id,
            plan_id=plan.id,
            session_id=plan.session_id,
            plan_fingerprint_sha256=plan.fingerprint_sha256,
            target_disk_fingerprint_sha256=plan.target_disk.fingerprint_sha256,
            operator_uid=operator_uid,
            expires_at=min(plan.expires_at, datetime.now(UTC) + timedelta(minutes=5)),
        )
        async with self._lock:
            self._grants[grant.id] = grant
        await self.audit.append(
            event_type="boot.authorization.granted",
            source="ares-tool-broker",
            correlation_id=plan.repair_id,
            session_id=plan.session_id,
            payload={
                "grant_id_hash": canonical_sha256(grant.id)[:24],
                "challenge_id": challenge_id,
                "operator_uid": operator_uid,
                "plan_fingerprint": plan.fingerprint_sha256,
                "target_disk_fingerprint": plan.target_disk.fingerprint_sha256,
                "one_use": True,
            },
        )
        return grant.model_dump(mode="json")

    async def _execute(self, request: dict[str, Any], send: BrokerSend) -> dict[str, Any]:
        plan = BootRepairPlan.model_validate(request.get("plan"))
        grant = BootAuthorizationGrant.model_validate(request.get("grant"))
        async with self._lock:
            stored = self._grants.pop(grant.id, None)
        if stored != grant or not _grant_matches(plan, grant):
            raise BootToolError("BOOT_AUTHORIZATION_INVALID")
        await self._validate_plan(plan, checkpoint_required=True)
        await self.audit.append(
            event_type="boot.repair.intent",
            source="ares-tool-broker",
            correlation_id=plan.repair_id,
            session_id=plan.session_id,
            payload={**_audit_plan(plan), "operator_uid": grant.operator_uid},
        )
        observer_lost = False

        async def stage(name: str, payload: dict[str, object]) -> None:
            nonlocal observer_lost
            safe_payload = _sanitize(payload)
            if not observer_lost:
                try:
                    await send({"type": "stage", "name": name, "payload": safe_payload})
                except (OSError, RuntimeError):
                    observer_lost = True
            await self.audit.append(
                event_type=name,
                source="ares-tool-broker",
                correlation_id=plan.repair_id,
                session_id=plan.session_id,
                payload={
                    "target_disk_fingerprint": plan.target_disk.fingerprint_sha256,
                    **safe_payload,
                },
            )

        environment = await self.tools.environment.prepare(plan)
        await stage("boot.repair-environment.created", {})
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
                    await stage("boot.bootloader.installed", {})
                elif operation.kind is BootOperationKind.REGENERATE_GRUB_CONFIG:
                    await self.tools.grub.regenerate_config(plan, environment)
                    await stage("boot.configuration.regenerated", {})
                elif operation.kind is BootOperationKind.REGENERATE_INITRAMFS:
                    await self.tools.grub.regenerate_initramfs(plan, environment)
                    await stage("boot.initramfs.regenerated", {})
                elif operation.kind is BootOperationKind.UPDATE_EFI_ENTRY:
                    if plan.target_esp is None:
                        raise BootToolError("BOOT_ESP_REQUIRED")
                    await self.tools.entries.create_grub_entry(
                        disk_path=plan.target_disk.canonical_path,
                        partition_number=plan.target_esp.partition_number,
                        loader_path="\\EFI\\debian\\grubx64.efi",
                    )
                    await stage("boot.entry.updated", {})
                changed.append(operation.kind)
        except BaseException as exc:
            await self._audit_failure(plan, exc)
            raise
        finally:
            try:
                await self.tools.environment.cleanup(environment)
            except BootToolError as exc:
                await self._audit_failure(plan, exc)
                raise
        outcome = BootRepairOutcome(
            repair_id=plan.repair_id,
            changed_operations=tuple(changed),
            environment=environment.model_copy(update={"cleaned": True}),
        )
        try:
            await self.audit.append(
                event_type="boot.repair.execution-completed",
                source="ares-tool-broker",
                correlation_id=plan.repair_id,
                session_id=plan.session_id,
                payload={
                    "target_disk_fingerprint": plan.target_disk.fingerprint_sha256,
                    "changed_operations": [item.value for item in changed],
                    "observer_lost": observer_lost,
                },
            )
        except AuditLedgerError as exc:
            await asyncio.to_thread(
                self._emergency,
                {
                    "event": "boot.repair.execution-completed",
                    "repair_id": plan.repair_id,
                    "target_disk_fingerprint": plan.target_disk.fingerprint_sha256,
                },
            )
            raise BootToolError("BOOT_RECONCILIATION_REQUIRED") from exc
        return outcome.model_dump(mode="json")

    async def _verify(self, request: dict[str, Any]) -> dict[str, Any]:
        plan = BootRepairPlan.model_validate(request.get("plan"))
        await self._validate_plan(plan, checkpoint_required=True)
        verification = await self.tools.verification.verify(plan)
        await self.audit.append(
            event_type="boot.verification.completed",
            source="ares-tool-broker",
            correlation_id=plan.repair_id,
            session_id=plan.session_id,
            payload={
                "status": verification.status.value,
                "confidence": verification.confidence.value,
                "target_disk_fingerprint": plan.target_disk.fingerprint_sha256,
            },
        )
        return verification.model_dump(mode="json")

    async def _validate_plan(self, plan: BootRepairPlan, *, checkpoint_required: bool) -> None:
        if not boot_plan_integrity_valid(plan) or plan.expires_at <= datetime.now(UTC):
            raise BootToolError("BOOT_REPAIR_PLAN_INVALID")
        current = await asyncio.to_thread(self.identity.identify, plan.target_disk.requested_path)
        if current.fingerprint_sha256 != plan.target_disk.fingerprint_sha256:
            raise BootToolError("BOOT_DEVICE_IDENTITY_CHANGED")
        if checkpoint_required:
            checkpoint = plan.protection_checkpoint
            if (
                checkpoint is None
                or checkpoint.status is not ProtectionCheckpointStatus.READY
                or checkpoint.session_id != plan.session_id
                or plan.target_disk.resource_id not in checkpoint.protected_resources
                or checkpoint.resource_fingerprints.get(plan.target_disk.resource_id)
                != plan.target_disk.fingerprint_sha256
            ):
                raise BootToolError("BOOT_PROTECTION_CHECKPOINT_INVALID")
            durable = await self.checkpoints.get(checkpoint.id)
            artifact = await self.store.get_checkpoint(checkpoint.id)
            if durable != checkpoint or artifact is None:
                raise BootToolError("BOOT_PROTECTION_CHECKPOINT_INVALID")

    async def _audit_failure(self, plan: BootRepairPlan, exc: BaseException) -> None:
        try:
            await self.audit.append(
                event_type="boot.repair.failed",
                source="ares-tool-broker",
                correlation_id=plan.repair_id,
                session_id=plan.session_id,
                payload={
                    "target_disk_fingerprint": plan.target_disk.fingerprint_sha256,
                    "error_code": _safe_code(exc),
                },
            )
        except AuditLedgerError:
            await asyncio.to_thread(
                self._emergency,
                {
                    "event": "boot.repair.failed",
                    "repair_id": plan.repair_id,
                    "error_code": _safe_code(exc),
                },
            )

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


def _grant_matches(plan: BootRepairPlan, grant: BootAuthorizationGrant) -> bool:
    return bool(
        grant.expires_at > datetime.now(UTC)
        and grant.repair_id == plan.repair_id
        and grant.plan_id == plan.id
        and grant.session_id == plan.session_id
        and grant.plan_fingerprint_sha256 == plan.fingerprint_sha256
        and grant.target_disk_fingerprint_sha256 == plan.target_disk.fingerprint_sha256
    )


def _checkpoint_paths(plan: BootRepairPlan) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            (
                "etc/fstab",
                "etc/default/grub",
                "boot/grub/grub.cfg",
                *plan.current_configuration.kernels,
                *plan.current_configuration.initramfs,
            )
        )
    )


def _audit_plan(plan: BootRepairPlan) -> dict[str, Any]:
    return {
        "plan_id": plan.id,
        "plan_fingerprint": plan.fingerprint_sha256,
        "target_os": plan.target_os.id,
        "target_disk_fingerprint": plan.target_disk.fingerprint_sha256,
        "esp": plan.target_esp.resource_id if plan.target_esp else None,
        "bootloader": plan.bootloader.kind.value,
        "risk": plan.risk,
        "checkpoint_id": plan.protection_checkpoint.id if plan.protection_checkpoint else None,
        "operations": [item.kind.value for item in plan.operations if item.enabled],
    }


def _sanitize(payload: dict[str, object]) -> dict[str, object]:
    safe: dict[str, object] = {}
    for key in ("operation", "status", "result"):
        value = payload.get(key)
        if isinstance(value, (str, int, bool)) or value is None:
            safe[key] = value
    return safe


def _safe_code(exc: BaseException) -> str:
    if isinstance(exc, BootToolError):
        return exc.code
    if isinstance(exc, AuditLedgerError):
        return "AUDIT_LEDGER_UNAVAILABLE"
    if isinstance(exc, asyncio.CancelledError):
        return "BOOT_BROKER_CANCELLED"
    return "BOOT_BROKER_FAILED"
