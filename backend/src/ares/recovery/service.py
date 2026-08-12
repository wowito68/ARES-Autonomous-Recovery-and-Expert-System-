"""Shared API/CLI lifecycle service for System Recovery cases."""

from __future__ import annotations

import asyncio
from contextlib import suppress

from pydantic import BaseModel, ConfigDict

from ares.recovery.models import (
    RecoveryCase,
    RecoveryDiagnoseInput,
    RecoveryPlanInput,
    RecoveryStatus,
    SystemRecoveryPlan,
    SystemRecoveryVerification,
)
from ares.recovery.orchestrator import RecoveryOrchestrator, RecoveryOrchestratorError


class RecoveryServiceError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class RecoveryAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case: RecoveryCase
    instruction: str


class RecoveryService:
    def __init__(self, orchestrator: RecoveryOrchestrator) -> None:
        self.orchestrator = orchestrator
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()

    async def diagnose(
        self, request: RecoveryDiagnoseInput, *, session_id: str
    ) -> RecoveryCase:
        try:
            return await self.orchestrator.diagnose(request, session_id=session_id)
        except RecoveryOrchestratorError as exc:
            raise RecoveryServiceError(exc.code) from exc

    async def plan(self, request: RecoveryPlanInput) -> SystemRecoveryPlan:
        try:
            return await self.orchestrator.build_plan(request)
        except RecoveryOrchestratorError as exc:
            raise RecoveryServiceError(exc.code) from exc

    async def authorize(self, case_id: str) -> RecoveryAccepted:
        case = await self.get(case_id)
        if case is None:
            raise RecoveryServiceError("RECOVERY_CASE_NOT_FOUND")
        if case.status not in {
            RecoveryStatus.PLANNED,
            RecoveryStatus.PROTECTED,
        }:
            raise RecoveryServiceError("RECOVERY_CASE_NOT_AUTHORIZABLE")
        await self._start_task(case_id, self._run_authorize(case_id), "recovery-authorize")
        return RecoveryAccepted(
            case=case,
            instruction=(
                "Each child operation requests its own independent local consent challenge. "
                "Approve only after reviewing the exact operation/checkpoint with the trusted CLI."
            ),
        )

    async def execute(self, case_id: str) -> RecoveryAccepted:
        case = await self.get(case_id)
        if case is None:
            raise RecoveryServiceError("RECOVERY_CASE_NOT_FOUND")
        if case.status is not RecoveryStatus.AUTHORIZED:
            raise RecoveryServiceError("RECOVERY_CASE_NOT_AUTHORIZED")
        await self._start_task(case_id, self._run_execute(case_id), "recovery-execute")
        return RecoveryAccepted(
            case=case,
            instruction="Recovery execution started; poll durable case state and verification.",
        )

    async def get(self, case_id: str) -> RecoveryCase | None:
        return await self.orchestrator.store.get_case(case_id)

    async def verification(self, case_id: str) -> SystemRecoveryVerification | None:
        return await self.orchestrator.store.get_verification(case_id)

    async def abort(self, case_id: str) -> RecoveryCase:
        async with self._lock:
            task = self._tasks.get(case_id)
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        try:
            return await self.orchestrator.abort(case_id)
        except RecoveryOrchestratorError as exc:
            raise RecoveryServiceError(exc.code) from exc

    async def shutdown(self) -> None:
        async with self._lock:
            tasks = tuple(self._tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _start_task(
        self, case_id: str, coroutine: object, prefix: str
    ) -> None:
        async with self._lock:
            current = self._tasks.get(case_id)
            if current is not None and not current.done():
                if hasattr(coroutine, "close"):
                    coroutine.close()  # type: ignore[attr-defined]
                raise RecoveryServiceError("RECOVERY_CASE_TASK_ALREADY_RUNNING")
            task = asyncio.create_task(coroutine, name=f"{prefix}-{case_id}")  # type: ignore[arg-type]
            self._tasks[case_id] = task

    async def _run_authorize(self, case_id: str) -> None:
        try:
            await self.orchestrator.authorize(case_id)
        except asyncio.CancelledError:
            await self.orchestrator.abort(case_id)
            raise
        except RecoveryOrchestratorError:
            case = await self.get(case_id)
            if case is not None and case.status not in {
                RecoveryStatus.PARTIAL,
                RecoveryStatus.UNKNOWN,
                RecoveryStatus.ABORTED,
            }:
                await self.orchestrator.abort(case_id)
        finally:
            await self._forget(case_id)

    async def _run_execute(self, case_id: str) -> None:
        try:
            await self.orchestrator.execute(case_id)
        except asyncio.CancelledError:
            await self.orchestrator.abort(case_id)
            raise
        except RecoveryOrchestratorError:
            case = await self.get(case_id)
            if case is not None and case.status is RecoveryStatus.RECOVERING:
                updated = case.model_copy(
                    update={
                        "status": RecoveryStatus.UNKNOWN,
                        "final_state": "Recovery execution ended unexpectedly; explicit reconciliation is required.",
                    }
                )
                await self.orchestrator.store.put_case(updated)
        finally:
            await self._forget(case_id)

    async def _forget(self, case_id: str) -> None:
        async with self._lock:
            self._tasks.pop(case_id, None)
