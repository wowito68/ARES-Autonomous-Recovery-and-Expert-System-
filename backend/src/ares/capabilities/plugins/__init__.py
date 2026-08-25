"""Built-in plugins shipped in the signed ARES OS image."""

from ares.capabilities.plugins.backup import BackupPlugin
from ares.capabilities.plugins.boot_diagnostics import BootDiagnosticsPlugin
from ares.capabilities.plugins.diagnostic_analysis import DiagnosticAnalysisPlugin
from ares.capabilities.plugins.disk_analysis import DiskAnalysisPlugin
from ares.capabilities.plugins.filesystem_repair import FilesystemRepairPlugin
from ares.capabilities.plugins.recovery_core import RecoveryCorePlugin
from ares.capabilities.plugins.storage_partition import StoragePartitionPlugin

__all__ = [
    "BackupPlugin",
    "BootDiagnosticsPlugin",
    "DiagnosticAnalysisPlugin",
    "DiskAnalysisPlugin",
    "FilesystemRepairPlugin",
    "RecoveryCorePlugin",
    "StoragePartitionPlugin",
]
