"""Private actions used by capabilities; never exported as an HTTP command API."""

from ares.actions.backup import (
    CreateBackupFilesAction,
    ListBackupsAction,
    LoadBackupForVerificationAction,
    PersistBackupManifestAction,
    ProjectBackupGraphAction,
    RequestBackupAuthorizationAction,
    ValidateBackupPlanAction,
    VerifyBackupAction,
)
from ares.actions.base import Action, ActionContext, ActionError
from ares.actions.disk import (
    AnalyzeDiskInventoryAction,
    ReadDiskInventoryAction,
    UpdateStorageGraphAction,
)
from ares.actions.filesystem_repair import (
    ExecuteFilesystemRepairAction,
    ProjectFilesystemRepairGraphAction,
    RequestFilesystemRepairAuthorizationAction,
    ValidateFilesystemRepairAction,
    VerifyFilesystemRepairAction,
)
from ares.actions.storage_vertical import (
    BuildStorageSnapshotAction,
    CollectStorageEvidenceAction,
    PersistStorageSnapshotAction,
    ProjectStorageSnapshotAction,
    storage_capability_result,
)

__all__ = [
    "Action",
    "ActionContext",
    "ActionError",
    "AnalyzeDiskInventoryAction",
    "BuildStorageSnapshotAction",
    "CollectStorageEvidenceAction",
    "CreateBackupFilesAction",
    "ExecuteFilesystemRepairAction",
    "ListBackupsAction",
    "LoadBackupForVerificationAction",
    "PersistBackupManifestAction",
    "PersistStorageSnapshotAction",
    "ProjectBackupGraphAction",
    "ProjectFilesystemRepairGraphAction",
    "ProjectStorageSnapshotAction",
    "ReadDiskInventoryAction",
    "RequestBackupAuthorizationAction",
    "RequestFilesystemRepairAuthorizationAction",
    "UpdateStorageGraphAction",
    "ValidateBackupPlanAction",
    "ValidateFilesystemRepairAction",
    "VerifyBackupAction",
    "VerifyFilesystemRepairAction",
    "storage_capability_result",
]
