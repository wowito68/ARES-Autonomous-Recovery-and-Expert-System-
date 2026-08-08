"""Private workflow actions for the storage.disk-analysis vertical slice."""

from __future__ import annotations

from typing import Any

from ares.actions.base import ActionContext, ActionError
from ares.events import AresEvent
from ares.knowledge import GraphEdge, GraphKind, GraphNode
from ares.storage import (
    StorageCapabilityResult,
    StorageSnapshotStore,
    SystemStorageSnapshot,
    build_storage_snapshot,
)
from ares.tools import StorageEvidence, StorageToolSuite


class CollectStorageEvidenceAction:
    """Collect passive evidence through the fixed storage Tool Layer."""

    id = "storage.collect-evidence"
    idempotent = True

    def __init__(self, tools: StorageToolSuite) -> None:
        self.tools = tools

    async def run(self, inputs: dict[str, Any], context: ActionContext) -> dict[str, Any]:
        del inputs
        evidence = await self.tools.collect(context.event_bus, context.execution_id)
        if not evidence.devices and evidence.errors:
            raise ActionError("STORAGE_EVIDENCE_UNAVAILABLE")
        return evidence.model_dump(mode="json")

    async def compensate(self, output: dict[str, Any], context: ActionContext) -> None:
        del output, context


class BuildStorageSnapshotAction:
    """Normalize tool evidence into an auditable SystemStorageSnapshot."""

    id = "storage.build-snapshot"
    idempotent = True

    async def run(self, inputs: dict[str, Any], context: ActionContext) -> dict[str, Any]:
        try:
            evidence = StorageEvidence.model_validate(inputs)
        except ValueError as exc:
            raise ActionError("STORAGE_EVIDENCE_INVALID") from exc
        snapshot = build_storage_snapshot(evidence, context.execution_id)
        for disk in snapshot.disks:
            await context.event_bus.publish(
                AresEvent(
                    name="storage.disk.detected",
                    source=self.id,
                    correlation_id=context.execution_id,
                    payload={"disk_id": disk.id, "size_bytes": disk.size_bytes},
                )
            )
        for partition in snapshot.partitions:
            await context.event_bus.publish(
                AresEvent(
                    name="storage.partition.detected",
                    source=self.id,
                    correlation_id=context.execution_id,
                    payload={"partition_id": partition.id, "disk_id": partition.disk_id},
                )
            )
        for smart in snapshot.smart:
            await context.event_bus.publish(
                AresEvent(
                    name="storage.smart.analyzed",
                    source=self.id,
                    correlation_id=context.execution_id,
                    payload={
                        "smart_id": smart.id,
                        "disk_id": smart.disk_id,
                        "status": smart.status.value,
                        "reason": smart.reason,
                    },
                )
            )
        return snapshot.model_dump(mode="json")

    async def compensate(self, output: dict[str, Any], context: ActionContext) -> None:
        del output, context


class PersistStorageSnapshotAction:
    """Persist the immutable snapshot before graph projection and diagnosis."""

    id = "storage.persist-snapshot"
    idempotent = True

    def __init__(self, store: StorageSnapshotStore) -> None:
        self.store = store

    async def run(self, inputs: dict[str, Any], context: ActionContext) -> dict[str, Any]:
        try:
            snapshot = SystemStorageSnapshot.model_validate(inputs)
        except ValueError as exc:
            raise ActionError("STORAGE_SNAPSHOT_INVALID") from exc
        await self.store.put(snapshot)
        await context.event_bus.publish(
            AresEvent(
                name="storage.snapshot.created",
                source=self.id,
                correlation_id=context.execution_id,
                payload={
                    "snapshot_id": snapshot.id,
                    "evidence_sha256": snapshot.evidence_sha256,
                },
            )
        )
        return snapshot.model_dump(mode="json")

    async def compensate(self, output: dict[str, Any], context: ActionContext) -> None:
        del output, context


