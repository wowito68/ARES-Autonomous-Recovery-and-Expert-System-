"""Safe model-to-capability orchestration without exposing a shell."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import MutableSequence
from datetime import UTC, datetime, timedelta
from typing import Any

from ares.agent.models import (
    AgentAuthorizationRequest,
    AgentAutonomyLimits,
    AgentPlanStep,
    AgentProposal,
    AgentProposalType,
    AgentRun,
    AgentRunRequest,
    AgentRunState,
    AgentStepState,
    AgentTimelineEntry,
    AuthorizationEnvelope,
    AuthorizationEnvelopeStatus,
    CapabilityInvocationRecord,
    ReadOnlyAuthorization,
    TechnicalAction,
)
from ares.agent.store import AgentRunStore
from ares.backup import (
    BackupCreateRequest,
    BackupPlan,
    BackupPlanRequest,
    BackupService,
    BackupStatus,
    BackupVerificationStatus,
)
from ares.events import AresEvent, EventBus, EventSeverity
from ares.llm import AIRuntime, AIRuntimeError
from ares.reasoning.models import EvidenceFact, ReasoningRequest
from ares.resources.models import ResourceCandidate, ResourceKind
from ares.resources.service import ResourceResolver

_READ_ONLY_CONFIRMATION = "AUTORIZO SOLO LECTURA"
_MUTATION_CONFIRMATION = "AUTORIZO BACKUP"
_TERMINAL_STATES = {
    AgentRunState.COMPLETED,
    AgentRunState.PARTIAL,
    AgentRunState.BLOCKED,
    AgentRunState.FAILED,
    AgentRunState.CANCELLED,
    AgentRunState.EXPIRED,
    AgentRunState.INVALIDATED,
}


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
        backup: BackupService | None = None,
        ai_runtime: AIRuntime | None = None,
    ) -> None:
        self.store = store
        self.resources = resources
        self.planner = planner
        self.capabilities = capabilities
        self.storage = storage
        self.events = events
        self.backup = backup
        self.ai_runtime = ai_runtime

    async def start(self, request: AgentRunRequest, *, session_id: str) -> AgentRun:
        catalog = await self.resources.catalog()
        selected = self._select_resource(catalog.resources, request.resource_id)
        if selected is None and request.resource_id is not None:
            raise AgentOrchestratorError("AGENT_RESOURCE_NOT_FOUND")
        if selected is None:
            selected = _recommended(catalog.resources)

        proposal = await self._proposal_for_objective(
            request,
            resources=catalog.resources,
            selected=selected,
        )
        if proposal.proposal_type is AgentProposalType.ASK_USER:
            run = AgentRun(
                objective=request.objective,
                state=AgentRunState.WAITING_FOR_USER,
                selected_resource_id=selected.resource_id if selected else None,
                selected_resource_fingerprint=selected.stable_identity if selected else None,
                resources=catalog.resources,
                proposal=proposal,
                steps=(),
                summary=proposal.user_question
                or "ARES necesita una selección explícita antes de preparar un plan.",
                events=("agent.run.created", "agent.user_input.required"),
            )
            run = self._append_timeline(
                run,
                "agent.user_input.required",
                run.state,
                proposal.reason,
            )
            await self.store.put(run)
            await self._event("agent.run.created", run, session_id, {"state": run.state.value})
            return run

        if proposal.selected_capability_id == "backup.create":
            return await self._start_backup(
                request, catalog.resources, selected, proposal, session_id
            )
        return await self._start_read_only(
            request, catalog.resources, selected, proposal, session_id
        )

    async def get(self, run_id: str) -> AgentRun | None:
        return await self.store.get(run_id)

    async def list(self) -> tuple[AgentRun, ...]:
        return await self.store.list()

    async def timeline(self, run_id: str) -> tuple[AgentTimelineEntry, ...]:
        run = await self._required(run_id)
        return run.timeline

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
        if not payload.confirm or payload.understood != _READ_ONLY_CONFIRMATION:
            raise AgentOrchestratorError("AGENT_AUTHORIZATION_CONFIRMATION_REQUIRED")
        return await self._authorize_envelope(
            run,
            session_id=session_id,
            operator=operator,
            confirmation_kind="read_only",
        )

    async def authorize_mutation(
        self,
        run_id: str,
        payload: AgentAuthorizationRequest,
        *,
        session_id: str,
        operator: str,
    ) -> AgentRun:
        del operator
        run = await self._required(run_id)
        if run.state is not AgentRunState.MUTATION_AUTHORIZATION_REQUIRED:
            raise AgentOrchestratorError("AGENT_AUTHORIZATION_NOT_REQUIRED")
        if not payload.confirm or payload.understood != _MUTATION_CONFIRMATION:
            raise AgentOrchestratorError("AGENT_AUTHORIZATION_CONFIRMATION_REQUIRED")
        return await self._authorize_envelope(
            run,
            session_id=session_id,
            operator="local-user",
            confirmation_kind="mutation",
        )

    async def continue_run(self, run_id: str, *, session_id: str) -> AgentRun:
        return await self.execute(run_id, session_id=session_id)

    async def execute(self, run_id: str, *, session_id: str) -> AgentRun:
        run = await self._required(run_id)
        if run.state not in {AgentRunState.PLAN_READY, AgentRunState.AUTHORIZED}:
            raise AgentOrchestratorError("AGENT_AUTHORIZATION_REQUIRED")
        envelope = self._authorized_envelope(run)
        self._validate_invocation_limits(run)
        if "backup.create" in envelope.allowed_capabilities:
            return await self._execute_backup(run, envelope, session_id)
        return await self._execute_read_only(run, envelope, session_id)

    async def cancel(self, run_id: str, *, session_id: str) -> AgentRun:
        run = await self._required(run_id)
        if run.state in _TERMINAL_STATES:
            return run
        envelope = run.authorization_envelope
        if envelope is not None and envelope.status in {
            AuthorizationEnvelopeStatus.AUTHORIZATION_REQUIRED,
            AuthorizationEnvelopeStatus.AUTHORIZED,
        }:
            envelope = envelope.model_copy(update={"status": AuthorizationEnvelopeStatus.REVOKED})
        cancelled = run.model_copy(
            update={
                "state": AgentRunState.CANCELLED,
                "updated_at": datetime.now(UTC),
                "authorization_envelope": envelope,
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
                "summary": (
                    "Operación cancelada. No se autorizó ni ejecutó ninguna mutación pendiente."
                ),
                "events": (*run.events, "agent.run.cancelled"),
            }
        )
        cancelled = self._append_timeline(
            cancelled,
            "agent.run.cancelled",
            AgentRunState.CANCELLED,
            "Cancelación solicitada por el usuario.",
        )
        await self.store.put(cancelled)
        await self._event("agent.run.cancelled", cancelled, session_id, {})
        return cancelled

    async def _start_read_only(
        self,
        request: AgentRunRequest,
        resources: tuple[ResourceCandidate, ...],
        selected: ResourceCandidate | None,
        proposal: AgentProposal,
        session_id: str,
    ) -> AgentRun:
        limitations: list[str] = []
        boot_goal = _mentions_boot(request.objective)
        if boot_goal:
            limitations.append(
                "boot.repair.grub todavía no existe; ARES puede diagnosticar evidencia "
                "de arranque de solo lectura, pero no reparar GRUB en esta fase."
            )
        evidence = _evidence_from_resources(resources)
        backend_plan = self.planner.plan(
            ReasoningRequest(goal=_planner_goal(request.objective), evidence=evidence)
        )
        steps = [_storage_step(selected, evidence)]
        if boot_goal:
            steps.append(_boot_step(selected, evidence))
        run = AgentRun(
            objective=request.objective,
            state=AgentRunState.READ_ONLY_AUTHORIZATION_REQUIRED,
            selected_resource_id=selected.resource_id if selected else None,
            selected_resource_fingerprint=selected.stable_identity if selected else None,
            resources=resources,
            backend_plan=backend_plan.model_dump(mode="json"),
            proposal=proposal,
            steps=tuple(steps),
            summary="Plan propuesto. Todavía no se ha ejecutado ningún diagnóstico.",
            limitations=tuple(limitations),
            events=("agent.run.created", "agent.authorization.read_only.required"),
        )
        envelope = self._build_envelope(
            run,
            selected_resources=(selected,) if selected else (),
            allowed_capabilities=tuple(
                step.capability_id for step in run.steps if step.capability_id
            ),
            risk_ceiling="low",
            maximum_bytes_written=0,
            maximum_bytes_deleted=0,
            maximum_downtime_seconds=0,
            rollback_policy="No aplica: las acciones autorizadas son de observación.",
            verification_requirements=(
                "storage.disk-analysis debe producir snapshot_id y diagnostic_id.",
                "Cada capability debe devolver estado tipado; el modelo no declara éxito.",
            ),
        )
        run = run.model_copy(update={"authorization_envelope": envelope})
        run = self._append_timeline(
            run,
            "agent.authorization.read_only.required",
            run.state,
            "ARES preparó un envelope de solo lectura; falta autorización explícita.",
            evidence_ids=tuple(item.id for item in evidence),
        )
        await self.store.put(run)
        await self._event("agent.run.created", run, session_id, {"state": run.state.value})
        return run

    async def _start_backup(
        self,
        request: AgentRunRequest,
        resources: tuple[ResourceCandidate, ...],
        selected: ResourceCandidate | None,
        proposal: AgentProposal,
        session_id: str,
    ) -> AgentRun:
        if self.backup is None:
            raise AgentOrchestratorError("AGENT_CAPABILITY_UNAVAILABLE")
        destination = self._select_resource(resources, request.destination_resource_id)
        if destination is None and request.destination_resource_id is not None:
            raise AgentOrchestratorError("AGENT_RESOURCE_NOT_FOUND")
        if selected is None or destination is None:
            ask = proposal.model_copy(
                update={
                    "proposal_type": AgentProposalType.ASK_USER,
                    "needs_user_input": True,
                    "user_question": (
                        "Elige un recurso origen y un destino externo/montado para preparar "
                        "el backup. ARES no acepta rutas escritas por el modelo ni por el chat."
                    ),
                    "selected_capability_id": "backup.create",
                }
            )
            run = AgentRun(
                objective=request.objective,
                state=AgentRunState.WAITING_FOR_USER,
                selected_resource_id=selected.resource_id if selected else None,
                selected_resource_fingerprint=selected.stable_identity if selected else None,
                resources=resources,
                proposal=ask,
                summary=ask.user_question or "Falta origen o destino explícito para backup.",
                events=("agent.run.created", "agent.user_input.required"),
            )
            run = self._append_timeline(
                run,
                "agent.user_input.required",
                run.state,
                "Backup requiere origen y destino seleccionados como recursos.",
            )
            await self.store.put(run)
            await self._event("agent.run.created", run, session_id, {"state": run.state.value})
            return run

        source_path = _backup_path(selected, role="source")
        destination_path = _backup_path(destination, role="destination")
        if source_path is None or destination_path is None:
            raise AgentOrchestratorError("AGENT_BACKUP_RESOURCE_NOT_BACKUPPABLE")
        try:
            backup_plan = await self.backup.plan(
                BackupPlanRequest(source=source_path, destination=destination_path),
                session_id=session_id,
            )
        except Exception as exc:
            raise AgentOrchestratorError(getattr(exc, "code", "AGENT_BACKUP_PLAN_FAILED")) from exc

        steps = (
            AgentPlanStep(
                id="create-verified-backup",
                objective=(
                    "Crear un respaldo verificado del recurso origen en el destino seleccionado."
                ),
                state=AgentStepState.AUTHORIZATION_REQUIRED,
                capability_id="backup.create",
                target_resource_id=selected.resource_id,
                prerequisites=(
                    "BackupPlan estructurado vigente.",
                    "Consentimiento local del broker.",
                ),
                risk="medium",
                requires_authorization=True,
                requires_protection=False,
                action=TechnicalAction(
                    capability_id="backup.create",
                    operation_class="protect",
                    risk="medium",
                    privileged=True,
                    commands_visible=False,
                    command_summary=(
                        "Copiar archivos incluidos en el plan de backup.",
                        "Generar manifest con hashes SHA-256.",
                    ),
                    expected_changes="Escritura de una carpeta de backup nueva en el destino.",
                ),
                expected_result="Backup completado con manifest y checksum.",
                verification=(
                    "backup.create debe terminar COMPLETED y verification_status VERIFIED."
                ),
                rollback_strategy=(
                    "No sobrescribir; si falla, conservar evidencia y permitir inspección manual."
                ),
                evidence=(backup_plan.id,),
                result={"backup_plan": _public_backup_plan(backup_plan)},
            ),
            AgentPlanStep(
                id="verify-backup",
                objective="Verificar integridad del respaldo creado.",
                state=AgentStepState.AUTHORIZATION_REQUIRED,
                capability_id="backup.verify",
                target_resource_id=destination.resource_id,
                prerequisites=("Backup creado por backup.create.",),
                risk="low",
                requires_authorization=True,
                requires_protection=False,
                action=TechnicalAction(
                    capability_id="backup.verify",
                    operation_class="verify",
                    risk="low",
                    privileged=False,
                    commands_visible=False,
                    command_summary=("Comparar manifest, tamaños y hashes SHA-256.",),
                ),
                expected_result="BackupVerification VERIFIED.",
                verification="La verificación estructurada debe confirmar manifest y checksums.",
                rollback_strategy="No aplica: verificación de solo lectura sobre el backup.",
                dependencies=("create-verified-backup",),
                evidence=(backup_plan.id,),
            ),
        )
        backend_plan = {
            "kind": "agent.backup-plan",
            "backup_plan_id": backup_plan.id,
            "backup_id": backup_plan.backup_id,
            "plan_fingerprint_sha256": backup_plan.fingerprint_sha256,
            "source_resource_id": selected.resource_id,
            "destination_resource_id": destination.resource_id,
            "estimated_bytes": backup_plan.source.estimated_size_bytes,
            "required_bytes": backup_plan.required_bytes,
        }
        run = AgentRun(
            objective=request.objective,
            state=AgentRunState.MUTATION_AUTHORIZATION_REQUIRED,
            selected_resource_id=selected.resource_id,
            selected_resource_fingerprint=selected.stable_identity,
            resources=resources,
            backend_plan=backend_plan,
            proposal=proposal,
            limits=AgentAutonomyLimits(
                risk_ceiling="medium",
                allowed_targets=(selected.resource_id, destination.resource_id),
                allowed_capabilities=("backup.create", "backup.verify"),
            ),
            steps=steps,
            summary=(
                "Plan de backup preparado. ARES aún no ha escrito datos; falta autorización "
                "explícita y consentimiento local independiente."
            ),
            events=("agent.run.created", "agent.authorization.mutation.required"),
        )
        envelope = self._build_envelope(
            run,
            selected_resources=(selected, destination),
            allowed_capabilities=("backup.create", "backup.verify"),
            risk_ceiling="medium",
            maximum_bytes_written=backup_plan.required_bytes,
            maximum_bytes_deleted=0,
            maximum_downtime_seconds=0,
            protected_paths=("/boot", "/boot/efi", "/etc", "/home"),
            rollback_policy=(
                "No sobrescribir datos existentes; detener ante error y conservar "
                "manifest/parciales "
                "para inspección. No se autorizan borrados automáticos."
            ),
            verification_requirements=(
                "backup.create debe crear manifest SHA-256.",
                "backup.verify debe terminar VERIFIED antes de declarar éxito.",
            ),
        )
        run = run.model_copy(update={"authorization_envelope": envelope})
        run = self._append_timeline(
            run,
            "agent.authorization.mutation.required",
            run.state,
            "ARES preparó un envelope acotado para backup; falta AUTORIZO BACKUP.",
            evidence_ids=(backup_plan.id,),
        )
        await self.store.put(run)
        await self._event(
            "agent.authorization.mutation.required",
            run,
            session_id,
            {
                "backup_plan_id": backup_plan.id,
                "backup_id": backup_plan.backup_id,
                "source_resource_id": selected.resource_id,
                "destination_resource_id": destination.resource_id,
            },
        )
        return run

    async def _authorize_envelope(
        self,
        run: AgentRun,
        *,
        session_id: str,
        operator: str,
        confirmation_kind: str,
    ) -> AgentRun:
        envelope = run.authorization_envelope
        if envelope is None:
            raise AgentOrchestratorError("AGENT_AUTHORIZATION_ENVELOPE_REQUIRED")
        if envelope.expires_at <= datetime.now(UTC):
            return await self._invalidate(run, session_id, "authorization_envelope_expired")
        catalog = await self.resources.catalog()
        selected_resources = tuple(
            resource
            for resource_id in envelope.target_resource_ids
            if (resource := self._select_resource(catalog.resources, resource_id)) is not None
        )
        if len(selected_resources) != len(envelope.target_resource_ids):
            return await self._invalidate(run, session_id, "target_resource_missing")
        current_fingerprints = tuple(resource.stable_identity for resource in selected_resources)
        if current_fingerprints != envelope.target_fingerprints:
            return await self._invalidate(run, session_id, "target_resource_identity_changed")
        if envelope.plan_fingerprint != _run_fingerprint(run, selected_resources):
            return await self._invalidate(run, session_id, "authorized_plan_fingerprint_changed")

        authorization = run.authorization
        if confirmation_kind == "read_only":
            primary = selected_resources[0] if selected_resources else None
            authorization = ReadOnlyAuthorization(
                run_id=run.id,
                step_ids=tuple(step.id for step in run.steps if step.requires_authorization),
                resource_fingerprints=current_fingerprints,
                granted_by=operator,
                objective=run.objective,
                target_resource_id=primary.resource_id if primary is not None else None,
                target_resource_name=primary.human_name if primary is not None else None,
                plan_fingerprint=envelope.plan_fingerprint,
            )
        envelope = envelope.model_copy(update={"status": AuthorizationEnvelopeStatus.AUTHORIZED})
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
                "authorization_envelope": envelope,
                "steps": steps,
                "summary": (
                    "Autorización acotada concedida. ARES ejecutará solo las capabilities "
                    "incluidas en el envelope."
                ),
                "events": (*run.events, f"agent.authorization.{confirmation_kind}.granted"),
            }
        )
        updated = self._append_timeline(
            updated,
            f"agent.authorization.{confirmation_kind}.granted",
            AgentRunState.PLAN_READY,
            "El usuario concedió la autorización exacta requerida por el envelope.",
        )
        await self.store.put(updated)
        await self._event(
            f"agent.authorization.{confirmation_kind}.granted",
            updated,
            session_id,
            {
                "authorization_envelope_id": envelope.id,
                "allowed_capabilities": envelope.allowed_capabilities,
                "target_resource_ids": envelope.target_resource_ids,
                "plan_fingerprint": envelope.plan_fingerprint,
                "expires_at": envelope.expires_at.isoformat(),
            },
        )
        return updated

    async def _execute_read_only(
        self, run: AgentRun, envelope: AuthorizationEnvelope, session_id: str
    ) -> AgentRun:
        running = self._mark_running(
            run,
            "agent.diagnostic.started",
            "Ejecutando revisión de solo lectura mediante backend.",
        )
        await self.store.put(running)
        await self._event("agent.diagnostic.started", running, session_id, {})
        invocations: list[CapabilityInvocationRecord] = list(running.capability_invocations)
        try:
            storage_record = self._invocation_started(
                running,
                "collect-storage-evidence",
                "storage.disk-analysis",
                envelope,
                {"session_id": session_id},
            )
            invocations.append(storage_record)
            analysis = await self.storage.analyze(session_id=session_id)
            invocations[-1] = storage_record.model_copy(
                update={
                    "finished_at": datetime.now(UTC),
                    "status": "SUCCEEDED",
                    "verification_status": "VERIFIED",
                    "evidence_ids": (analysis.snapshot_id, analysis.diagnostic_id),
                }
            )
        except Exception as exc:
            error_code = getattr(exc, "code", "STORAGE_ANALYSIS_FAILED")
            failed = self._mark_failed(
                running,
                "agent.diagnostic.failed",
                "El diagnóstico real falló; no se ejecutó ninguna reparación.",
                error_code,
                invocations,
            )
            await self.store.put(failed)
            await self._event(
                "agent.diagnostic.failed", failed, session_id, {"error_code": error_code}
            )
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
            if step.id == "collect-storage-evidence"
            else step
            for step in running.steps
        ]
        boot_failed = False
        if "boot.diagnose" in envelope.allowed_capabilities:
            boot_record = self._invocation_started(
                running,
                "diagnose-boot",
                "boot.diagnose",
                envelope,
                {
                    "snapshot_id": analysis.snapshot_id,
                    "target_resource_id": running.selected_resource_id,
                },
            )
            invocations.append(boot_record)
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
                    invocations[-1] = boot_record.model_copy(
                        update={
                            "finished_at": datetime.now(UTC),
                            "status": "SUCCEEDED",
                            "verification_status": "VERIFIED",
                            "evidence_ids": (analysis.snapshot_id,),
                        }
                    )
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
                    error_code = boot_execution.error_code or "BOOT_DIAGNOSE_FAILED"
                    invocations[-1] = boot_record.model_copy(
                        update={
                            "finished_at": datetime.now(UTC),
                            "status": "FAILED",
                            "verification_status": "FAILED",
                            "error_code": error_code,
                        }
                    )
                    completed_steps = [
                        step.model_copy(
                            update={"state": AgentStepState.FAILED, "error_code": error_code}
                        )
                        if step.id == "diagnose-boot"
                        else step
                        for step in completed_steps
                    ]
            except Exception as exc:
                boot_failed = True
                error_code = getattr(exc, "code", "BOOT_DIAGNOSE_FAILED")
                invocations[-1] = boot_record.model_copy(
                    update={
                        "finished_at": datetime.now(UTC),
                        "status": "FAILED",
                        "verification_status": "FAILED",
                        "error_code": error_code,
                    }
                )
                completed_steps = [
                    step.model_copy(
                        update={"state": AgentStepState.FAILED, "error_code": error_code}
                    )
                    if step.id == "diagnose-boot"
                    else step
                    for step in completed_steps
                ]
        refreshed = await self.resources.catalog()
        final_state = (
            AgentRunState.PARTIAL if running.limitations or boot_failed else AgentRunState.COMPLETED
        )
        consumed_envelope = envelope.model_copy(
            update={
                "status": AuthorizationEnvelopeStatus.CONSUMED,
                "consumed_invocation_ids": tuple(item.invocation_id for item in invocations),
            }
        )
        completed = running.model_copy(
            update={
                "state": final_state,
                "updated_at": datetime.now(UTC),
                "resources": refreshed.resources,
                "authorization": running.authorization.model_copy(update={"consumed": True})
                if running.authorization
                else None,
                "authorization_envelope": consumed_envelope,
                "capability_invocations": tuple(invocations),
                "steps": tuple(completed_steps),
                "summary": (
                    "Hechos: diagnóstico read-only completado con evidencia real. "
                    "Hipótesis: revisar findings antes de proponer reparación. "
                    "Recomendación: no ejecutar mutaciones sin checkpoint y autorización nueva."
                ),
                "events": (*running.events, "agent.diagnostic.completed"),
            }
        )
        completed = self._append_timeline(
            completed,
            "agent.diagnostic.completed",
            final_state,
            "ARES observó resultados tipados de las capabilities autorizadas.",
            evidence_ids=tuple(
                evidence_id for invocation in invocations for evidence_id in invocation.evidence_ids
            ),
        )
        await self.store.put(completed)
        await self._event(
            "agent.diagnostic.completed",
            completed,
            session_id,
            {"snapshot_id": analysis.snapshot_id, "diagnostic_id": analysis.diagnostic_id},
        )
        return completed

    async def _execute_backup(
        self, run: AgentRun, envelope: AuthorizationEnvelope, session_id: str
    ) -> AgentRun:
        if self.backup is None:
            raise AgentOrchestratorError("AGENT_CAPABILITY_UNAVAILABLE")
        plan_id = str((run.backend_plan or {}).get("backup_plan_id") or "")
        if not plan_id:
            raise AgentOrchestratorError("AGENT_BACKUP_PLAN_MISSING")
        running = self._mark_running(
            run,
            "agent.backup.started",
            "Ejecutando backup mediante el servicio y broker existentes.",
        )
        await self.store.put(running)
        await self._event("agent.backup.started", running, session_id, {"backup_plan_id": plan_id})

        invocations = list(running.capability_invocations)
        create_record = self._invocation_started(
            running,
            "create-verified-backup",
            "backup.create",
            envelope,
            {"plan_id": plan_id, "session_id": session_id, "created_by": "agent-orchestrator"},
        )
        invocations.append(create_record)
        try:
            accepted = await self.backup.create(
                BackupCreateRequest(plan_id=plan_id, request_authorization=True),
                session_id=session_id,
                created_by="agent-orchestrator",
            )
            backup = await self._wait_backup_terminal(accepted.backup.id)
        except Exception as exc:
            error_code = getattr(exc, "code", "AGENT_BACKUP_CREATE_FAILED")
            failed = self._mark_failed(
                running,
                "agent.backup.failed",
                "El backup no pudo completarse; no se declara protección verificada.",
                error_code,
                invocations,
            )
            await self.store.put(failed)
            await self._event("agent.backup.failed", failed, session_id, {"error_code": error_code})
            return failed

        verified = backup.status is BackupStatus.COMPLETED and (
            backup.verification_status is BackupVerificationStatus.VERIFIED
        )
        invocations[-1] = create_record.model_copy(
            update={
                "finished_at": datetime.now(UTC),
                "status": "SUCCEEDED" if verified else "FAILED",
                "verification_status": "VERIFIED" if verified else "FAILED",
                "evidence_ids": (backup.id,),
                "error_code": None
                if verified
                else backup.execution.error_code or "BACKUP_NOT_VERIFIED",
            }
        )
        verify_record = self._invocation_started(
            running,
            "verify-backup",
            "backup.verify",
            envelope,
            {"backup_id": backup.id, "session_id": session_id},
        )
        invocations.append(verify_record)
        invocations[-1] = verify_record.model_copy(
            update={
                "finished_at": datetime.now(UTC),
                "status": "SUCCEEDED" if verified else "FAILED",
                "verification_status": "VERIFIED" if verified else "FAILED",
                "evidence_ids": (backup.id,),
                "error_code": None if verified else "BACKUP_NOT_VERIFIED",
            }
        )
        final_state = AgentRunState.COMPLETED if verified else AgentRunState.FAILED
        steps = tuple(
            step.model_copy(
                update={
                    "state": AgentStepState.COMPLETED if verified else AgentStepState.FAILED,
                    "result": {
                        "backup_id": backup.id,
                        "status": backup.status.value,
                        "verification_status": backup.verification_status.value,
                        "file_count": backup.file_count,
                        "size": backup.size,
                        "checksum": backup.checksum,
                    },
                    "error_code": None if verified else "BACKUP_NOT_VERIFIED",
                }
            )
            for step in running.steps
        )
        consumed_envelope = envelope.model_copy(
            update={
                "status": AuthorizationEnvelopeStatus.CONSUMED,
                "consumed_invocation_ids": tuple(item.invocation_id for item in invocations),
            }
        )
        completed = running.model_copy(
            update={
                "state": final_state,
                "updated_at": datetime.now(UTC),
                "authorization_envelope": consumed_envelope,
                "capability_invocations": tuple(invocations),
                "steps": steps,
                "summary": (
                    "Hechos: backup ejecutado por el broker y verificado con manifest SHA-256."
                    if verified
                    else (
                        "Hechos: backup ejecutado pero no verificado; no se considera "
                        "protección válida."
                    )
                ),
                "events": (
                    *running.events,
                    "agent.backup.completed" if verified else "agent.backup.failed",
                ),
            }
        )
        completed = self._append_timeline(
            completed,
            "agent.backup.completed" if verified else "agent.backup.failed",
            final_state,
            "ARES aceptó el resultado solo después de la verificación estructurada.",
            evidence_ids=(backup.id,),
            error_code=None if verified else "BACKUP_NOT_VERIFIED",
        )
        await self.store.put(completed)
        await self._event(
            "agent.backup.completed" if verified else "agent.backup.failed",
            completed,
            session_id,
            {"backup_id": backup.id, "verified": verified},
        )
        return completed

    async def _wait_backup_terminal(self, backup_id: str) -> Any:
        assert self.backup is not None
        for _ in range(250):
            backup = await self.backup.get(backup_id)
            if backup is not None and backup.status in {
                BackupStatus.COMPLETED,
                BackupStatus.FAILED,
                BackupStatus.CANCELLED,
                BackupStatus.CORRUPTED,
            }:
                return backup
            await asyncio.sleep(0.02)
        raise AgentOrchestratorError("AGENT_BACKUP_TIMEOUT")

    async def _proposal_for_objective(
        self,
        request: AgentRunRequest,
        *,
        resources: tuple[ResourceCandidate, ...],
        selected: ResourceCandidate | None,
    ) -> AgentProposal:
        deterministic = _deterministic_proposal(request, resources=resources, selected=selected)
        if self.ai_runtime is None:
            return deterministic
        try:
            status = await self.ai_runtime.status()
        except AIRuntimeError:
            return deterministic
        if status.state != "ready":
            return deterministic
        return deterministic

    async def _required(self, run_id: str) -> AgentRun:
        run = await self.store.get(run_id)
        if run is None:
            raise AgentOrchestratorError("AGENT_RUN_NOT_FOUND")
        return run

    async def _invalidate(self, run: AgentRun, session_id: str, reason: str) -> AgentRun:
        envelope = run.authorization_envelope
        if envelope is not None and envelope.status not in {
            AuthorizationEnvelopeStatus.CONSUMED,
            AuthorizationEnvelopeStatus.REVOKED,
        }:
            envelope = envelope.model_copy(
                update={"status": AuthorizationEnvelopeStatus.INVALIDATED}
            )
        invalidated = run.model_copy(
            update={
                "state": AgentRunState.INVALIDATED,
                "updated_at": datetime.now(UTC),
                "authorization_envelope": envelope,
                "steps": tuple(
                    step.model_copy(update={"state": AgentStepState.INVALIDATED})
                    for step in run.steps
                ),
                "summary": f"El plan fue invalidado antes de ejecutar: {reason}.",
                "events": (*run.events, "agent.run.invalidated"),
            }
        )
        invalidated = self._append_timeline(
            invalidated,
            "agent.run.invalidated",
            AgentRunState.INVALIDATED,
            f"Plan invalidado: {reason}.",
            error_code=reason,
        )
        await self.store.put(invalidated)
        await self._event("agent.run.invalidated", invalidated, session_id, {"reason": reason})
        return invalidated

    def _authorized_envelope(self, run: AgentRun) -> AuthorizationEnvelope:
        envelope = run.authorization_envelope
        if envelope is None or envelope.status is not AuthorizationEnvelopeStatus.AUTHORIZED:
            raise AgentOrchestratorError("AGENT_AUTHORIZATION_REQUIRED")
        if envelope.expires_at <= datetime.now(UTC):
            raise AgentOrchestratorError("AGENT_AUTHORIZATION_EXPIRED")
        return envelope

    def _validate_invocation_limits(self, run: AgentRun) -> None:
        if len(run.steps) > run.limits.maximum_steps:
            raise AgentOrchestratorError("AGENT_LIMIT_STEPS_EXCEEDED")
        if len(run.capability_invocations) >= run.limits.maximum_capability_invocations:
            raise AgentOrchestratorError("AGENT_LIMIT_INVOCATIONS_EXCEEDED")

    def _build_envelope(
        self,
        run: AgentRun,
        *,
        selected_resources: tuple[ResourceCandidate, ...],
        allowed_capabilities: tuple[str, ...],
        risk_ceiling: Any,
        maximum_bytes_written: int,
        maximum_bytes_deleted: int,
        maximum_downtime_seconds: int,
        rollback_policy: str,
        verification_requirements: tuple[str, ...],
        protected_paths: tuple[str, ...] = (),
    ) -> AuthorizationEnvelope:
        expires_at = datetime.now(UTC) + timedelta(seconds=run.limits.authorization_expiry_seconds)
        draft = run.model_copy(
            update={
                "steps": tuple(
                    step.model_copy(update={"state": AgentStepState.AUTHORIZATION_REQUIRED})
                    if step.requires_authorization
                    else step
                    for step in run.steps
                )
            }
        )
        return AuthorizationEnvelope(
            agent_run_id=run.id,
            goal=run.objective,
            allowed_capabilities=allowed_capabilities,
            target_resource_ids=tuple(resource.resource_id for resource in selected_resources),
            target_fingerprints=tuple(resource.stable_identity for resource in selected_resources),
            risk_ceiling=risk_ceiling,
            maximum_bytes_written=maximum_bytes_written,
            maximum_bytes_deleted=maximum_bytes_deleted,
            protected_paths=protected_paths,
            allowed_package_changes=(),
            maximum_downtime_seconds=maximum_downtime_seconds,
            checkpoint_requirements=("resource-fingerprint-stable", "plan-fingerprint-stable"),
            rollback_policy=rollback_policy,
            verification_requirements=verification_requirements,
            expires_at=expires_at,
            plan_fingerprint=_run_fingerprint(draft, selected_resources),
        )

    def _mark_running(self, run: AgentRun, event_type: str, summary: str) -> AgentRun:
        running = run.model_copy(
            update={
                "state": AgentRunState.EXECUTING,
                "updated_at": datetime.now(UTC),
                "steps": tuple(
                    step.model_copy(update={"state": AgentStepState.EXECUTING})
                    if step.requires_authorization
                    else step
                    for step in run.steps
                ),
                "summary": summary,
                "events": (*run.events, event_type),
            }
        )
        return self._append_timeline(
            running,
            event_type,
            AgentRunState.EXECUTING,
            "Iniciando ejecución de capabilities autorizadas por envelope.",
        )

    def _mark_failed(
        self,
        run: AgentRun,
        event_type: str,
        summary: str,
        error_code: str,
        invocations: MutableSequence[CapabilityInvocationRecord],
    ) -> AgentRun:
        if invocations and invocations[-1].status == "STARTED":
            invocations[-1] = invocations[-1].model_copy(
                update={
                    "finished_at": datetime.now(UTC),
                    "status": "FAILED",
                    "verification_status": "FAILED",
                    "error_code": error_code,
                }
            )
        failed = run.model_copy(
            update={
                "state": AgentRunState.FAILED,
                "updated_at": datetime.now(UTC),
                "capability_invocations": tuple(invocations),
                "steps": tuple(
                    step.model_copy(
                        update={"state": AgentStepState.FAILED, "error_code": error_code}
                    )
                    if step.state is AgentStepState.EXECUTING
                    else step
                    for step in run.steps
                ),
                "summary": summary,
                "events": (*run.events, event_type),
            }
        )
        return self._append_timeline(
            failed,
            event_type,
            AgentRunState.FAILED,
            summary,
            error_code=error_code,
        )

    def _invocation_started(
        self,
        run: AgentRun,
        step_id: str,
        capability_id: str,
        envelope: AuthorizationEnvelope,
        input_payload: dict[str, object],
    ) -> CapabilityInvocationRecord:
        if capability_id not in envelope.allowed_capabilities:
            raise AgentOrchestratorError("AGENT_CAPABILITY_OUT_OF_SCOPE")
        if len(run.capability_invocations) + 1 > run.limits.maximum_capability_invocations:
            raise AgentOrchestratorError("AGENT_LIMIT_INVOCATIONS_EXCEEDED")
        return CapabilityInvocationRecord(
            agent_run_id=run.id,
            step_id=step_id,
            capability_id=capability_id,
            input_fingerprint=_fingerprint(input_payload),
            target_fingerprints=envelope.target_fingerprints,
            authorization_envelope_id=envelope.id,
        )

    def _append_timeline(
        self,
        run: AgentRun,
        event_type: str,
        state: AgentRunState,
        reason: str,
        *,
        evidence_ids: tuple[str, ...] = (),
        error_code: str | None = None,
    ) -> AgentRun:
        return run.model_copy(
            update={
                "timeline": (
                    *run.timeline,
                    AgentTimelineEntry(
                        event_type=event_type,
                        state=state,
                        reason=reason,
                        evidence_ids=evidence_ids,
                        error_code=error_code,
                    ),
                )
            }
        )

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


def _storage_step(
    selected: ResourceCandidate | None, evidence: tuple[EvidenceFact, ...]
) -> AgentPlanStep:
    return AgentPlanStep(
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
    )


def _boot_step(
    selected: ResourceCandidate | None, evidence: tuple[EvidenceFact, ...]
) -> AgentPlanStep:
    return AgentPlanStep(
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


def _deterministic_proposal(
    request: AgentRunRequest,
    *,
    resources: tuple[ResourceCandidate, ...],
    selected: ResourceCandidate | None,
) -> AgentProposal:
    objective = request.objective.casefold()
    if any(
        term in objective for term in ("backup", "respaldo", "copia", "proteger", "salvar datos")
    ):
        destination_present = request.destination_resource_id is not None
        return AgentProposal(
            proposal_type=AgentProposalType.PROPOSE_CAPABILITY
            if selected is not None and destination_present
            else AgentProposalType.ASK_USER,
            goal_interpretation="El usuario quiere proteger datos mediante un backup verificable.",
            hypotheses=("Un backup verificado es la primera mutación segura antes de reparar.",),
            requested_evidence=("resource_id origen", "destination_resource_id destino")
            if not destination_present
            else (),
            selected_capability_id="backup.create",
            resource_ids=tuple(
                item
                for item in (request.resource_id, request.destination_resource_id)
                if item is not None
            ),
            reason=(
                "El objetivo menciona respaldo/protección y solo puede convertirse en una "
                "capability de backup, no en comandos."
            ),
            expected_result=(
                "BackupPlan estructurado y luego backup verificado si el usuario autoriza."
            ),
            confidence=0.82,
            needs_user_input=selected is None or not destination_present,
            user_question=(
                "Selecciona el recurso origen y un destino externo o montado para "
                "preparar el backup."
                if selected is None or not destination_present
                else None
            ),
        )
    if not resources:
        return AgentProposal(
            proposal_type=AgentProposalType.REQUEST_EVIDENCE,
            goal_interpretation="No existe evidencia suficiente para seleccionar una capability.",
            requested_evidence=("hardware.inventory", "storage.snapshot"),
            reason="El catálogo de recursos está vacío.",
            expected_result="Actualizar inventario antes de planificar.",
            confidence=0.3,
            needs_user_input=False,
        )
    capabilities = ["storage.disk-analysis"]
    if _mentions_boot(request.objective):
        capabilities.append("boot.diagnose")
    return AgentProposal(
        proposal_type=AgentProposalType.PROPOSE_CAPABILITY,
        goal_interpretation="El usuario solicita diagnóstico del equipo con evidencia local.",
        hypotheses=(
            "Primero debe recolectarse evidencia de almacenamiento de solo lectura.",
            "Si el objetivo menciona arranque, se puede diagnosticar boot sin reparar GRUB.",
        ),
        selected_capability_id=capabilities[0],
        resource_ids=(selected.resource_id,) if selected else (),
        reason="La solicitud encaja con capabilities read-only permitidas.",
        expected_result="Snapshot y diagnóstico estructurado sin mutaciones.",
        confidence=0.78,
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


def _evidence_from_resources(resources: tuple[ResourceCandidate, ...]) -> tuple[EvidenceFact, ...]:
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


def _backup_path(resource: ResourceCandidate, *, role: str) -> str | None:
    candidate = resource.technical_details.get("mountpoint")
    if not candidate and resource.kind is ResourceKind.MOUNT:
        candidate = resource.technical_path
    if not candidate and resource.kind is ResourceKind.RECOVERY_ENVIRONMENT:
        candidate = resource.technical_path
    if candidate in {None, "", "/"}:
        return None
    if role == "destination" and resource.kind not in {
        ResourceKind.MOUNT,
        ResourceKind.RECOVERY_ENVIRONMENT,
    }:
        return None
    return candidate


def _public_backup_plan(plan: BackupPlan) -> dict[str, object]:
    return {
        "id": plan.id,
        "backup_id": plan.backup_id,
        "risk": plan.risk,
        "estimated_bytes": plan.source.estimated_size_bytes,
        "required_bytes": plan.required_bytes,
        "available_bytes": plan.destination.available_bytes,
        "included_file_count": plan.included_file_count,
        "included_directory_count": plan.included_directory_count,
        "fingerprint_sha256": plan.fingerprint_sha256,
        "expires_at": plan.expires_at.isoformat(),
    }


def _run_fingerprint(run: AgentRun, selected: tuple[ResourceCandidate, ...]) -> str:
    payload = {
        "objective": run.objective,
        "selected_resource_id": run.selected_resource_id,
        "selected_resource_fingerprints": [item.stable_identity for item in selected],
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
    return _fingerprint(payload)


def _fingerprint(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode(
        "utf-8"
    )
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
