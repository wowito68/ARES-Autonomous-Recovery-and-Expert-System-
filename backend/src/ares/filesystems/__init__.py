"""Filesystem Recovery & Repair domain."""

from ares.filesystems.adapters import (
    AdapterCapabilities,
    FilesystemAdapter,
    adapter_for,
    supported_filesystems,
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

__all__ = [
    "AdapterCapabilities",
    "DeviceIdentity",
    "FilesystemAdapter",
    "FilesystemAuthorizationGrant",
    "FilesystemCheckResult",
    "FilesystemHealth",
    "FilesystemInspection",
    "FilesystemRepairInput",
    "FilesystemRepairOutcome",
    "FilesystemRepairPlan",
    "FilesystemRepairRecord",
    "FilesystemRepairResult",
    "FilesystemType",
    "MountSafetyReport",
    "RepairExecution",
    "RepairExecutionStatus",
    "RepairVerification",
    "RepairVerificationStatus",
    "adapter_for",
    "supported_filesystems",
]
