"""Typed, read-only storage probes used by storage capabilities."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import stat
from pathlib import Path
from time import monotonic
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from ares.events import AresEvent, EventBus

_MAX_OUTPUT_BYTES = 4_000_000
_SAFE_DEVICE = re.compile(r"^/dev/[A-Za-z0-9_.:+/-]{1,128}$")


class ToolAvailability(BaseModel):
    """Availability of one low-level diagnostic tool."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: str
    available: bool
    reason: str | None = None


class ProcessResult(BaseModel):
    """Bounded result from one fixed process invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: str
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: float = Field(ge=0)


class BlockDeviceProbe(BaseModel):
    """Sanitized block-device observation from lsblk or boot inventory."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    path: str
    device_type: str
    size_bytes: int = Field(ge=0)
    read_only: bool = False
    removable: bool = False
    model: str | None = None
    vendor: str | None = None
    transport: str | None = None
    parent_path: str | None = None
    filesystem_type: str | None = None
    filesystem_version: str | None = None
    filesystem_uuid: str | None = None
    filesystem_label: str | None = None
    mountpoints: tuple[str, ...] = ()
    hardware_identity: str | None = None


class MountProbe(BaseModel):
    """Read-only mount observation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str
    target: str
    filesystem_type: str | None = None
    options: tuple[str, ...] = ()


class UsageProbe(BaseModel):
    """Filesystem usage returned by df."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str
    target: str
    total_bytes: int = Field(ge=0)
    used_bytes: int = Field(ge=0)
    available_bytes: int = Field(ge=0)
    used_percent: float = Field(ge=0, le=100)


class BlkidProbe(BaseModel):
    """Parsed blkid export record. Device probing is broker-gated in this slice."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    device: str
    filesystem_type: str | None = None
    uuid: str | None = None
    label: str | None = None
    version: str | None = None


class SmartProbe(BaseModel):
    """Parsed SMART health observation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    device: str
    status: str
    passed: bool | None = None
    temperature_celsius: float | None = None
    reason: str | None = None


class OperatingSystemProbe(BaseModel):
    """OS metadata observed from an already-mounted filesystem."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str
    mountpoint: str
    name: str
    version: str | None = None
    os_id: str | None = None


class StorageEvidence(BaseModel):
    """Complete low-level evidence bundle consumed by the storage workflow."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    devices: tuple[BlockDeviceProbe, ...]
    mounts: tuple[MountProbe, ...] = ()
    usage: tuple[UsageProbe, ...] = ()
    smart: tuple[SmartProbe, ...] = ()
    operating_systems: tuple[OperatingSystemProbe, ...] = ()
    tool_availability: tuple[ToolAvailability, ...] = ()
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


class ProcessRunner(Protocol):
    """Narrow process boundary that can be replaced by deterministic tests."""

    def inspect(self, tool: str) -> ToolAvailability:
        """Return whether a trusted executable exists."""

    async def run(
        self,
        tool: str,
        args: tuple[str, ...],
        *,
        timeout_seconds: float,
    ) -> ProcessResult:
        """Run one fixed executable without a shell."""


