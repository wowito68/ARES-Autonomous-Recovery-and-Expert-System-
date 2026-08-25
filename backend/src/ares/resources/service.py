"""Resolve human resource selections from real storage snapshots."""

from __future__ import annotations

import hashlib
from collections import Counter
from typing import Any

from ares.resources.models import (
    MountState,
    ResourceCandidate,
    ResourceCatalog,
    ResourceKind,
    ResourceResolution,
)


class ResourceResolver:
    """Build and validate a user-facing catalog without hiding technical identity."""

    def __init__(self, snapshots: Any) -> None:
        self.snapshots = snapshots

    async def catalog(self) -> ResourceCatalog:
        snapshot = await self.snapshots.latest()
        if snapshot is None:
            return ResourceCatalog(
                snapshot_id=None,
                resources=(
                    ResourceCandidate(
                        resource_id="recovery:ares-live",
                        human_name="Entorno de recuperación ARES",
                        kind=ResourceKind.RECOVERY_ENVIRONMENT,
                        mount_state=MountState.MOUNTED,
                        health="ok",
                        technical_path="/",
                        stable_identity=_stable("recovery", "ares-live"),
                        confidence=1.0,
                        recommended=True,
                        technical_details={"scope": "live_environment"},
                    ),
                ),
                count=1,
                generated_from="recovery-environment",
                warnings=("No existe todavía un snapshot de almacenamiento; ejecuta análisis.",),
            )
        resources = self._from_snapshot(snapshot)
        return ResourceCatalog(
            snapshot_id=snapshot.id,
            resources=resources,
            count=len(resources),
            generated_from=f"storage-snapshot:{snapshot.id}",
            warnings=snapshot.warnings,
        )

    async def resolve(self, resource_id: str) -> ResourceResolution | None:
        catalog = await self.catalog()
        for resource in catalog.resources:
            if resource.resource_id == resource_id:
                return ResourceResolution(resource=resource, valid=True)
        return None

    def _from_snapshot(self, snapshot: Any) -> tuple[ResourceCandidate, ...]:
        filesystems = {item.id: item for item in snapshot.filesystems}
        mounts = {item.id: item for item in snapshot.mounts}
        smart = {item.disk_id: item for item in snapshot.smart}
        partitions = {item.id: item for item in snapshot.partitions}
        os_by_source = {item.source: item for item in snapshot.operating_systems}
        os_count_by_disk = Counter(item.disk_id for item in snapshot.operating_systems)
        resources: list[ResourceCandidate] = [
            ResourceCandidate(
                resource_id="recovery:ares-live",
                human_name="Entorno de recuperación ARES",
                kind=ResourceKind.RECOVERY_ENVIRONMENT,
                mount_state=MountState.MOUNTED,
                health="ok",
                technical_path="/",
                stable_identity=_stable("recovery", "ares-live"),
                confidence=1.0,
                recommended=not snapshot.operating_systems,
                snapshot_id=snapshot.id,
                technical_details={"scope": "live_environment"},
            )
        ]
        for disk in snapshot.disks:
            resources.append(self._disk(snapshot, disk, smart.get(disk.id), os_count_by_disk[disk.id]))
        for partition in snapshot.partitions:
            filesystem = filesystems.get(partition.filesystem_id or "")
            partition_mounts = tuple(
                mount for mount_id in partition.mount_point_ids if (mount := mounts.get(mount_id))
            )
            os_item = os_by_source.get(partition.path)
            resources.append(
                self._partition(snapshot, partition, filesystem, partition_mounts, os_item)
            )
            if filesystem is not None:
                resources.append(
                    self._filesystem(snapshot, partition, filesystem, partition_mounts, os_item)
                )
        for os_item in snapshot.operating_systems:
            partition = next((item for item in partitions.values() if item.path == os_item.source), None)
            filesystem = filesystems.get(partition.filesystem_id) if partition else None
            resources.append(self._operating_system(snapshot, os_item, partition, filesystem))
        for mount in snapshot.mounts:
            resources.append(self._mount(snapshot, mount))
        return tuple(_mark_ambiguity(resources))

    def _disk(
        self,
        snapshot: Any,
        disk: Any,
        smart: Any | None,
        os_count: int,
    ) -> ResourceCandidate:
        location = "externo" if disk.removable or disk.transport == "usb" else "interno"
        size = _format_bytes(disk.size_bytes)
        model = disk.model or disk.vendor or disk.name
        human = f"Disco {location} de {size}"
        if model:
            human = f"{human} · {model}"
        health = smart.status.value if smart else "unknown"
        return ResourceCandidate(
            resource_id=f"res:{disk.id}",
            human_name=human,
            kind=ResourceKind.DISK,
            size_bytes=disk.size_bytes,
            mount_state=MountState.UNKNOWN,
            health=health,
            technical_path=disk.path,
            stable_identity=disk.hardware_identity or _stable("disk", disk.path),
            confidence=0.92 if disk.hardware_identity else 0.78,
            recommended=os_count > 0 and not disk.removable,
            snapshot_id=snapshot.id,
            related_resource_ids=disk.partition_ids,
            technical_details={
                "path": disk.path,
                "transport": disk.transport or "unknown",
                "removable": str(disk.removable).lower(),
                "read_only": str(disk.read_only).lower(),
            },
        )

    def _partition(
        self,
        snapshot: Any,
        partition: Any,
        filesystem: Any | None,
        mounts: tuple[Any, ...],
        os_item: Any | None,
    ) -> ResourceCandidate:
        role = "Partición del sistema" if os_item else "Partición"
        if filesystem and filesystem.filesystem_type in {"vfat", "fat32"}:
            role = "Partición EFI o de arranque"
        human = f"{role} · {partition.path} · {_format_bytes(partition.size_bytes)}"
        if os_item:
            human = f"{role} de {os_item.name} · {_format_bytes(partition.size_bytes)}"
        return ResourceCandidate(
            resource_id=f"res:{partition.id}",
            human_name=human,
            kind=ResourceKind.PARTITION,
            operating_system=os_item.name if os_item else None,
            size_bytes=partition.size_bytes,
            filesystem=filesystem.filesystem_type if filesystem else None,
            mount_state=_mount_state(mounts),
            health="read_only" if partition.read_only else "unknown",
            technical_path=partition.path,
            stable_identity=_partition_identity(partition, filesystem),
            confidence=0.95 if filesystem and filesystem.uuid else 0.76,
            recommended=os_item is not None,
            snapshot_id=snapshot.id,
            parent_resource_id=partition.disk_id,
            related_resource_ids=tuple(item.id for item in mounts),
            technical_details={
                "path": partition.path,
                "filesystem_uuid": filesystem.uuid if filesystem and filesystem.uuid else "",
                "label": filesystem.label if filesystem and filesystem.label else "",
            },
        )

    def _filesystem(
        self,
        snapshot: Any,
        partition: Any,
        filesystem: Any,
        mounts: tuple[Any, ...],
        os_item: Any | None,
    ) -> ResourceCandidate:
        name = filesystem.label or filesystem.uuid or partition.path
        if os_item:
            human = f"Filesystem raíz de {os_item.name}"
        else:
            human = f"Filesystem {filesystem.filesystem_type or 'desconocido'} · {name}"
        return ResourceCandidate(
            resource_id=f"res:{filesystem.id}",
            human_name=human,
            kind=ResourceKind.FILESYSTEM,
            operating_system=os_item.name if os_item else None,
            size_bytes=partition.size_bytes,
            filesystem=filesystem.filesystem_type,
            mount_state=_mount_state(mounts),
            health="unknown",
            technical_path=filesystem.device_path,
            stable_identity=_partition_identity(partition, filesystem),
            confidence=0.96 if filesystem.uuid else 0.8,
            recommended=os_item is not None,
            snapshot_id=snapshot.id,
            parent_resource_id=partition.id,
            related_resource_ids=tuple(item.id for item in mounts),
            technical_details={
                "device_path": filesystem.device_path,
                "uuid": filesystem.uuid or "",
                "label": filesystem.label or "",
            },
        )

    def _operating_system(
        self,
        snapshot: Any,
        os_item: Any,
        partition: Any | None,
        filesystem: Any | None,
    ) -> ResourceCandidate:
        version = f" {os_item.version}" if os_item.version else ""
        human = f"Sistema Linux principal · {os_item.name}{version}"
        return ResourceCandidate(
            resource_id=f"res:{os_item.id}",
            human_name=human,
            kind=ResourceKind.OPERATING_SYSTEM,
            operating_system=os_item.name,
            size_bytes=partition.size_bytes if partition else None,
            filesystem=filesystem.filesystem_type if filesystem else None,
            mount_state=MountState.MOUNTED if os_item.mountpoint else MountState.UNKNOWN,
            health="unknown",
            technical_path=os_item.source,
            stable_identity=_stable("os", f"{os_item.source}:{os_item.name}:{os_item.version or ''}"),
            confidence=0.9 if partition else 0.7,
            recommended=True,
            snapshot_id=snapshot.id,
            parent_resource_id=os_item.disk_id,
            related_resource_ids=(partition.id,) if partition else (),
            technical_details={
                "source": os_item.source,
                "mountpoint": os_item.mountpoint,
                "os_id": os_item.os_id or "",
            },
        )

    def _mount(self, snapshot: Any, mount: Any) -> ResourceCandidate:
        human = f"Punto de montaje {mount.path}"
        if mount.path.startswith("/home/"):
            human = f"Archivos personales · {mount.path}"
        elif mount.path in {"/boot", "/boot/efi"}:
            human = f"Partición de arranque montada · {mount.path}"
        return ResourceCandidate(
            resource_id=f"res:{mount.id}",
            human_name=human,
            kind=ResourceKind.MOUNT,
            size_bytes=mount.total_bytes,
            filesystem=mount.filesystem_type,
            mount_state=MountState.MOUNTED,
            health="unknown",
            technical_path=mount.path,
            stable_identity=_stable("mount", f"{mount.source}:{mount.path}"),
            confidence=0.82,
            recommended=mount.path in {"/", "/home", "/boot/efi"},
            snapshot_id=snapshot.id,
            technical_details={
                "source": mount.source,
                "path": mount.path,
                "options": ",".join(mount.options),
            },
        )


