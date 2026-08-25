"""Strongly typed storage snapshot contracts."""

from __future__ import annotations

import hashlib
import json
import socket
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from ares.tools.storage import StorageEvidence, ToolAvailability


class StorageHealth(StrEnum):
    """Conservative state used for disks and diagnostics."""

    OK = "ok"
    WARNING = "warning"
    ERROR = "error"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"


class FilesystemSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    device_path: str
    filesystem_type: str | None = None
    version: str | None = None
    uuid: str | None = None
    label: str | None = None


class MountPointSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    source: str
    path: str
    filesystem_type: str | None = None
    options: tuple[str, ...] = ()
    total_bytes: int | None = Field(default=None, ge=0)
    used_bytes: int | None = Field(default=None, ge=0)
    available_bytes: int | None = Field(default=None, ge=0)
    used_percent: float | None = Field(default=None, ge=0, le=100)


class SmartSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    disk_id: str
    status: StorageHealth
    passed: bool | None = None
    temperature_celsius: float | None = None
    reason: str | None = None


class PartitionSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    disk_id: str | None = None
    name: str
    path: str
    size_bytes: int = Field(ge=0)
    read_only: bool = False
    filesystem_id: str | None = None
    mount_point_ids: tuple[str, ...] = ()


class DiskSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    name: str
    path: str
    size_bytes: int = Field(ge=0)
    model: str | None = None
    vendor: str | None = None
    transport: str | None = None
    read_only: bool = False
    removable: bool = False
    hardware_identity: str | None = None
    partition_ids: tuple[str, ...] = ()
    smart_id: str | None = None


class OperatingSystemSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    name: str
    version: str | None = None
    os_id: str | None = None
    source: str
    mountpoint: str
    disk_id: str | None = None


class StorageSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    disk_count: int = Field(ge=0)
    partition_count: int = Field(ge=0)
    filesystem_count: int = Field(ge=0)
    mounted_filesystem_count: int = Field(ge=0)
    total_capacity_bytes: int = Field(ge=0)


class SystemStorageSnapshot(BaseModel):
    """Reproducible, auditable observation of storage state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: uuid4().hex)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    hostname: str = Field(default_factory=socket.gethostname)
    session_id: str
    evidence_sha256: str
    summary: StorageSummary
    disks: tuple[DiskSnapshot, ...]
    partitions: tuple[PartitionSnapshot, ...]
    filesystems: tuple[FilesystemSnapshot, ...]
    mounts: tuple[MountPointSnapshot, ...]
    operating_systems: tuple[OperatingSystemSnapshot, ...]
    smart: tuple[SmartSnapshot, ...]
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    tool_availability: tuple[ToolAvailability, ...] = ()


class GraphUpdateSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    revision: int = Field(ge=1)
    node_count: int = Field(ge=0)
    edge_count: int = Field(ge=0)


class StorageCapabilityResult(BaseModel):
    """Typed public output contract for storage.disk-analysis."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot: SystemStorageSnapshot
    knowledge_graph: GraphUpdateSummary


