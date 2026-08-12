"""Operation-specific protection checkpoints for controlled recovery mutations."""

from __future__ import annotations

import hashlib
from pathlib import Path

from ares.protection import ProtectionCheckpoint, ProtectionCheckpointStatus
from ares.recovery.models import ConfigurationDiff, RecoveryOperation, RecoveryStrategyKind


class RecoveryCheckpointError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class RecoveryCheckpointProvider:
    """Create truthful checkpoints for supported mutable recovery resources."""

    def create(
        self,
        operation: RecoveryOperation,
        *,
        root: Path,
        session_id: str,
    ) -> ProtectionCheckpoint:
        if operation.strategy is RecoveryStrategyKind.CONFIGURATION:
            raw = operation.payload.get("configuration_diff")
            change = ConfigurationDiff.model_validate(raw)
            target = root / change.path.lstrip("/")
            if not target.is_file():
                raise RecoveryCheckpointError("RECOVERY_CONFIGURATION_TARGET_MISSING")
            current = target.read_bytes()
            digest = hashlib.sha256(current).hexdigest()
            if digest != change.current_sha256:
                raise RecoveryCheckpointError("RECOVERY_CONFIGURATION_CHANGED_SINCE_PLAN")
            return ProtectionCheckpoint(
                status=ProtectionCheckpointStatus.READY,
                provider_capability="configuration.recover",
                provider_id=operation.operation_id,
                artifact_id=f"config:{operation.operation_id}:{digest[:16]}",
                protected_resource_ids=(change.path,),
                protected_resource_fingerprints={change.path: digest},
                verification_id=f"sha256:{digest}",
                session_id=session_id,
                evidence_sha256=digest,
            )
        if operation.strategy is RecoveryStrategyKind.PACKAGE:
            status = root / "var/lib/dpkg/status"
            if not status.is_file():
                raise RecoveryCheckpointError("RECOVERY_PACKAGE_DATABASE_MISSING")
            digest = hashlib.sha256(status.read_bytes()).hexdigest()
            return ProtectionCheckpoint(
                status=ProtectionCheckpointStatus.READY,
                provider_capability="package.repair",
                provider_id=operation.operation_id,
                artifact_id=f"dpkg-status:{operation.operation_id}:{digest[:16]}",
                protected_resource_ids=("/var/lib/dpkg/status",),
                protected_resource_fingerprints={"/var/lib/dpkg/status": digest},
                verification_id=f"sha256:{digest}",
                session_id=session_id,
                evidence_sha256=digest,
            )
        if operation.strategy is RecoveryStrategyKind.INITRAMFS:
            kernel = str(operation.payload.get("kernel_version", ""))
            if not kernel:
                raise RecoveryCheckpointError("RECOVERY_KERNEL_VERSION_MISSING")
            kernel_path = root / "boot" / f"vmlinuz-{kernel}"
            if not kernel_path.is_file():
                raise RecoveryCheckpointError("RECOVERY_KERNEL_ARTIFACT_MISSING")
            digest = hashlib.sha256(kernel_path.read_bytes()).hexdigest()
            return ProtectionCheckpoint(
                status=ProtectionCheckpointStatus.READY,
                provider_capability="initramfs.rebuild",
                provider_id=operation.operation_id,
                artifact_id=f"kernel:{operation.operation_id}:{digest[:16]}",
                protected_resource_ids=(f"/boot/vmlinuz-{kernel}",),
                protected_resource_fingerprints={f"/boot/vmlinuz-{kernel}": digest},
                verification_id=f"sha256:{digest}",
                session_id=session_id,
                evidence_sha256=digest,
            )
        raise RecoveryCheckpointError("RECOVERY_CHECKPOINT_STRATEGY_UNSUPPORTED")
