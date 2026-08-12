"""Bounded semantic Tools for System Recovery; no shell strings and no LLM argv."""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from ares.recovery.models import (
    ConfigurationDiff,
    DiagnosticEvidence,
    EvidenceSeverity,
    PackageManagerKind,
    RecoveryIssue,
    RecoveryIssueCode,
    RecoveryLayer,
    SystemBootMode,
)

_MAX_OUTPUT_BYTES = 2_000_000
_MAX_CONFIG_BYTES = 64_000
_ALLOWED_CONFIG_RELATIVE = frozenset(
    {
        "etc/fstab",
        "etc/default/grub",
        "etc/apt/sources.list",
    }
)
_ALLOWED_TOOLS = frozenset(
    {
        "systemctl",
        "journalctl",
        "dpkg",
        "apt-get",
        "findmnt",
        "blkid",
        "ls",
        "uname",
        "update-initramfs",
    }
)


class RecoveryToolError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ToolResult(Protocol):
    exit_code: int
    stdout: str
    stderr: str


class RecoveryProcessResult:
    def __init__(self, exit_code: int, stdout: str = "", stderr: str = "") -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


class RecoveryProcessRunner(Protocol):
    def available(self, tool: str) -> bool: ...

    async def run(
        self,
        tool: str,
        args: tuple[str, ...],
        *,
        timeout_seconds: float,
        env: dict[str, str] | None = None,
    ) -> RecoveryProcessResult: ...


