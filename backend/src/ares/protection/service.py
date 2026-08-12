"""Create exact, verified protection checkpoints from existing backup evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ares.backup.models import BackupStatus, BackupVerificationStatus
from ares.backup.store import BackupStore
from ares.protection.models import ProtectionCheckpoint, ProtectionCheckpointStatus
from ares.protection.store import ProtectionCheckpointStore


class ProtectionCheckpointError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ProtectionCheckpointService:
    """Promote verified full-filesystem backups into exact checkpoint evidence."""

    def __init__(self, backups: BackupStore, checkpoints: ProtectionCheckpointStore) -> None:
        self.backups = backups
        self.checkpoints = checkpoints

    async def from_backup(
        self,
        *,
        backup_id: str,
        resource_id: str,
        resource_fingerprint_sha256: str,
        expected_device_id: str,
        session_id: str,
    ) -> ProtectionCheckpoint:
        backup = await self.backups.get_backup(backup_id)
        verification = await self.backups.get_verification(backup_id)
        manifest = await self.backups.get_manifest(backup_id)
        if backup is None or verification is None or manifest is None:
            raise ProtectionCheckpointError("PROTECTION_BACKUP_NOT_FOUND")
        if backup.status is not BackupStatus.COMPLETED:
            raise ProtectionCheckpointError("PROTECTION_BACKUP_NOT_COMPLETED")
        if verification.status is not BackupVerificationStatus.VERIFIED:
            raise ProtectionCheckpointError("PROTECTION_BACKUP_NOT_VERIFIED")
        source = backup.source
        if source.device_id != expected_device_id:
            raise ProtectionCheckpointError("PROTECTION_BACKUP_TARGET_MISMATCH")
        if _normalized(source.path) != _normalized(source.mount_point):
            raise ProtectionCheckpointError("PROTECTION_BACKUP_NOT_FULL_FILESYSTEM")
        if manifest.source != source or manifest.backup_id != backup.id:
            raise ProtectionCheckpointError("PROTECTION_BACKUP_MANIFEST_MISMATCH")
        body = {
            "backup_id": backup.id,
            "verification_id": verification.id,
            "manifest_sha256": manifest.manifest_checksum_sha256,
            "resource_id": resource_id,
            "resource_fingerprint_sha256": resource_fingerprint_sha256,
            "device_id": expected_device_id,
        }
        evidence = hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        checkpoint = ProtectionCheckpoint(
            status=ProtectionCheckpointStatus.READY,
            protected_resources=(resource_id,),
            resource_fingerprints={resource_id: resource_fingerprint_sha256},
            provider_capability_id="backup.create",
            protection_kind="backup",
            backup_id=backup.id,
            verification_id=verification.id,
            session_id=session_id,
            evidence_sha256=evidence,
            limitations=(
                (
                    "El checkpoint es una copia a nivel de archivos, no un snapshot de "
                    "bloques ni rollback del metadata del filesystem."
                ),
                "Solo protege los datos que pudieron leerse y verificarse durante backup.create.",
            ),
        )
        await self.checkpoints.put(checkpoint)
        return checkpoint


def _normalized(value: str) -> str:
    try:
        return str(Path(value).resolve(strict=False))
    except OSError:
        return value
