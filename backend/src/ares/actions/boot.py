"""Workflow actions for boot diagnosis and authorized GRUB recovery."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ares.actions.base import ActionContext, ActionError
from ares.boot.engine import BootRecoveryEngine, BootRecoveryEngineError
from ares.boot.models import (
    BootDiagnosticResult,
    BootDiagnoseInput,
    BootRepairRecord,
    BootVerificationStatus,
)
from ares.events import AresEvent, EventSeverity
from ares.knowledge import GraphEdge, GraphKind, GraphNode
from ares.workflows.models import StepOutputs


class BootRepairExecutionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    repair_id: str = Field(min_length=8, max_length=128)
    session_id: str = Field(min_length=8, max_length=128)


class DiagnoseBootAction:
    id = "boot.diagnose-environment"
    idempotent = True

    def __init__(self, engine: BootRecoveryEngine) -> None:
        self.engine = engine

    async def run(self, inputs: dict[str, Any], context: ActionContext) -> dict[str, Any]:
        request = BootDiagnoseInput.model_validate(inputs)
        await _event(context, "boot.diagnosis.started", {})
        try:
            result = await self.engine.diagnose(request, session_id=context.execution_id)
        except BootRecoveryEngineError as exc:
            raise ActionError(exc.code) from exc
        await _event(
            context,
            "boot.environment.detected",
            {
                "diagnostic_id": result.id,
                "firmware": result.environment.firmware.mode.value,
                "os_count": len(result.environment.operating_systems),
            },
        )
        await _event(
            context,
            "boot.bootloader.detected",
            {
                "diagnostic_id": result.id,
                "bootloader": result.environment.bootloader.kind.value,
            },
        )
        for issue in result.issues:
            await _event(
                context,
                "boot.issue.detected",
                {
                    "diagnostic_id": result.id,
                    "issue": issue.code.value,
                    "severity": issue.severity.value,
                    "confidence": issue.confidence,
                },
                EventSeverity.WARNING,
            )
        return result.model_dump(mode="json")

    async def compensate(self, output: dict[str, Any], context: ActionContext) -> None:
        del output, context


class ProjectBootDiagnosisGraphAction:
    id = "knowledge.project-boot-diagnosis"
    idempotent = True

    async def run(self, inputs: dict[str, Any], context: ActionContext) -> dict[str, Any]:
        result = BootDiagnosticResult.model_validate(inputs)
        environment = result.environment
        firmware_id = f"firmware:{environment.firmware.mode.value.lower()}"
        loader_id = f"bootloader:{environment.bootloader.kind.value.lower()}"
        nodes: list[GraphNode] = [
            GraphNode(
                id=firmware_id,
                kind=GraphKind.FIRMWARE,
                attributes={
                    "mode": environment.firmware.mode.value,
                    "efivars_available": environment.firmware.efivars_available,
                },
            ),
            GraphNode(
                id=loader_id,
                kind=GraphKind.BOOTLOADER,
                attributes={
                    "kind": environment.bootloader.kind.value,
                    "distribution_family": environment.bootloader.distribution_family.value,
                    "repair_supported": environment.bootloader.repair_supported,
                },
            ),
        ]
        edges: list[GraphEdge] = []
        for entry in environment.boot_entries:
            nodes.append(
                GraphNode(
                    id=entry.id,
                    kind=GraphKind.BOOT_ENTRY,
                    attributes={
                        "number": entry.number,
                        "label": entry.label,
                        "loader_path": entry.loader_path,
                        "active": entry.active,
                    },
                )
            )
            edges.append(GraphEdge(source=firmware_id, relation="contains", target=entry.id))
        if environment.esp is not None:
            edges.append(
                GraphEdge(
                    source=loader_id,
                    relation="depends_on",
                    target=environment.esp.resource_id,
                )
            )
        for operating_system in environment.operating_systems:
            nodes.append(
                GraphNode(
                    id=operating_system.id,
                    kind=GraphKind.OPERATING_SYSTEM,
                    attributes={
                        "name": operating_system.name,
                        "version": operating_system.version,
                        "family": operating_system.family.value,
                    },
                )
            )
        config_id = f"boot-config:{result.id}"
        nodes.append(
            GraphNode(
                id=config_id,
                kind=GraphKind.BOOT_CONFIGURATION,
                attributes={
                    "grub_config": environment.configuration.grub_config_path,
                    "kernel_count": len(environment.configuration.kernels),
                    "initramfs_count": len(environment.configuration.initramfs),
                    "invalid_fstab_count": len(
                        environment.configuration.invalid_fstab_references
                    ),
                },
            )
        )
        edges.append(GraphEdge(source=loader_id, relation="configured_by", target=config_id))
        for index, kernel in enumerate(environment.configuration.kernels):
            kernel_id = f"kernel:{result.id}:{index}"
            nodes.append(
                GraphNode(id=kernel_id, kind=GraphKind.KERNEL, attributes={"path": kernel})
            )
            edges.append(GraphEdge(source=loader_id, relation="loads", target=kernel_id))
        for index, initramfs in enumerate(environment.configuration.initramfs):
            initramfs_id = f"initramfs:{result.id}:{index}"
            nodes.append(
                GraphNode(
                    id=initramfs_id,
                    kind=GraphKind.INITRAMFS,
                    attributes={"path": initramfs},
                )
            )
            edges.append(GraphEdge(source=loader_id, relation="loads", target=initramfs_id))
        for dependency in environment.dependencies:
            edges.append(
                GraphEdge(
                    source=dependency.source_id,
                    relation=dependency.relation,
                    target=dependency.target_id,
                )
            )
        snapshot = await context.graph.apply(tuple(nodes), tuple(edges))
        projected = result.model_copy(update={"knowledge_graph_revision": snapshot.revision})
        await _event(
            context,
            "knowledge.graph.updated",
            {"diagnostic_id": result.id, "revision": snapshot.revision},
        )
        return projected.model_dump(mode="json")

    async def compensate(self, output: dict[str, Any], context: ActionContext) -> None:
        del output, context


class ExecuteBootRepairAction:
    id = "boot.execute-authorized-repair"
    idempotent = False

    def __init__(self, engine: BootRecoveryEngine) -> None:
        self.engine = engine

    async def run(self, inputs: dict[str, Any], context: ActionContext) -> dict[str, Any]:
        request = BootRepairExecutionInput.model_validate(inputs)

        async def stage(name: str, payload: dict[str, object]) -> None:
            await _event(
                context,
                name,
                {"repair_id": request.repair_id, **payload},
                EventSeverity.WARNING
                if "installed" in name or "updated" in name
                else EventSeverity.INFO,
                session_id=request.session_id,
            )

        await _event(
            context,
            "boot.repair.started",
            {"repair_id": request.repair_id},
            EventSeverity.WARNING,
            session_id=request.session_id,
        )
        try:
            verification = await self.engine.execute_authorized(
                request.repair_id,
                session_id=request.session_id,
                on_stage=stage,
            )
        except BootRecoveryEngineError as exc:
            await _event(
                context,
                "boot.repair.failed",
                {"repair_id": request.repair_id, "error_code": exc.code},
                EventSeverity.ERROR,
                session_id=request.session_id,
            )
            raise ActionError(exc.code) from exc
        record = await self.engine.store.get_record(request.repair_id)
        if record is None:
            raise ActionError("BOOT_REPAIR_NOT_FOUND")
        await _event(
            context,
            "boot.verification.completed",
            {
                "repair_id": request.repair_id,
                "status": verification.status.value,
                "confidence": verification.confidence.value,
            },
            session_id=request.session_id,
        )
        await _event(
            context,
            "boot.repair.completed",
            {
                "repair_id": request.repair_id,
                "verification_status": verification.status.value,
            },
            session_id=request.session_id,
        )
        return record.model_dump(mode="json")

    async def compensate(self, output: dict[str, Any], context: ActionContext) -> None:
        del output, context


class ProjectBootRepairGraphAction:
    id = "knowledge.project-boot-repair"
    idempotent = True

    async def run(self, inputs: dict[str, Any], context: ActionContext) -> dict[str, Any]:
        record = BootRepairRecord.model_validate(inputs)
        verification = record.verification
        if verification is None:
            raise ActionError("BOOT_VERIFICATION_NOT_FOUND")
        repair_node = f"boot-repair:{record.execution.repair_id}"
        verification_node = f"boot-verification:{verification.id}"
        nodes = (
            GraphNode(
                id=repair_node,
                kind=GraphKind.BOOT_REPAIR,
                attributes={
                    "status": record.execution.status.value,
                    "plan_fingerprint": record.plan.fingerprint_sha256,
                    "target_os": record.plan.target_os.id,
                },
            ),
            GraphNode(
                id=verification_node,
                kind=GraphKind.BOOT_VERIFICATION,
                attributes={
                    "status": verification.status.value,
                    "confidence": verification.confidence.value,
                    "offline_reboot_proven": False,
                },
            ),
        )
        edges = (
            GraphEdge(source=repair_node, relation="verified_by", target=verification_node),
            GraphEdge(source=repair_node, relation="repairs", target=record.plan.target_os.id),
        )
        snapshot = await context.graph.apply(nodes, edges)
        await _event(
            context,
            "knowledge.graph.updated",
            {"repair_id": record.execution.repair_id, "revision": snapshot.revision},
            session_id=record.execution.session_id,
        )
        return record.model_dump(mode="json")

    async def compensate(self, output: dict[str, Any], context: ActionContext) -> None:
        del output, context


class BootRepairPostcheck:
    async def __call__(self, output: dict[str, Any], state: StepOutputs) -> bool:
        del state
        try:
            record = BootRepairRecord.model_validate(output)
        except ValueError:
            return False
        verification = record.verification
        return verification is not None and verification.status in {
            BootVerificationStatus.VERIFIED,
            BootVerificationStatus.PARTIAL,
        }


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
            source="boot.recovery",
            correlation_id=context.execution_id,
            session_id=session_id or context.execution_id,
            severity=severity,
            payload=payload,
        )
    )
