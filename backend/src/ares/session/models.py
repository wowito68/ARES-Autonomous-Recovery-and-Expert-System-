"""Closed contracts for leaving the ARES Live session safely."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from ares.resources.models import ResourceCandidate


class SessionOperation(StrEnum):
    POWEROFF = "poweroff"
    REBOOT = "reboot"
    REBOOT_TO_INSTALLED_SYSTEM = "reboot_to_installed_system"
    REBOOT_TO_BOOT_MENU = "reboot_to_boot_menu"


class FirmwareKind(StrEnum):
    UEFI = "uefi"
    BIOS = "bios"
    UNKNOWN = "unknown"


class BootReturnCapability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: SessionOperation
    available: bool
    verified: bool
    label: str
    explanation: str
    warnings: tuple[str, ...] = ()


class SessionPreflight(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    requested_operation: SessionOperation
    allowed: bool
    firmware: FirmwareKind
    active_operations: tuple[str, ...] = ()
    prepared_terminals: tuple[str, ...] = ()
    mounted_resources: tuple[str, ...] = ()
    installed_systems: tuple[ResourceCandidate, ...] = ()
    bootloader_detected: bool = False
    bootloader_name: str | None = None
    capabilities: tuple[BootReturnCapability, ...]
    steps_before_exit: tuple[str, ...]
    message: str
    requires_confirmation: bool = True


class SessionActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: SessionOperation
    confirm: bool = False
    understood: Annotated[str | None, Field(max_length=160)] = None


class SessionActionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    accepted: bool
    executed: bool
    operation: SessionOperation
    preflight: SessionPreflight
    message: str


class TerminalContextKind(StrEnum):
    ARES_RECOVERY = "ares_recovery"
    INSTALLED_DIRECTORY = "installed_directory"
    INSTALLED_CHROOT = "installed_chroot"
    ADMINISTRATIVE = "administrative"
    READ_ONLY = "read_only"


class TerminalContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: TerminalContextKind
    label: str
    available: bool
    requires_authorization: bool
    privilege: str
    explanation: str
    resource_id: str | None = None
    limitations: tuple[str, ...] = ()


class TerminalContextCollection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contexts: tuple[TerminalContext, ...]
    count: int


class TerminalOpenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: TerminalContextKind
    resource_id: str | None = None
    confirm: bool = False
    understood: Annotated[str | None, Field(max_length=160)] = None


class TerminalOpenResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    accepted: bool
    request_id: str | None = None
    context: TerminalContextKind
    message: str
    limitations: tuple[str, ...] = ()
