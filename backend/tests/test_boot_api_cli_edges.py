from __future__ import annotations

import builtins
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from ares import cli
from ares.api.routes.boot import router as boot_router
from ares.boot.engine import BootRecoveryEngine
from ares.boot.models import (
    BootDiagnosticResult,
    BootRepairPlan,
    BootRepairPlanInput,
    BootRepairRecord,
    BootVerification,
)
from ares.boot.service import (
    BootDiagnoseRequest,
    BootRecoveryServiceError,
    BootRepairAccepted,
    BootRepairPlanRequest,
    BootRepairRequest,
)
from ares.core.problems import install_problem_handlers
from tests.test_boot_engine import _diagnostic, _engine
from tests.test_boot_tools import _root


class _BootServiceFixture:
    def __init__(
        self,
        *,
        engine: BootRecoveryEngine,
        diagnostic: BootDiagnosticResult,
        plan: BootRepairPlan,
        record: BootRepairRecord,
        verification: BootVerification,
    ) -> None:
        self.engine = engine
        self.diagnostic_result = diagnostic
        self.repair_plan = plan
        self.record = record
        self.verification_result = verification
        self.missing = False
        self.error_code: str | None = None

    def _fail(self) -> None:
        if self.error_code is not None:
            raise BootRecoveryServiceError(self.error_code)

    async def diagnose(
        self, request: BootDiagnoseRequest, *, session_id: str
    ) -> BootDiagnosticResult:
        del request, session_id
        self._fail()
        return self.diagnostic_result

    async def plan(self, request: BootRepairPlanRequest, *, session_id: str) -> BootRepairPlan:
        del request, session_id
        self._fail()
        return self.repair_plan

    async def start(
        self,
        request: BootRepairRequest,
        *,
        session_id: str,
        created_by: str,
    ) -> BootRepairAccepted:
        del request, session_id, created_by
        self._fail()
        return BootRepairAccepted(
            repair=self.record,
            authorization_instruction="Approve only through trusted local consent.",
        )

    async def get(self, repair_id: str) -> BootRepairRecord | None:
        del repair_id
        return None if self.missing else self.record

    async def verification(self, repair_id: str) -> BootVerification | None:
        del repair_id
        return None if self.missing else self.verification_result

    async def cancel(self, repair_id: str, *, session_id: str) -> BootRepairRecord:
        del repair_id, session_id
        self._fail()
        return self.record

    async def reconcile(self, repair_id: str, *, session_id: str) -> BootVerification:
        del repair_id, session_id
        self._fail()
        return self.verification_result


async def _fixture(tmp_path: Path) -> _BootServiceFixture:
    root = _root(tmp_path, grub=False, initramfs=False)
    engine, store, _ = _engine(tmp_path)
    diagnostic = _diagnostic(root)
    await store.put_diagnostic(diagnostic)
    plan = await engine.plan(
        BootRepairPlanInput(diagnostic_id=diagnostic.id),
        session_id="boot-control-session",
    )
    protected = await engine.protect(plan.id, session_id="boot-control-session")
    await engine.request_authorization(
        protected.id,
        session_id="boot-control-session",
        on_challenge=_ignore_challenge,
    )
    verification = await engine.execute_authorized(
        protected.repair_id,
        session_id="boot-control-session",
        on_stage=_ignore_stage,
    )
    record = await store.get_record(protected.repair_id)
    assert record is not None
    return _BootServiceFixture(
        engine=engine,
        diagnostic=diagnostic,
        plan=protected,
        record=record,
        verification=verification,
    )


