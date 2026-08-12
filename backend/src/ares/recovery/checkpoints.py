"""Operation-specific protection checkpoints for controlled recovery mutations."""

from __future__ import annotations

import hashlib
import os
import shutil
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
        try:
            if operation.strategy is RecoveryStrategyKind.CONFIGURATION:
                change = ConfigurationDiff.model_validate(operation.payload.get("configuration_diff"))
                target = root / change.path.lstrip("/")
                digest = self._copy_verified(target, directory / "configuration.original")
                if digest != change.current_sha256:
                    raise RecoveryCheckpointError("RECOVERY_CONFIGURATION_CHANGED_SINCE_PLAN")
                resource = change.path
                capability = "configuration.recover"
            elif operation.strategy is RecoveryStrategyKind.PACKAGE:
                target = root / "var/lib/dpkg/status"
                digest = self._copy_verified(target, directory / "dpkg.status")
                resource = "/var/lib/dpkg/status"
                capability = "package.repair"
            elif operation.strategy is RecoveryStrategyKind.INITRAMFS:
                kernel = str(operation.payload.get("kernel_version", ""))
                if not kernel:
                    raise RecoveryCheckpointError("RECOVERY_KERNEL_VERSION_MISSING")
                target = root / "boot" / f"vmlinuz-{kernel}"
                digest = self._copy_verified(target, directory / f"vmlinuz-{kernel}")
                existing = root / "boot" / f"initrd.img-{kernel}"
                if existing.is_file():
                    self._copy_verified(existing, directory / f"initrd.img-{kernel}")
                resource = f"/boot/vmlinuz-{kernel}"
                capability = "initramfs.rebuild"
            else:
                raise RecoveryCheckpointError("RECOVERY_CHECKPOINT_STRATEGY_UNSUPPORTED")
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        return ProtectionCheckpoint(
            status=ProtectionCheckpointStatus.READY,
            protected_resources=(resource,),
            resource_fingerprints={resource: digest},
            provider_capability_id=capability,
            protection_kind="backup",
            backup_id=operation.operation_id,
            verification_id=f"sha256:{digest}",
            session_id=session_id,
            evidence_sha256=digest,
            limitations=(
                "This operation checkpoint protects only the exact mutable recovery resource, "
                "not the complete target filesystem.",
            ),
        )

    @staticmethod
    def _copy_verified(source: Path, destination: Path) -> str:
        if source.is_symlink() or not source.is_file():
            raise RecoveryCheckpointError("RECOVERY_CHECKPOINT_SOURCE_INVALID")
        content = source.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        destination.write_bytes(content)
        os.chmod(destination, 0o600)
        if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
            raise RecoveryCheckpointError("RECOVERY_CHECKPOINT_VERIFICATION_FAILED")
        return digest
