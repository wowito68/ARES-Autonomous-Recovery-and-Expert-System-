"""Privileged, fixed-policy filesystem inspection and repair tools."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
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
_DEVICE_ROOT = Path("/dev")
_MAX_PROCESSES = 32_768
_MAX_FDS_PER_PROCESS = 256
_MAX_BUSY_PIDS = 64


class FilesystemToolError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class MountSafetyChecker:
    """Inspect mount, swap and process references without changing the target."""

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
        if active:
            reasons.append("active_process_handles")
        if unsupported:
            reasons.append("mount_options_cannot_be_restored_safely")
        mounted = bool(mounts)
        return MountSafetyReport(
            mounted=mounted,
            busy=bool(active),
            swap=swap,
            mounts=mounts,
            nested_mounts=nested,
            active_processes=active,
            unsupported_mount_options=unsupported,
            safe_to_unmount=mounted and not reasons,
            safe_to_remount=(
                mounted and not unsupported and len(mounts) == 1 and not bind
            ),
            reasons=tuple(reasons),
        )

    def _mounts(self, identity: DeviceIdentity) -> tuple[MountRecord, ...]:
        try:
            lines = self.mountinfo_path.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
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
            if left[2] != identity.major_minor and right[1] != identity.canonical_path:
                continue
            root = _unescape_mount(left[3])
            mount_point = _unescape_mount(left[4])
            options = tuple(item for item in left[5].split(",") if item)
            records.append(
                MountRecord(
                    mount_point=mount_point,
                    root=root,
                    source=right[1],
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
        own = {item.mount_point for item in mounts}
        try:
            lines = self.mountinfo_path.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
        except OSError:
            return ()
        nested: set[str] = set()
        for line in lines:
            before, separator, _ = line.partition(" - ")
            fields = before.split()
            if not separator or len(fields) < 5:
                continue
            candidate = _unescape_mount(fields[4])
            if candidate in own:
                continue
            if _path_under_roots(candidate, roots):
                nested.add(candidate)
        return tuple(sorted(nested))

    def _is_swap(self, identity: DeviceIdentity) -> bool:
        try:
            lines = self.swaps_path.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()[1:]
        except OSError:
            return False
        for line in lines:
            if not line:
                continue
            if line.split(maxsplit=1)[0] == identity.canonical_path:
                return True
        return False

    def _active_processes(self, mounts: tuple[MountRecord, ...]) -> tuple[int, ...]:
        if not mounts:
            return ()
        roots = tuple(Path(item.mount_point) for item in mounts)
        try:
            processes = tuple(self.proc_root.iterdir())[:_MAX_PROCESSES]
        except OSError:
            return ()
        found: set[int] = set()
        for process in processes:
            if not process.name.isdigit():
                continue
            pid = int(process.name)
            if self._process_references_mount(process, roots):
                found.add(pid)
            if len(found) >= _MAX_BUSY_PIDS:
                break
        return tuple(sorted(found))

    @staticmethod
    def _process_references_mount(process: Path, roots: tuple[Path, ...]) -> bool:
        for special in ("cwd", "root", "exe"):
            try:
                target = os.readlink(process / special)
            except OSError:
                continue
            if _path_under_roots(target, roots):
                return True
        try:
            descriptors = tuple((process / "fd").iterdir())[:_MAX_FDS_PER_PROCESS]
        except OSError:
            return False
        for descriptor in descriptors:
            try:
                target = os.readlink(descriptor)
            except OSError:
                continue
            if _path_under_roots(target, roots):
                return True
        return False


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
        mount = await asyncio.to_thread(self.mount_checker.inspect, identity)
        writable = await asyncio.to_thread(os.access, identity.canonical_path, os.W_OK)
        if filesystem is None:
            return FilesystemInspection(
                identity=identity,
                mount=mount,
                writable=writable,
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
        if not self.allow_regular_file_targets and not _valid_device_request(requested):
            raise FilesystemToolError("FILESYSTEM_TARGET_INVALID")
        try:
            canonical, info = await asyncio.to_thread(_resolve_target, requested)
        except OSError as exc:
            raise FilesystemToolError("FILESYSTEM_TARGET_NOT_FOUND") from exc
        if not self.allow_regular_file_targets:
            try:
                canonical.relative_to(_DEVICE_ROOT)
            except ValueError as exc:
                raise FilesystemToolError("FILESYSTEM_TARGET_INVALID") from exc
        block = stat.S_ISBLK(info.st_mode)
        regular_test_image = self.allow_regular_file_targets and stat.S_ISREG(info.st_mode)
        if not block and not regular_test_image:
            raise FilesystemToolError("FILESYSTEM_TARGET_NOT_BLOCK_DEVICE")
        major_minor = (
            f"{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}"
            if block
            else f"file:{info.st_dev}:{info.st_ino}"
        )
        blkid = await self._blkid(str(canonical))
        lsblk = await self._lsblk(str(canonical)) if block else {}
        filesystem_uuid = blkid.get("UUID") or _string(lsblk.get("uuid"))
        partuuid = blkid.get("PARTUUID") or _string(lsblk.get("partuuid"))
        serial = _string(lsblk.get("serial"))
        model = _string(lsblk.get("model"))
        size = _integer(lsblk.get("size"), info.st_size if not block else 0)
        identity_body = {
            "canonical_path": str(canonical),
            "major_minor": major_minor,
            "filesystem_uuid": filesystem_uuid,
            "partuuid": partuuid,
            "serial": serial,
            "model": model,
            "size_bytes": size,
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                identity_body, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
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
            result = await self.runner.run(tool, args, timeout_seconds=1_800)
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
        current_mount = await asyncio.to_thread(self.mount_checker.inspect, current)
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
        requirements: list[ToolRequirement] = []
        for name in dict.fromkeys(names):
            availability: ToolAvailability = self.runner.inspect(name)
            requirements.append(
                ToolRequirement(
                    tool=name,
                    available=availability.available,
                    reason=availability.reason,
                )
            )
        return tuple(requirements)

    async def _detect_filesystem(self, identity: DeviceIdentity) -> FilesystemType | None:
        values = await self._blkid(identity.canonical_path)
        raw = (values.get("TYPE") or "").casefold()
        normalized = {"ntfs3": "ntfs", "fuseblk": "ntfs"}.get(raw, raw)
        try:
            return FilesystemType(normalized)
        except ValueError:
            return None

    async def _blkid(self, target: str) -> dict[str, str]:
        if not self.runner.inspect("blkid").available:
            return {}
        try:
            result = await self.runner.run(
                "blkid", ("-o", "export", target), timeout_seconds=10
            )
        except (FileNotFoundError, TimeoutError):
            return {}
        if result.exit_code not in {0, 2}:
            return {}
        values: dict[str, str] = {}
        for line in result.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator and key.isupper() and len(value) <= 4_096:
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
            result = await self.runner.run(
                "umount", ("--", record.mount_point), timeout_seconds=120
            )
        except (FileNotFoundError, TimeoutError) as exc:
            raise FilesystemToolError("FILESYSTEM_UNMOUNT_FAILED") from exc
        if result.exit_code != 0:
            raise FilesystemToolError("FILESYSTEM_UNMOUNT_FAILED")

    async def _remount(self, plan: FilesystemRepairPlan) -> bool:
        if len(plan.mount.mounts) != 1:
            return False
        record = plan.mount.mounts[0]
        options = tuple(
            option for option in record.options if option in _SAFE_MOUNT_OPTIONS
        )
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
            args = (
                "-t",
                plan.filesystem.value,
                plan.target.canonical_path,
                record.mount_point,
            )
        try:
            result = await self.runner.run("mount", args, timeout_seconds=120)
        except (FileNotFoundError, TimeoutError):
            return False
        return result.exit_code == 0


def _valid_device_request(requested: str) -> bool:
    path = Path(requested)
    if not path.is_absolute() or len(requested) > 4_096:
        return False
    parts = path.parts
    if len(parts) < 3 or parts[0] != "/" or parts[1] != "dev":
        return False
    return all(
        part not in {"", ".", ".."}
        and len(part) <= 255
        and all(character.isalnum() or character in "_.:+@-" for character in part)
        for part in parts[2:]
    )


def _resolve_target(requested: str) -> tuple[Path, os.stat_result]:
    canonical = Path(requested).resolve(strict=True)
    return canonical, canonical.stat()


def _path_under_roots(value: str, roots: tuple[Path, ...]) -> bool:
    candidate_text = value.removesuffix(" (deleted)")
    candidate = Path(candidate_text)
    if not candidate.is_absolute():
        return False
    for root in roots:
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        return True
    return False


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