class SafeProcessRunner:
    """Execute only root-owned, non-writable binaries with a minimal environment."""

    def inspect(self, tool: str) -> ToolAvailability:
        path = shutil.which(tool)
        if path is None:
            return ToolAvailability(tool=tool, available=False, reason="tool_not_installed")
        try:
            info = os.stat(path, follow_symlinks=True)
        except OSError:
            return ToolAvailability(tool=tool, available=False, reason="tool_unavailable")
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            return ToolAvailability(tool=tool, available=False, reason="untrusted_executable")
        return ToolAvailability(tool=tool, available=True)

    async def run(
        self,
        tool: str,
        args: tuple[str, ...],
        *,
        timeout_seconds: float,
    ) -> ProcessResult:
        availability = self.inspect(tool)
        if not availability.available:
            raise FileNotFoundError(availability.reason or "tool_unavailable")
        executable = shutil.which(tool)
        if executable is None:  # pragma: no cover - protected by inspect
            raise FileNotFoundError("tool_not_installed")
        started = monotonic()
        process = await asyncio.create_subprocess_exec(
            executable,
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd="/",
            env={"LANG": "C", "LC_ALL": "C", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), timeout_seconds
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            raise
        if len(stdout_bytes) > _MAX_OUTPUT_BYTES or len(stderr_bytes) > _MAX_OUTPUT_BYTES:
            raise ValueError("tool_output_too_large")
        return ProcessResult(
            tool=tool,
            exit_code=process.returncode or 0,
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace")[:16_384],
            duration_ms=(monotonic() - started) * 1_000,
        )


class StorageToolSuite:
    """Fixed set of passive probes. Device-opening tools remain broker-gated."""

    def __init__(
        self,
        inventory_path: Path,
        *,
        runner: ProcessRunner | None = None,
        process_probes_enabled: bool = True,
    ) -> None:
        self.inventory_path = inventory_path
        self.runner = runner or SafeProcessRunner()
        self.process_probes_enabled = process_probes_enabled

    async def collect(self, event_bus: EventBus, correlation_id: str) -> StorageEvidence:
        availability: list[ToolAvailability] = []
        warnings: list[str] = []
        errors: list[str] = []
        devices: tuple[BlockDeviceProbe, ...] = ()
        mounts: tuple[MountProbe, ...] = ()
        usage: tuple[UsageProbe, ...] = ()

        if self.process_probes_enabled:
            lsblk = await self._probe(
                event_bus,
                correlation_id,
                "lsblk",
                (
                    "--json",
                    "--bytes",
                    "--output",
                    "NAME,PATH,TYPE,SIZE,RO,RM,MODEL,VENDOR,TRAN,PKNAME,FSTYPE,FSVER,UUID,LABEL,MOUNTPOINTS,SERIAL,WWN",
                ),
                timeout_seconds=4,
            )
            availability.append(lsblk[0])
            if lsblk[1] is not None and lsblk[1].exit_code == 0:
                try:
                    devices = parse_lsblk_json(lsblk[1].stdout)
                except ValueError:
                    warnings.append("lsblk_parse_error")
            elif lsblk[1] is not None:
                warnings.append("lsblk_command_failed")

            findmnt = await self._probe(
                event_bus,
                correlation_id,
                "findmnt",
                ("--json", "--bytes", "--output", "SOURCE,TARGET,FSTYPE,OPTIONS"),
                timeout_seconds=4,
            )
            availability.append(findmnt[0])
            if findmnt[1] is not None and findmnt[1].exit_code == 0:
                try:
                    mounts = parse_findmnt_json(findmnt[1].stdout)
                except ValueError:
                    warnings.append("findmnt_parse_error")

            df = await self._probe(
                event_bus,
                correlation_id,
                "df",
                ("-B1", "--output=source,size,used,avail,pcent,target"),
                timeout_seconds=4,
            )
            availability.append(df[0])
            if df[1] is not None and df[1].exit_code == 0:
                usage = parse_df_output(df[1].stdout)
        else:
            availability.extend(
                ToolAvailability(tool=name, available=False, reason="process_probes_disabled")
                for name in ("lsblk", "findmnt", "df")
            )

        if not devices:
            fallback = self._read_boot_inventory()
            if fallback:
                devices = fallback
                warnings.append("using_boot_inventory_fallback")
            else:
                errors.append("block_device_evidence_unavailable")

        for name in ("blkid", "smartctl"):
            state = self.runner.inspect(name)
            if state.available:
                state = ToolAvailability(
                    tool=name,
                    available=False,
                    reason="privileged_broker_required",
                )
            availability.append(state)

        smart_reason = next(
            (item.reason for item in availability if item.tool == "smartctl"),
            "tool_unavailable",
        )
        smart = tuple(
            SmartProbe(device=device.path, status="unavailable", reason=smart_reason)
            for device in devices
            if device.device_type == "disk"
        )
        operating_systems = detect_operating_systems(mounts)
        return StorageEvidence(
            devices=devices,
            mounts=mounts,
            usage=usage,
            smart=smart,
            operating_systems=operating_systems,
            tool_availability=tuple(availability),
            warnings=tuple(dict.fromkeys(warnings)),
            errors=tuple(dict.fromkeys(errors)),
        )

    async def _probe(
        self,
        event_bus: EventBus,
        correlation_id: str,
        tool: str,
        args: tuple[str, ...],
        *,
        timeout_seconds: float,
    ) -> tuple[ToolAvailability, ProcessResult | None]:
        state = self.runner.inspect(tool)
        if not state.available:
            await _tool_event(
                event_bus, correlation_id, "tool.execution.completed", tool, state.reason
            )
            return state, None
        await _tool_event(event_bus, correlation_id, "tool.execution.started", tool, None)
        try:
            result = await self.runner.run(tool, args, timeout_seconds=timeout_seconds)
        except TimeoutError:
            state = ToolAvailability(tool=tool, available=False, reason="timeout")
            await _tool_event(
                event_bus, correlation_id, "tool.execution.completed", tool, "timeout"
            )
            return state, None
        except (FileNotFoundError, PermissionError, OSError, ValueError) as exc:
            reason = str(exc) or "command_failed"
            state = ToolAvailability(tool=tool, available=False, reason=reason[:64])
            await _tool_event(
                event_bus, correlation_id, "tool.execution.completed", tool, state.reason
            )
            return state, None
        reason = None if result.exit_code == 0 else "command_failed"
        await _tool_event(event_bus, correlation_id, "tool.execution.completed", tool, reason)
        return state, result

    def _read_boot_inventory(self) -> tuple[BlockDeviceProbe, ...]:
        try:
            if self.inventory_path.is_symlink() or not self.inventory_path.is_file():
                return ()
            if self.inventory_path.stat().st_size > _MAX_OUTPUT_BYTES:
                return ()
            payload = json.loads(self.inventory_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ()
        probes = payload.get("probes") if isinstance(payload, dict) else None
        storage = probes.get("storage") if isinstance(probes, dict) else None
        data = storage.get("data") if isinstance(storage, dict) else None
        raw = data.get("blockdevices") if isinstance(data, dict) else None
        if not isinstance(raw, list):
            return ()
        output: list[BlockDeviceProbe] = []
        for item in raw[:256]:
            _append_inventory_device(item, output, None)
        return tuple(output)


async def _tool_event(
    event_bus: EventBus,
    correlation_id: str,
    event_type: str,
    tool: str,
    result: str | None,
) -> None:
    await event_bus.publish(
        AresEvent(
            name=event_type,
            source="storage.tool-layer",
            correlation_id=correlation_id,
            payload={
                "actor": "ares-backend",
                "reason": "storage.disk-analysis evidence collection",
                "resource": "local-system",
                "tool": tool,
                "result": result or "ok",
                "decision": "observe_only",
            },
        )
    )


def parse_lsblk_json(text: str) -> tuple[BlockDeviceProbe, ...]:
    """Parse lsblk JSON without trusting arbitrary fields or paths."""

    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise ValueError("invalid lsblk json") from exc
    raw = payload.get("blockdevices") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        raise ValueError("missing blockdevices")
    output: list[BlockDeviceProbe] = []
    for item in raw[:256]:
        _append_lsblk_device(item, output, None)
    return tuple(output)


def _append_lsblk_device(raw: object, output: list[BlockDeviceProbe], parent: str | None) -> None:
    if not isinstance(raw, dict) or len(output) >= 512:
        return
    path = raw.get("path")
    name = raw.get("name")
    if (
        not isinstance(path, str)
        or _SAFE_DEVICE.fullmatch(path) is None
        or not isinstance(name, str)
    ):
        return
    identity_parts = [raw.get("wwn"), raw.get("serial"), raw.get("model"), path]
    identity = _identity_hash(tuple(item for item in identity_parts if isinstance(item, str)))
    mountpoints = raw.get("mountpoints")
    safe_mounts = (
        tuple(
            item[:4096]
            for item in mountpoints
            if isinstance(item, str) and item.startswith("/") and "\x00" not in item
        )
        if isinstance(mountpoints, list)
        else ()
    )
    parent_name = raw.get("pkname")
    parent_path = parent
    if parent_path is None and isinstance(parent_name, str) and parent_name:
        candidate = f"/dev/{parent_name}"
        parent_path = candidate if _SAFE_DEVICE.fullmatch(candidate) else None
    output.append(
        BlockDeviceProbe(
            name=_clean(name, 96) or "unknown",
            path=path,
            device_type=_clean(raw.get("type"), 24) or "unknown",
            size_bytes=_nonnegative_int(raw.get("size")),
            read_only=_as_bool(raw.get("ro")),
            removable=_as_bool(raw.get("rm")),
            model=_clean(raw.get("model"), 128),
            vendor=_clean(raw.get("vendor"), 64),
            transport=_clean(raw.get("tran"), 32),
            parent_path=parent_path,
            filesystem_type=_clean(raw.get("fstype"), 64),
            filesystem_version=_clean(raw.get("fsver"), 32),
            filesystem_uuid=_clean(raw.get("uuid"), 128),
            filesystem_label=_clean(raw.get("label"), 128),
            mountpoints=safe_mounts,
            hardware_identity=identity,
        )
    )
    children = raw.get("children")
    if isinstance(children, list):
        for child in children[:128]:
            _append_lsblk_device(child, output, path)


def _append_inventory_device(
    raw: object,
    output: list[BlockDeviceProbe],
    parent: str | None,
) -> None:
    if not isinstance(raw, dict) or len(output) >= 512:
        return
    name = raw.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.:+-]{1,96}", name):
        return
    path = f"/dev/{name}"
    output.append(
        BlockDeviceProbe(
            name=name,
            path=path,
            device_type=_clean(raw.get("type"), 24) or "unknown",
            size_bytes=_nonnegative_int(raw.get("size")),
            read_only=_as_bool(raw.get("ro")),
            removable=_as_bool(raw.get("rm")),
            model=_clean(raw.get("model"), 128),
            vendor=_clean(raw.get("vendor"), 64),
            transport=_clean(raw.get("tran"), 32),
            parent_path=parent,
            filesystem_type=_clean(raw.get("fstype"), 64),
            filesystem_version=_clean(raw.get("fsver"), 32),
            filesystem_uuid=_clean(raw.get("uuid"), 128),
            filesystem_label=_clean(raw.get("label"), 128),
            mountpoints=tuple(
                item[:4096]
                for item in raw.get("mountpoints", [])
                if isinstance(item, str) and item.startswith("/")
            ),
            hardware_identity=_identity_hash((name, _clean(raw.get("model"), 128) or "")),
        )
    )
    children = raw.get("children")
    if isinstance(children, list):
        for child in children[:128]:
            _append_inventory_device(child, output, path)


def parse_findmnt_json(text: str) -> tuple[MountProbe, ...]:
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise ValueError("invalid findmnt json") from exc
    raw = payload.get("filesystems") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        raise ValueError("missing filesystems")
    output: list[MountProbe] = []
    for item in raw[:512]:
        if not isinstance(item, dict):
            continue
        source = item.get("source")
        target = item.get("target")
        if not isinstance(source, str) or not isinstance(target, str) or not target.startswith("/"):
            continue
        options = item.get("options")
        output.append(
            MountProbe(
                source=source[:512],
                target=target[:4096],
                filesystem_type=_clean(item.get("fstype"), 64),
                options=tuple(part for part in options.split(",") if part)[:64]
                if isinstance(options, str)
                else (),
            )
        )
    return tuple(output)


def parse_df_output(text: str) -> tuple[UsageProbe, ...]:
    output: list[UsageProbe] = []
    for line in text.splitlines()[1:513]:
        parts = line.split(maxsplit=5)
        if len(parts) != 6:
            continue
        source, size, used, available, percent, target = parts
        if not target.startswith("/") or not percent.endswith("%"):
            continue
        try:
            output.append(
                UsageProbe(
                    source=source[:512],
                    target=target[:4096],
                    total_bytes=max(0, int(size)),
                    used_bytes=max(0, int(used)),
                    available_bytes=max(0, int(available)),
                    used_percent=min(100.0, max(0.0, float(percent[:-1]))),
                )
            )
        except ValueError:
            continue
    return tuple(output)


def parse_blkid_export(text: str) -> tuple[BlkidProbe, ...]:
    records: list[BlkidProbe] = []
    current: dict[str, str] = {}
    for line in (*text.splitlines(), ""):
        if not line.strip():
            device = current.get("DEVNAME")
            if device and _SAFE_DEVICE.fullmatch(device):
                records.append(
                    BlkidProbe(
                        device=device,
                        filesystem_type=current.get("TYPE"),
                        uuid=current.get("UUID"),
                        label=current.get("LABEL"),
                        version=current.get("VERSION"),
                    )
                )
            current = {}
            continue
        key, separator, value = line.partition("=")
        if separator and key.isupper():
            current[key] = value[:512]
    return tuple(records)


def parse_smartctl_json(text: str, device: str) -> SmartProbe:
    if _SAFE_DEVICE.fullmatch(device) is None:
        raise ValueError("invalid device path")
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise ValueError("invalid smartctl json") from exc
    passed = None
    status = "unknown"
    smart_status = payload.get("smart_status") if isinstance(payload, dict) else None
    if isinstance(smart_status, dict) and isinstance(smart_status.get("passed"), bool):
        passed = smart_status["passed"]
        status = "passed" if passed else "failed"
    temperature = payload.get("temperature") if isinstance(payload, dict) else None
    current = temperature.get("current") if isinstance(temperature, dict) else None
    temp = float(current) if isinstance(current, int | float) else None
    return SmartProbe(device=device, status=status, passed=passed, temperature_celsius=temp)


def detect_operating_systems(mounts: tuple[MountProbe, ...]) -> tuple[OperatingSystemProbe, ...]:
    output: list[OperatingSystemProbe] = []
    seen: set[tuple[str, str]] = set()
    for mount in mounts[:128]:
        target = Path(mount.target)
        candidate = target / "etc/os-release"
        try:
            info = candidate.lstat()
            if candidate.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_size > 65_536:
                continue
            values = _parse_os_release(candidate.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        name = values.get("PRETTY_NAME") or values.get("NAME")
        if not name:
            continue
        key = (mount.source, name)
        if key in seen:
            continue
        seen.add(key)
        output.append(
            OperatingSystemProbe(
                source=mount.source,
                mountpoint=mount.target,
                name=name[:160],
                version=values.get("VERSION_ID"),
                os_id=values.get("ID"),
            )
        )
    return tuple(output)


def _parse_os_release(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines()[:128]:
        key, separator, value = line.partition("=")
        if separator and re.fullmatch(r"[A-Z0-9_]+", key):
            values[key] = value.strip().strip("\"'")[:512]
    return values


def _identity_hash(parts: tuple[str, ...]) -> str | None:
    cleaned = tuple(part.strip() for part in parts if part.strip())
    if not cleaned:
        return None
    import hashlib

    return hashlib.sha256("\x1f".join(cleaned).encode("utf-8")).hexdigest()[:32]


def _clean(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    clean = "".join(character for character in value.strip() if character.isprintable())[:limit]
    return clean or None


def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return 0


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    return isinstance(value, str) and value.casefold() in {"1", "true", "yes"}
