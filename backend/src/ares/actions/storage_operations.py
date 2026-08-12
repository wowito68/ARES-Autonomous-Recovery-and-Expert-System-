"""Private workflow actions for declarative storage partition operations."""

from __future__ import annotations

from typing import Any

from ares.actions.base import ActionContext, ActionError
from ares.events import AresEvent, EventSeverity
from ares.knowledge import GraphEdge, GraphKind, GraphNode
from ares.storage_operations.engine import StorageOperationEngine, StorageOperationEngineError
from ares.storage_operations.integrity import storage_plan_integrity_valid
from ares.storage_operations.models import (
    PartitionInspectInput,
    PartitionInspectResult,
    StorageLayout,
    StorageOperationExecutionInput,
    StorageOperationRecord,
    StorageOperationResult,
    StorageTransactionStatus,
    StorageVerification,
)
from ares.storage_operations.store import StorageOperationStore


class InspectStorageLayoutAction:
    id = "storage.partition.inspect-layout"
    idempotent = True

    def __init__(self, engine: StorageOperationEngine) -> None:
        self.engine = engine

    async def run(self, inputs: dict[str, Any], context: ActionContext) -> dict[str, Any]:
        request = PartitionInspectInput.model_validate(inputs)
        try:
            layout = await self.engine.inspect(request.target_disk)
        except StorageOperationEngineError as exc:
            raise ActionError(exc.code) from exc
        await _event(
            context,
            "storage.partition.inspected",
            {
                "disk_id": layout.disk.id,
                "target_fingerprint": layout.disk.identity.fingerprint_sha256,
                "table_type": layout.partition_table.type.value,
                "partition_count": len(layout.partition_table.partitions),
            },
        )
        return layout.model_dump(mode="json")

    async def compensate(self, output: dict[str, Any], context: ActionContext) -> None:
        del output, context


class ProjectStorageLayoutGraphAction:
    id = "knowledge.project-storage-layout"
    idempotent = True

    async def run(self, inputs: dict[str, Any], context: ActionContext) -> dict[str, Any]:
        layout = StorageLayout.model_validate(inputs)
        nodes, edges = _layout_graph(layout)
        snapshot = await context.graph.apply(tuple(nodes), tuple(edges))
        await _event(
            context,
            "knowledge.graph.updated",
            {"disk_id": layout.disk.id, "revision": snapshot.revision},
        )
        return PartitionInspectResult(
            layout=layout,
            knowledge_graph_revision=snapshot.revision,
        ).model_dump(mode="json")

    async def compensate(self, output: dict[str, Any], context: ActionContext) -> None:
        del output, context


class ValidateAuthorizedStorageOperationAction:
    id = "storage.validate-authorized-operation"
    idempotent = True

    def __init__(self, store: StorageOperationStore) -> None:
        self.store = store

    async def run(self, inputs: dict[str, Any], context: ActionContext) -> dict[str, Any]:
        request = StorageOperationExecutionInput.model_validate(inputs)
        record = await self.store.get_record(request.operation_id)
        if record is None or record.plan.id != request.plan_id:
            raise ActionError("STORAGE_OPERATION_NOT_FOUND")
        plan = record.plan
        checkpoint = plan.protection_checkpoint
        if record.transaction.status is not StorageTransactionStatus.AUTHORIZED:
            raise ActionError("STORAGE_OPERATION_NOT_AUTHORIZED")
        if request.session_id != plan.session_id:
            raise ActionError("STORAGE_OPERATION_SESSION_MISMATCH")
        if not storage_plan_integrity_valid(plan):
            raise ActionError("STORAGE_OPERATION_PLAN_TAMPERED")
        if (
            not plan.executable
            or checkpoint is None
            or request.protected_resource_id != plan.protected_resource_id
            or request.protected_resource_fingerprint_sha256 != plan.target_disk.fingerprint_sha256
        ):
            raise ActionError("STORAGE_PROTECTION_CHECKPOINT_INVALID")
        await _event(
            context,
            "storage.preflight.completed",
            {
                "operation_id": plan.operation_id,
                "plan_id": plan.id,
                "plan_fingerprint": plan.fingerprint_sha256,
                "target_fingerprint": plan.target_disk.fingerprint_sha256,
                "checkpoint_id": checkpoint.id,
                "dry_run_valid": bool(plan.dry_run and plan.dry_run.valid),
            },
            EventSeverity.WARNING,
            session_id=request.session_id,
        )
        return request.model_dump(mode="json")

    async def compensate(self, output: dict[str, Any], context: ActionContext) -> None:
        del output, context


