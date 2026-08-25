"""Safe model-to-capability orchestration without exposing a shell."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

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

if TYPE_CHECKING:
    from ares.reasoning.models import EvidenceFact


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
        requested_recovery = _requested_recovery_capabilities(request.objective)
        if _mentions_boot(request.objective) and not requested_recovery:
            limitations.append(
                "boot.repair.grub (catálogo: boot.repair-grub) permanece bloqueada; "
                "este plan solo diagnostica evidencia de arranque."
            )
        for capability_id in requested_recovery:
            metadata = self.capabilities.get(capability_id)
            if metadata is None:
                limitations.append(f"{capability_id}: capability no instalada.")
            elif not metadata.enabled:
                limitations.append(
                    f"{capability_id}: "
                    f"{metadata.disabled_reason or 'provider de ejecución no disponible.'}"
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
        steps = list(
            _diagnostic_steps(
                request.objective,
                target_resource_id=selected.resource_id if selected else None,
                evidence_ids=tuple(item.id for item in evidence),
            )
        )
        steps.extend(
            _recovery_steps(
                requested_recovery,
                capabilities=self.capabilities,
                target_resource_id=selected.resource_id if selected else None,
                evidence_ids=tuple(item.id for item in evidence),
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
            step_ids=tuple(
                step.id
                for step in run.steps
                if step.requires_authorization
                and step.action is not None
                and step.action.operation_class == "observe"
            ),
            capability_ids=tuple(
                step.capability_id
                for step in run.steps
                if step.requires_authorization
                and step.capability_id is not None
                and step.action is not None
                and step.action.operation_class == "observe"
            ),
            resource_fingerprints=((selected.stable_identity,) if selected is not None else ()),
            granted_by=operator,
            objective=run.objective,
            target_resource_id=selected.resource_id if selected is not None else None,
            target_resource_name=selected.human_name if selected is not None else None,
            plan_fingerprint=_run_fingerprint(run, selected),
        )
        steps = tuple(
            step.model_copy(update={"state": AgentStepState.AUTHORIZED})
            if step.id in authorization.step_ids
            else step
            for step in run.steps
        )
        updated = run.model_copy(
            update={
                "state": AgentRunState.PLAN_READY,
                "updated_at": datetime.now(UTC),
                "authorization": authorization,
                "steps": steps,
                "summary": (
                    "Autorización limitada de solo lectura concedida. "
                    "Aún no hay mutaciones autorizadas."
                ),
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
        authorization = run.authorization
        if authorization.expires_at <= datetime.now(UTC):
            return await self._invalidate(run, session_id, "authorization_expired")
        catalog = await self.resources.catalog()
        selected = self._select_resource(catalog.resources, run.selected_resource_id)
        if (
            selected is not None
            and run.selected_resource_fingerprint is not None
            and selected.stable_identity != run.selected_resource_fingerprint
        ):
            return await self._invalidate(run, session_id, "selected_resource_identity_changed")
        if authorization.plan_fingerprint != _run_fingerprint(run, selected):
            return await self._invalidate(run, session_id, "authorized_plan_fingerprint_changed")
        running = run.model_copy(
            update={
                "state": AgentRunState.RUNNING_DIAGNOSTIC,
                "updated_at": datetime.now(UTC),
                "steps": tuple(
                    step.model_copy(update={"state": AgentStepState.EXECUTING})
                    if step.id in authorization.step_ids
                    else step
                    for step in run.steps
                ),
                "summary": "Ejecutando revisión de solo lectura mediante backend.",
                "events": (*run.events, "agent.diagnostic.started"),
            }
        )
        await self.store.put(running)
        await self._event("agent.diagnostic.started", running, session_id, {})
        completed_steps = list(running.steps)
        snapshot_id: str | None = None
        diagnostic_id: str | None = None
        succeeded: list[str] = []
        failed_capabilities: list[str] = []
        storage_failed = False
        for index, step in enumerate(completed_steps):
            if step.id not in authorization.step_ids or step.capability_id is None:
                continue
            capability_id = step.capability_id
            if storage_failed:
                completed_steps[index] = step.model_copy(
                    update={
                        "state": AgentStepState.BLOCKED,
                        "error_code": "DIAGNOSTIC_DEPENDENCY_FAILED",
                    }
                )
                failed_capabilities.append(capability_id)
                continue
            try:
                if capability_id == "storage.disk-analysis":
                    analysis = await self.storage.analyze(session_id=session_id)
                    snapshot_id = analysis.snapshot_id
                    diagnostic_id = analysis.diagnostic_id
                    result: dict[str, object] = {
                        "snapshot_id": analysis.snapshot_id,
                        "diagnostic_id": analysis.diagnostic_id,
                        "message": analysis.message,
                    }
                else:
                    execution = await self.capabilities.execute(
                        capability_id,
                        _capability_payload(
                            capability_id,
                            snapshot_id=snapshot_id,
                            target_resource_id=running.selected_resource_id,
                        ),
                    )
                    if (
                        getattr(execution.status, "value", execution.status) != "succeeded"
                        or execution.result is None
                    ):
                        raise AgentOrchestratorError(
                            execution.error_code or "DIAGNOSTIC_CAPABILITY_FAILED"
                        )
                    result = execution.result
                completed_steps[index] = step.model_copy(
                    update={"state": AgentStepState.COMPLETED, "result": result}
                )
                succeeded.append(capability_id)
            except Exception as exc:
                error_code = getattr(exc, "code", "DIAGNOSTIC_CAPABILITY_FAILED")
                completed_steps[index] = step.model_copy(
                    update={"state": AgentStepState.FAILED, "error_code": error_code}
                )
                failed_capabilities.append(capability_id)
                if capability_id == "storage.disk-analysis":
                    storage_failed = True
            progress = running.model_copy(
                update={
                    "updated_at": datetime.now(UTC),
                    "steps": tuple(completed_steps),
                    "summary": (
                        f"Diagnósticos completados: {len(succeeded)} "
                        f"de {len(running.steps)}."
                    ),
                }
            )
            await self.store.put(progress)
        refreshed = await self.resources.catalog()
        if not succeeded:
            final_state = AgentRunState.FAILED
        elif failed_capabilities or running.limitations:
            final_state = AgentRunState.PARTIAL
        else:
            final_state = AgentRunState.COMPLETED
        completed = running.model_copy(
            update={
                "state": final_state,
                "updated_at": datetime.now(UTC),
                "resources": refreshed.resources,
                "authorization": authorization.model_copy(update={"consumed": True}),
                "steps": tuple(completed_steps),
                "summary": (
                    f"Diagnóstico read-only: {len(succeeded)} capability(s) completada(s), "
                    f"{len(failed_capabilities)} fallida(s) o bloqueada(s). "
                    "No se ejecutó ninguna reparación."
                ),
                "events": (
                    *running.events,
                    "agent.diagnostic.completed"
                    if final_state is not AgentRunState.FAILED
                    else "agent.diagnostic.failed",
                ),
            }
        )
        await self.store.put(completed)
        await self._event(
            "agent.diagnostic.failed"
            if final_state is AgentRunState.FAILED
            else "agent.diagnostic.completed",
            completed,
            session_id,
            {
                "snapshot_id": snapshot_id,
                "diagnostic_id": diagnostic_id,
                "completed_capabilities": succeeded,
                "failed_capabilities": failed_capabilities,
            },
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
    for kind in (
        ResourceKind.OPERATING_SYSTEM,
        ResourceKind.DISK,
        ResourceKind.RECOVERY_ENVIRONMENT,
    ):
        candidates = [item for item in resources if item.kind is kind and item.recommended]
        if len(candidates) == 1:
            return candidates[0]
    return next(iter(resources), None)


def _diagnostic_steps(
    objective: str,
    *,
    target_resource_id: str | None,
    evidence_ids: tuple[str, ...],
) -> tuple[AgentPlanStep, ...]:
    requested = _requested_capabilities(objective)
    details: dict[str, dict[str, object]] = {
        "storage.disk-analysis": {
            "step_id": "collect-storage-evidence",
            "objective": "Detectar discos, particiones, filesystems y sistemas instalados.",
            "summary": (
                "Ejecutar probes pasivos allowlisted de almacenamiento.",
                "Persistir snapshot local de ARES.",
                "Actualizar Knowledge Graph local.",
            ),
            "result": "Snapshot estructurado de almacenamiento y diagnóstico inicial.",
            "verification": "El workflow debe producir snapshot_id mediante evidencia real.",
            "dependencies": (),
        },
        "storage.space-analysis": {
            "step_id": "analyze-storage-space",
            "objective": "Medir capacidad, inodos y categorías que consumen espacio sin borrar.",
            "summary": (
                "Consultar statvfs del target resuelto por ARES.",
                "Recorrer metadatos con límites, sin seguir symlinks ni leer contenido personal.",
                "Estimar categorías recuperables sin ejecutar limpieza.",
            ),
            "result": "SpaceAnalysisResult con consumidores, estimaciones y limitaciones.",
            "verification": "El resultado debe incluir fingerprint, evidencia y métricas tipadas.",
            "dependencies": ("collect-storage-evidence",),
        },
        "system.memory-analysis": {
            "step_id": "analyze-system-memory",
            "objective": "Distinguir presión real, caché, swap y evidencia histórica OOM.",
            "summary": (
                "Normalizar métricas /proc únicamente para el runtime accesible.",
                "Separar explícitamente evidencia histórica de un target offline.",
                "No terminar procesos, vaciar cachés ni cambiar sysctl.",
            ),
            "result": "MemoryAnalysisResult con scope runtime u offline inequívoco.",
            "verification": "No debe atribuir métricas actuales a un sistema offline.",
            "dependencies": ("collect-storage-evidence",),
        },
        "packages.health-check": {
            "step_id": "check-package-health",
            "objective": "Revisar coherencia dpkg/APT sin red, locks de escritura ni scripts.",
            "summary": (
                "Leer metadatos locales dpkg/APT de forma acotada.",
                "Detectar estados incompletos y actualizaciones interrumpidas.",
                "No ejecutar apt update, install, remove ni dpkg --configure.",
            ),
            "result": "PackageHealthResult con estado y evidencia normalizada.",
            "verification": "El target debe conservarse sin cambios y sin conexiones de red.",
            "dependencies": ("collect-storage-evidence",),
        },
        "services.failure-analysis": {
            "step_id": "analyze-service-failures",
            "objective": "Identificar unidades fallidas y evidencia persistente sin reiniciarlas.",
            "summary": (
                "Consultar el estado systemd con una invocación fija allowlisted.",
                "Tratar logs y nombres de unidad como datos no confiables.",
                "No reiniciar, habilitar, deshabilitar ni modificar servicios.",
            ),
            "result": "ServiceFailureResult con unidades y hallazgos acotados.",
            "verification": "La salida debe estar normalizada y libre de comandos arbitrarios.",
            "dependencies": ("collect-storage-evidence",),
        },
        "boot.diagnose": {
            "step_id": "diagnose-boot",
            "objective": "Diagnosticar evidencia de arranque sin modificar GRUB ni NVRAM.",
            "summary": (
                "Leer snapshot de almacenamiento persistido por ARES.",
                "Determinar firmware UEFI/BIOS desde evidencia local.",
                "Identificar sistemas instalados, candidatos EFI y GRUB visible.",
            ),
            "result": "BootDiagnosticResult con hallazgos, evidencia y limitaciones.",
            "verification": "El workflow debe producir findings tipados sin reparar.",
            "dependencies": ("collect-storage-evidence",),
        },
    }
    steps: list[AgentPlanStep] = []
    for capability_id in requested:
        item = details[capability_id]
        steps.append(
            AgentPlanStep(
                id=str(item["step_id"]),
                objective=str(item["objective"]),
                state=AgentStepState.AUTHORIZATION_REQUIRED,
                capability_id=capability_id,
                target_resource_id=target_resource_id,
                prerequisites=("Target identificado por ARES y autorización contextual vigente.",),
                risk="low",
                requires_authorization=True,
                requires_protection=False,
                action=TechnicalAction(
                    capability_id=capability_id,
                    operation_class="observe",
                    risk="low",
                    privileged=True,
                    command_summary=tuple(item["summary"]),  # type: ignore[arg-type]
                    expected_changes="Ninguno sobre el target; solo evidencia local de ARES.",
                ),
                expected_result=str(item["result"]),
                verification=str(item["verification"]),
                rollback_strategy="No aplica: diagnóstico de solo lectura.",
                dependencies=tuple(item["dependencies"]),  # type: ignore[arg-type]
                evidence=evidence_ids,
            )
        )
    return tuple(steps)


def _requested_capabilities(objective: str) -> tuple[str, ...]:
    lowered = objective.casefold()
    comprehensive = any(
        term in lowered
        for term in (
            "diagnóstico completo",
            "diagnostico completo",
            "analiza todo",
            "salud del sistema",
        )
    )
    requested = ["storage.disk-analysis"]
    groups = (
        (
            "storage.space-analysis",
            (
                "espacio",
                "disco lleno",
                "archivos grandes",
                "liberar",
                "caché",
                "cache",
                "disk usage",
            ),
        ),
        ("system.memory-analysis", ("memoria", " ram", "swap", "oom", "memory")),
        (
            "packages.health-check",
            ("paquete", "apt", "dpkg", "dependencia", "actualización", "actualizacion"),
        ),
        (
            "services.failure-analysis",
            ("servicio", "systemd", "daemon", "unidades fallidas", "failed service"),
        ),
        ("boot.diagnose", ("arranque", "no inicia", "boot", "grub", "uefi", "bios")),
    )
    for capability_id, terms in groups:
        if comprehensive or any(term in lowered for term in terms):
            requested.append(capability_id)
    return tuple(requested)


def _requested_recovery_capabilities(objective: str) -> tuple[str, ...]:
    lowered = objective.casefold()
    requested: list[str] = []
    groups = (
        (
            "boot.repair-grub",
            ("reparar grub", "reinstalar grub", "regenerar grub", "grub roto"),
        ),
        (
            "boot.repair-efi-entry",
            ("reparar entrada efi", "reparar uefi", "entrada uefi", "bootorder"),
        ),
        (
            "boot.rebuild-initramfs",
            ("reconstruir initramfs", "regenerar initramfs", "rebuild initramfs"),
        ),
        (
            "boot.repair-fstab",
            ("reparar fstab", "corregir fstab", "fstab inválido", "fstab invalido"),
        ),
        (
            "kernel.rollback",
            ("rollback kernel", "volver al kernel", "kernel anterior", "último kernel funcional"),
        ),
        (
            "kernel.reinstall",
            ("reinstalar kernel", "reparar kernel", "reinstalar módulos del kernel"),
        ),
        (
            "packages.rollback",
            (
                "rollback paquete",
                "revertir actualización",
                "revertir actualizacion",
                "deshacer actualización",
                "deshacer actualizacion",
            ),
        ),
        (
            "services.restore-configuration",
            (
                "restaurar configuración del servicio",
                "restaurar configuracion del servicio",
                "recuperar configuración del servicio",
            ),
        ),
        (
            "network.restore-configuration",
            (
                "restaurar red",
                "restaurar configuración de red",
                "recuperar configuración de red",
            ),
        ),
        (
            "files.recover",
            ("recuperar archivos borrados", "recuperar archivo borrado", "undelete"),
        ),
        (
            "system.rollback-checkpoint",
            ("rollback checkpoint", "restaurar checkpoint", "volver al checkpoint"),
        ),
        (
            "user.account-recovery",
            (
                "recuperar cuenta",
                "recuperar contraseña",
                "recuperar contrasena",
                "desbloquear usuario",
            ),
        ),
    )
    for capability_id, terms in groups:
        if any(term in lowered for term in terms):
            requested.append(capability_id)
    return tuple(requested)


def _recovery_steps(
    requested: tuple[str, ...],
    *,
    capabilities: Any,
    target_resource_id: str | None,
    evidence_ids: tuple[str, ...],
) -> tuple[AgentPlanStep, ...]:
    steps: list[AgentPlanStep] = []
    for index, capability_id in enumerate(requested, start=1):
        metadata = capabilities.get(capability_id)
        if metadata is None:
            continue
        steps.append(
            AgentPlanStep(
                id=f"recovery-{index}-{capability_id.replace('.', '-').replace('_', '-')}",
                objective=metadata.objective,
                state=AgentStepState.BLOCKED,
                capability_id=capability_id,
                target_resource_id=target_resource_id,
                prerequisites=(
                    "Diagnóstico y target exacto verificados por ARES.",
                    "ProtectionCheckpoint READY ligado al fingerprint del target.",
                    "Autorización de mutación de un solo uso ligada al plan.",
                    "Broker privilegiado específico instalado y saludable.",
                    f"Evidencia requerida: {', '.join(metadata.required_evidence)}.",
                ),
                risk=metadata.risk.value,
                requires_authorization=True,
                requires_protection=True,
                action=TechnicalAction(
                    capability_id=capability_id,
                    operation_class=metadata.operation.value,
                    risk=metadata.risk.value,
                    privileged=True,
                    command_summary=(
                        "Revalidar fingerprint, plan y checkpoint en backend.",
                        "Solicitar autorización contextual para la mutación exacta.",
                        "Delegar únicamente al broker dedicado y verificar el resultado.",
                    ),
                    expected_changes=metadata.description,
                ),
                expected_result=(
                    "Resultado tipado con cambios, evidencia de verificación y rollback."
                ),
                verification="Los postchecks declarados deben completarse antes de reportar éxito.",
                rollback_strategy=metadata.rollback.strategy,
                dependencies=metadata.dependencies,
                evidence=evidence_ids,
                error_code="RECOVERY_PROVIDER_UNAVAILABLE",
            )
        )
    return tuple(steps)


def _capability_payload(
    capability_id: str,
    *,
    snapshot_id: str | None,
    target_resource_id: str | None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "snapshot_id": snapshot_id,
        "target_resource_id": target_resource_id,
    }
    if capability_id == "storage.space-analysis":
        payload["analysis_depth"] = "standard"
    return payload


def _evidence_from_resources(
    resources: tuple[ResourceCandidate, ...],
) -> tuple[EvidenceFact, ...]:
    from ares.reasoning.models import EvidenceFact

    facts: list[EvidenceFact] = []
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
