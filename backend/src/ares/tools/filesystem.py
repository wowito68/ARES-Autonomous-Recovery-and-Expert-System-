"""Privileged, fixed-policy filesystem inspection and repair tools."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ares.filesystems.adapters import FilesystemAdapter, adapter_for
from ares.filesystems.models import (
    DeviceIdentity,
    FilesystemCheckResult,
    FilesystemHealth,
    FilesystemInspection,
    FilesystemRepairOutcome,
    FilesystemRepairPlan,
    FilesystemType,
    MountRecord,
    MountSafetyReport,
    ToolRequirement,
)
from ares.tools.storage import ProcessRunner, SafeProcessRunner, ToolAvailability

RepairEventCallback = Callable[[str, dict[str, Any]], Awaitable[None]]
_SAFE_MOUNT_OPTIONS = frozenset(
    {"rw", "ro", "relatime", "noatime", "nodiratime", "lazytime", "sync", "dirsync"}
)
_SAFE_DEVICE_PATH = re.compile(r"^/dev/[A-Za-z0-9_.:+/@-]{1,255}$")


class FilesystemToolError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class MountSafetyChecker:
    """Inspect mount/swap/process state without changing it."""

    def __init__(
        self,
        *,
        mountinfo_path: Path = Path("/proc/self/mountinfo"),
        swaps_path: Path = Path("/proc/swaps"),
        proc_root: Path = Path("/proc"),
        scan_processes: bool = True,
    ) -> None:
        self.mountinfo_path = mountinfo_path
        self.swaps_path = swaps_path
        self.proc_root = proc_root
        self.scan_processes = scan_processes

    def inspect(self, identity: DeviceIdentity) -> MountSafetyReport:
        mounts = self._mounts(identity)
        nested = self._nested_mounts(mounts)
        swap = self._is_swap(identity)
        active = self._active_processes(mounts) if self.scan_processes else ()
        busy = bool(active)
        unsupported = tuple(
            sorted(
                {
                    option
                    for mount in mounts
                    for option in mount.options
                    if option not in _SAFE_MOUNT_OPTIONS
                }
            )
        )
        bind = any(item.bind_mount for item in mounts)
        reasons: list[str] = []
        if len(mounts) > 1:
            reasons.append("multiple_mounts")
        if nested:
            reasons.append("nested_mounts")
        if bind:
            reasons.append("bind_mount")
        if swap:
            reasons.append("active_swap")
        if busy:
            reasons.append("active_process_handles")
        if unsupported:
            reasons.append("mount_options_cannot_be_restored_safely")
        mounted = bool(mounts)
        safe_to_unmount = mounted and not reasons
        safe_to_remount = mounted and not unsupported and len(mounts) == 1 and not bind
        return MountSafetyReport(
            mounted=mounted,
            busy=busy,
            swap=swap,
            mounts=mounts,
            nested_mounts=nested,
            active_processes=active,
            unsupported_mount_options=unsupported,
            safe_to_unmount=safe_to_unmount,
            safe_to_remount=safe_to_remount,
            reasons=tuple(reasons),
        )

    def _mounts(self, identity: DeviceIdentity) -> tuple[MountRecord, ...]:
        try:
            lines = self.mountinfo_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ()
        records: list[MountRecord] = []
        for line in lines:
            before, separator, after = line.partition(" - ")
            if not separator:
                continue
            left = before.split()
            right = after.split()
            if len(left) < 6 or len(right) < 2:
                continue
            major_minor = left[2]
            source = right[1]
            if major_minor != identity.major_minor and source != identity.canonical_path:
                continue
            root = _unescape_mount(left[3])
            mount_point = _unescape_mount(left[4])
            options = tuple(item for item in left[5].split(",") if item)
            records.append(
                MountRecord(
                    mount_point=mount_point,
                    root=root,
                    source=source,
                    filesystem_type=right[0],
                    options=options,
                    bind_mount=root != "/",
                )
            )
        return tuple(records)

    def _nested_mounts(self, mounts: tuple[MountRecord, ...]) -> tuple[str, ...]:
        if not mounts:
            return ()
        roots = tuple(Path(item.mount_point) for item in mounts)
        try:
            lines = self.mountinfo_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ()
        nested: set[str] = set()
        own = {item.mount_point for item in mounts}
        for line in lines:
            before, separator, _ = line.partition(" - ")
            fields = before.split()
            if not separator or len(fields) < 5:
                continue
            candidate = _unescape_mount(fields[4])
            if candidate in own:
                continue
            path = Path(candidate)
            for root in roots:
                try:
                    path.relative_to(root)
                except ValueError:
                    continue
                nested.add(candidate)
        return tuple(sorted(nested))

    def _is_swap(self, identity: DeviceIdentity) -> bool:
        try:
            lines = self.swaps_path.read_text(encoding="utf-8", errors="replace").splitlines()[1:]
        except OSError:
            return False
        return any(line.split(maxsplit=1)[0] == identity.canonical_path for line in lines if line)

    def _active_processes(self, mounts: tuple[MountRecord, ...]) -> tuple[int, ...]:
        if not mounts:
            return ()
        roots = tuple(Path(item.mount_point) for item in mounts)
        found: list[int] = []
        try:
            processes = tuple(self.proc_root.iterdir())
        except OSError:
            return ()
        for process in processes:
            if not process.name.isdigit():
                continue
            fd_dir = process / "fd"
            try:
                descriptors = tuple(fd_dir.iterdir())[:256]
            except OSError:
                continue
            matched = False
            for descriptor in descriptors:
                try:
                    target = Path(os.readlink(descriptor))
                except OSError:
                    continue
                for root in roots:
                    try:
                        target.relative_to(root)
                    except ValueError:
                        continue
                    found.append(int(process.name))
                    matched = True
                    break
                if matched:
                    break
            if len(found) >= 64:
                break
        return tuple(sorted(set(found)))


class FilesystemToolSuite:
    """Fixed semantic filesystem tools. Callers never supply executables or argv."""

    def __init__(
        self,
        *,
        runner: ProcessRunner | None = None,
        mount_checker: MountSafetyChecker | None = None,
        allow_regular_file_targets: bool = False,
    ) -> None:
        self.runner = runner or SafeProcessRunner()
        self.mount_checker = mount_checker or MountSafetyChecker()
        self.allow_regular_file_targets = allow_regular_file_targets

    async def inspect(self, requested: str) -> FilesystemInspection:
        identity = await self.identify(requested)
        filesystem = await self._detect_filesystem(identity)
        mount = self.mount_checker.inspect(identity)
        if filesystem is None:
            return FilesystemInspection(
                identity=identity,
                mount=mount,
                writable=os.access(identity.canonical_path, os.W_OK),
                supported=False,
                repair_supported=False,
                limitations=("filesystem_type_unsupported_or_unknown",),
            )
        adapter = adapter_for(filesystem)
        requirements = self._requirements(adapter, mount.mounted)
        missing = tuple(item.tool for item in requirements if not item.available)
        limitations = list(adapter.capabilities.limitations)
        check: FilesystemCheckResult | None = None
        health = FilesystemHealth.UNKNOWN
        if missing:
            limitations.append("required_tools_unavailable:" + ",".join(missing))
        elif mount.mounted and adapter.capabilities.requires_unmounted:
            limitations.append("dry_run_deferred_until_safe_unmount")
        else:
            check = await self.check(identity, filesystem)
            health = check.health
        writable = os.access(identity.canonical_path, os.W_OK)
        if not writable:
            limitations.append("target_not_writable")
        return FilesystemInspection(
            identity=identity,
            filesystem=filesystem,
            health=health,
            mount=mount,
            check=check,
            required_tools=requirements,
            writable=writable,
            supported=True,
            repair_supported=adapter.capabilities.repair_supported,
            limitations=tuple(dict.fromkeys(limitations)),
        )

    async def identify(self, requested: str) -> DeviceIdentity:
        if not requested or "\x00" in requested or not os.path.isabs(requested):
            raise FilesystemToolError("FILESYSTEM_TARGET_INVALID")
        if not self.allow_regular_file_targets and _SAFE_DEVICE_PATH.fullmatch(requested) is None:
            raise FilesystemToolError("FILESYSTEM_TARGET_INVALID")
        path = Path(requested)
        try:
            canonical = path.resolve(strict=True)
            info = canonical.stat()
        except OSError as exc:
            raise FilesystemToolError("FILESYSTEM_TARGET_NOT_FOUND") from exc
        block = stat.S_ISBLK(info.st_mode)
        if not block and not (self.allow_regular_file_targets and stat.S_ISREG(info.st_mode)):
            raise FilesystemToolError("FILESYSTEM_TARGET_NOT_BLOCK_DEVICE")
        if block:
            major_minor = f"{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}"
        else:
            major_minor = f"file:{info.st_dev}:{info.st_ino}"
        blkid = await self._blkid(str(canonical))
        lsblk = await self._lsblk(str(canonical)) if block else {}
        filesystem_uuid = blkid.get("UUID") or _string(lsblk.get("uuid"))
        partuuid = blkid.get("PARTUUID") or _string(lsblk.get("partuuid"))
        serial = _string(lsblk.get("serial"))
        model = _string(lsblk.get("model"))
        size = _integer(lsblk.get("size"), info.st_size if not block else 0)
        body = {
            "canonical_path": str(canonical),
            "major_minor": major_minor,
            "filesystem_uuid": filesystem_uuid,
            "partuuid": partuuid,
            "serial": serial,
            "model": model,
            "size_bytes": size,
        }
        fingerprint = hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return DeviceIdentity(
            requested_path=requested,
            canonical_path=str(canonical),
            major_minor=major_minor,
            uuid=filesystem_uuid,
            filesystem_uuid=filesystem_uuid,
            partuuid=partuuid,
            serial=serial,
            model=model,
            size_bytes=size,
            block_device=block,
            fingerprint_sha256=fingerprint,
        )

    async def check(
        self,
        identity: DeviceIdentity,
        filesystem: FilesystemType,
    ) -> FilesystemCheckResult:
        adapter = adapter_for(filesystem)
        tool, args = adapter.check_invocation(
            identity.canonical_path, image=not identity.block_device
        )
        try:
            result = await self.runner.run(tool, args, timeout_seconds=1800)
        except FileNotFoundError as exc:
            raise FilesystemToolError("FILESYSTEM_TOOL_UNAVAILABLE") from exc
        except TimeoutError as exc:
            raise FilesystemToolError("FILESYSTEM_CHECK_TIMEOUT") from exc
        return adapter.parse_check(result, filesystem)

    async def revalidate(self, expected: DeviceIdentity) -> DeviceIdentity:
        current = await self.identify(expected.requested_path)
        if current.fingerprint_sha256 != expected.fingerprint_sha256:
            raise FilesystemToolError("FILESYSTEM_DEVICE_IDENTITY_CHANGED")
        return current

    async def execute_repair(
        self,
        plan: FilesystemRepairPlan,
        on_event: RepairEventCallback,
    ) -> FilesystemRepairOutcome:
        adapter = adapter_for(plan.filesystem)
        if not adapter.capabilities.repair_supported:
            raise FilesystemToolError("FILESYSTEM_REPAIR_UNSUPPORTED")
        current = await self.revalidate(plan.target)
        current_mount = self.mount_checker.inspect(current)
        originally_mounted = plan.mount.mounted
        if current_mount != plan.mount:
            raise FilesystemToolError("FILESYSTEM_MOUNT_STATE_CHANGED")
        if originally_mounted:
            if not current_mount.safe_to_unmount or not current_mount.safe_to_remount:
                raise FilesystemToolError("FILESYSTEM_REPAIR_BLOCKED")
            await on_event("filesystem.unmount.started", {})
            await self._unmount(current_mount)
            await on_event("filesystem.unmounted", {})
        await self.revalidate(plan.target)
        before = await self.check(plan.target, plan.filesystem)
        repair_tool = "none"
        repair_exit_code = 0
        repair_evidence: tuple[str, ...] = ("no_mutation_needed=true",)
        limitations: list[str] = list(adapter.capabilities.limitations)
        if before.health is not FilesystemHealth.HEALTHY:
            tool, args = adapter.repair_invocation(
                plan.target.canonical_path, image=not plan.target.block_device
            )
            repair_tool = tool
            await on_event("filesystem.repair-command.started", {"tool": tool})
            try:
                result = await self.runner.run(tool, args, timeout_seconds=21_600)
            except FileNotFoundError as exc:
                raise FilesystemToolError("FILESYSTEM_TOOL_UNAVAILABLE") from exc
            except TimeoutError as exc:
                raise FilesystemToolError("FILESYSTEM_REPAIR_TIMEOUT") from exc
            repair_exit_code = result.exit_code
            repair_evidence = (
                f"tool={tool}",
                f"exit_code={result.exit_code}",
                f"stdout_present={str(bool(result.stdout)).lower()}",
                f"stderr_present={str(bool(result.stderr)).lower()}",
            )
            await on_event(
                "filesystem.repair-command.completed",
                {"tool": tool, "exit_code": result.exit_code},
            )
            if not adapter.repair_succeeded(result):
                if adapter.repair_partial(result):
                    limitations.append("repair_tool_reported_unresolved_errors")
                else:
                    limitations.append("repair_tool_failed")
        await on_event("filesystem.verification.started", {})
        after = await self.check(plan.target, plan.filesystem)
        await on_event(
            "filesystem.verification.completed",
            {"health": after.health.value, "exit_code": after.exit_code},
        )
        remounted: bool | None = None
        if originally_mounted:
            if after.health is FilesystemHealth.HEALTHY:
                await on_event("filesystem.remount.started", {})
                remounted = await self._remount(plan)
                await on_event("filesystem.remounted", {"success": remounted})
            else:
                remounted = False
                limitations.append("filesystem_left_unmounted_after_failed_verification")
        return FilesystemRepairOutcome(
            before=before,
            repair_tool=repair_tool,
            repair_exit_code=repair_exit_code,
            repair_evidence=repair_evidence,
            after=after,
            remounted=remounted,
            limitations=tuple(dict.fromkeys(limitations)),
        )

    def _requirements(
        self, adapter: FilesystemAdapter, mounted: bool
    ) -> tuple[ToolRequirement, ...]:
        names = ["blkid", *adapter.required_tools]
        if mounted:
            names.extend(("umount", "mount"))
        result: list[ToolRequirement] = []
        for name in dict.fromkeys(names):
            availability: ToolAvailability = self.runner.inspect(name)
            result.append(
                ToolRequirement(
                    tool=name,
                    available=availability.available,
                    reason=availability.reason,
                )
            )
        return tuple(result)

    async def _detect_filesystem(self, identity: DeviceIdentity) -> FilesystemType | None:
        values = await self._blkid(identity.canonical_path)
        raw = (values.get("TYPE") or "").casefold()
        aliases = {"ntfs3": "ntfs", "fuseblk": "ntfs"}
        normalized = aliases.get(raw, raw)
        try:
            return FilesystemType(normalized)
        except ValueError:
            return None

    async def _blkid(self, target: str) -> dict[str, str]:
        if not self.runner.inspect("blkid").available:
            return {}
        try:
            result = await self.runner.run("blkid", ("-o", "export", target), timeout_seconds=10)
        except (FileNotFoundError, TimeoutError):
            return {}
        if result.exit_code not in {0, 2}:
            return {}
        values: dict[str, str] = {}
        for line in result.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator and key.isupper() and len(value) <= 4096:
                values[key] = value
        return values

    async def _lsblk(self, target: str) -> dict[str, Any]:
        if not self.runner.inspect("lsblk").available:
            return {}
        args = (
            "--json",
            "--bytes",
            "--output",
            "MAJ:MIN,PATH,TYPE,SIZE,FSTYPE,UUID,PARTUUID,SERIAL,MODEL,MOUNTPOINTS",
            target,
        )
        try:
            result = await self.runner.run("lsblk", args, timeout_seconds=10)
        except (FileNotFoundError, TimeoutError):
            return {}
        if result.exit_code != 0:
            return {}
        try:
            payload = json.loads(result.stdout)
        except ValueError:
            return {}
        devices = payload.get("blockdevices") if isinstance(payload, dict) else None
        if not isinstance(devices, list) or not devices or not isinstance(devices[0], dict):
            return {}
        return devices[0]

    async def _unmount(self, mount: MountSafetyReport) -> None:
        if len(mount.mounts) != 1:
            raise FilesystemToolError("FILESYSTEM_REPAIR_BLOCKED")
        record = mount.mounts[0]
        try:
            result = await self.runner.run("umount", ("--", record.mount_point), timeout_seconds=120)
        except (FileNotFoundError, TimeoutError) as exc:
            raise FilesystemToolError("FILESYSTEM_UNMOUNT_FAILED") from exc
        if result.exit_code != 0:
            raise FilesystemToolError("FILESYSTEM_UNMOUNT_FAILED")

    async def _remount(self, plan: FilesystemRepairPlan) -> bool:
        if len(plan.mount.mounts) != 1:
            return False
        record = plan.mount.mounts[0]
        options = tuple(option for option in record.options if option in _SAFE_MOUNT_OPTIONS)
        args: tuple[str, ...]
        if options:
            args = (
                "-t",
                plan.filesystem.value,
                "-o",
                ",".join(options),
                plan.target.canonical_path,
                record.mount_point,
            )
        else:
            args = ("-t", plan.filesystem.value, plan.target.canonical_path, record.mount_point)
        try:
            result = await self.runner.run("mount", args, timeout_seconds=120)
        except (FileNotFoundError, TimeoutError):
            return False
        return result.exit_code == 0


def _unescape_mount(value: str) -> str:
    return (
        value.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _integer(value: object, default: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return default