def _mark_ambiguity(resources: list[ResourceCandidate]) -> list[ResourceCandidate]:
    by_kind_name = Counter((item.kind, item.human_name) for item in resources)
    output: list[ResourceCandidate] = []
    for item in resources:
        if by_kind_name[(item.kind, item.human_name)] > 1:
            item = item.model_copy(update={"ambiguity_reason": "human_name_not_unique"})
        output.append(item)
    os_candidates = [item for item in output if item.kind is ResourceKind.OPERATING_SYSTEM]
    if len(os_candidates) > 1:
        output = [
            item.model_copy(
                update={
                    "recommended": False,
                    "ambiguity_reason": item.ambiguity_reason or "multiple_operating_systems",
                }
            )
            if item.kind is ResourceKind.OPERATING_SYSTEM
            else item
            for item in output
        ]
    return output


def _mount_state(mounts: tuple[Any, ...]) -> MountState:
    if not mounts:
        return MountState.UNMOUNTED
    if any("rw" in mount.options for mount in mounts):
        return MountState.MOUNTED
    return MountState.MOUNTED


def _partition_identity(
    partition: Any, filesystem: Any | None
) -> str:
    if filesystem and filesystem.uuid:
        return f"fs-uuid:{filesystem.uuid}"
    return _stable("partition", f"{partition.path}:{partition.size_bytes}:{partition.name}")


def _stable(kind: str, value: str) -> str:
    return f"{kind}:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _format_bytes(value: int | None) -> str:
    if value is None:
        return "tamaño desconocido"
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value} B"
