"""Operation-specific protection checkpoints for controlled recovery mutations."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from ares.protection import ProtectionCheckpoint, ProtectionCheckpointStatus
from ares.recovery.models import ConfigurationDiff, RecoveryOperation, RecoveryStrategyKind


class RecoveryCheckpointError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class RecoveryCheckpointProvider:
    """Copy and verify the exact mutable state protected by a recovery operation."""

    def __init__(self, artifact_root: Path) -> None:
        self.artifact_root = artifact_root
        artifact_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(artifact_root, 0o700)

    def create(
        self,
        operation: RecoveryOperation,
        *,
        root: Path,
        session_id: str,
    ) -> ProtectionCheckpoint:
        directory = self.artifact_root / operation.operation_id
        directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        if operation.strategy is RecoveryStrategyKind.CONFIGURATION:
            change = ConfigurationDiff.model_validate(operation.payload.get("configuration_diff"))
            target = root / change.path.lstrip("/")
            current, digest = self._copy_verified(target, directory / "configuration.original")
            if digest != change.current_sha256:
                raise RecoveryCheckpointError("RECOVERY_CONFIGURATION_CHANGED_SINCE_PLAN")
            resource = change.path
            capability = "configuration.recover"
            artifact = str(directory / "configuration.original")
        elif operation.strategy is RecoveryStrategyKind.PACKAGE:
            target = root / "var/lib/dpkg/status"
            current, digest = self._copy_verified(target, directory / "dpkg.status")
            resource = "/var/lib/dpkg/status"
            capability = "package.repair"
            artifact = str(directory / "dpkg.status")
        elif operation.strategy is RecoveryStrategyKind.INITRAMFS:
            kernel = str(operation.payload.get("kernel_version", ""))
            if not kernel:
                raise RecoveryCheckpointError("RECOVERY_KERNEL_VERSION_MISSING")
            target = root / "boot" / f"vmlinuz-{kernel}"
            current, digest = self._copy_verified(target, directory / f"vmlinuz-{kernel}")
            existing = root / "boot" / f"initrd.img-{kernel}"
            if existing.is_file():
                self._copy_verified(existing, directory / f"initrd.img-{kernel}")
            resource = f"/boot/vmlinuz-{kernel}"
            capability = "initramfs.rebuild"
            artifact = str(directory)
        else:
            raise RecoveryCheckpointError("RECOVERY_CHECKPOINT_STRATEGY_UNSUPPORTED")
        del current
        return ProtectionCheckpoint(
            status=ProtectionCheckpointStatus.READY,
            provider_capability=capability,
            provider_id=operation.operation_id,
            artifact_id=artifact,
            protected_resource_ids=(resource,),
            protected_resource_fingerprints={resource: digest},
            verification_id=f"sha256:{digest}",
            session_id=session_id,
            evidence_sha256=digest,
        )

    @staticmethod
    def _copy_verified(source: Path, destination: Path) -> tuple[bytes, str]:
        if source.is_symlink() or not source.is_file():
            raise RecoveryCheckpointError("RECOVERY_CHECKPOINT_SOURCE_INVALID")
        content = source.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        destination.write_bytes(content)
        os.chmod(destination, 0o600)
        if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
            raise RecoveryCheckpointError("RECOVERY_CHECKPOINT_VERIFICATION_FAILED")
        return content, digest
