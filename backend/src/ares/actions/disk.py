"""Private, read-only actions for the Storage / Disk Analysis capability."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from ares.actions.base import ActionContext, ActionError
from ares.events import AresEvent
from ares.knowledge import GraphEdge, GraphKind, GraphNode

_MAX_INVENTORY_BYTES = 4_000_000
_DEVICE_NAME = re.compile(r"^[A-Za-z0-9_.:+-]{1,96}$")
_ALLOWED_DEVICE_TYPES = {"disk", "part", "rom", "loop", "lvm", "crypt", "raid"}


class ReadDiskInventoryAction:
    """Read only the root-owned, redacted hardware inventory."""

    id = "storage.read-hardware-inventory"
    idempotent = True

    def __init__(self, inventory_path: Path) -> None:
        self.inventory_path = inventory_path

    async def run(self, inputs: dict[str, Any], context: ActionContext) -> dict[str, Any]:
        del inputs, context
        return await asyncio.to_thread(self._read)

    async def compensate(self, output: dict[str, Any], context: ActionContext) -> None:
        del output, context

    def _read(self) -> dict[str, Any]:
        try:
            stat = self.inventory_path.lstat()
            if not self.inventory_path.is_file() or self.inventory_path.is_symlink():
                raise ActionError("DISK_INVENTORY_UNAVAILABLE")
            if stat.st_size > _MAX_INVENTORY_BYTES:
                raise ActionError("DISK_INVENTORY_TOO_LARGE")
            payload = json.loads(self.inventory_path.read_text(encoding="utf-8"))
        except ActionError:
            raise
        except (OSError, ValueError) as exc:
            raise ActionError("DISK_INVENTORY_UNAVAILABLE") from exc
        if not isinstance(payload, dict):
            raise ActionError("DISK_INVENTORY_INVALID")
        probes = payload.get("probes")
        storage = probes.get("storage") if isinstance(probes, dict) else None
        data = storage.get("data") if isinstance(storage, dict) else None
        raw_devices = data.get("blockdevices") if isinstance(data, dict) else None
        if not isinstance(raw_devices, list):
            raise ActionError("DISK_INVENTORY_INVALID")
        devices: list[dict[str, Any]] = []
        for item in raw_devices[:256]:
            self._append_device(item, devices, parent=None)
        generation = payload.get("generation")
        return {
            "source": "hardware.public.inventory-v1",
            "generation": generation if isinstance(generation, int) and generation > 0 else 0,
            "inventory_status": (
                storage.get("status", "unknown") if isinstance(storage, dict) else "unknown"
            ),
            "smart_status": _nested_string(payload, "smart_health", "status"),
            "devices": devices,
        }

    def _append_device(
        self,
        raw: object,
        output: list[dict[str, Any]],
        *,
        parent: str | None,
    ) -> None:
        if not isinstance(raw, dict) or len(output) >= 512:
            return
        name = raw.get("name")
        device_type = raw.get("type")
        if not isinstance(name, str) or _DEVICE_NAME.fullmatch(name) is None:
            return
        if not isinstance(device_type, str) or device_type not in _ALLOWED_DEVICE_TYPES:
            device_type = "disk"
        size = raw.get("size")
        size_bytes = (
            size if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else 0
        )
        read_only = _as_bool(raw.get("ro"))
        removable = _as_bool(raw.get("rm"))
        mountpoints = raw.get("mountpoints")
        safe_mountpoints = (
            [value[:256] for value in mountpoints if isinstance(value, str)][:32]
            if isinstance(mountpoints, list)
            else []
        )
        output.append(
            {
                "id": f"disk:{name.lower()}",
                "name": name,
                "path": f"/dev/{name}",
                "type": device_type,
                "size_bytes": size_bytes,
                "read_only": read_only,
                "removable": removable,
                "transport": _safe_optional_string(raw.get("tran"), 32),
                "model": _safe_optional_string(raw.get("model"), 128),
                "vendor": _safe_optional_string(raw.get("vendor"), 64),
                "mountpoints": safe_mountpoints,
                "parent": parent,
            }
        )
        children = raw.get("children")
        if isinstance(children, list):
            for child in children[:128]:
                self._append_device(child, output, parent=f"disk:{name.lower()}")


class AnalyzeDiskInventoryAction:
    """Derive conservative observations without opening a block device."""

    id = "storage.analyze-disk-inventory"
    idempotent = True

    async def run(self, inputs: dict[str, Any], context: ActionContext) -> dict[str, Any]:
        del context
        devices = inputs.get("devices")
        if not isinstance(devices, list):
            raise ActionError("DISK_ANALYSIS_INPUT_INVALID")
        disks = [
            device
            for device in devices
            if isinstance(device, dict) and device.get("type") == "disk"
        ]
        fixed = [device for device in disks if not device.get("removable")]
        total_bytes = sum(
            size
            for device in disks
            if isinstance((size := device.get("size_bytes")), int) and not isinstance(size, bool)
        )
        findings: list[dict[str, str]] = []
        if not fixed:
            findings.append(
                {
                    "code": "NO_FIXED_DISK_OBSERVED",
                    "severity": "warning",
                    "message": "No se observó un disco fijo en el inventario actual.",
                }
            )
        if any(device.get("read_only") is True for device in disks):
            findings.append(
                {
                    "code": "READ_ONLY_DISK_OBSERVED",
                    "severity": "info",
                    "message": "Al menos un disco se presentó como solo lectura.",
                }
            )
        if inputs.get("inventory_status") != "ok":
            findings.append(
                {
                    "code": "INVENTORY_PARTIAL",
                    "severity": "warning",
                    "message": "El inventario de almacenamiento está incompleto.",
                }
            )
        if inputs.get("smart_status") != "ok":
            findings.append(
                {
                    "code": "SMART_NOT_EVALUATED",
                    "severity": "info",
                    "message": "La salud SMART no fue evaluada por esta capability pasiva.",
                }
            )
        return {
            "summary": {
                "disk_count": len(disks),
                "fixed_disk_count": len(fixed),
                "removable_disk_count": len(disks) - len(fixed),
                "read_only_disk_count": sum(device.get("read_only") is True for device in disks),
                "total_capacity_bytes": total_bytes,
            },
            "findings": findings,
            "devices": devices,
            "evidence": {
                "source": inputs.get("source"),
                "generation": inputs.get("generation"),
                "smart_status": inputs.get("smart_status"),
            },
        }

    async def compensate(self, output: dict[str, Any], context: ActionContext) -> None:
        del output, context


class UpdateStorageGraphAction:
    """Project the validated disk result into the System Knowledge Graph."""

    id = "knowledge.update-storage-graph"
    idempotent = True

    async def run(self, inputs: dict[str, Any], context: ActionContext) -> dict[str, Any]:
        devices = inputs.get("devices")
        if not isinstance(devices, list):
            raise ActionError("GRAPH_INPUT_INVALID")
        nodes = [GraphNode(id="system:local", kind=GraphKind.SYSTEM, attributes={"local": True})]
        edges: list[GraphEdge] = []
        known_ids = {"system:local"}
        disk_ids: set[str] = set()
        partition_ids: set[str] = set()
        for device in devices:
            if not isinstance(device, dict) or device.get("type") not in {"disk", "part"}:
                continue
            node_id = device.get("id")
            if not isinstance(node_id, str):
                continue
            is_disk = device.get("type") == "disk"
            nodes.append(
                GraphNode(
                    id=node_id,
                    kind=GraphKind.DISK if is_disk else GraphKind.PARTITION,
                    attributes={
                        key: device.get(key)
                        for key in (
                            "name",
                            "path",
                            "size_bytes",
                            "read_only",
                            "removable",
                            "transport",
                            "model",
                            "vendor",
                        )
                    },
                )
            )
            known_ids.add(node_id)
            if is_disk:
                disk_ids.add(node_id)
                edges.append(GraphEdge(source="system:local", relation="contains", target=node_id))
            else:
                partition_ids.add(node_id)
                parent = device.get("parent")
                if isinstance(parent, str) and parent in known_ids:
                    edges.append(GraphEdge(source=parent, relation="contains", target=node_id))
        snapshot = await context.graph.apply(tuple(nodes), tuple(edges))
        await context.event_bus.publish(
            AresEvent(
                name="knowledge.graph.updated",
                source=self.id,
                correlation_id=context.execution_id,
                payload={
                    "revision": snapshot.revision,
                    "node_count": len(snapshot.nodes),
                    "updated_kinds": [GraphKind.DISK.value],
                },
            )
        )
        return {
            "revision": snapshot.revision,
            "node_count": len(snapshot.nodes),
            "disk_node_count": sum(node.id in disk_ids for node in snapshot.nodes),
            "partition_node_count": sum(node.id in partition_ids for node in snapshot.nodes),
        }

    async def compensate(self, output: dict[str, Any], context: ActionContext) -> None:
        del output, context


def _safe_optional_string(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = "".join(character for character in value.strip() if character.isprintable())
    return value[:limit] or None


def _nested_string(payload: dict[str, Any], parent: str, child: str) -> str:
    value = payload.get(parent)
    nested = value.get(child) if isinstance(value, dict) else None
    return nested if isinstance(nested, str) else "unknown"


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    return isinstance(value, str) and value.casefold() in {"1", "true"}
