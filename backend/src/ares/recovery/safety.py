"""Case and operation safety policy for compound System Recovery."""

from __future__ import annotations

from ares.capabilities.models import RiskLevel
from ares.protection import ProtectionCheckpointStatus
from ares.recovery.integrity import recovery_plan_integrity_valid
from ares.recovery.models import (
    RecoveryMode,
    RecoveryOperation,
    RecoveryOperationStatus,
    SystemRecoveryPlan,
)


class RecoverySafetyError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class RecoverySafetyGate:
    """Require exact target/protection/auth/dependency evidence before every write."""

    def validate_plan(self, plan: SystemRecoveryPlan) -> None:
        if not recovery_plan_integrity_valid(plan):
            raise RecoverySafetyError("RECOVERY_PLAN_TAMPERED")
        if not plan.executable:
            raise RecoverySafetyError("RECOVERY_PLAN_BLOCKED")
        if plan.mode is RecoveryMode.READ_ONLY:
            raise RecoverySafetyError("RECOVERY_MODE_READ_ONLY")
        if not plan.operations:
            raise RecoverySafetyError("RECOVERY_PLAN_EMPTY")

    def validate_operation(
        self,
        operation: RecoveryOperation,
        *,
        mode: RecoveryMode,
        completed_operation_ids: frozenset[str],
    ) -> None:
        if operation.status not in {
            RecoveryOperationStatus.PLANNED,
            RecoveryOperationStatus.PROTECTED,
            RecoveryOperationStatus.AUTHORIZED,
        }:
            raise RecoverySafetyError("RECOVERY_OPERATION_STATE_INVALID")
        missing = set(operation.depends_on) - completed_operation_ids
        if missing:
            raise RecoverySafetyError("RECOVERY_DEPENDENCY_NOT_SATISFIED")
        if operation.risk is RiskLevel.CRITICAL and mode is not RecoveryMode.ADVANCED:
            raise RecoverySafetyError("RECOVERY_ADVANCED_MODE_REQUIRED")
        if operation.risk in {RiskLevel.HIGH, RiskLevel.CRITICAL}:
            checkpoint = operation.protection_checkpoint
            if checkpoint is None or checkpoint.status is not ProtectionCheckpointStatus.READY:
                raise RecoverySafetyError("RECOVERY_PROTECTION_REQUIRED")
        if operation.status is RecoveryOperationStatus.PROTECTED:
            raise RecoverySafetyError("RECOVERY_AUTHORIZATION_REQUIRED")
        if not operation.verification_requirements:
            raise RecoverySafetyError("RECOVERY_VERIFICATION_UNDECLARED")
        if operation.rollback_supported and not operation.rollback_strategy.strip():
            raise RecoverySafetyError("RECOVERY_ROLLBACK_STRATEGY_INVALID")

    @staticmethod
    def can_continue_after(status: RecoveryOperationStatus) -> bool:
        return status is RecoveryOperationStatus.COMPLETED
