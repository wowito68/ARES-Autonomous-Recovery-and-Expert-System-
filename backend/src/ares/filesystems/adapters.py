"""Filesystem-specific repair policies and fixed command construction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ares.filesystems.models import (
    FilesystemCheckResult,
    FilesystemHealth,
    FilesystemType,
)
from ares.tools.storage import ProcessResult


@dataclass(frozen=True, slots=True)
class AdapterCapabilities:
    requires_unmounted: bool
    supports_dry_run: bool
    supports_verification: bool
    supports_rollback: bool
    repair_supported: bool
    limitations: tuple[str, ...] = ()


class FilesystemAdapter(Protocol):
    filesystems: tuple[FilesystemType, ...]
    capabilities: AdapterCapabilities

    @property
    def required_tools(self) -> tuple[str, ...]: ...

    def check_invocation(
        self, target: str, *, image: bool = False
    ) -> tuple[str, tuple[str, ...]]: ...

    def repair_invocation(
        self, target: str, *, image: bool = False
    ) -> tuple[str, tuple[str, ...]]: ...

    def parse_check(
        self, result: ProcessResult, filesystem: FilesystemType
    ) -> FilesystemCheckResult: ...

    def repair_succeeded(self, result: ProcessResult) -> bool: ...

    def repair_partial(self, result: ProcessResult) -> bool: ...


class ExtFilesystemAdapter:
    filesystems = (FilesystemType.EXT2, FilesystemType.EXT3, FilesystemType.EXT4)
    capabilities = AdapterCapabilities(
        requires_unmounted=True,
        supports_dry_run=True,
        supports_verification=True,
        supports_rollback=False,
        repair_supported=True,
        limitations=(
            "ARES usa e2fsck -p: solo corrige problemas que e2fsck considera seguros para reparación automática.",
            "No se implementa e2undo ni reparación interactiva en esta versión.",
        ),
    )

    @property
    def required_tools(self) -> tuple[str, ...]:
        return ("e2fsck",)

    def check_invocation(self, target: str, *, image: bool = False) -> tuple[str, tuple[str, ...]]:
        del image
        return "e2fsck", ("-f", "-n", target)

    def repair_invocation(self, target: str, *, image: bool = False) -> tuple[str, tuple[str, ...]]:
        del image
        return "e2fsck", ("-f", "-p", target)

    def parse_check(
        self, result: ProcessResult, filesystem: FilesystemType
    ) -> FilesystemCheckResult:
        code = result.exit_code
        if code == 0:
            health = FilesystemHealth.HEALTHY
            problems: tuple[str, ...] = ()
        elif code & 4:
            health = FilesystemHealth.INCONSISTENT
            problems = ("filesystem_errors_detected",)
        elif code & (8 | 16 | 32 | 128):
            health = FilesystemHealth.UNKNOWN
            problems = ("filesystem_check_operational_failure",)
        else:
            health = FilesystemHealth.INCONSISTENT
            problems = ("filesystem_changes_would_be_required",)
        return FilesystemCheckResult(
            filesystem=filesystem,
            health=health,
            tool="e2fsck",
            dry_run=True,
            exit_code=code,
            problems=problems,
            evidence=_evidence(result),
            limitations=self.capabilities.limitations,
            duration_ms=result.duration_ms,
        )

    def repair_succeeded(self, result: ProcessResult) -> bool:
        return result.exit_code in {0, 1, 2}

    def repair_partial(self, result: ProcessResult) -> bool:
        return bool(result.exit_code & 4)


class XfsFilesystemAdapter:
    filesystems = (FilesystemType.XFS,)
    capabilities = AdapterCapabilities(
        requires_unmounted=True,
        supports_dry_run=True,
        supports_verification=True,
        supports_rollback=False,
        repair_supported=True,
        limitations=(
            "ARES no usa xfs_repair -L; un dirty log que requiera borrado bloquea la reparación automática.",
            "xfs_repair -n no detecta todas las inconsistencias posibles.",
        ),
    )

    @property
    def required_tools(self) -> tuple[str, ...]:
        return ("xfs_repair",)

    def check_invocation(self, target: str, *, image: bool = False) -> tuple[str, tuple[str, ...]]:
        args = ("-n", "-f", target) if image else ("-n", target)
        return "xfs_repair", args

    def repair_invocation(self, target: str, *, image: bool = False) -> tuple[str, tuple[str, ...]]:
        args = ("-f", target) if image else (target,)
        return "xfs_repair", args

    def parse_check(
        self, result: ProcessResult, filesystem: FilesystemType
    ) -> FilesystemCheckResult:
        if result.exit_code == 0:
            health = FilesystemHealth.HEALTHY
            problems: tuple[str, ...] = ()
        elif result.exit_code == 1:
            health = FilesystemHealth.INCONSISTENT
            problems = ("filesystem_corruption_detected",)
        elif result.exit_code == 2:
            health = FilesystemHealth.INCONSISTENT
            problems = ("xfs_dirty_log_requires_safe_replay",)
        else:
            health = FilesystemHealth.UNKNOWN
            problems = ("filesystem_check_operational_failure",)
        return FilesystemCheckResult(
            filesystem=filesystem,
            health=health,
            tool="xfs_repair",
            dry_run=True,
            exit_code=result.exit_code,
            problems=problems,
            evidence=_evidence(result),
            limitations=self.capabilities.limitations,
            duration_ms=result.duration_ms,
        )

    def repair_succeeded(self, result: ProcessResult) -> bool:
        return result.exit_code == 0

    def repair_partial(self, result: ProcessResult) -> bool:
        return False


class BtrfsFilesystemAdapter:
    filesystems = (FilesystemType.BTRFS,)
    capabilities = AdapterCapabilities(
        requires_unmounted=True,
        supports_dry_run=True,
        supports_verification=True,
        supports_rollback=False,
        repair_supported=False,
        limitations=(
            "btrfs check --repair no se automatiza: la documentación upstream exige criterio experto y puede agravar corrupción.",
            "ARES limita Btrfs 1.0 a inspección estructural read-only.",
        ),
    )

    @property
    def required_tools(self) -> tuple[str, ...]:
        return ("btrfs",)

    def check_invocation(self, target: str, *, image: bool = False) -> tuple[str, tuple[str, ...]]:
        del image
        return "btrfs", ("check", "--readonly", target)

    def repair_invocation(self, target: str, *, image: bool = False) -> tuple[str, tuple[str, ...]]:
        del target, image
        raise PermissionError("FILESYSTEM_REPAIR_UNSUPPORTED_BTRFS")

    def parse_check(
        self, result: ProcessResult, filesystem: FilesystemType
    ) -> FilesystemCheckResult:
        health = (
            FilesystemHealth.HEALTHY if result.exit_code == 0 else FilesystemHealth.INCONSISTENT
        )
        problems = () if result.exit_code == 0 else ("filesystem_structural_errors_detected",)
        return FilesystemCheckResult(
            filesystem=filesystem,
            health=health,
            tool="btrfs",
            dry_run=True,
            exit_code=result.exit_code,
            problems=problems,
            evidence=_evidence(result),
            limitations=self.capabilities.limitations,
            duration_ms=result.duration_ms,
        )

    def repair_succeeded(self, result: ProcessResult) -> bool:
        del result
        return False

    def repair_partial(self, result: ProcessResult) -> bool:
        del result
        return False


class NtfsFilesystemAdapter:
    filesystems = (FilesystemType.NTFS,)
    capabilities = AdapterCapabilities(
        requires_unmounted=True,
        supports_dry_run=True,
        supports_verification=True,
        supports_rollback=False,
        repair_supported=True,
        limitations=(
            "ntfsfix solo corrige inconsistencias NTFS fundamentales y solicita comprobación posterior de Windows.",
            "Un exit code exitoso de ntfsfix no equivale a una verificación completa de CHKDSK.",
        ),
    )

    @property
    def required_tools(self) -> tuple[str, ...]:
        return ("ntfsfix",)

    def check_invocation(self, target: str, *, image: bool = False) -> tuple[str, tuple[str, ...]]:
        del image
        return "ntfsfix", ("-n", target)

    def repair_invocation(self, target: str, *, image: bool = False) -> tuple[str, tuple[str, ...]]:
        del image
        return "ntfsfix", (target,)

    def parse_check(
        self, result: ProcessResult, filesystem: FilesystemType
    ) -> FilesystemCheckResult:
        health = (
            FilesystemHealth.HEALTHY if result.exit_code == 0 else FilesystemHealth.INCONSISTENT
        )
        problems = () if result.exit_code == 0 else ("ntfs_common_errors_detected",)
        return FilesystemCheckResult(
            filesystem=filesystem,
            health=health,
            tool="ntfsfix",
            dry_run=True,
            exit_code=result.exit_code,
            problems=problems,
            evidence=_evidence(result),
            limitations=self.capabilities.limitations,
            duration_ms=result.duration_ms,
        )

    def repair_succeeded(self, result: ProcessResult) -> bool:
        return result.exit_code == 0

    def repair_partial(self, result: ProcessResult) -> bool:
        del result
        return False


_ADAPTERS: tuple[FilesystemAdapter, ...] = (
    ExtFilesystemAdapter(),
    XfsFilesystemAdapter(),
    BtrfsFilesystemAdapter(),
    NtfsFilesystemAdapter(),
)


def adapter_for(filesystem: FilesystemType) -> FilesystemAdapter:
    for adapter in _ADAPTERS:
        if filesystem in adapter.filesystems:
            return adapter
    raise LookupError("FILESYSTEM_TYPE_UNSUPPORTED")


def supported_filesystems() -> tuple[str, ...]:
    return tuple(item.value for adapter in _ADAPTERS for item in adapter.filesystems)


def _evidence(result: ProcessResult) -> tuple[str, ...]:
    evidence = [f"tool={result.tool}", f"exit_code={result.exit_code}"]
    if result.stderr:
        evidence.append("stderr_present=true")
    if result.stdout:
        evidence.append("stdout_present=true")
    return tuple(evidence)
