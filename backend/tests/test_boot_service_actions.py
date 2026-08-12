from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ares.actions.base import ActionContext, ActionError
from ares.actions.boot import (
    BootRepairPostcheck,
    ProjectBootDiagnosisGraphAction,
    ProjectBootRepairGraphAction,
)
from ares.audit import MemoryAuditLedger
from ares.boot.engine import BootRecoveryEngine
from ares.boot.models import BootRepairPlanInput, BootRepairStatus
from ares.boot.service import (
    BootRecoveryService,
    BootRecoveryServiceError,
    BootRepairPlanRequest,
    BootRepairRequest,
)
from ares.capabilities import CapabilityManager
from ares.capabilities.plugins.boot_recovery import BootRecoveryPlugin
from ares.events import EventBus, MemoryEventSink
from ares.knowledge import GraphKind, KnowledgeGraph
from ares.workflows import StepOutputs, WorkflowEngine
from tests.test_boot_engine import _diagnostic, _engine
from tests.test_boot_tools import _root


def _platform(
    tmp_path: Path,
    engine: BootRecoveryEngine,
) -> tuple[CapabilityManager, EventBus, MemoryEventSink, KnowledgeGraph]:
    sink = MemoryEventSink()
    event_bus = EventBus(sink)
    event_bus.prepare()
    graph = KnowledgeGraph(tmp_path / "knowledge.json")
    graph.prepare()
    workflows = WorkflowEngine(event_bus, graph)
    manager = CapabilityManager(
        workflows,
        os_family="debian",
        os_version="13",
        architecture="amd64",
        live_mode="live",
    )
    manager.load((BootRecoveryPlugin(engine),))
    manager.seal()
    return manager, event_bus, sink, graph


async def test_boot_service_runs_capability_and_projects_repair_graph(tmp_path: Path) -> None:
    root = _root(tmp_path, grub=False, initramfs=False)
    engine, store, _ = _engine(tmp_path)
    diagnostic = _diagnostic(root)
    await store.put_diagnostic(diagnostic)
    manager, event_bus, sink, graph = _platform(tmp_path, engine)
    audit = MemoryAuditLedger()
    service = BootRecoveryService(
        engine=engine,
        capabilities=manager,
        event_bus=event_bus,
        audit=audit,
    )

    context = ActionContext(
        execution_id="boot-diagnosis-seed",
        event_bus=event_bus,
        graph=graph,
    )
    projected = await ProjectBootDiagnosisGraphAction().run(
        diagnostic.model_dump(mode="json"), context
    )
    assert projected["knowledge_graph_revision"] >= 1

    plan = await service.plan(
        BootRepairPlanRequest(diagnostic_id=diagnostic.id),
        session_id="boot-service-session",
    )
    assert plan.executable is True
    assert audit.records[-1]["event_type"] == "boot.repair.planned"

    accepted = await service.start(
        BootRepairRequest(plan_id=plan.id, request_authorization=True),
        session_id="boot-service-session",
        created_by="test-suite",
    )
    repair_id = accepted.repair.execution.repair_id
    assert accepted.repair.execution.status is BootRepairStatus.PROTECTED

    terminal = None
    for _ in range(200):
        terminal = await service.get(repair_id)
        if terminal is not None and terminal.execution.status in {
            BootRepairStatus.COMPLETED,
            BootRepairStatus.REPAIR_FAILED,
            BootRepairStatus.UNKNOWN,
        }:
            break
        await asyncio.sleep(0.01)
    assert terminal is not None
    assert terminal.execution.status is BootRepairStatus.COMPLETED
    assert terminal.verification is not None
    assert await service.verification(repair_id) == terminal.verification

    snapshot = await graph.snapshot()
    kinds = {node.kind for node in snapshot.nodes}
    assert GraphKind.BOOT_REPAIR in kinds
    assert GraphKind.BOOT_VERIFICATION in kinds
    event_names = {event.event_type for event in sink.events}
    assert "boot.protection-checkpoint.created" in event_names
    assert "boot.repair-authorization.requested" in event_names
    assert "boot.repair.started" in event_names
    assert "boot.repair.completed" in event_names
    assert "boot.verification.completed" in event_names
    await service.shutdown()


