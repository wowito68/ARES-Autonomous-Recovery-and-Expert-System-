"""Filesystem Recovery & Repair domain."""

from ares.filesystems.adapters import (
    AdapterCapabilities,
    FilesystemAdapter,
    adapter_for,
    supported_filesystems,
)
from ares.filesystems.executor import (
    FilesystemExecutor,
    FilesystemExecutorError,
    LocalTestFilesystemExecutor,
    UnixBrokerFilesystemExecutor,
)
from ares.filesystems.models import (
    DeviceIdentity,
    FilesystemAuthorizationGrant,
    FilesystemCheckResult,
    FilesystemHealth,
    FilesystemInspection,
    FilesystemRepairInput,
    FilesystemRepairOutcome,
    FilesystemRepairPlan,
    FilesystemRepairRecord,
    FilesystemRepairResult,
    FilesystemType,
    MountSafetyReport,
    RepairExecution,
    RepairExecutionStatus,
    RepairVerification,
    RepairVerificationStatus,
)
from ares.filesystems.service import (
    FilesystemInspectRequest,
    FilesystemRepairPlanRequest,
    FilesystemRepairService,
    FilesystemRepairStartRequest,
    FilesystemServiceError,
)
from ares.filesystems.store import FilesystemRepairStore

__all__ = [
    "AdapterCapabilities",
    "DeviceIdentity",
    "FilesystemAdapter",
    "FilesystemAuthorizationGrant",
    "FilesystemCheckResult",
    "FilesystemExecutor",
    "FilesystemExecutorError",
    "FilesystemHealth",
    "FilesystemInspectRequest",
    "FilesystemInspection",
    "FilesystemRepairInput",
    "FilesystemRepairOutcome",
    "FilesystemRepairPlan",
    "FilesystemRepairPlanRequest",
    "FilesystemRepairRecord",
    "FilesystemRepairResult",
    "FilesystemRepairService",
    "FilesystemRepairStartRequest",
    "FilesystemRepairStore",
    "FilesystemServiceError",
    "FilesystemType",
    "LocalTestFilesystemExecutor",
    "MountSafetyReport",
    "RepairExecution",
    "RepairExecutionStatus",
    "RepairVerification",
    "RepairVerificationStatus",
    "UnixBrokerFilesystemExecutor",
    "adapter_for",
    "supported_filesystems",
]