def build_storage_snapshot(evidence: StorageEvidence, session_id: str) -> SystemStorageSnapshot:
    """Convert low-level evidence into normalized storage entities and relations."""

    disks_by_path: dict[str, DiskSnapshot] = {}
    partitions: list[PartitionSnapshot] = []
    filesystems: list[FilesystemSnapshot] = []
    mounts: list[MountPointSnapshot] = []
    smart: list[SmartSnapshot] = []

    mount_by_path = {mount.path: mount for mount in _build_mounts(evidence)}
    mounts.extend(mount_by_path.values())

    for device in evidence.devices:
        if device.device_type != "disk":
            continue
        disk_resource_id = _resource_id("disk", device.hardware_identity or device.path)
        disks_by_path[device.path] = DiskSnapshot(
            id=disk_resource_id,
            name=device.name,
            path=device.path,
            size_bytes=device.size_bytes,
            model=device.model,
            vendor=device.vendor,
            transport=device.transport,
            read_only=device.read_only,
            removable=device.removable,
            hardware_identity=device.hardware_identity,
        )

    partition_ids_by_disk: dict[str, list[str]] = {}
    source_to_disk: dict[str, str] = {}
    for device in evidence.devices:
        if device.device_type == "disk":
            source_to_disk[device.path] = disks_by_path[device.path].id
            continue
        if device.device_type not in {"part", "lvm", "crypt", "raid"}:
            continue
        partition_id = _resource_id("partition", device.path)
        disk = disks_by_path.get(device.parent_path or "")
        disk_id = disk.id if disk is not None else None
        if disk_id is not None:
            partition_ids_by_disk.setdefault(disk_id, []).append(partition_id)
        source_to_disk[device.path] = disk_id or ""
        filesystem_id = None
        if device.filesystem_type or device.filesystem_uuid or device.filesystem_label:
            filesystem_id = _resource_id("filesystem", device.path)
            filesystems.append(
                FilesystemSnapshot(
                    id=filesystem_id,
                    device_path=device.path,
                    filesystem_type=device.filesystem_type,
                    version=device.filesystem_version,
                    uuid=device.filesystem_uuid,
                    label=device.filesystem_label,
                )
            )
        mount_ids = tuple(
            mount_by_path[path].id for path in device.mountpoints if path in mount_by_path
        )
        partitions.append(
            PartitionSnapshot(
                id=partition_id,
                disk_id=disk_id,
                name=device.name,
                path=device.path,
                size_bytes=device.size_bytes,
                read_only=device.read_only,
                filesystem_id=filesystem_id,
                mount_point_ids=mount_ids,
            )
        )

    smart_id_by_disk: dict[str, str] = {}
    for item in evidence.smart:
        disk = disks_by_path.get(item.device)
        if disk is None:
            continue
        health = (
            StorageHealth.OK
            if item.passed is True
            else StorageHealth.ERROR
            if item.passed is False
            else StorageHealth.UNAVAILABLE
            if item.status == "unavailable"
            else StorageHealth.UNKNOWN
        )
        smart_id = _resource_id("smart", disk.id)
        smart_id_by_disk[disk.id] = smart_id
        smart.append(
            SmartSnapshot(
                id=smart_id,
                disk_id=disk.id,
                status=health,
                passed=item.passed,
                temperature_celsius=item.temperature_celsius,
                reason=item.reason,
            )
        )

    normalized_disks = tuple(
        disk.model_copy(
            update={
                "partition_ids": tuple(partition_ids_by_disk.get(disk.id, ())),
                "smart_id": smart_id_by_disk.get(disk.id),
            }
        )
        for disk in disks_by_path.values()
    )
    operating_systems = tuple(
        OperatingSystemSnapshot(
            id=_resource_id("os", f"{item.source}:{item.name}"),
            name=item.name,
            version=item.version,
            os_id=item.os_id,
            source=item.source,
            mountpoint=item.mountpoint,
            disk_id=source_to_disk.get(item.source) or None,
        )
        for item in evidence.operating_systems
    )
    canonical = evidence.model_dump(mode="json")
    evidence_hash = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return SystemStorageSnapshot(
        session_id=session_id,
        evidence_sha256=evidence_hash,
        summary=StorageSummary(
            disk_count=len(normalized_disks),
            partition_count=len(partitions),
            filesystem_count=len(filesystems),
            mounted_filesystem_count=len(mounts),
            total_capacity_bytes=sum(item.size_bytes for item in normalized_disks),
        ),
        disks=normalized_disks,
        partitions=tuple(partitions),
        filesystems=tuple(filesystems),
        mounts=tuple(mounts),
        operating_systems=operating_systems,
        smart=tuple(smart),
        warnings=evidence.warnings,
        errors=evidence.errors,
        tool_availability=evidence.tool_availability,
    )


def _build_mounts(evidence: StorageEvidence) -> tuple[MountPointSnapshot, ...]:
    usage_by_target = {item.target: item for item in evidence.usage}
    output: list[MountPointSnapshot] = []
    for item in evidence.mounts:
        usage = usage_by_target.get(item.target)
        output.append(
            MountPointSnapshot(
                id=_resource_id("mount", item.target),
                source=item.source,
                path=item.target,
                filesystem_type=item.filesystem_type,
                options=item.options,
                total_bytes=usage.total_bytes if usage else None,
                used_bytes=usage.used_bytes if usage else None,
                available_bytes=usage.available_bytes if usage else None,
                used_percent=usage.used_percent if usage else None,
            )
        )
    return tuple(output)


def _resource_id(kind: str, identity: str) -> str:
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return f"{kind}:{digest}"
