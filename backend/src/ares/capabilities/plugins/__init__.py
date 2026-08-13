"""Built-in plugins shipped in the signed ARES OS image."""

from ares.capabilities.plugins.backup import BackupPlugin
from ares.capabilities.plugins.boot_recovery import BootRecoveryPlugin
from ares.capabilities.plugins.disk_analysis import DiskAnalysisPlugin
from ares.capabilities.plugins.filesystem_repair import FilesystemRepairPlugin
from ares.capabilities.plugins.storage_partition import StoragePartitionPlugin

__all__ = [
    "BackupPlugin",
    "BootRecoveryPlugin",
    "DiskAnalysisPlugin",
    "FilesystemRepairPlugin",
    "StoragePartitionPlugin",
]
