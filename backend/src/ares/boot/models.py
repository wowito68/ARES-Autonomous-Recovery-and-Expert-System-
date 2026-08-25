"""Read-only boot diagnostics contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class FirmwareMode(StrEnum):
    UEFI = "uefi"
    BIOS = "bios"
    UNKNOWN = "unknown"


class BootFindingSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class BootFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{2,95}$")]
    severity: BootFindingSeverity
    title: Annotated[str, Field(min_length=3, max_length=160)]
    detail: Annotated[str, Field(min_length=3, max_length=600)]
    resource_id: str | None = None
    evidence: tuple[str, ...] = ()


class BootInstalledSystem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    name: str
    version: str | None = None
    source: str
    mountpoint: str
    disk_id: str | None = None
    bootloader_checked: bool = False
    grub_config_found: bool = False


class BootDiagnosticInput(BaseModel):
    """Semantic boot diagnostic input; no commands or paths from the LLM."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: str | None = None
    target_resource_id: str | None = None


class BootDiagnosticResult(BaseModel):
    """Read-only boot diagnostic based on already available evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: str
    firmware: FirmwareMode
    installed_systems: tuple[BootInstalledSystem, ...]
    efi_partitions: tuple[str, ...] = ()
    bootloader_verified: bool = False
    bootloader_name: str | None = None
    findings: tuple[BootFinding, ...]
    evidence: tuple[str, ...]
    limitations: tuple[str, ...]
    repair_capability_available: bool = False
    summary: str