async def test_boot_service_cancel_reconcile_and_validation_edges(tmp_path: Path) -> None:
    root = _root(tmp_path, grub=False, initramfs=False)
    engine, store, _ = _engine(tmp_path)
    diagnostic = _diagnostic(root)
    await store.put_diagnostic(diagnostic)
    manager, event_bus, _, graph = _platform(tmp_path, engine)
    service = BootRecoveryService(
        engine=engine,
        capabilities=manager,
        event_bus=event_bus,
        audit=MemoryAuditLedger(),
    )

    with pytest.raises(
        BootRecoveryServiceError, match="BOOT_EXPLICIT_AUTHORIZATION_REQUEST_REQUIRED"
    ):
        await service.start(
            BootRepairRequest(plan_id="missing-plan", request_authorization=False),
            session_id="boot-service-session",
            created_by="test-suite",
        )

    plan = await engine.plan(
        BootRepairPlanInput(diagnostic_id=diagnostic.id),
        session_id="boot-cancel-session",
    )
    sleeping = asyncio.create_task(asyncio.sleep(60))
    service._tasks[plan.repair_id] = sleeping
    cancelled = await service.cancel(plan.repair_id, session_id="boot-cancel-session")
    assert cancelled.execution.status is BootRepairStatus.ABORTED
    assert sleeping.cancelled() or sleeping.cancelling()

    with pytest.raises(BootRecoveryServiceError, match="BOOT_REPAIR_SESSION_MISMATCH"):
        await service.cancel(plan.repair_id, session_id="boot-other-session")
    with pytest.raises(BootRecoveryServiceError, match="BOOT_REPAIR_NOT_FOUND"):
        await service.cancel("missing-repair", session_id="boot-cancel-session")

    second = await engine.plan(
        BootRepairPlanInput(diagnostic_id=diagnostic.id),
        session_id="boot-reconcile-session",
    )
    protected = await engine.protect(second.id, session_id="boot-reconcile-session")
    execution = await store.get_execution(protected.repair_id)
    assert execution is not None
    await store.put_execution(
        execution.model_copy(
            update={
                "status": BootRepairStatus.UNKNOWN,
                "reconciliation_required": True,
            }
        )
    )
    verification = await service.reconcile(
        protected.repair_id, session_id="boot-reconcile-session"
    )
    assert verification.status.value == "PARTIAL"
    reconciled = await service.get(protected.repair_id)
    assert reconciled is not None
    assert reconciled.execution.status is BootRepairStatus.COMPLETED

    postcheck = BootRepairPostcheck()
    assert await postcheck({}, StepOutputs()) is False

    with pytest.raises(BootRecoveryServiceError, match="BOOT_REPAIR_NOT_UNKNOWN"):
        await service.reconcile(plan.repair_id, session_id="boot-cancel-session")

    await service.shutdown()
    del graph


async def test_boot_repair_graph_requires_verification(tmp_path: Path) -> None:
    root = _root(tmp_path)
    engine, store, _ = _engine(tmp_path)
    diagnostic = _diagnostic(root)
    await store.put_diagnostic(diagnostic)
    plan = await engine.plan(
        BootRepairPlanInput(diagnostic_id=diagnostic.id),
        session_id="boot-graph-session",
    )
    record = await store.get_record(plan.repair_id)
    assert record is not None

    sink = MemoryEventSink()
    event_bus = EventBus(sink)
    event_bus.prepare()
    graph = KnowledgeGraph(tmp_path / "graph-missing-verification.json")
    graph.prepare()
    context = ActionContext("boot-repair-graph", event_bus, graph)

    with pytest.raises(ActionError, match="BOOT_VERIFICATION_NOT_FOUND"):
        await ProjectBootRepairGraphAction().run(record.model_dump(mode="json"), context)
