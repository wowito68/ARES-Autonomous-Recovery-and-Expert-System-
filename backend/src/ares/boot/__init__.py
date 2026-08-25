"""Boot diagnostics domain."""

from ares.boot.models import (
    BootDiagnosticInput,
    BootDiagnosticResult,
    BootFinding,
    BootFindingSeverity,
    BootInstalledSystem,
    FirmwareMode,
)

__all__ = [
    "BootDiagnosticInput",
    "BootDiagnosticResult",
    "BootFinding",
    "BootFindingSeverity",
    "BootInstalledSystem",
    "FirmwareMode",
]
