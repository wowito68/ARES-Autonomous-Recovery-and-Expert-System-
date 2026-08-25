"""Safe model-to-capability orchestration without exposing a shell."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from ares.agent.models import (
    AgentAuthorizationRequest,
    AgentPlanStep,
    AgentRun,
    AgentRunRequest,
    AgentRunState,
    AgentStepState,
    ReadOnlyAuthorization,
    TechnicalAction,
)
from ares.agent.store import AgentRunStore
from ares.events import AresEvent, EventBus, EventSeverity
from ares.resources.models import ResourceCandidate, ResourceKind
from ares.resources.service import ResourceResolver


class AgentOrchestratorError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class AgentOrchestrator:
    """Create, authorize and execute bounded operational recovery runs."""

    def __init__(
        self,
        *,
        store: AgentRunStore,
        resources: ResourceResolver,
        planner: Any,
        capabilities: Any,
        storage: Any,
        events: EventBus,
    ) -> None:
        self.store = store
        self.resources = resources
        self.planner = planner
        self.capabilities = capabilities
        self.storage = storage
        self.events = events

    async def start(self, request: AgentRunRequest, *, session_id: str) -> AgentRun:
        catalog = await self.resources.catalog()
        selected = self._select_resource(catalog.resources, request.resource_id)
        limitations: list[str] = []
        boot_goal = _mentions_boot(request.objective)
        if boot_goal:
            limitations.append(
                "boot.repair.grub todavía no existe; ARES puede diagnosticar evidencia "
                "de arranque de solo lectura, pero no reparar GRUB en esta fase."
            )
        if selected is None and request.resource_id is not None:
            raise AgentOrchestratorError("AGENT_RESOURCE_NOT_FOUND")
        if selected is None:
            selected = _recommended(catalog.resources)
        resources = catalog.resources
        from ares.reasoning.models import ReasoningRequest

        evidence = _evidence_from_resources(resources)
        backend_plan = self.planner.plan(
            ReasoningRequest(goal=_planner_goal(request.objective), evidence=evidence)
        )
        steps: list[AgentPlanStep] = [
            AgentPlanStep(
                id="collect-storage-evidence",
                objective="Detectar discos, particiones, filesystems, montajes y sistemas instalados.",
                state=AgentStepState.AUTHORIZATION_REQUIRED,
                capability_id="storage.disk-analysis",
                target_resource_id=selected.resource_id if selected else None,
                prerequisites=("Inventario público de hardware o probes pasivos disponibles.",),
                risk="low",
                requires_authorization=True,
                requires_protection=False,
                action=TechnicalAction(
                    capability_id="storage.disk-analysis",
                    operation_class="observe",
                    risk="low",
                    command_summary=(
                        "Ejecutar probes pasivos allowlisted de almacenamiento.",
                        "Persistir snapshot local de ARES.",
                        "Actualizar Knowledge Graph local.",
                    ),
                ),
                expected_result="Snapshot estructurado de almacenamiento y diagnóstico inicial.",
                verification="El workflow debe terminar como SUCCEEDED y producir snapshot_id.",
                rollback_strategy="No aplica: no se escriben dispositivos; solo evidencia local de ARES.",
                evidence=tuple(item.id for item in evidence),
            ),
        ]
        if boot_goal:
            steps.append(
                AgentPlanStep(
                    id="diagnose-boot",
                    objective="Diagnosticar evidencia de arranque sin modificar GRUB ni montar sistemas.",
                    state=AgentStepState.AUTHORIZATION_REQUIRED,
                    capability_id="boot.diagnose",
                    target_resource_id=selected.resource_id if selected else None,
                    prerequisites=("Snapshot de almacenamiento generado por ARES.",),
                    risk="low",
                    requires_authorization=True,
                    requires_protection=False,
                    action=TechnicalAction(
                        capability_id="boot.diagnose",
                        operation_class="observe",
                        risk="low",
                        command_summary=(
                            "Leer snapshot de almacenamiento persistido por ARES.",
                            "Determinar firmware UEFI/BIOS desde evidencia local.",
                            "Identificar sistemas instalados y candidatos EFI.",
                            "Comprobar GRUB solo si sus archivos ya están visibles.",
                        ),
                    ),
                    expected_result="BootDiagnosticResult con hallazgos, evidencia y limitaciones.",
                    verification="El workflow debe terminar como SUCCEEDED y producir findings tipados.",
                    rollback_strategy="No aplica: diagnóstico de solo lectura.",
                    dependencies=("collect-storage-evidence",),
                    evidence=tuple(item.id for item in evidence),
                )
            )
        run = AgentRun(
            objective=request.objective,
            state=AgentRunState.READ_ONLY_AUTHORIZATION_REQUIRED,
            selected_resource_id=selected.resource_id if selected else None,
            selected_resource_fingerprint=selected.stable_identity if selected else None,
            resources=resources,
            backend_plan=backend_plan.model_dump(mode="json"),
            steps=tuple(steps),
            summary="Plan propuesto. Todavía no se ha ejecutado ningún diagnóstico.",
            limitations=tuple(limitations),
            events=("agent.run.created", "agent.authorization.read_only.required"),
        )
        await self.store.put(run)
        await self._event("agent.run.created", run, session_id, {"state": run.state.value})
        return run

    async def get(self, run_id: str) -> AgentRun | None:
        return await self.store.get(run_id)

    async def list(self) -> tuple[AgentRun, ...]:
        return await self.store.list()

    async def authorize_read_only(
        self,
        run_id: str,
        payload: AgentAuthorizationRequest,
        *,
        session_id: str,
        operator: str,
    ) -> AgentRun:
        run = await self._required(run_id)
        if run.state is not AgentRunState.READ_ONLY_AUTHORIZATION_REQUIRED:
            raise AgentOrchestratorError("AGENT_AUTHORIZATION_NOT_REQUIRED")
        if not payload.confirm or payload.understood != "AUTORIZO SOLO LECTURA":
            raise AgentOrchestratorError("AGENT_AUTHORIZATION_CONFIRMATION_REQUIRED")
        catalog = await self.resources.catalog()
        selected = self._select_resource(catalog.resources, run.selected_resource_id)
        if selected is None and run.selected_resource_id is not None:
            return await self._invalidate(run, session_id, "selected_resource_missing")
        if (
            selected is not None
            and run.selected_resource_fingerprint is not None
            and selected.stable_identity != run.selected_resource_fingerprint
        ):
            return await self._invalidate(run, session_id, "selected_resource_identity_changed")
        authorization = ReadOnlyAuthorization(
            run_id=run.id,
            step_ids=tuple(step.id for step in run.steps if step.requires_authorization),
            resource_fingerprints=(
                (selected.stable_identity,) if selected is not None else ()
            ),
            granted_by=operator,
            objective=run.objective,
            target_resource_id=selected.resource_id if selected is not None else None,
            target_resource_name=selected.human_name if selected is not None else None,
            plan_fingerprint=_run_fingerprint(run, selected),
        )
        steps = tuple(
            step.model_copy(update={"state": AgentStepState.AUTHORIZED})
            if step.requires_authorization
            else step
            for step in run.steps
        )
        updated = run.model_copy(
            update={
                "state": AgentRunState.PLAN_READY,
                "updated_at": datetime.now(UTC),
                "authorization": authorization,
                "steps": steps,
                "summary": "Autorización limitada de solo lectura concedida. Aún no hay mutaciones autorizadas.",
                "events": (*run.events, "agent.authorization.read_only.granted"),
            }
        )
        await self.store.put(updated)
        await self._event(
            "agent.authorization.read_only.granted",
            updated,
            session_id,
            {
                "authorization_id": authorization.id,
                "step_ids": authorization.step_ids,
                "target_resource_id": authorization.target_resource_id,
                "target_resource_name": authorization.target_resource_name,
                "plan_fingerprint": authorization.plan_fingerprint,
                "expires_at": authorization.expires_at.isoformat(),
            },
        )
        return updated

    async def execute(self, run_id: str, *, session_id: str) -> AgentRun:
        run = await self._required(run_id)
        if run.authorization is None or run.authorization.consumed:
            raise AgentOrchestratorError("AGENT_AUTHORIZATION_REQUIRED")
        if run.authorization.expires_at <= datetime.now(UTC):
            return await self._invalidate(run, session_id, "authorization_expired")
        catalog = await self.resources.catalog()
        selected = self._select_resource(catalog.resources, run.selected_resource_id)
        if (
            selected is not None
            and run.selected_resource_fingerprint is not None
            and selected.stable_identity != run.selected_resource_fingerprint
        ):
            return await self._invalidate(run, session_id, "selected_resource_identity_changed")
        if run.authorization.plan_fingerprint != _run_fingerprint(run, selected):
            return await self._invalidate(run, session_id, "authorized_plan_fingerprint_changed")
        running = run.model_copy(
            update={
                "state": AgentRunState.RUNNING_DIAGNOSTIC,
                "updated_at": datetime.now(UTC),
                "steps": tuple(
                    step.model_copy(update={"state": AgentStepState.EXECUTING})
                    if step.id in run.authorization.step_ids
                    else step
                    for step in run.steps
                ),
                "summary": "Ejecutando revisión de solo lectura mediante backend.",
                "events": (*run.events, "agent.diagnostic.started"),
            }
        )
        await self.store.put(running)
        await self._event("agent.diagnostic.started", running, session_id, {})
        try:
            analysis = await self.storage.analyze(session_id=session_id)
        except Exception as exc:
            error_code = getattr(exc, "code", "STORAGE_ANALYSIS_FAILED")
            failed = running.model_copy(
                update={
                    "state": AgentRunState.FAILED,
                    "updated_at": datetime.now(UTC),
                    "steps": tuple(
                        step.model_copy(
                            update={
                                "state": AgentStepState.FAILED,
                                "error_code": error_code,
                            }
                        )
                        if step.id in running.authorization.step_ids
                        else step
                        for step in running.steps
                    ),
                    "summary": "El diagnóstico real falló; no se ejecutó ninguna reparación.",
                    "events": (*running.events, "agent.diagnostic.failed"),
                }
            )
            await self.store.put(failed)
            await self._event("agent.diagnostic.failed", failed, session_id, {"error_code": error_code})
            return failed
        completed_steps = [
            step.model_copy(
                update={
                    "state": AgentStepState.COMPLETED,
                    "result": {
                        "snapshot_id": analysis.snapshot_id,
                        "diagnostic_id": analysis.diagnostic_id,
                        "message": analysis.message,
                    },
                }
            )
            if step.id in running.authorization.step_ids
            else step
            for step in running.steps
        ]
        boot_failed = False
        if any(step.id == "diagnose-boot" for step in running.steps):
            try:
                boot_execution = await self.capabilities.execute(
                    "boot.diagnose",
                    {
                        "snapshot_id": analysis.snapshot_id,
                        "target_resource_id": running.selected_resource_id,
                    },
                )
                if (
                    getattr(boot_execution.status, "value", boot_execution.status) == "succeeded"
                    and boot_execution.result is not None
                ):
                    completed_steps = [
                        step.model_copy(
                            update={
                                "state": AgentStepState.COMPLETED,
                                "result": boot_execution.result,
                            }
                        )
                        if step.id == "diagnose-boot"
                        else step
                        for step in completed_steps
                    ]
                else:
                    boot_failed = True
                    completed_steps = [
                        step.model_copy(
                            update={
                                "state": AgentStepState.FAILED,
                                "error_code": boot_execution.error_code or "BOOT_DIAGNOSE_FAILED",
                            }
                        )
                        if step.id == "diagnose-boot"
                        else step
                        for step in completed_steps
                    ]
            except Exception as exc:
                boot_failed = True
                completed_steps = [
                    step.model_copy(
                        update={
                            "state": AgentStepState.FAILED,
                            "error_code": getattr(exc, "code", "BOOT_DIAGNOSE_FAILED"),
                        }
                    )
                    if step.id == "diagnose-boot"
                    else step
                    for step in completed_steps
                ]
        refreshed = await self.resources.catalog()
        completed = running.model_copy(
            update={
                "state": AgentRunState.PARTIAL
                if running.limitations or boot_failed
                else AgentRunState.COMPLETED,
                "updated_at": datetime.now(UTC),
                "resources": refreshed.resources,
                "authorization": running.authorization.model_copy(update={"consumed": True}),
                "steps": tuple(completed_steps),
                "summary": (
                    "Diagnóstico read-only completado con evidencia real. "
                    "No se ejecutó ninguna reparación."
                ),
                "events": (*running.events, "agent.diagnostic.completed"),
            }
        )
        await self.store.put(completed)
        await self._event(
            "agent.diagnostic.completed",
            completed,
            session_id,
            {"snapshot_id": analysis.snapshot_id, "diagnostic_id": analysis.diagnostic_id},
        )
        return completed

    async def cancel(self, run_id: str, *, session_id: str) -> AgentRun:
        run = await self._required(run_id)
        if run.state in {
            AgentRunState.COMPLETED,
            AgentRunState.FAILED,
            AgentRunState.CANCELLED,
            AgentRunState.INVALIDATED,
        }:
            return run
        cancelled = run.model_copy(
            update={
                "state": AgentRunState.CANCELLED,
                "updated_at": datetime.now(UTC),
                "steps": tuple(
                    step.model_copy(update={"state": AgentStepState.CANCELLED})
                    if step.state
                    in {
                        AgentStepState.DRAFT,
                        AgentStepState.READY,
                        AgentStepState.AUTHORIZATION_REQUIRED,
                        AgentStepState.AUTHORIZED,
                        AgentStepState.EXECUTING,
                    }
                    else step
                    for step in run.steps
                ),
                "summary": "Operación cancelada. No se autorizó ni ejecutó ninguna mutación.",
                "events": (*run.events, "agent.run.cancelled"),
            }
        )
        await self.store.put(cancelled)
        await self._event("agent.run.cancelled", cancelled, session_id, {})
        return cancelled

    async def _required(self, run_id: str) -> AgentRun:
        run = await self.store.get(run_id)
        if run is None:
            raise AgentOrchestratorError("AGENT_RUN_NOT_FOUND")
        return run

    async def _invalidate(self, run: AgentRun, session_id: str, reason: str) -> AgentRun:
        invalidated = run.model_copy(
            update={
                "state": AgentRunState.INVALIDATED,
                "updated_at": datetime.now(UTC),
                "steps": tuple(
                    step.model_copy(update={"state": AgentStepState.INVALIDATED})
                    for step in run.steps
                ),
                "summary": f"El plan fue invalidado antes de ejecutar: {reason}.",
                "events": (*run.events, "agent.run.invalidated"),
            }
        )
        await self.store.put(invalidated)
        await self._event("agent.run.invalidated", invalidated, session_id, {"reason": reason})
        return invalidated

    def _select_resource(
        self, resources: tuple[ResourceCandidate, ...], resource_id: str | None
    ) -> ResourceCandidate | None:
        if resource_id is None:
            return None
        return next((item for item in resources if item.resource_id == resource_id), None)

    async def _event(
        self, event_type: str, run: AgentRun, session_id: str, payload: dict[str, Any]
    ) -> None:
        await self.events.publish(
            AresEvent(
                event_type=event_type,
                source="agent-orchestrator",
                severity=EventSeverity.INFO,
                correlation_id=run.id,
                session_id=session_id,
                payload={"run_id": run.id, **payload},
            )
        )


def _recommended(resources: tuple[ResourceCandidate, ...]) -> ResourceCandidate | None:
    for kind in (ResourceKind.OPERATING_SYSTEM, ResourceKind.DISK, ResourceKind.RECOVERY_ENVIRONMENT):
        candidates = [item for item in resources if item.kind is kind and item.recommended]
        if len(candidates) == 1:
            return candidates[0]
    return next(iter(resources), None)


def _evidence_from_resources(resources: tuple[ResourceCandidate, ...]) -> tuple[object, ...]:
    from ares.reasoning.models import EvidenceFact

    facts: list[object] = []
    if any(item.kind is ResourceKind.DISK for item in resources):
        facts.append(EvidenceFact(id="hardware.block-devices", confidence=0.8))
    return tuple(facts)


def _planner_goal(objective: str) -> str:
    lowered = objective.casefold()
    if _mentions_boot(objective) and not any(
        term in lowered for term in ("disco", "disk", "storage", "particion", "filesystem")
    ):
        return f"{objective}. Analizar almacenamiento, particiones, filesystem y montajes."
    return objective


def _mentions_boot(objective: str) -> bool:
    lowered = objective.casefold()
    return any(term in lowered for term in ("arranque", "inicia", "boot", "grub", "uefi", "bios"))


def _run_fingerprint(run: AgentRun, selected: ResourceCandidate | None) -> str:
    payload = {
        "objective": run.objective,
        "selected_resource_id": run.selected_resource_id,
        "selected_resource_fingerprint": selected.stable_identity if selected else None,
        "backend_plan": run.backend_plan,
        "steps": [
            {
                "id": step.id,
                "capability_id": step.capability_id,
                "target_resource_id": step.target_resource_id,
                "requires_authorization": step.requires_authorization,
                "risk": step.risk,
                "action": step.action.model_dump(mode="json") if step.action else None,
                "expected_result": step.expected_result,
                "verification": step.verification,
                "dependencies": step.dependencies,
            }
            for step in run.steps
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
