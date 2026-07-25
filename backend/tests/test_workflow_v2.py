"""Behavioral tests for workflows, events, compensation and graph persistence."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from ares.actions import ActionContext, ActionError
from ares.events import AresEvent, EventBus, JsonlEventSink, MemoryEventSink
from ares.knowledge import GraphEdge, GraphKind, GraphNode, KnowledgeGraph
from ares.workflows import (
    ExecutionStatus,
    RetryPolicy,
    StageMode,
    WorkflowDefinition,
    WorkflowEngine,
    WorkflowStage,
    WorkflowStep,
)
from ares.workflows.models import StepOutputs, passing_postcheck


@dataclass
class FakeAction:
    id: str
    output: dict[str, Any] = field(default_factory=lambda: {"ok": True})
    failures: int = 0
    error_code: str = "TEST_ACTION_FAILED"
    delay: float = 0
    idempotent: bool = True
    compensation_fails: bool = False
    calls: int = 0
    compensated: int = 0
    started: asyncio.Event | None = None
    release: asyncio.Event | None = None
    unexpected: bool = False

    async def run(self, inputs: dict[str, Any], context: ActionContext) -> dict[str, Any]:
        del inputs, context
        self.calls += 1
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            await self.release.wait()
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.unexpected:
            raise RuntimeError("private detail")
        if self.calls <= self.failures:
            raise ActionError(self.error_code)
        return dict(self.output)

    async def compensate(
        self,
        output: dict[str, Any],
        context: ActionContext,
    ) -> None:
        del output, context
        self.compensated += 1
        if self.compensation_fails:
            raise OSError("private compensation detail")


class RejectPostcheck:
    async def __call__(self, output: dict[str, Any], state: StepOutputs) -> bool:
        del output, state
        return False


def _engine(tmp_path: Path) -> tuple[WorkflowEngine, MemoryEventSink]:
    sink = MemoryEventSink()
    return WorkflowEngine(EventBus(sink), KnowledgeGraph(tmp_path / "graph.json")), sink


def _workflow(*stages: WorkflowStage) -> WorkflowDefinition:
    return WorkflowDefinition(
        id="test.workflow",
        version="1.0.0",
        capability_id="test.capability",
        stages=stages,
        result=lambda state: {"completed": sorted(state)},
    )


async def test_workflow_runs_sequential_parallel_conditional_and_postchecks(
    tmp_path: Path,
) -> None:
    engine, sink = _engine(tmp_path)
    first = FakeAction("test.first", {"value": 1})
    parallel_a = FakeAction("test.parallel-a", {"value": 2})
    parallel_b = FakeAction("test.parallel-b", {"value": 3})
    skipped = FakeAction("test.skipped")
    definition = _workflow(
        WorkflowStage(
            "prepare",
            StageMode.SEQUENTIAL,
            (
                WorkflowStep(
                    "first",
                    first,
                    lambda _: {},
                    postchecks=(passing_postcheck,),
                ),
                WorkflowStep(
                    "skipped",
                    skipped,
                    lambda _: {},
                    condition=lambda _: False,
                ),
            ),
        ),
        WorkflowStage(
            "parallel",
            StageMode.PARALLEL,
            (
                WorkflowStep("parallel-a", parallel_a, lambda state: dict(state["first"])),
                WorkflowStep("parallel-b", parallel_b, lambda state: dict(state["first"])),
            ),
        ),
    )

    record = await engine.execute(definition, execution_id="workflow-success")

    assert record.status is ExecutionStatus.SUCCEEDED
    assert record.result == {"completed": ["first", "parallel-a", "parallel-b"]}
    assert [step.status.value for step in record.steps] == [
        "succeeded",
        "skipped",
        "succeeded",
        "succeeded",
    ]
    assert skipped.calls == 0
    assert engine.get(record.id) == record
    assert engine.executions() == (record,)
    assert {event.name for event in sink.events}.issuperset(
        {
            "workflow.started",
            "capability.started",
            "workflow.step.skipped",
            "workflow.postcheck.passed",
            "action.completed",
            "workflow.completed",
            "capability.completed",
        }
    )

    with pytest.raises(ValueError, match="already exists"):
        await engine.execute(definition, execution_id="workflow-success")


async def test_workflow_retries_then_compensates_after_failure(tmp_path: Path) -> None:
    engine, sink = _engine(tmp_path)
    completed = FakeAction("test.completed")
    failing = FakeAction("test.failing", failures=3, error_code="EXPECTED_FAILURE")
    definition = _workflow(
        WorkflowStage(
            "failure",
            StageMode.SEQUENTIAL,
            (
                WorkflowStep(
                    "completed",
                    completed,
                    lambda _: {},
                    compensate_on_failure=True,
                ),
                WorkflowStep(
                    "failing",
                    failing,
                    lambda _: {},
                    retry=RetryPolicy(max_attempts=2, delay_seconds=0.001),
                ),
            ),
        )
    )

    record = await engine.execute(definition)

    assert record.status is ExecutionStatus.FAILED
    assert record.error_code == "EXPECTED_FAILURE"
    assert record.rollback_performed is True
    assert record.steps[-1].attempts == 2
    assert completed.compensated == 1
    assert [event.name for event in sink.events].count("action.failed") == 2
    assert "workflow.rollback.completed" in [event.name for event in sink.events]


@pytest.mark.parametrize(
    ("action", "postchecks", "error_code"),
    [
        (FakeAction("test.timeout", delay=0.05), (), "ACTION_TIMEOUT"),
        (FakeAction("test.exception", unexpected=True), (), "ACTION_FAILED"),
        (FakeAction("test.postcheck"), (RejectPostcheck(),), "POSTCHECK_FAILED"),
    ],
)
async def test_workflow_redacts_timeout_exception_and_postcheck_failures(
    tmp_path: Path,
    action: FakeAction,
    postchecks: tuple[RejectPostcheck, ...],
    error_code: str,
) -> None:
    engine, _ = _engine(tmp_path)
    definition = _workflow(
        WorkflowStage(
            "one",
            StageMode.SEQUENTIAL,
            (
                WorkflowStep(
                    "step",
                    action,
                    lambda _: {},
                    timeout_seconds=0.005 if action.delay else 1,
                    postchecks=postchecks,
                ),
            ),
        )
    )

    record = await engine.execute(definition)

    assert record.status is ExecutionStatus.FAILED
    assert record.error_code == error_code
    assert "private detail" not in record.model_dump_json()


async def test_workflow_pause_resume_cancel_and_failed_compensation(tmp_path: Path) -> None:
    engine, sink = _engine(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()
    first = FakeAction(
        "test.blocking",
        started=started,
        release=release,
        compensation_fails=True,
    )
    second = FakeAction("test.never")
    definition = _workflow(
        WorkflowStage(
            "first",
            StageMode.SEQUENTIAL,
            (
                WorkflowStep(
                    "first",
                    first,
                    lambda _: {},
                    compensate_on_failure=True,
                ),
            ),
        ),
        WorkflowStage(
            "second",
            StageMode.SEQUENTIAL,
            (WorkflowStep("second", second, lambda _: {}),),
        ),
    )
    task = asyncio.create_task(engine.execute(definition, execution_id="controlled-run"))
    await started.wait()

    assert await engine.pause("missing") is False
    assert await engine.resume("missing") is False
    assert await engine.cancel("missing") is False
    assert await engine.pause("controlled-run") is True
    assert await engine.resume("controlled-run") is True
    assert await engine.pause("controlled-run") is True
    release.set()
    for _ in range(100):
        if "action.completed" in {event.name for event in sink.events}:
            break
        await asyncio.sleep(0)
    assert "action.completed" in {event.name for event in sink.events}
    assert await engine.cancel("controlled-run") is True
    record = await task

    assert record.status is ExecutionStatus.CANCELLED
    assert record.error_code == "WORKFLOW_CANCELLED"
    assert second.calls == 0
    assert first.compensated == 1
    assert {
        "workflow.paused",
        "workflow.resumed",
        "workflow.cancellation-requested",
        "workflow.cancelled",
        "workflow.compensation.failed",
    }.issubset({event.name for event in sink.events})


def test_workflow_definition_rejects_unsafe_control_values() -> None:
    action = FakeAction("test.non-idempotent", idempotent=False)

    with pytest.raises(ValueError, match="between one and five"):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError, match="between zero and thirty"):
        RetryPolicy(delay_seconds=31)
    with pytest.raises(ValueError, match="idempotent"):
        WorkflowStep("step", action, lambda _: {}, retry=RetryPolicy(max_attempts=2))
    with pytest.raises(ValueError, match="timeout"):
        WorkflowStep("step", action, lambda _: {}, timeout_seconds=0)
    with pytest.raises(ValueError, match="stage"):
        WorkflowStage("", StageMode.SEQUENTIAL, ())
    with pytest.raises(ValueError, match="duplicate"):
        _workflow(
            WorkflowStage(
                "duplicates",
                StageMode.SEQUENTIAL,
                (
                    WorkflowStep("same", action, lambda _: {}),
                    WorkflowStep("same", action, lambda _: {}),
                ),
            )
        )


async def test_event_bus_persists_before_dispatch_and_graph_reloads(tmp_path: Path) -> None:
    sink = JsonlEventSink(tmp_path / "events/events.jsonl")
    bus = EventBus(sink)
    received: list[str] = []

    async def receive(event: AresEvent) -> None:
        received.append(event.name)

    bus.subscribe("test.event", receive)
    bus.subscribe("*", receive)
    bus.prepare()
    event = AresEvent(
        name="test.event",
        source="test.source",
        correlation_id="correlation-1",
        payload={"safe": True},
    )
    await bus.publish(event)

    stored = json.loads(sink.path.read_text(encoding="utf-8"))
    assert stored["id"] == event.id
    assert received == ["test.event", "test.event"]
    assert sink.path.stat().st_mode & 0o777 == 0o600

    graph_path = tmp_path / "graph/state.json"
    graph = KnowledgeGraph(graph_path)
    graph.prepare()
    snapshot = await graph.apply(
        (
            GraphNode(id="system:local", kind=GraphKind.SYSTEM),
            GraphNode(id="disk:test", kind=GraphKind.DISK, attributes={"size": 4}),
        ),
        (GraphEdge(source="system:local", relation="contains", target="disk:test"),),
    )
    assert snapshot.revision == 1
    assert (await graph.snapshot()).nodes[1].id == "system:local"

    reloaded = KnowledgeGraph(graph_path)
    reloaded.prepare()
    assert (await reloaded.snapshot()).revision == 1
    with pytest.raises(ValueError, match="unknown node"):
        await reloaded.apply(
            (),
            (GraphEdge(source="system:local", relation="contains", target="disk:missing"),),
        )
