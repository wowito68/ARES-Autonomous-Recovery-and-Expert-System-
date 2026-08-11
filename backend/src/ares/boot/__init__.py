"""Boot Recovery & Bootloader Management public contracts."""

from ares.boot.engine import BootRecoveryEngine, BootRecoveryEngineError
from ares.boot.executor import (
    BootExecutorError,
    BootRepairExecutor,
    LocalTestBootExecutor,
    UnixBrokerBootExecutor,
)
from ares.boot.models import (
    BootDiagnosticResult,
    BootDiagnoseInput,
    BootEnvironment,
    BootIssue,
    BootIssueCode,
    BootRepairPlan,
    BootRepairRecord,
    BootRepairStatus,
    BootVerification,
    BootVerificationStatus,
    BootloaderKind,
    FirmwareMode,
)
from ares.boot.service import BootRecoveryService, BootRecoveryServiceError
from ares.boot.store import BootRecoveryStore

__all__ = [
    "BootDiagnosticResult",
    "BootDiagnoseInput",
    "BootEnvironment",
    "BootExecutorError",
    "BootIssue",
    "BootIssueCode",
    "BootRecoveryEngine",
    "BootRecoveryEngineError",
    "BootRecoveryService",
    "BootRecoveryServiceError",
    "BootRecoveryStore",
    "BootRepairExecutor",
    "BootRepairPlan",
    "BootRepairRecord",
    "BootRepairStatus",
    "BootVerification",
    "BootVerificationStatus",
    "BootloaderKind",
    "FirmwareMode",
    "LocalTestBootExecutor",
    "UnixBrokerBootExecutor",
]