class ExecuteStorageTransactionAction:
    id = "storage.execute-transaction"
    idempotent = False

    def __init__(self, engine: StorageOperationEngine, store: StorageOperationStore) -> None:
        self.engine = engine
        self.store = store

    async def run(self, inputs: dict[str, Any], context: ActionContext) -> dict[str, Any]:
        request = StorageOperationExecutionInput.model_validate(inputs)

        async def stage(name: str, payload: dict[str, object]) -> None:
            await _event(
                context,
                name,
                {"operation_id": request.operation_id, **payload},
                EventSeverity.WARNING
                if "write" in name or "started" in name
                else EventSeverity.INFO,
                session_id=request.session_id,
            )

        await _event(
            context,
            "storage.transaction.started",
            {"operation_id": request.operation_id, "plan_id": request.plan_id},
            EventSeverity.WARNING,
            session_id=request.session_id,
        )
        try:
            verification = await self.engine.execute_authorized(
                request.operation_id,
                session_id=request.session_id,
                on_stage=stage,
            )
        except StorageOperationEngineError as exc:
            record = await self.store.get_record(request.operation_id)
            event_name = (
                "storage.transaction.unknown"
                if record is not None
                and record.transaction.status is StorageTransactionStatus.UNKNOWN
                else "storage.transaction.failed"
            )
            await _event(
                context,
                event_name,
                {"operation_id": request.operation_id, "error_code": exc.code},
                EventSeverity.ERROR,
                session_id=request.session_id,
            )
            raise ActionError(exc.code) from exc
        record = await self.store.get_record(request.operation_id)
        if record is None:
            raise ActionError("STORAGE_OPERATION_NOT_FOUND")
        await _event(
            context,
            "storage.transaction.committed",
            {
                "operation_id": request.operation_id,
                "verification_id": verification.id,
                "verification_status": verification.status.value,
            },
            session_id=request.session_id,
        )
        return {
            "record": record.model_dump(mode="json"),
            "verification": verification.model_dump(mode="json"),
        }

    async def compensate(self, output: dict[str, Any], context: ActionContext) -> None:
        # Partition-table rollback is intentionally not automatic. A checkpoint is retained
        # for explicit recovery/reconciliation rather than blind compensation after a write.
        del output, context


class ProjectStorageTransactionGraphAction:
    id = "knowledge.project-storage-transaction"
    idempotent = True

    async def run(self, inputs: dict[str, Any], context: ActionContext) -> dict[str, Any]:
        record = StorageOperationRecord.model_validate(inputs["record"])
        verification = StorageVerification.model_validate(inputs["verification"])
        plan = record.plan
        layout = verification.after or plan.original_layout
        nodes, edges = _layout_graph(layout)
        transaction_node = f"storage-transaction:{record.transaction.id}"
        verification_node = f"storage-verification:{verification.id}"
        checkpoint = plan.protection_checkpoint
        nodes.extend(
            (
                GraphNode(
                    id=transaction_node,
                    kind=GraphKind.STORAGE_TRANSACTION,
                    attributes={
                        "operation_id": plan.operation_id,
                        "capability": plan.capability,
                        "status": record.transaction.status.value,
                        "plan_fingerprint": plan.fingerprint_sha256,
                    },
                ),
                GraphNode(
                    id=verification_node,
                    kind=GraphKind.STORAGE_VERIFICATION,
                    attributes={"status": verification.status.value},
                ),
            )
        )
        edges.extend(
            (
                GraphEdge(source=layout.disk.id, relation="modified_by", target=transaction_node),
                GraphEdge(
                    source=transaction_node, relation="verified_by", target=verification_node
                ),
            )
        )
        for partition in plan.proposed_layout.partition_table.partitions:
            if partition.id in plan.affected_resources:
                edges.append(
                    GraphEdge(
                        source=partition.id, relation="was_modified_by", target=transaction_node
                    )
                )
        if checkpoint is not None:
            checkpoint_node = f"protection-checkpoint:{checkpoint.id}"
            nodes.append(
                GraphNode(
                    id=checkpoint_node,
                    kind=GraphKind.PROTECTION_CHECKPOINT,
                    attributes={
                        "checkpoint_id": checkpoint.id,
                        "provider": checkpoint.provider_capability_id,
                        "verification_id": checkpoint.verification_id,
                    },
                )
            )
            edges.append(
                GraphEdge(source=transaction_node, relation="protected_by", target=checkpoint_node)
            )
        snapshot = await context.graph.apply(tuple(nodes), tuple(edges))
        result = StorageOperationResult(
            record=record,
            verification=verification,
            knowledge_graph_revision=snapshot.revision,
        )
        await _event(
            context,
            "knowledge.graph.updated",
            {"operation_id": plan.operation_id, "revision": snapshot.revision},
            session_id=plan.session_id,
        )
        return result.model_dump(mode="json")

    async def compensate(self, output: dict[str, Any], context: ActionContext) -> None:
        del output, context


