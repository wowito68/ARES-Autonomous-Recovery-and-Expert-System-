"""Private actions for System Recovery and its independently controlled child repairs."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ares.actions.base import ActionContext, ActionError
from ares.recovery.executor import (
    RecoveryAuthorizationGrant,
    RecoveryExecutorError,
    RecoveryMutationExecutor,
)
from ares.recovery.models import (
    RecoveryDiagnoseInput,
    RecoveryOperation,
    RecoveryPlanInput,
    RecoveryStrategyKind,
)
from ares.recovery.orchestrator import RecoveryOrchestrator, RecoveryOrchestratorError
from ares.tools.recovery import (
    AptPackageManagerAdapter,
    KernelDiagnosticTool,
    RecoveryProcessRunner,
)


class SystemRecoveryActionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    command: str = Field(pattern=r"^(diagnose|plan|authorize|execute|verify|abort)$")
    session_id: str = Field(min_length=8, max_length=128)
    payload: dict[str, Any] = Field(default_factory=dict)


class RecoveryChildExecutionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    operation: RecoveryOperation
    target_root: str
    grant: RecoveryAuthorizationGrant
    session_id: str


class PackageDiagnoseInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target_root: str = Field(min_length=1, max_length=4096)


class KernelDiagnoseInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target_root: str = Field(min_length=1, max_length=4096)


class RunSystemRecoveryAction:
    id = "recovery.system-orchestrate"
    idempotent = False

    def __init__(self, orchestrator: RecoveryOrchestrator) -> None:
        self.orchestrator = orchestrator

    async def run(self, inputs: dict[str, Any], context: ActionContext) -> dict[str, Any]:
        request = SystemRecoveryActionInput.model_validate(inputs)
        try:
            if request.command == "diagnose":
                case = await self.orchestrator.diagnose(
                    RecoveryDiagnoseInput.model_validate(request.payload),
                    session_id=request.session_id,
                )
                return case.model_dump(mode="json")
            if request.command == "plan":
                plan = await self.orchestrator.build_plan(
                    RecoveryPlanInput.model_validate(request.payload)
                )
                return plan.model_dump(mode="json")
            case_id = str(request.payload.get("case_id", ""))
            if request.command == "authorize":
                return (await self.orchestrator.authorize(case_id)).model_dump(mode="json")
            if request.command == "execute":
                return (await self.orchestrator.execute(case_id)).model_dump(mode="json")
            if request.command == "verify":
                return (await self.orchestrator.verify(case_id)).model_dump(mode="json")
            if request.command == "abort":
                return (await self.orchestrator.abort(case_id)).model_dump(mode="json")
        except RecoveryOrchestratorError as exc:
            raise ActionError(exc.code) from exc
        raise ActionError("RECOVERY_COMMAND_UNSUPPORTED")

    async def compensate(self, output: dict[str, Any], context: ActionContext) -> None:
        del output, context


class ExecuteRecoveryChildAction:
    id = "recovery.child-execute"
    idempotent = False

    def __init__(self, executor: RecoveryMutationExecutor, runner: RecoveryProcessRunner) -> None:
        self.executor = executor
        self.packages = AptPackageManagerAdapter(runner)
        self.kernel = KernelDiagnosticTool()

    async def run(self, inputs: dict[str, Any], context: ActionContext) -> dict[str, Any]:
        request = RecoveryChildExecutionInput.model_validate(inputs)
        operation = request.operation
        root = _root(request.target_root)
        try:
            changes = await self.executor.execute(operation, root=root, grant=request.grant)
        except RecoveryExecutorError as exc:
            raise ActionError(exc.code) from exc
        verification = await self._verify(operation, root)
        if not verification["valid"]:
            raise ActionError(str(verification["error_code"]))
        return {"changes": list(changes), "verification": verification}

    async def _verify(self, operation: RecoveryOperation, root: Path) -> dict[str, Any]:
        if operation.strategy is RecoveryStrategyKind.CONFIGURATION:
            raw = operation.payload.get("configuration_diff")
            from ares.recovery.models import ConfigurationDiff

            change = ConfigurationDiff.model_validate(raw)
            target = root / change.path.lstrip("/")
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            expected = hashlib.sha256(change.proposed_text.encode("utf-8")).hexdigest()
            return {
                "valid": digest == expected,
                "error_code": "CONFIGURATION_POSTCHECK_FAILED",
                "sha256": digest,
            }
        if operation.strategy is RecoveryStrategyKind.PACKAGE:
            evidence = await self.packages.diagnose(root)
            failing_events = {"package-audit", "interrupted-transaction", "database-missing"}
            valid = not any(item.event in failing_events for item in evidence)
            return {
                "valid": valid,
                "error_code": "PACKAGE_POSTCHECK_FAILED",
                "remaining_events": [item.event for item in evidence],
            }
        if operation.strategy is RecoveryStrategyKind.INITRAMFS:
            kernel = str(operation.payload.get("kernel_version", ""))
            artifact = root / "boot" / f"initrd.img-{kernel}"
            valid = artifact.is_file() and artifact.stat().st_size > 0
            return {
                "valid": valid,
                "error_code": "INITRAMFS_POSTCHECK_FAILED",
                "artifact": str(artifact),
            }
        return {"valid": False, "error_code": "RECOVERY_POSTCHECK_UNSUPPORTED"}

    async def compensate(self, output: dict[str, Any], context: ActionContext) -> None:
        del output, context


class DiagnosePackagesAction:
    id = "recovery.package-diagnose"
    idempotent = True

    def __init__(self, runner: RecoveryProcessRunner) -> None:
        self.adapter = AptPackageManagerAdapter(runner)

    async def run(self, inputs: dict[str, Any], context: ActionContext) -> dict[str, Any]:
        request = PackageDiagnoseInput.model_validate(inputs)
        evidence = await self.adapter.diagnose(_root(request.target_root))
        return {"manager": self.adapter.kind.value, "evidence": [item.model_dump(mode="json") for item in evidence]}

    async def compensate(self, output: dict[str, Any], context: ActionContext) -> None:
        del output, context


class DiagnoseKernelAction:
    id = "recovery.kernel-diagnose"
    idempotent = True

    def __init__(self) -> None:
        self.tool = KernelDiagnosticTool()

    async def run(self, inputs: dict[str, Any], context: ActionContext) -> dict[str, Any]:
        request = KernelDiagnoseInput.model_validate(inputs)
        evidence = self.tool.inspect(_root(request.target_root))
        return {"evidence": [item.model_dump(mode="json") for item in evidence]}

    async def compensate(self, output: dict[str, Any], context: ActionContext) -> None:
        del output, context


def _root(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise ActionError("RECOVERY_TARGET_ROOT_INVALID")
    return path.resolve()
