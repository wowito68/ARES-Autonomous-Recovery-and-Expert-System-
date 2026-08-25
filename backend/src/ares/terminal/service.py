"""High-level terminal orchestration without privileged side effects in FastAPI."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from ares.audit import AuditLedger, AuditLedgerError
from ares.config import Environment, Settings
from ares.resources.models import ResourceCandidate, ResourceKind
from ares.resources.service import ResourceResolver
from ares.terminal.executor import (
    LocalTestTerminalExecutor,
    TerminalExecutor,
    TerminalExecutorError,
)
from ares.terminal.models import (
    TerminalAuthorizationRequest,
    TerminalCleanupEvidence,
    TerminalCleanupStatus,
    TerminalCloseRequest,
    TerminalContext,
    TerminalContextCollection,
    TerminalContextKind,
    TerminalPlan,
    TerminalPlanRequest,
    TerminalPlanResult,
    TerminalRisk,
    TerminalSession,
    TerminalSessionCollection,
    TerminalSessionStatus,
    TerminalStartResult,
    TerminalTarget,
)
from ares.terminal.store import TerminalStore


class TerminalServiceError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class TerminalService:
    def __init__(
        self,
        *,
        settings: Settings,
        resources: ResourceResolver,
        store: TerminalStore,
        executor: TerminalExecutor,
        audit: AuditLedger,
    ) -> None:
        self.settings = settings
        self.resources = resources
        self.store = store
        self.executor = executor
        self.audit = audit

    async def contexts(self) -> TerminalContextCollection:
        catalog = await self.resources.catalog()
        installed = tuple(
            item for item in catalog.resources if item.kind is ResourceKind.OPERATING_SYSTEM
        )
        installed_available = len(installed) >= 1
        ambiguous = len(installed) > 1
        contexts = (
            TerminalContext(
                id=TerminalContextKind.ARES_LOCAL,
                label="Terminal de ARES",
                available=True,
                requires_authorization=False,
                privilege="usuario gráfico ares sin privilegios",
                explanation="Trabaja dentro del entorno de recuperación. El LLM no puede leerla ni controlarla.",
                risk=TerminalRisk.LOW,
            ),
            TerminalContext(
                id=TerminalContextKind.INSTALLED_SYSTEM_READ_ONLY,
                label="Revisar el sistema instalado",
                available=installed_available,
                requires_authorization=True,
                privilege="terminal local con filesystem instalado montado en solo lectura",
                explanation=(
                    "Prepara una terminal local independiente con el sistema detectado como destino "
                    "de solo lectura. No ejecuta binarios del sistema instalado."
                ),
                risk=TerminalRisk.MEDIUM,
                resource_id=installed[0].resource_id if len(installed) == 1 else None,
                blocked_reason=None if installed_available else "no_installed_system_detected",
                requirements=(
                    "resource_id de sistema instalado" if ambiguous else "snapshot de almacenamiento",
                    "autorización contextual independiente",
                    "broker terminal.* disponible",
                ),
                limitations=(
                    ("Selecciona explícitamente una instalación; hay múltiples candidatos.",)
                    if ambiguous
                    else ()
                ),
            ),
            TerminalContext(
                id=TerminalContextKind.INSTALLED_SYSTEM_CHROOT_READ_ONLY,
                label="Terminal dentro del sistema instalado",
                available=False,
                requires_authorization=True,
                privilege="bloqueado",
                explanation="Ejecutaría herramientas del sistema instalado.",
                risk=TerminalRisk.HIGH,
                blocked_reason="chroot_is_not_a_sandbox_and_target_binary_execution_is_not_proven",
                requirements=(
                    "namespace de red aislado",
                    "/run y /tmp privados",
                    "/proc restringido",
                    "/dev mínimo",
                    "sin sockets ARES/Docker/broker/audit/consent",
                    "pruebas privilegiadas de aislamiento",
                ),
            ),
            TerminalContext(
                id=TerminalContextKind.INSTALLED_SYSTEM_ADMIN,
                label="Terminal administrativa",
                available=False,
                requires_authorization=True,
                privilege="bloqueado",
                explanation="Permitiría modificar el sistema instalado.",
                risk=TerminalRisk.HIGH,
                blocked_reason="writable_admin_boundary_not_implemented",
                requirements=("frontera privilegiada explícita", "doble confirmación", "rollback"),
            ),
        )
        return TerminalContextCollection(contexts=contexts, count=len(contexts))

    async def create_plan(
        self, request: TerminalPlanRequest, *, session_id: str
    ) -> TerminalPlanResult:
        target: TerminalTarget | None = None
        limitations: list[str] = []
        if request.context_kind is TerminalContextKind.ARES_LOCAL:
            pass
        elif request.context_kind is TerminalContextKind.INSTALLED_SYSTEM_READ_ONLY:
            target = await self._resolve_installed_target(request.resource_id)
        else:
            raise TerminalServiceError("TERMINAL_CONTEXT_BLOCKED")
        plan = TerminalPlan.build(
            session_id=uuid4().hex,
            context_kind=request.context_kind,
            target=target,
            limitations=tuple(limitations),
        )
        session = TerminalSession(
            id=plan.session_id,
            plan_id=plan.id,
            context_kind=plan.context_kind,
            status=TerminalSessionStatus.AUTHORIZATION_REQUIRED
            if plan.requires_authorization
            else TerminalSessionStatus.PLANNED,
            expires_at=plan.expires_at,
        )
        await self.store.put_plan(plan)
        await self.store.put_session(session)
        await self._audit(
            "terminal.plan.created",
            plan.session_id,
            {
                "plan_id": plan.id,
                "context_kind": plan.context_kind.value,
                "target_resource_id": target.resource_id if target is not None else None,
                "target_fingerprint": target.fingerprint if target is not None else None,
                "plan_fingerprint": plan.fingerprint_sha256,
            },
        )
        return TerminalPlanResult(plan=plan, session=session)

    async def authorize_and_start(
        self, plan_id: str, payload: TerminalAuthorizationRequest, *, operator_uid: int
    ) -> TerminalStartResult:
        plan = await self.store.get_plan(plan_id)
        if plan is None:
            raise TerminalServiceError("TERMINAL_PLAN_NOT_FOUND")
        session = await self.store.get_session(plan.session_id)
        if session is None:
            raise TerminalServiceError("TERMINAL_SESSION_NOT_FOUND")
        if session.status not in {
            TerminalSessionStatus.PLANNED,
            TerminalSessionStatus.AUTHORIZATION_REQUIRED,
        }:
            raise TerminalServiceError("TERMINAL_SESSION_STATE_INVALID")
        if plan.expires_at <= datetime.now(UTC):
            expired = session.model_copy(update={"status": TerminalSessionStatus.EXPIRED})
            await self.store.put_session(expired)
            raise TerminalServiceError("TERMINAL_PLAN_EXPIRED")
        if plan.requires_authorization and (
            not payload.confirm or payload.understood != plan.confirmation_phrase
        ):
            raise TerminalServiceError("TERMINAL_AUTHORIZATION_CONFIRMATION_REQUIRED")
        if plan.context_kind is TerminalContextKind.ARES_LOCAL:
            started = await self._start_local(plan, session)
        elif plan.context_kind is TerminalContextKind.INSTALLED_SYSTEM_READ_ONLY:
            await self._revalidate_target(plan)
            try:
                grant = await self.executor.authorize(plan, operator_uid=operator_uid)
                started = await self.executor.start(plan, grant)
            except TerminalExecutorError as exc:
                failed = session.model_copy(
                    update={"status": TerminalSessionStatus.FAILED, "error_code": exc.code}
                )
                await self.store.put_session(failed)
                raise TerminalServiceError(exc.code) from exc
        else:
            raise TerminalServiceError("TERMINAL_CONTEXT_BLOCKED")
        await self.store.put_session(started)
        await self._audit(
            "terminal.session.started",
            started.id,
            {
                "plan_id": plan.id,
                "context_kind": plan.context_kind.value,
                "plan_fingerprint": plan.fingerprint_sha256,
                "target_fingerprint": plan.target_fingerprint,
            },
        )
        return TerminalStartResult(
            accepted=True,
            session=started,
            plan=plan,
            message="Terminal autorizada e iniciada. La interacción ocurre fuera del navegador.",
        )

    async def get_session(self, session_id: str) -> TerminalSession:
        session = await self.store.get_session(session_id)
        if session is None:
            raise TerminalServiceError("TERMINAL_SESSION_NOT_FOUND")
        return session

    async def list_sessions(self) -> TerminalSessionCollection:
        sessions = await self.store.list_sessions()
        return TerminalSessionCollection(sessions=sessions, count=len(sessions))

    async def close(
        self, session_id: str, payload: TerminalCloseRequest
    ) -> TerminalStartResult:
        del payload
        session = await self.get_session(session_id)
        plan = await self.store.get_plan(session.plan_id)
        if plan is None:
            raise TerminalServiceError("TERMINAL_PLAN_NOT_FOUND")
        if session.status is TerminalSessionStatus.CLOSED:
            return TerminalStartResult(
                accepted=True,
                session=session,
                plan=plan,
                message="La terminal ya estaba cerrada y reconciliada.",
            )
        if plan.context_kind is TerminalContextKind.ARES_LOCAL:
            closed = session.model_copy(
                update={
                    "status": TerminalSessionStatus.CLOSED,
                    "closed_at": datetime.now(UTC),
                    "cleanup_status": TerminalCleanupStatus.VERIFIED,
                }
            )
        else:
            try:
                closed = await self.executor.close(session_id)
            except TerminalExecutorError as exc:
                closed = session.model_copy(
                    update={
                        "status": TerminalSessionStatus.CLEANUP_REQUIRED,
                        "error_code": exc.code,
                        "cleanup_status": TerminalCleanupStatus.REQUIRED,
                    }
                )
        await self.store.put_session(closed)
        await self._audit(
            "terminal.session.closed",
            session_id,
            {
                "plan_id": plan.id,
                "status": closed.status.value,
                "cleanup_status": closed.cleanup_status.value,
            },
        )
        return TerminalStartResult(
            accepted=closed.status is TerminalSessionStatus.CLOSED,
            session=closed,
            plan=plan,
            message="Cierre de terminal procesado.",
        )

    async def cleanup_evidence(self, session_id: str) -> TerminalCleanupEvidence:
        session = await self.get_session(session_id)
        if session.context_kind is TerminalContextKind.ARES_LOCAL:
            return TerminalCleanupEvidence(
                session_id=session_id,
                status=session.cleanup_status,
                process_active=session.status
                in {TerminalSessionStatus.STARTING, TerminalSessionStatus.ACTIVE},
                mount_active=False,
                socket_active=False,
                cleanup_verified=session.status is TerminalSessionStatus.CLOSED,
                evidence=("local-terminal-session-record",),
            )
        try:
            return await self.executor.cleanup(session_id)
        except TerminalExecutorError as exc:
            return TerminalCleanupEvidence(
                session_id=session_id,
                status=TerminalCleanupStatus.FAILED,
                process_active=True,
                mount_active=True,
                socket_active=False,
                cleanup_verified=False,
                evidence=(exc.code,),
            )

    async def blocking_sessions(self) -> tuple[str, ...]:
        sessions = await self.store.list_sessions()
        blocking = {
            TerminalSessionStatus.STARTING,
            TerminalSessionStatus.ACTIVE,
            TerminalSessionStatus.CLOSING,
            TerminalSessionStatus.ORPHANED,
            TerminalSessionStatus.CLEANUP_REQUIRED,
            TerminalSessionStatus.CLEANUP_FAILED,
        }
        return tuple(
            f"terminal:{session.id}:{session.status.value}"
            for session in sessions
            if session.status in blocking
        )

    async def _resolve_installed_target(self, resource_id: str | None) -> TerminalTarget:
        catalog = await self.resources.catalog()
        installed = tuple(
            item for item in catalog.resources if item.kind is ResourceKind.OPERATING_SYSTEM
        )
        if resource_id is None:
            if len(installed) == 1:
                resource = installed[0]
            elif not installed:
                raise TerminalServiceError("TERMINAL_TARGET_NOT_FOUND")
            else:
                raise TerminalServiceError("TERMINAL_TARGET_AMBIGUOUS")
        else:
            resource = next((item for item in installed if item.resource_id == resource_id), None)
            if resource is None:
                raise TerminalServiceError("TERMINAL_TARGET_NOT_FOUND")
        if resource.technical_path in {None, "/"}:
            raise TerminalServiceError("TERMINAL_TARGET_UNSAFE")
        return TerminalTarget.from_resource(resource)

    async def _revalidate_target(self, plan: TerminalPlan) -> None:
        if plan.target is None:
            raise TerminalServiceError("TERMINAL_TARGET_NOT_FOUND")
        catalog = await self.resources.catalog()
        resource = next(
            (item for item in catalog.resources if item.resource_id == plan.target.resource_id),
            None,
        )
        if resource is None:
            raise TerminalServiceError("TERMINAL_TARGET_NOT_FOUND")
        current = TerminalTarget.from_resource(resource)
        if current.fingerprint != plan.target_fingerprint:
            raise TerminalServiceError("TERMINAL_TARGET_FINGERPRINT_CHANGED")
        if current.technical_path in {None, "/"}:
            raise TerminalServiceError("TERMINAL_TARGET_UNSAFE")

    async def _start_local(self, plan: TerminalPlan, session: TerminalSession) -> TerminalSession:
        terminal_dir = self.settings.runtime_state_dir / "terminal"
        terminal_dir.mkdir(mode=0o770, parents=True, exist_ok=True)
        request_file = terminal_dir / f"{session.id}.json"
        payload = {
            "request_id": session.id,
            "context": "ares_local",
            "label": "Terminal de ARES",
            "privilege": "usuario gráfico ares sin privilegios",
            "plan_id": plan.id,
        }
        await asyncio.to_thread(
            request_file.write_text,
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        return session.model_copy(
            update={
                "status": TerminalSessionStatus.ACTIVE,
                "authorized_at": datetime.now(UTC),
                "started_at": datetime.now(UTC),
                "cleanup_status": TerminalCleanupStatus.NOT_REQUIRED,
                "technical_details": {"launcher_request": request_file.name},
            }
        )

    async def _audit(self, event_type: str, session_id: str, payload: dict[str, object]) -> None:
        try:
            await self.audit.append(
                event_type=event_type,
                source="ares-api",
                correlation_id=session_id,
                session_id=session_id,
                payload=payload,
            )
        except AuditLedgerError:
            if self.settings.environment is not Environment.TEST:
                raise