async def test_boot_api_success_not_found_and_problem_mapping(tmp_path: Path) -> None:
    service = await _fixture(tmp_path)
    app = FastAPI()
    app.state.boot_recovery_service = service
    app.include_router(boot_router, prefix="/api/v1")
    install_problem_handlers(app)
    transport = ASGITransport(app=app, raise_app_exceptions=False)

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        diagnose = await client.post(
            "/api/v1/boot/diagnose",
            json={
                "target_disk": None,
                "root_path": service.diagnostic_result.environment.operating_systems[0].root_path,
            },
        )
        assert diagnose.status_code == 200
        planned = await client.post(
            "/api/v1/boot/repair/plan",
            json={"diagnostic_id": service.diagnostic_result.id, "target_os_id": None},
        )
        assert planned.status_code == 200
        started = await client.post(
            "/api/v1/boot/repair",
            json={"plan_id": service.repair_plan.id, "request_authorization": True},
        )
        assert started.status_code == 202
        repair_id = service.record.execution.repair_id
        status = await client.get(f"/api/v1/boot/repairs/{repair_id}")
        assert status.status_code == 200
        verified = await client.get(f"/api/v1/boot/repairs/{repair_id}/verification")
        assert verified.status_code == 200
        cancelled = await client.post(f"/api/v1/boot/repairs/{repair_id}/cancel")
        assert cancelled.status_code == 200
        reconciled = await client.post(f"/api/v1/boot/repairs/{repair_id}/reconcile")
        assert reconciled.status_code == 200

        service.missing = True
        assert (await client.get("/api/v1/boot/repairs/missing")).status_code == 404
        assert (await client.get("/api/v1/boot/repairs/missing/verification")).status_code == 404
        service.missing = False

        service.error_code = "BOOT_FIXTURE_REJECTED"
        rejected_diagnose = await client.post(
            "/api/v1/boot/diagnose",
            json={"target_disk": None, "root_path": None},
        )
        assert rejected_diagnose.status_code == 422
        assert rejected_diagnose.json()["code"] == "BOOT_FIXTURE_REJECTED"
        rejected_plan = await client.post(
            "/api/v1/boot/repair/plan",
            json={"diagnostic_id": service.diagnostic_result.id, "target_os_id": None},
        )
        assert rejected_plan.status_code == 422
        rejected_start = await client.post(
            "/api/v1/boot/repair",
            json={"plan_id": service.repair_plan.id, "request_authorization": True},
        )
        assert rejected_start.status_code == 409
        assert (await client.post(f"/api/v1/boot/repairs/{repair_id}/cancel")).status_code == 409
        assert (await client.post(f"/api/v1/boot/repairs/{repair_id}/reconcile")).status_code == 409


async def test_boot_cli_commands_and_repair_loop(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = await _fixture(tmp_path)
    app = FastAPI()
    app.state.boot_recovery_service = service
    parser = cli.build_parser()

    commands: tuple[list[str], ...] = (
        ["boot", "diagnose", "--root-path", str(tmp_path)],
        ["boot", "plan", service.diagnostic_result.id],
        ["boot", "status", service.record.execution.repair_id],
        ["boot", "verify", service.record.execution.repair_id],
        ["boot", "cancel", service.record.execution.repair_id],
        ["boot", "reconcile", service.record.execution.repair_id],
    )
    for arguments in commands:
        parsed = parser.parse_args(arguments)
        assert await cli._boot_command(parsed, app) == 0

    service.missing = True
    assert (
        await cli._boot_command(parser.parse_args(["boot", "status", "missing-repair"]), app) == 3
    )
    assert (
        await cli._boot_command(parser.parse_args(["boot", "verify", "missing-repair"]), app) == 3
    )
    service.missing = False

    assert await cli._boot_command(parser.parse_args(["boot", "repair", "missing-plan"]), app) == 3

    monkeypatch.setattr(builtins, "input", lambda prompt: "NO")
    assert (
        await cli._boot_command(parser.parse_args(["boot", "repair", service.repair_plan.id]), app)
        == 4
    )

    monkeypatch.setattr(builtins, "input", lambda prompt: "REQUEST")
    assert (
        await cli._boot_command(parser.parse_args(["boot", "repair", service.repair_plan.id]), app)
        == 0
    )

    service.error_code = "BOOT_CLI_REJECTED"
    assert (
        await cli._boot_command(
            parser.parse_args(["boot", "diagnose", "--root-path", str(tmp_path)]), app
        )
        == 2
    )
    output = capsys.readouterr()
    assert "BOOT REPAIR PLAN" in output.out
    assert "Boot repair plan not found" in output.err
    assert "ARES boot recovery failed: BOOT_CLI_REJECTED" in output.err


async def _ignore_challenge(challenge_id: str) -> None:
    del challenge_id


async def _ignore_stage(name: str, payload: dict[str, object]) -> None:
    del name, payload