class ProjectStorageSnapshotAction:
    """Project disks, partitions, filesystems, mounts, OS and SMART into the graph."""

    id = "knowledge.project-storage-snapshot"
    idempotent = True

    async def run(self, inputs: dict[str, Any], context: ActionContext) -> dict[str, Any]:
        try:
            snapshot = SystemStorageSnapshot.model_validate(inputs)
        except ValueError as exc:
            raise ActionError("STORAGE_SNAPSHOT_INVALID") from exc

        nodes: list[GraphNode] = [
            GraphNode(
                id="system:local",
                kind=GraphKind.SYSTEM,
                attributes={"hostname": snapshot.hostname, "snapshot_id": snapshot.id},
            )
        ]
        edges: list[GraphEdge] = []
        for disk in snapshot.disks:
            nodes.append(
                GraphNode(
                    id=disk.id,
                    kind=GraphKind.DISK,
                    attributes={
                        "name": disk.name,
                        "path": disk.path,
                        "size_bytes": disk.size_bytes,
                        "model": disk.model,
                        "vendor": disk.vendor,
                        "transport": disk.transport,
                        "read_only": disk.read_only,
                        "removable": disk.removable,
                        "hardware_identity": disk.hardware_identity,
                    },
                )
            )
            edges.append(GraphEdge(source="system:local", relation="contains", target=disk.id))
        for partition in snapshot.partitions:
            nodes.append(
                GraphNode(
                    id=partition.id,
                    kind=GraphKind.PARTITION,
                    attributes={
                        "name": partition.name,
                        "path": partition.path,
                        "size_bytes": partition.size_bytes,
                        "read_only": partition.read_only,
                    },
                )
            )
            if partition.disk_id is not None:
                edges.append(
                    GraphEdge(source=partition.disk_id, relation="contains", target=partition.id)
                )
        for filesystem in snapshot.filesystems:
            nodes.append(
                GraphNode(
                    id=filesystem.id,
                    kind=GraphKind.FILESYSTEM,
                    attributes={
                        "type": filesystem.filesystem_type,
                        "version": filesystem.version,
                        "uuid": filesystem.uuid,
                        "label": filesystem.label,
                    },
                )
            )
            partition = next(
                (item for item in snapshot.partitions if item.path == filesystem.device_path),
                None,
            )
            if partition is not None:
                edges.append(
                    GraphEdge(
                        source=partition.id,
                        relation="formatted_as",
                        target=filesystem.id,
                    )
                )
        for mount in snapshot.mounts:
            nodes.append(
                GraphNode(
                    id=mount.id,
                    kind=GraphKind.MOUNT_POINT,
                    attributes={
                        "path": mount.path,
                        "source": mount.source,
                        "used_percent": mount.used_percent,
                    },
                )
            )
            partition = next(
                (item for item in snapshot.partitions if item.path == mount.source),
                None,
            )
            if partition is not None:
                edges.append(
                    GraphEdge(source=partition.id, relation="mounted_at", target=mount.id)
                )
        for os_item in snapshot.operating_systems:
            nodes.append(
                GraphNode(
                    id=os_item.id,
                    kind=GraphKind.OPERATING_SYSTEM,
                    attributes={
                        "name": os_item.name,
                        "version": os_item.version,
                        "os_id": os_item.os_id,
                        "mountpoint": os_item.mountpoint,
                    },
                )
            )
            if os_item.disk_id is not None:
                edges.append(
                    GraphEdge(source=os_item.disk_id, relation="contains", target=os_item.id)
                )
        for smart in snapshot.smart:
            nodes.append(
                GraphNode(
                    id=smart.id,
                    kind=GraphKind.SMART_STATUS,
                    attributes={
                        "status": smart.status.value,
                        "passed": smart.passed,
                        "temperature_celsius": smart.temperature_celsius,
                        "reason": smart.reason,
                    },
                )
            )
            edges.append(
                GraphEdge(source=smart.disk_id, relation="has_health_status", target=smart.id)
            )

        graph_snapshot = await context.graph.apply(tuple(nodes), tuple(edges))
        await context.event_bus.publish(
            AresEvent(
                name="knowledge.graph.updated",
                source=self.id,
                correlation_id=context.execution_id,
                payload={
                    "revision": graph_snapshot.revision,
                    "node_count": len(graph_snapshot.nodes),
                    "edge_count": len(graph_snapshot.edges),
                    "snapshot_id": snapshot.id,
                },
            )
        )
        return {
            "revision": graph_snapshot.revision,
            "node_count": len(graph_snapshot.nodes),
            "edge_count": len(graph_snapshot.edges),
        }

    async def compensate(self, output: dict[str, Any], context: ActionContext) -> None:
        del output, context


def storage_capability_result(state: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Build the typed public result from completed workflow state."""

    snapshot = SystemStorageSnapshot.model_validate(state["persist-snapshot"])
    graph = state["project-knowledge-graph"]
    result = StorageCapabilityResult(
        snapshot=snapshot,
        knowledge_graph={
            "revision": graph["revision"],
            "node_count": graph["node_count"],
            "edge_count": graph["edge_count"],
        },
    )
    return result.model_dump(mode="json")