def _layout_graph(layout: StorageLayout) -> tuple[list[GraphNode], list[GraphEdge]]:
    disk = layout.disk
    table = layout.partition_table
    table_id = disk.partition_table_id
    nodes: list[GraphNode] = [
        GraphNode(
            id=disk.id,
            kind=GraphKind.DISK,
            attributes={
                "path": disk.identity.canonical_path,
                "size_bytes": disk.identity.size_bytes,
                "device_kind": disk.identity.device_kind,
                "fingerprint": disk.identity.fingerprint_sha256,
            },
        ),
        GraphNode(
            id=table_id,
            kind=GraphKind.PARTITION_TABLE,
            attributes={
                "type": table.type.value,
                "guid": table.guid,
                "sector_size": table.sector_size,
                "fingerprint": table.fingerprint_sha256,
            },
        ),
    ]
    edges: list[GraphEdge] = [
        GraphEdge(source=disk.id, relation="has_partition_table", target=table_id)
    ]
    filesystem_by_id = {item.id: item for item in layout.filesystems}
    mount_by_id = {item.id: item for item in layout.mount_points}
    os_by_id = {item.id: item for item in layout.operating_systems}
    volume_by_id = {item.id: item for item in layout.volumes}
    for partition in table.partitions:
        nodes.append(
            GraphNode(
                id=partition.id,
                kind=GraphKind.PARTITION,
                attributes={
                    "number": partition.number,
                    "path": partition.path,
                    "start_sector": partition.start_sector,
                    "end_sector": partition.end_sector,
                    "size_bytes": partition.size_bytes,
                    "role": partition.role.value,
                    "partuuid": partition.partuuid,
                    "encryption": partition.encryption_status.value,
                },
            )
        )
        edges.append(GraphEdge(source=table_id, relation="contains_partition", target=partition.id))
        if partition.filesystem_id and partition.filesystem_id in filesystem_by_id:
            fs = filesystem_by_id[partition.filesystem_id]
            nodes.append(
                GraphNode(
                    id=fs.id,
                    kind=GraphKind.FILESYSTEM,
                    attributes={"type": fs.filesystem_type, "uuid": fs.uuid, "label": fs.label},
                )
            )
            edges.append(
                GraphEdge(source=partition.id, relation="contains_filesystem", target=fs.id)
            )
            edges.append(GraphEdge(source=disk.id, relation="contains_filesystem", target=fs.id))
        for mount_id in partition.mount_point_ids:
            mount = mount_by_id.get(mount_id)
            if mount is None:
                continue
            nodes.append(
                GraphNode(
                    id=mount.id,
                    kind=GraphKind.MOUNT_POINT,
                    attributes={"path": mount.path, "source": mount.source},
                )
            )
            edges.append(GraphEdge(source=partition.id, relation="mounted_at", target=mount.id))
        for os_id in partition.operating_system_ids:
            os_item = os_by_id.get(os_id)
            if os_item is None:
                continue
            nodes.append(
                GraphNode(
                    id=os_item.id,
                    kind=GraphKind.OPERATING_SYSTEM,
                    attributes={"name": os_item.name, "version": os_item.version},
                )
            )
            edges.append(GraphEdge(source=partition.id, relation="contains", target=os_item.id))
            edges.append(
                GraphEdge(source=disk.id, relation="hosts_operating_system", target=os_item.id)
            )
        for volume_id in partition.volume_ids:
            volume = volume_by_id.get(volume_id)
            if volume is None:
                continue
            nodes.append(
                GraphNode(
                    id=volume.id,
                    kind=GraphKind.VOLUME,
                    attributes={"kind": volume.kind.value, "name": volume.name},
                )
            )
            edges.append(GraphEdge(source=partition.id, relation="contains", target=volume.id))
    for dependency in layout.boot_dependencies:
        nodes.append(
            GraphNode(
                id=dependency.id,
                kind=GraphKind.BOOT_DEPENDENCY,
                attributes={
                    "kind": dependency.kind,
                    "partition_number": dependency.partition_number,
                    "critical": dependency.critical,
                    "reason": dependency.reason,
                },
            )
        )
        edges.append(
            GraphEdge(source=disk.id, relation="has_boot_dependency", target=dependency.id)
        )
    return nodes, edges


async def _event(
    context: ActionContext,
    name: str,
    payload: dict[str, Any],
    severity: EventSeverity = EventSeverity.INFO,
    *,
    session_id: str | None = None,
) -> None:
    await context.event_bus.publish(
        AresEvent(
            event_type=name,
            source="storage.operations",
            correlation_id=context.execution_id,
            session_id=session_id or context.execution_id,
            severity=severity,
            payload=payload,
        )
    )