class SafeRecoveryProcessRunner:
    def available(self, tool: str) -> bool:
        return tool in _ALLOWED_TOOLS and shutil.which(tool) is not None

    async def run(
        self,
        tool: str,
        args: tuple[str, ...],
        *,
        timeout_seconds: float,
        env: dict[str, str] | None = None,
    ) -> RecoveryProcessResult:
        if tool not in _ALLOWED_TOOLS:
            raise RecoveryToolError("RECOVERY_TOOL_NOT_ALLOWED")
        binary = shutil.which(tool)
        if binary is None:
            raise RecoveryToolError(f"RECOVERY_TOOL_UNAVAILABLE_{tool.upper().replace('-', '_')}")
        process = await asyncio.create_subprocess_exec(
            binary,
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout_seconds
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise RecoveryToolError("RECOVERY_TOOL_TIMEOUT") from exc
        if len(stdout) + len(stderr) > _MAX_OUTPUT_BYTES:
            raise RecoveryToolError("RECOVERY_TOOL_OUTPUT_TOO_LARGE")
        return RecoveryProcessResult(
            process.returncode or 0,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )


class SystemdDiagnosticTool:
    def __init__(self, runner: RecoveryProcessRunner) -> None:
        self.runner = runner

    async def inspect(self, root: Path) -> tuple[DiagnosticEvidence, ...]:
        if not self.runner.available("systemctl"):
            return (_evidence("systemd", "tool-unavailable", "systemctl unavailable", 0.4),)
        args = ("--root", str(root), "--no-pager", "--plain", "--failed")
        failed = await self.runner.run("systemctl", args, timeout_seconds=8)
        state = await self.runner.run(
            "systemctl", ("--root", str(root), "is-system-running"), timeout_seconds=5
        )
        evidence: list[DiagnosticEvidence] = []
        state_text = state.stdout.strip() or state.stderr.strip() or "unknown"
        evidence.append(
            _evidence(
                "systemd",
                "system-state",
                f"systemd state: {state_text}",
                0.95,
                severity=(
                    EvidenceSeverity.WARNING
                    if state_text in {"degraded", "maintenance"}
                    else EvidenceSeverity.INFO
                ),
                metadata={"state": state_text, "exit_code": state.exit_code},
            )
        )
        for line in failed.stdout.splitlines():
            stripped = " ".join(line.split())
            if not stripped or stripped.startswith("UNIT ") or stripped.startswith("0 loaded"):
                continue
            unit = stripped.split(" ", 1)[0]
            severity = EvidenceSeverity.ERROR if unit.endswith((".mount", ".service")) else EvidenceSeverity.WARNING
            evidence.append(
                _evidence(
                    "systemd",
                    "failed-unit",
                    f"failed unit: {unit}",
                    0.9,
                    severity=severity,
                    metadata={"unit": unit},
                )
            )
        return tuple(evidence)


class JournalDiagnosticTool:
    _PATTERNS = (
        ("mount failure", RecoveryIssueCode.MOUNT_FAILURE, RecoveryLayer.FILESYSTEM),
        ("dependency failed", RecoveryIssueCode.SERVICE_DEPENDENCY_FAILED, RecoveryLayer.SYSTEMD),
        ("kernel panic", RecoveryIssueCode.BOOT_DEGRADED, RecoveryLayer.KERNEL),
        ("timed out", RecoveryIssueCode.CRITICAL_SERVICE_FAILED, RecoveryLayer.SERVICES),
        ("out of memory", RecoveryIssueCode.OOM, RecoveryLayer.KERNEL),
        ("i/o error", RecoveryIssueCode.IO_ERROR, RecoveryLayer.STORAGE),
        ("permission denied", RecoveryIssueCode.PERMISSION_ERROR, RecoveryLayer.CONFIGURATION),
        ("no such device", RecoveryIssueCode.MISSING_DEVICE, RecoveryLayer.STORAGE),
        ("dpkg", RecoveryIssueCode.PACKAGE_INTERRUPTED, RecoveryLayer.PACKAGES),
    )

    def __init__(self, runner: RecoveryProcessRunner) -> None:
        self.runner = runner

    async def inspect(self, root: Path, *, max_entries: int = 300) -> tuple[DiagnosticEvidence, ...]:
        if not self.runner.available("journalctl"):
            return (_evidence("journal", "tool-unavailable", "journalctl unavailable", 0.4),)
        directory = root / "var/log/journal"
        args = (
            "--directory",
            str(directory),
            "--output=json",
            "--no-pager",
            "--lines",
            str(min(max_entries, 500)),
        )
        result = await self.runner.run("journalctl", args, timeout_seconds=10)
        evidence: list[DiagnosticEvidence] = []
        for raw in result.stdout.splitlines()[:max_entries]:
            try:
                item = json.loads(raw)
            except json.JSONDecodeError:
                continue
            message = str(item.get("MESSAGE", ""))[:1024]
            lower = message.lower()
            for phrase, code, layer in self._PATTERNS:
                if phrase in lower:
                    evidence.append(
                        DiagnosticEvidence(
                            source="journalctl",
                            severity=EvidenceSeverity.ERROR,
                            subsystem=layer.value,
                            event=code.value,
                            normalized_message=_normalize(message),
                            confidence=0.86,
                            metadata={"priority": str(item.get("PRIORITY", ""))},
                        )
                    )
                    break
        return tuple(evidence)


class PackageManagerAdapter(Protocol):
    kind: PackageManagerKind

    async def diagnose(self, root: Path) -> tuple[DiagnosticEvidence, ...]: ...

    async def repair(self, root: Path, operation: str) -> tuple[str, ...]: ...


class AptPackageManagerAdapter:
    kind = PackageManagerKind.APT

    def __init__(self, runner: RecoveryProcessRunner) -> None:
        self.runner = runner

    def detected(self, root: Path) -> bool:
        os_release = root / "etc/os-release"
        if not os_release.is_file():
            return False
        text = os_release.read_text(encoding="utf-8", errors="replace").lower()
        return any(token in text for token in ("id=debian", "id=ubuntu", "id_like=debian"))

    async def diagnose(self, root: Path) -> tuple[DiagnosticEvidence, ...]:
        if not self.detected(root):
            return ()
        evidence: list[DiagnosticEvidence] = []
        if self.runner.available("dpkg"):
            audit = await self.runner.run(
                "dpkg", ("--root", str(root), "--audit"), timeout_seconds=10
            )
            text = _normalize(audit.stdout + "\n" + audit.stderr)
            if text:
                evidence.append(
                    _evidence(
                        "dpkg",
                        "package-audit",
                        text,
                        0.95,
                        severity=EvidenceSeverity.ERROR,
                        metadata={"exit_code": audit.exit_code},
                    )
                )
        updates = root / "var/lib/dpkg/updates"
        if updates.is_dir() and any(updates.iterdir()):
            evidence.append(
                _evidence(
                    "dpkg",
                    "interrupted-transaction",
                    "dpkg has pending update fragments from an interrupted transaction",
                    0.98,
                    severity=EvidenceSeverity.ERROR,
                )
            )
        status = root / "var/lib/dpkg/status"
        if not status.is_file():
            evidence.append(
                _evidence(
                    "dpkg",
                    "database-missing",
                    "dpkg status database is missing",
                    0.99,
                    severity=EvidenceSeverity.CRITICAL,
                )
            )
        return tuple(evidence)

    async def repair(self, root: Path, operation: str) -> tuple[str, ...]:
        if operation == "configure-pending":
            if not self.runner.available("dpkg"):
                raise RecoveryToolError("DPKG_UNAVAILABLE")
            result = await self.runner.run(
                "dpkg", ("--root", str(root), "--configure", "-a"), timeout_seconds=120
            )
            if result.exit_code != 0:
                raise RecoveryToolError("DPKG_CONFIGURE_FAILED")
            return ("dpkg --configure -a semantic repair completed",)
        if operation == "fix-broken-offline":
            if not self.runner.available("apt-get"):
                raise RecoveryToolError("APT_GET_UNAVAILABLE")
            env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
            result = await self.runner.run(
                "apt-get",
                (
                    "-o",
                    f"Dir={root}",
                    "--no-download",
                    "--fix-broken",
                    "install",
                    "-y",
                ),
                timeout_seconds=180,
                env=env,
            )
            combined = (result.stdout + "\n" + result.stderr).lower()
            if "unable to fetch" in combined or "download" in combined and result.exit_code != 0:
                raise RecoveryToolError("PACKAGE_REPAIR_EXTERNAL_DEPENDENCY")
            if result.exit_code != 0:
                raise RecoveryToolError("APT_FIX_BROKEN_FAILED")
            return ("APT broken dependencies repaired using local package cache only",)
        raise RecoveryToolError("PACKAGE_REPAIR_OPERATION_UNSUPPORTED")


class ConfigurationRecoveryTool:
    def build_diff(self, root: Path, relative_path: str, proposed_text: str) -> ConfigurationDiff:
        normalized = relative_path.lstrip("/")
        if normalized not in _ALLOWED_CONFIG_RELATIVE:
            raise RecoveryToolError("CONFIGURATION_PATH_NOT_ALLOWED")
        path = (root / normalized).resolve()
        root_real = root.resolve()
        if not path.is_relative_to(root_real) or path.is_symlink() or not path.is_file():
            raise RecoveryToolError("CONFIGURATION_TARGET_INVALID")
        current = path.read_bytes()
        if len(current) > _MAX_CONFIG_BYTES or len(proposed_text.encode()) > _MAX_CONFIG_BYTES:
            raise RecoveryToolError("CONFIGURATION_TOO_LARGE")
        current_text = current.decode("utf-8", errors="strict")
        diff = tuple(
            difflib.unified_diff(
                current_text.splitlines(),
                proposed_text.splitlines(),
                fromfile=f"a/{normalized}",
                tofile=f"b/{normalized}",
                lineterm="",
            )
        )
        return ConfigurationDiff(
            path=f"/{normalized}",
            current_sha256=hashlib.sha256(current).hexdigest(),
            current_text=current_text,
            proposed_text=proposed_text,
            unified_diff=diff,
        )

    def apply(self, root: Path, change: ConfigurationDiff) -> tuple[str, str]:
        relative = change.path.lstrip("/")
        if relative not in _ALLOWED_CONFIG_RELATIVE:
            raise RecoveryToolError("CONFIGURATION_PATH_NOT_ALLOWED")
        target = (root / relative).resolve()
        if hashlib.sha256(target.read_bytes()).hexdigest() != change.current_sha256:
            raise RecoveryToolError("CONFIGURATION_CHANGED_SINCE_PLAN")
        backup = target.with_name(f".{target.name}.ares-recovery-backup")
        backup.write_bytes(target.read_bytes())
        os.chmod(backup, 0o600)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(change.proposed_text.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        if relative == "etc/fstab":
            self._validate_fstab(change.proposed_text)
        return str(backup), hashlib.sha256(target.read_bytes()).hexdigest()

    def rollback(self, root: Path, change: ConfigurationDiff) -> None:
        target = root / change.path.lstrip("/")
        backup = target.with_name(f".{target.name}.ares-recovery-backup")
        if not backup.is_file():
            raise RecoveryToolError("CONFIGURATION_ROLLBACK_UNAVAILABLE")
        os.replace(backup, target)

    @staticmethod
    def _validate_fstab(text: str) -> None:
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if len(stripped.split()) < 4:
                raise RecoveryToolError("FSTAB_VALIDATION_FAILED")


class KernelDiagnosticTool:
    def inspect(self, root: Path) -> tuple[DiagnosticEvidence, ...]:
        boot = root / "boot"
        kernels = sorted(path.name for path in boot.glob("vmlinuz-*") if path.is_file())
        initramfs = {path.name.removeprefix("initrd.img-") for path in boot.glob("initrd.img-*")}
        evidence: list[DiagnosticEvidence] = []
        for kernel in kernels:
            version = kernel.removeprefix("vmlinuz-")
            if version not in initramfs:
                evidence.append(
                    _evidence(
                        "kernel",
                        "initramfs-missing",
                        f"kernel {version} has no matching initramfs",
                        0.98,
                        severity=EvidenceSeverity.ERROR,
                        metadata={"kernel": version},
                    )
                )
        if not kernels:
            evidence.append(
                _evidence(
                    "kernel",
                    "kernel-missing",
                    "no installed kernel image was found under /boot",
                    0.98,
                    severity=EvidenceSeverity.CRITICAL,
                )
            )
        return tuple(evidence)


class InitramfsRecoveryTool:
    def __init__(self, runner: RecoveryProcessRunner) -> None:
        self.runner = runner

    async def rebuild(self, root: Path, kernel_version: str) -> tuple[str, ...]:
        if not kernel_version or "/" in kernel_version or ".." in kernel_version:
            raise RecoveryToolError("INITRAMFS_KERNEL_INVALID")
        os_release = root / "etc/os-release"
        text = os_release.read_text(encoding="utf-8", errors="replace").lower() if os_release.is_file() else ""
        if not any(token in text for token in ("id=debian", "id=ubuntu", "id_like=debian")):
            raise RecoveryToolError("INITRAMFS_DISTRIBUTION_UNSUPPORTED")
        if not self.runner.available("update-initramfs"):
            raise RecoveryToolError("UPDATE_INITRAMFS_UNAVAILABLE")
        result = await self.runner.run(
            "update-initramfs",
            ("-c", "-k", kernel_version, "-b", str(root / "boot")),
            timeout_seconds=180,
        )
        if result.exit_code != 0:
            raise RecoveryToolError("INITRAMFS_REBUILD_FAILED")
        artifact = root / "boot" / f"initrd.img-{kernel_version}"
        if not artifact.is_file():
            raise RecoveryToolError("INITRAMFS_VERIFICATION_FAILED")
        return (f"rebuilt initramfs for kernel {kernel_version}",)


class EmergencyModeTool:
    @staticmethod
    def classify(systemd_evidence: Sequence[DiagnosticEvidence]) -> SystemBootMode:
        messages = " ".join(item.normalized_message.lower() for item in systemd_evidence)
        if "emergency" in messages or "maintenance" in messages:
            return SystemBootMode.EMERGENCY
        if "rescue" in messages:
            return SystemBootMode.RESCUE
        if "single-user" in messages or "single user" in messages:
            return SystemBootMode.SINGLE_USER
        if "running" in messages or "degraded" in messages:
            return SystemBootMode.NORMAL
        return SystemBootMode.UNKNOWN


def detect_package_manager(root: Path) -> PackageManagerKind:
    os_release = root / "etc/os-release"
    if not os_release.is_file():
        return PackageManagerKind.UNKNOWN
    text = os_release.read_text(encoding="utf-8", errors="replace").lower()
    if any(token in text for token in ("id=debian", "id=ubuntu", "id_like=debian")):
        return PackageManagerKind.APT
    if "id=fedora" in text or "id_like=fedora" in text:
        return PackageManagerKind.DNF
    if "id=arch" in text:
        return PackageManagerKind.PACMAN
    if "id=opensuse" in text or "id=sles" in text:
        return PackageManagerKind.ZYPPER
    return PackageManagerKind.UNKNOWN


def evidence_to_issues(evidence: Sequence[DiagnosticEvidence]) -> tuple[RecoveryIssue, ...]:
    mapping = {
        "failed-unit": (RecoveryIssueCode.CRITICAL_SERVICE_FAILED, RecoveryLayer.SERVICES),
        "package-audit": (RecoveryIssueCode.PACKAGE_BROKEN_DEPENDENCIES, RecoveryLayer.PACKAGES),
        "interrupted-transaction": (RecoveryIssueCode.PACKAGE_INTERRUPTED, RecoveryLayer.PACKAGES),
        "database-missing": (RecoveryIssueCode.PACKAGE_DATABASE_INCONSISTENT, RecoveryLayer.PACKAGES),
        "initramfs-missing": (RecoveryIssueCode.INITRAMFS_MISSING, RecoveryLayer.INITRAMFS),
        "kernel-missing": (RecoveryIssueCode.KERNEL_INITRAMFS_MISMATCH, RecoveryLayer.KERNEL),
        RecoveryIssueCode.MOUNT_FAILURE.value: (RecoveryIssueCode.MOUNT_FAILURE, RecoveryLayer.FILESYSTEM),
        RecoveryIssueCode.IO_ERROR.value: (RecoveryIssueCode.IO_ERROR, RecoveryLayer.STORAGE),
        RecoveryIssueCode.OOM.value: (RecoveryIssueCode.OOM, RecoveryLayer.KERNEL),
        RecoveryIssueCode.PERMISSION_ERROR.value: (RecoveryIssueCode.PERMISSION_ERROR, RecoveryLayer.CONFIGURATION),
        RecoveryIssueCode.MISSING_DEVICE.value: (RecoveryIssueCode.MISSING_DEVICE, RecoveryLayer.STORAGE),
        RecoveryIssueCode.SERVICE_DEPENDENCY_FAILED.value: (
            RecoveryIssueCode.SERVICE_DEPENDENCY_FAILED,
            RecoveryLayer.SYSTEMD,
        ),
    }
    issues: list[RecoveryIssue] = []
    for item in evidence:
        selected = mapping.get(item.event)
        if selected is None:
            continue
        code, layer = selected
        issues.append(
            RecoveryIssue(
                code=code,
                layer=layer,
                severity=item.severity,
                summary=item.normalized_message,
                evidence_ids=(item.id,),
                confidence=item.confidence,
                affected_components=(item.subsystem,),
            )
        )
    return tuple(issues)


def _evidence(
    source: str,
    event: str,
    message: str,
    confidence: float,
    *,
    severity: EvidenceSeverity = EvidenceSeverity.INFO,
    metadata: dict[str, object] | None = None,
) -> DiagnosticEvidence:
    return DiagnosticEvidence(
        source=source,
        severity=severity,
        subsystem=source,
        event=event,
        normalized_message=_normalize(message),
        confidence=confidence,
        metadata=dict(metadata or {}),
    )


def _normalize(message: str) -> str:
    return " ".join(message.replace("\x00", "").split())[:2048] or "empty diagnostic message"
