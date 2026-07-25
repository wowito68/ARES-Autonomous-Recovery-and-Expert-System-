"""Generic workflow engine with audit, retries, controls and compensation."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import OrderedDict
from dataclasses import dataclass
from time import monotonic
from typing import Any
from uuid import uuid4

from ares.actions import ActionContext, ActionError
from ares.events import AresEvent, EventBus
from ares.events.models import utc_now
from ares.knowledge import KnowledgeGraph
from ares.workflows.models import (
    ExecutionStatus,
    StageMode,
    StepExecution,
    StepOutputs,
    StepStatus,
    WorkflowDefinition,
    WorkflowExecution,
    WorkflowStep,
)

_MAX_RETAINED_EXECUTIONS = 256


class _ExecutionCancelled(Exception):
    pass


class _StepFailed(Exception):
    def __init__(self, record: StepExecution) -> None:
        super().__init__(record.error_code)
        self.record = record


class _ParallelStageFailed(Exception):
    def __init__(self, error_code: str | None) -> None:
        super().__init__(error_code)
        self.error_code = error_code


@dataclass(slots=True)
class _ExecutionControl:
    pause_gate: asyncio.Event
    cancelled: asyncio.Event
    definition: WorkflowDefinition

    async def checkpoint(self) -> None:
        await self.pause_gate.wait()
        if self.cancelled.is_set():
            raise _ExecutionCancelled


class WorkflowEngine:
    """Execute capability plans without exposing actions or commands to an LLM."""

    def __init__(self, event_bus: EventBus, graph: KnowledgeGraph) -> None:
        self.event_bus = event_bus
        self.graph = graph
        self._controls: dict[str, _ExecutionControl] = {}
        self._executions: OrderedDict[str, WorkflowExecution] = OrderedDict()

    def get(self, execution_id: str) -> WorkflowExecution | None:
        return self._executions.get(execution_id)

    def executions(self) -> tuple[WorkflowExecution, ...]:
        return tuple(reversed(self._executions.values()))

    async def pause(self, execution_id: str) -> bool:
        control = self._controls.get(execution_id)
        if control is None:
            return False
        control.pause_gate.clear()
        await self._control_event("workflow.paused", execution_id, control)
        return True

    async def resume(self, execution_id: str) -> bool:
        control = self._controls.get(execution_id)
        if control is None:
            return False
        control.pause_gate.set()
        await self._control_event("workflow.resumed", execution_id, control)
        return True

    async def cancel(self, execution_id: str) -> bool:
        control = self._controls.get(execution_id)
        if control is None:
            return False
        control.cancelled.set()
        control.pause_gate.set()
        await self._control_event("workflow.cancellation-requested", execution_id, control)
        return True

    async def execute(
        self,
        definition: WorkflowDefinition,
        *,
        execution_id: str | None = None,
    ) -> WorkflowExecution:
        execution_id = execution_id or uuid4().hex
        if execution_id in self._controls or execution_id in self._executions:
            raise ValueError("workflow execution ID already exists")
        control = _ExecutionControl(asyncio.Event(), asyncio.Event(), definition)
        control.pause_gate.set()
        self._controls[execution_id] = control
        started_at = utc_now()
        started_clock = monotonic()
        records: list[StepExecution] = []
        outputs: dict[str, dict[str, Any]] = {}
        compensations: list[tuple[WorkflowStep, dict[str, Any]]] = []
        status = ExecutionStatus.SUCCEEDED
        error_code: str | None = None
        rollback_performed = False
        context = ActionContext(
            execution_id=execution_id,
            event_bus=self.event_bus,
            graph=self.graph,
        )
        await self._event(
            "workflow.started",
            definition,
            execution_id,
            {"workflow_version": definition.version},
        )
        await self._event("capability.started", definition, execution_id, {})
        try:
            for stage in definition.stages:
                await control.checkpoint()
                if stage.mode is StageMode.SEQUENTIAL:
                    for step in stage.steps:
                        record, output = await self._run_step(
                            definition,
                            execution_id,
                            step,
                            dict(outputs),
                            context,
                            control,
                        )
                        records.append(record)
                        if output is not None:
                            outputs[step.id] = output
                            if step.compensate_on_failure:
                                compensations.append((step, output))
                else:
                    snapshot = dict(outputs)
                    results = await asyncio.gather(
                        *(
                            self._run_step(
                                definition,
                                execution_id,
                                step,
                                snapshot,
                                context,
                                control,
                            )
                            for step in stage.steps
                        ),
                        return_exceptions=True,
                    )
                    failures: list[StepExecution] = []
                    cancelled = False
                    for step, outcome in zip(stage.steps, results, strict=True):
                        if isinstance(outcome, _StepFailed):
                            failures.append(outcome.record)
                            continue
                        if isinstance(outcome, _ExecutionCancelled):
                            cancelled = True
                            continue
                        if isinstance(outcome, BaseException):
                            failures.append(
                                self._failed_record(
                                    step,
                                    utc_now(),
                                    monotonic(),
                                    0,
                                    "WORKFLOW_STEP_INVALID",
                                )
                            )
                            continue
                        record, output = outcome
                        records.append(record)
                        if output is not None:
                            outputs[step.id] = output
                            if step.compensate_on_failure:
                                compensations.append((step, output))
                    if cancelled:
                        raise _ExecutionCancelled
                    if failures:
                        records.extend(failures)
                        raise _ParallelStageFailed(failures[0].error_code)
        except _StepFailed as exc:
            records.append(exc.record)
            status = ExecutionStatus.FAILED
            error_code = exc.record.error_code
        except _ParallelStageFailed as exc:
            status = ExecutionStatus.FAILED
            error_code = exc.error_code
        except _ExecutionCancelled:
            status = ExecutionStatus.CANCELLED
            error_code = "WORKFLOW_CANCELLED"

        result: dict[str, Any] | None = None
        result_receipt: dict[str, Any] | None = None
        if status is ExecutionStatus.SUCCEEDED:
            try:
                result = definition.result(outputs)
                result_receipt = self._result_receipt(result)
            except (TypeError, ValueError):
                status = ExecutionStatus.FAILED
                error_code = "WORKFLOW_RESULT_INVALID"

        if status is not ExecutionStatus.SUCCEEDED and compensations:
            rollback_performed = await self._compensate(
                definition,
                execution_id,
                compensations,
                context,
            )

        finished_at = utc_now()
        execution = WorkflowExecution(
            id=execution_id,
            workflow_id=definition.id,
            workflow_version=definition.version,
            capability_id=definition.capability_id,
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=(monotonic() - started_clock) * 1_000,
            steps=tuple(records),
            result=result,
            error_code=error_code,
            rollback_performed=rollback_performed,
        )
        terminal = (
            "workflow.completed"
            if status is ExecutionStatus.SUCCEEDED
            else "workflow.cancelled"
            if status is ExecutionStatus.CANCELLED
            else "workflow.failed"
        )
        await self._event(
            terminal,
            definition,
            execution_id,
            {
                "status": status.value,
                "error_code": error_code,
                "duration_ms": round(execution.duration_ms, 3),
            },
        )
        await self._event(
            "capability.completed" if status is ExecutionStatus.SUCCEEDED else "capability.failed",
            definition,
            execution_id,
            {
                "status": status.value,
                "error_code": error_code,
                "result_receipt": result_receipt,
            },
        )
        self._controls.pop(execution_id, None)
        self._remember(execution)
        return execution

    async def _run_step(
        self,
        definition: WorkflowDefinition,
        execution_id: str,
        step: WorkflowStep,
        state: StepOutputs,
        context: ActionContext,
        control: _ExecutionControl,
    ) -> tuple[StepExecution, dict[str, Any] | None]:
        started_at = utc_now()
        started_clock = monotonic()
        try:
            should_run = step.condition is None or step.condition(state)
        except Exception as exc:
            raise _StepFailed(
                self._failed_record(
                    step,
                    started_at,
                    started_clock,
                    0,
                    "WORKFLOW_CONDITION_INVALID",
                )
            ) from exc
        if not should_run:
            record = StepExecution(
                step_id=step.id,
                action_id=step.action.id,
                status=StepStatus.SKIPPED,
                attempts=0,
                started_at=started_at,
                finished_at=utc_now(),
                duration_ms=(monotonic() - started_clock) * 1_000,
            )
            await self._event(
                "workflow.step.skipped",
                definition,
                execution_id,
                {"step_id": step.id, "action_id": step.action.id},
            )
            return record, None

        try:
            inputs = step.inputs(state)
        except Exception as exc:
            raise _StepFailed(
                self._failed_record(
                    step,
                    started_at,
                    started_clock,
                    0,
                    "WORKFLOW_INPUT_INVALID",
                )
            ) from exc
        attempts = 0
        last_error = "ACTION_FAILED"
        while attempts < step.retry.max_attempts:
            attempts += 1
            await control.checkpoint()
            await self._event(
                "action.started",
                definition,
                execution_id,
                {"step_id": step.id, "action_id": step.action.id, "attempt": attempts},
            )
            try:
                output = await self._run_action(step, inputs, context, control)
                for index, postcheck in enumerate(step.postchecks):
                    if not await postcheck(output, state):
                        raise ActionError("POSTCHECK_FAILED")
                    await self._event(
                        "workflow.postcheck.passed",
                        definition,
                        execution_id,
                        {"step_id": step.id, "postcheck_index": index},
                    )
            except TimeoutError:
                last_error = "ACTION_TIMEOUT"
            except ActionError as exc:
                last_error = exc.code
            except _ExecutionCancelled:
                raise
            except Exception:
                last_error = "ACTION_FAILED"
            else:
                record = StepExecution(
                    step_id=step.id,
                    action_id=step.action.id,
                    status=StepStatus.SUCCEEDED,
                    attempts=attempts,
                    started_at=started_at,
                    finished_at=utc_now(),
                    duration_ms=(monotonic() - started_clock) * 1_000,
                )
                await self._event(
                    "action.completed",
                    definition,
                    execution_id,
                    {
                        "step_id": step.id,
                        "action_id": step.action.id,
                        "attempts": attempts,
                        "duration_ms": round(record.duration_ms, 3),
                    },
                )
                return record, output
            await self._event(
                "action.failed",
                definition,
                execution_id,
                {
                    "step_id": step.id,
                    "action_id": step.action.id,
                    "attempt": attempts,
                    "error_code": last_error,
                    "retrying": attempts < step.retry.max_attempts,
                },
            )
            if attempts < step.retry.max_attempts and step.retry.delay_seconds:
                await asyncio.sleep(step.retry.delay_seconds)

        record = StepExecution(
            step_id=step.id,
            action_id=step.action.id,
            status=StepStatus.FAILED,
            attempts=attempts,
            started_at=started_at,
            finished_at=utc_now(),
            duration_ms=(monotonic() - started_clock) * 1_000,
            error_code=last_error,
        )
        raise _StepFailed(record)

    async def _run_action(
        self,
        step: WorkflowStep,
        inputs: dict[str, Any],
        context: ActionContext,
        control: _ExecutionControl,
    ) -> dict[str, Any]:
        action_task = asyncio.create_task(step.action.run(inputs, context))
        cancellation_task = asyncio.create_task(control.cancelled.wait())
        try:
            done, _ = await asyncio.wait(
                {action_task, cancellation_task},
                timeout=step.timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                action_task.cancel()
                await asyncio.gather(action_task, return_exceptions=True)
                raise TimeoutError
            if action_task in done:
                return await action_task
            if control.cancelled.is_set():
                action_task.cancel()
                await asyncio.gather(action_task, return_exceptions=True)
                raise _ExecutionCancelled
            raise _ExecutionCancelled
        finally:
            cancellation_task.cancel()
            await asyncio.gather(cancellation_task, return_exceptions=True)

    @staticmethod
    def _failed_record(
        step: WorkflowStep,
        started_at: Any,
        started_clock: float,
        attempts: int,
        error_code: str,
    ) -> StepExecution:
        return StepExecution(
            step_id=step.id,
            action_id=step.action.id,
            status=StepStatus.FAILED,
            attempts=attempts,
            started_at=started_at,
            finished_at=utc_now(),
            duration_ms=(monotonic() - started_clock) * 1_000,
            error_code=error_code,
        )

    async def _compensate(
        self,
        definition: WorkflowDefinition,
        execution_id: str,
        completed: list[tuple[WorkflowStep, dict[str, Any]]],
        context: ActionContext,
    ) -> bool:
        await self._event("workflow.rollback.started", definition, execution_id, {})
        complete = True
        for step, output in reversed(completed):
            try:
                await step.action.compensate(output, context)
            except Exception:
                complete = False
                await self._event(
                    "workflow.compensation.failed",
                    definition,
                    execution_id,
                    {"step_id": step.id, "action_id": step.action.id},
                )
            else:
                await self._event(
                    "workflow.compensation.completed",
                    definition,
                    execution_id,
                    {"step_id": step.id, "action_id": step.action.id},
                )
        await self._event(
            "workflow.rollback.completed",
            definition,
            execution_id,
            {"complete": complete},
        )
        return True

    async def _event(
        self,
        name: str,
        definition: WorkflowDefinition,
        execution_id: str,
        payload: dict[str, Any],
    ) -> None:
        await self.event_bus.publish(
            AresEvent(
                name=name,
                source=definition.capability_id,
                correlation_id=execution_id,
                payload={"workflow_id": definition.id, **payload},
            )
        )

    async def _control_event(
        self,
        name: str,
        execution_id: str,
        control: _ExecutionControl,
    ) -> None:
        await self._event(name, control.definition, execution_id, {})

    def _remember(self, execution: WorkflowExecution) -> None:
        self._executions[execution.id] = execution
        while len(self._executions) > _MAX_RETAINED_EXECUTIONS:
            self._executions.popitem(last=False)

    @staticmethod
    def _result_receipt(result: dict[str, Any]) -> dict[str, Any]:
        encoded = json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return {
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "bytes": len(encoded),
            "top_level_fields": sorted(result),
        }
