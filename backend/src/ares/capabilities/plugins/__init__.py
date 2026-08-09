"""Built-in plugins shipped in the signed ARES OS image."""

from ares.capabilities.plugins.backup import BackupPlugin
from ares.capabilities.plugins.disk_analysis import DiskAnalysisPlugin

__all__ = ["BackupPlugin", "DiskAnalysisPlugin"]
