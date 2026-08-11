"""Filesystem Tool Layer for planning, creating and verifying local backups."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import stat
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any

from ares.backup.models import (
    Backup,
    BackupDestination,
    BackupDestinationKind,
    BackupEntry,
    BackupEntryType,
    BackupExclusion,
    BackupManifest,
    BackupPlan,
    BackupPolicy,
    BackupProgress,
    BackupSource,
    BackupVerification,
    BackupVerificationStatus,
)

ProgressCallback = Callable[[BackupProgress], Awaitable[None]]
EntryCallback = Callable[[BackupEntry], Awaitable[None]]
ChunkCallback = Callable[[int], Awaitable[None]]

_CHUNK_BYTES = 1024 * 1024
_MIN_HEADROOM_BYTES = 16 * 1024 * 1024
_MAX_MANIFEST_BYTES = 128_000_000
_INCOMPATIBLE_DESTINATION_FILESYSTEMS = {
    "proc",
    "sysfs",
    "devtmpfs",
    "devpts",
    "tmpfs",
    "overlay",
    "squashfs",
    "iso9660",
    "cgroup",
    "cgroup2",
    "efivarfs",
    "securityfs",
    "tracefs",
    "debugfs",
}


class BackupToolError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class _MountInfo:
    device_id: str
    mount_point: Path
    filesystem_type: str
    source: str
    options: frozenset[str]


@dataclass(frozen=True, slots=True)
class _Scan:
    bytes_total: int
    file_count: int
    directory_count: int
    exclusions: tuple[BackupExclusion, ...]


class BackupFilesystemTools:
    """Structured filesystem operations. No shell or user executable is accepted."""

    def __init__(self, mountinfo_path: Path = Path("/proc/self/mountinfo")) -> None:
        self.mountinfo_path = mountinfo_path

    def build_plan(
        self,
        source_path: str,
        destination_path: str,
        policy: BackupPolicy,
    ) -> BackupPlan:
        source_root = self._canonical_existing(source_path, "BACKUP_SOURCE_INVALID")
        destination_root = self._canonical_existing(destination_path, "BACKUP_DESTINATION_INVALID")
        source_info = self._mount_for(source_root)
        destination_info = self._mount_for(destination_root)
        if source_info is None:
            raise BackupToolError("BACKUP_SOURCE_FILESYSTEM_UNKNOWN")
        if destination_info is None:
            raise BackupToolError("BACKUP_DESTINATION_FILESYSTEM_UNKNOWN")
        if source_root == destination_root or _is_relative_to(destination_root, source_root):
            raise BackupToolError("BACKUP_DESTINATION_INSIDE_SOURCE")
        source_physical = _physical_device_id(source_info.device_id)
        destination_physical = _physical_device_id(destination_info.device_id)
        _reject_same_storage(
            source_info.device_id,
            destination_info.device_id,
            source_physical,
            destination_physical,
        )
        self._validate_destination(destination_root, destination_info)

        scan = self._scan_source(source_root, source_info.device_id, policy)
        available = _available_bytes(destination_root)
        required = scan.bytes_total + max(_MIN_HEADROOM_BYTES, scan.bytes_total // 50)
        if available < required:
            raise BackupToolError("BACKUP_INSUFFICIENT_SPACE")

        backup_id = _random_id()
        plan_id = _random_id()
        final_path = destination_root / "ARES" / backup_id
        if _path_exists_or_symlink(final_path):
            raise BackupToolError("BACKUP_DESTINATION_EXISTS")
        source = BackupSource(
            path=str(source_root),
            device_id=source_info.device_id,
            physical_device_id=source_physical,
            mount_point=str(source_info.mount_point),
            filesystem_type=source_info.filesystem_type,
            estimated_size_bytes=scan.bytes_total,
            estimated_file_count=scan.file_count,
            estimated_directory_count=scan.directory_count,
        )
        destination = BackupDestination(
            root_path=str(destination_root),
            backup_path=str(final_path),
            device_id=destination_info.device_id,
            physical_device_id=destination_physical,
            mount_point=str(destination_info.mount_point),
            filesystem_type=destination_info.filesystem_type,
            kind=self._destination_kind(destination_info),
            available_bytes=available,
        )
        payload: dict[str, Any] = {
            "id": plan_id,
            "backup_id": backup_id,
            "version": "1.0",
            "source": source.model_dump(mode="json"),
            "destination": destination.model_dump(mode="json"),
            "policy": policy.model_dump(mode="json"),
            "required_bytes": required,
            "included_file_count": scan.file_count,
            "included_directory_count": scan.directory_count,
            "exclusions": [item.model_dump(mode="json") for item in scan.exclusions],
            "risk": "medium",
            "estimated_duration_seconds": None,
            "authorization_required": True,
        }
        fingerprint = _sha256_json(payload)
        return BackupPlan.model_validate({**payload, "fingerprint_sha256": fingerprint})

    def revalidate_plan(self, plan: BackupPlan) -> None:
        if plan.expires_at <= _utc_now():
            raise BackupToolError("BACKUP_PLAN_EXPIRED")
        source = self._canonical_existing(plan.source.path, "BACKUP_SOURCE_INVALID")
        destination = self._canonical_existing(
            plan.destination.root_path, "BACKUP_DESTINATION_INVALID"
        )
        source_info = self._mount_for(source)
        destination_info = self._mount_for(destination)
        if source_info is None or source_info.device_id != plan.source.device_id:
            raise BackupToolError("BACKUP_SOURCE_IDENTITY_CHANGED")
        if destination_info is None or destination_info.device_id != plan.destination.device_id:
            raise BackupToolError("BACKUP_DESTINATION_IDENTITY_CHANGED")
        source_physical = _physical_device_id(source_info.device_id)
        destination_physical = _physical_device_id(destination_info.device_id)
        if source_physical != plan.source.physical_device_id:
            raise BackupToolError("BACKUP_SOURCE_IDENTITY_CHANGED")
        if destination_physical != plan.destination.physical_device_id:
            raise BackupToolError("BACKUP_DESTINATION_IDENTITY_CHANGED")
        _reject_same_storage(
            source_info.device_id,
            destination_info.device_id,
            source_physical,
            destination_physical,
        )
        self._validate_destination(destination, destination_info)
        expected_path = destination / "ARES" / plan.backup_id
        if str(expected_path) != plan.destination.backup_path:
            raise BackupToolError("BACKUP_DESTINATION_PLAN_MISMATCH")
        if _path_exists_or_symlink(expected_path):
            raise BackupToolError("BACKUP_DESTINATION_EXISTS")
        scan = self._scan_source(source, source_info.device_id, plan.policy)
        if (
            scan.bytes_total != plan.source.estimated_size_bytes
            or scan.file_count != plan.included_file_count
            or scan.directory_count != plan.included_directory_count
            or scan.exclusions != plan.exclusions
        ):
            raise BackupToolError("BACKUP_SOURCE_CHANGED")
        if _available_bytes(destination) < plan.required_bytes:
            raise BackupToolError("BACKUP_INSUFFICIENT_SPACE")
        if plan.fingerprint_sha256 != _plan_fingerprint(plan):
            raise BackupToolError("BACKUP_PLAN_FINGERPRINT_MISMATCH")

    async def create_backup(
        self,
        plan: BackupPlan,
        on_progress: ProgressCallback,
        on_entry: EntryCallback,
    ) -> BackupManifest:
        await asyncio.to_thread(self.revalidate_plan, plan)
        source = Path(plan.source.path)
        root = Path(plan.destination.root_path)
        ares_root = root / "ARES"
        final = Path(plan.destination.backup_path)
        partial = ares_root / f".partial-{plan.backup_id}"
        await asyncio.to_thread(_prepare_backup_destination, ares_root, final, partial)
        entries: list[BackupEntry] = []
        files_done = 0
        bytes_done = 0
        started = monotonic()

        async def chunk_progress(delta: int) -> None:
            nonlocal bytes_done
            bytes_done += delta
            await on_progress(
                _progress(
                    files_done,
                    plan.included_file_count,
                    bytes_done,
                    plan.source.estimated_size_bytes,
                    started,
                )
            )

        try:
            source_info = self._mount_for(source)
            if source_info is None:
                raise BackupToolError("BACKUP_SOURCE_FILESYSTEM_UNKNOWN")
            for item, relative in self._walk_copy_entries(
                source, source_info.device_id, plan.policy
            ):
                target = partial / relative
                _assert_descendant(target, partial)
                info = await asyncio.to_thread(os.lstat, item)
                if stat.S_ISDIR(info.st_mode):
                    await asyncio.to_thread(
                        target.mkdir,
                        mode=stat.S_IMODE(info.st_mode),
                        parents=True,
                        exist_ok=False,
                    )
                    entry = BackupEntry(
                        relative_path=relative.as_posix(),
                        size_bytes=0,
                        mtime_ns=info.st_mtime_ns,
                        mode=stat.S_IMODE(info.st_mode),
                        entry_type=BackupEntryType.DIRECTORY,
                    )
                elif stat.S_ISREG(info.st_mode):
                    await asyncio.to_thread(
                        target.parent.mkdir, mode=0o700, parents=True, exist_ok=True
                    )
                    checksum = await self._copy_regular_file(item, target, info, chunk_progress)
                    entry = BackupEntry(
                        relative_path=relative.as_posix(),
                        size_bytes=info.st_size,
                        mtime_ns=info.st_mtime_ns,
                        mode=stat.S_IMODE(info.st_mode),
                        entry_type=BackupEntryType.FILE,
                        checksum_sha256=checksum,
                    )
                    files_done += 1
                else:
                    raise BackupToolError("BACKUP_SOURCE_CHANGED")
                entries.append(entry)
                await on_entry(entry)
                await on_progress(
                    _progress(
                        files_done,
                        plan.included_file_count,
                        bytes_done,
                        plan.source.estimated_size_bytes,
                        started,
                    )
                )

            file_count = sum(item.entry_type is BackupEntryType.FILE for item in entries)
            directory_count = sum(item.entry_type is BackupEntryType.DIRECTORY for item in entries)
            total_size = sum(
                item.size_bytes for item in entries if item.entry_type is BackupEntryType.FILE
            )
            if (
                file_count != plan.included_file_count
                or directory_count != plan.included_directory_count
                or total_size != plan.source.estimated_size_bytes
                or bytes_done != total_size
            ):
                raise BackupToolError("BACKUP_SOURCE_CHANGED")
            manifest_payload = {
                "backup_id": plan.backup_id,
                "format_version": "1.0",
                "source": plan.source.model_dump(mode="json"),
                "entries": [item.model_dump(mode="json") for item in entries],
                "file_count": file_count,
                "directory_count": directory_count,
                "total_size_bytes": total_size,
            }
            manifest = BackupManifest.model_validate(
                {
                    **manifest_payload,
                    "manifest_checksum_sha256": _sha256_json(manifest_payload),
                }
            )
            await asyncio.to_thread(self._write_manifest_file, partial, manifest)
            await asyncio.to_thread(_publish_backup, partial, final, ares_root)
            await on_progress(
                BackupProgress(
                    files_completed=file_count,
                    files_total=file_count,
                    bytes_completed=total_size,
                    bytes_total=total_size,
                    percent=100.0,
                    speed_bytes_per_second=_speed(total_size, started),
                    eta_seconds=0.0,
                )
            )
            return manifest
        except asyncio.CancelledError:
            await asyncio.to_thread(_remove_partial, partial)
            raise
        except BackupToolError:
            await asyncio.to_thread(_remove_partial, partial)
            raise
        except OSError as exc:
            await asyncio.to_thread(_remove_partial, partial)
            raise BackupToolError("BACKUP_IO_FAILED") from exc

    async def verify_backup(self, backup: Backup, manifest: BackupManifest) -> BackupVerification:
        root = Path(backup.destination.backup_path)
        if not await asyncio.to_thread(_safe_backup_root, root):
            return _verification_failure(
                backup,
                manifest,
                BackupVerificationStatus.CORRUPTED,
                missing_entries=("backup-root",),
                message="Backup destination is missing or unsafe.",
            )
        if manifest.manifest_checksum_sha256 != _manifest_fingerprint(manifest):
            return _verification_failure(
                backup,
                manifest,
                BackupVerificationStatus.CORRUPTED,
                message="Backup manifest checksum is invalid.",
            )
        if not await asyncio.to_thread(_destination_manifest_matches, root, manifest):
            return _verification_failure(
                backup,
                manifest,
                BackupVerificationStatus.CORRUPTED,
                message="Destination manifest is missing, changed or invalid.",
            )

        mismatches: list[str] = []
        missing: list[str] = []
        source_changed: list[str] = []
        verified_files = 0
        verified_bytes = 0
        expected_paths = {".ares-manifest.json"}
        source_root = Path(manifest.source.path)
        for entry in manifest.entries:
            relative = Path(entry.relative_path)
            if relative.is_absolute() or ".." in relative.parts:
                mismatches.append(_path_token(entry.relative_path))
                continue
            expected_paths.add(relative.as_posix())
            target = root / relative
            _assert_descendant(target, root)
            try:
                target_info = await asyncio.to_thread(os.lstat, target)
            except OSError:
                missing.append(_path_token(entry.relative_path))
                continue
            if entry.entry_type is BackupEntryType.DIRECTORY:
                if not stat.S_ISDIR(target_info.st_mode) or stat.S_ISLNK(target_info.st_mode):
                    mismatches.append(_path_token(entry.relative_path))
                continue
            if not stat.S_ISREG(target_info.st_mode) or stat.S_ISLNK(target_info.st_mode):
                mismatches.append(_path_token(entry.relative_path))
                continue
            checksum = await asyncio.to_thread(_hash_file_no_follow, target)
            if checksum != entry.checksum_sha256 or target_info.st_size != entry.size_bytes:
                mismatches.append(_path_token(entry.relative_path))
                continue
            verified_files += 1
            verified_bytes += target_info.st_size
            source_item = source_root / relative
            if await asyncio.to_thread(_regular_file_exists_no_symlink, source_item):
                try:
                    source_checksum = await asyncio.to_thread(_hash_file_no_follow, source_item)
                except OSError:
                    source_changed.append(_path_token(entry.relative_path))
                else:
                    if source_checksum != checksum:
                        source_changed.append(_path_token(entry.relative_path))

        unexpected = tuple(
            sorted(await asyncio.to_thread(self._unexpected_paths, root, expected_paths))
        )
        if mismatches or missing or unexpected:
            status = BackupVerificationStatus.CORRUPTED
            message = "Backup verification detected missing, unexpected or corrupt entries."
        elif source_changed:
            status = BackupVerificationStatus.FAILED
            message = "Backup is internally consistent, but the source changed after copying."
        else:
            status = BackupVerificationStatus.VERIFIED
            message = "Backup manifest, file counts, sizes and SHA-256 checksums verified."
        return BackupVerification(
            backup_id=backup.id,
            status=status,
            expected_file_count=manifest.file_count,
            verified_file_count=verified_files,
            expected_size_bytes=manifest.total_size_bytes,
            verified_size_bytes=verified_bytes,
            manifest_valid=True,
            checksum_mismatches=tuple(sorted(mismatches)),
            missing_entries=tuple(sorted(missing)),
            unexpected_entries=unexpected,
            source_changed_entries=tuple(sorted(source_changed)),
            message=message,
        )

    def _scan_source(self, root: Path, device_id: str, policy: BackupPolicy) -> _Scan:
        files = 0
        directories = 0
        total = 0
        exclusions: list[BackupExclusion] = []
        try:
            if root.is_file():
                info = os.lstat(root)
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                    raise BackupToolError("BACKUP_SOURCE_UNSUPPORTED")
                return _Scan(info.st_size, 1, 0, ())
            for item, relative, excluded_reason in self._walk_source(root, device_id, policy):
                if excluded_reason is not None:
                    if len(exclusions) < 1024:
                        exclusions.append(
                            BackupExclusion(
                                relative_path=relative.as_posix(),
                                reason=excluded_reason,
                            )
                        )
                    continue
                info = os.lstat(item)
                if stat.S_ISDIR(info.st_mode):
                    directories += 1
                elif stat.S_ISREG(info.st_mode):
                    files += 1
                    total += info.st_size
        except PermissionError as exc:
            raise BackupToolError("BACKUP_SOURCE_NOT_READABLE") from exc
        except OSError as exc:
            raise BackupToolError("BACKUP_SOURCE_SCAN_FAILED") from exc
        return _Scan(total, files, directories, tuple(exclusions))

    def _walk_source(
        self, root: Path, device_id: str, policy: BackupPolicy
    ) -> Iterator[tuple[Path, Path, str | None]]:
        stack: list[tuple[Path, Path]] = [(root, Path())]
        while stack:
            directory, relative_root = stack.pop()
            with os.scandir(directory) as iterator:
                items = sorted(iterator, key=lambda item: item.name)
            for entry in items:
                relative = relative_root / entry.name
                if entry.name in policy.excluded_names:
                    yield Path(entry.path), relative, "policy_excluded_name"
                    continue
                if entry.is_symlink():
                    yield Path(entry.path), relative, "symlink_not_followed"
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    yield Path(entry.path), relative, "stat_failed"
                    continue
                current_device = f"{os.major(info.st_dev)}:{os.minor(info.st_dev)}"
                if not policy.cross_filesystems and current_device != device_id:
                    yield Path(entry.path), relative, "cross_filesystem"
                    continue
                if stat.S_ISDIR(info.st_mode):
                    yield Path(entry.path), relative, None
                    stack.append((Path(entry.path), relative))
                elif stat.S_ISREG(info.st_mode):
                    yield Path(entry.path), relative, None
                else:
                    yield Path(entry.path), relative, "special_file"

    def _walk_copy_entries(
        self, root: Path, device_id: str, policy: BackupPolicy
    ) -> Iterator[tuple[Path, Path]]:
        if root.is_file():
            yield root, Path(root.name)
            return
        for item, relative, reason in self._walk_source(root, device_id, policy):
            if reason is None:
                yield item, relative

    async def _copy_regular_file(
        self,
        source: Path,
        destination: Path,
        info: os.stat_result,
        on_chunk: ChunkCallback,
    ) -> str:
        source_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        destination_flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        digest = hashlib.sha256()
        source_fd = os.open(source, source_flags)
        try:
            current = os.fstat(source_fd)
            if (
                not stat.S_ISREG(current.st_mode)
                or current.st_ino != info.st_ino
                or current.st_dev != info.st_dev
            ):
                raise BackupToolError("BACKUP_SOURCE_CHANGED")
            destination_fd = os.open(destination, destination_flags, 0o600)
            try:
                while True:
                    block = os.read(source_fd, _CHUNK_BYTES)
                    if not block:
                        break
                    digest.update(block)
                    view = memoryview(block)
                    while view:
                        written = os.write(destination_fd, view)
                        if written <= 0:
                            raise OSError("short backup write")
                        view = view[written:]
                    await on_chunk(len(block))
                    await asyncio.sleep(0)
                final_source = os.fstat(source_fd)
                if (
                    final_source.st_size != info.st_size
                    or final_source.st_mtime_ns != info.st_mtime_ns
                ):
                    raise BackupToolError("BACKUP_SOURCE_CHANGED")
                os.fchmod(destination_fd, stat.S_IMODE(info.st_mode))
                os.fsync(destination_fd)
            finally:
                os.close(destination_fd)
        finally:
            os.close(source_fd)
        os.utime(
            destination,
            ns=(info.st_atime_ns, info.st_mtime_ns),
            follow_symlinks=False,
        )
        return digest.hexdigest()

    def _mount_for(self, path: Path) -> _MountInfo | None:
        try:
            lines = self.mountinfo_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            info = os.stat(path, follow_symlinks=False)
            return _MountInfo(
                device_id=f"{os.major(info.st_dev)}:{os.minor(info.st_dev)}",
                mount_point=Path("/"),
                filesystem_type="unknown",
                source="unknown",
                options=frozenset(),
            )
        candidates: list[_MountInfo] = []
        for line in lines:
            parsed = _parse_mountinfo(line)
            if parsed is not None and _is_relative_to(path, parsed.mount_point):
                candidates.append(parsed)
        return max(
            candidates,
            key=lambda item: len(item.mount_point.parts),
            default=None,
        )

    @staticmethod
    def _destination_kind(info: _MountInfo) -> BackupDestinationKind:
        if _is_removable(info.device_id):
            return BackupDestinationKind.USB_STORAGE
        if info.source.startswith("/dev/") and info.mount_point != Path("/"):
            return BackupDestinationKind.MOUNTED_EXTERNAL_DISK
        return BackupDestinationKind.LOCAL_FILESYSTEM

    @staticmethod
    def _canonical_existing(raw: str, code: str) -> Path:
        if "\x00" in raw:
            raise BackupToolError(code)
        path = Path(raw)
        if not path.is_absolute() or ".." in path.parts:
            raise BackupToolError(code)
        try:
            _reject_symlink_components(path, code)
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise BackupToolError(code) from exc
        return resolved

    @staticmethod
    def _validate_destination(path: Path, info: _MountInfo) -> None:
        if "ro" in info.options:
            raise BackupToolError("BACKUP_DESTINATION_READ_ONLY")
        if info.filesystem_type in _INCOMPATIBLE_DESTINATION_FILESYSTEMS:
            raise BackupToolError("BACKUP_DESTINATION_FILESYSTEM_INCOMPATIBLE")
        if not os.access(path, os.W_OK | os.X_OK):
            raise BackupToolError("BACKUP_DESTINATION_NOT_WRITABLE")

    @staticmethod
    def _write_manifest_file(root: Path, manifest: BackupManifest) -> None:
        path = root / ".ares-manifest.json"
        encoded = (
            json.dumps(
                manifest.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
        if len(encoded) > _MAX_MANIFEST_BYTES:
            raise BackupToolError("BACKUP_MANIFEST_TOO_LARGE")
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            view = memoryview(encoded)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("manifest write failed")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _unexpected_paths(root: Path, expected: set[str]) -> set[str]:
        unexpected: set[str] = set()
        for directory, directories, files in os.walk(root, followlinks=False):
            base = Path(directory)
            for name in (*directories, *files):
                item = base / name
                relative = item.relative_to(root).as_posix()
                if relative not in expected:
                    unexpected.add(_path_token(relative))
        return unexpected


def _parse_mountinfo(line: str) -> _MountInfo | None:
    if " - " not in line:
        return None
    left, right = line.split(" - ", 1)
    fields = left.split()
    tail = right.split()
    if len(fields) < 6 or len(tail) < 2:
        return None
    mount_point = Path(_decode_mount(fields[4]))
    if not mount_point.is_absolute():
        return None
    return _MountInfo(
        device_id=fields[2],
        mount_point=mount_point,
        filesystem_type=tail[0],
        source=_decode_mount(tail[1]),
        options=frozenset(fields[5].split(",")),
    )


def _decode_mount(value: str) -> str:
    return (
        value.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


def _reject_symlink_components(path: Path, code: str) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = os.lstat(current)
        except OSError as exc:
            raise BackupToolError(code) from exc
        if stat.S_ISLNK(info.st_mode):
            raise BackupToolError(code)


def _available_bytes(path: Path) -> int:
    try:
        info = os.statvfs(path)
    except OSError as exc:
        raise BackupToolError("BACKUP_DESTINATION_SPACE_UNKNOWN") from exc
    return info.f_bavail * info.f_frsize


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _assert_descendant(path: Path, root: Path) -> None:
    if not _is_relative_to(path, root):
        raise BackupToolError("BACKUP_PATH_TRAVERSAL")


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _plan_fingerprint(plan: BackupPlan) -> str:
    payload = plan.model_dump(
        mode="json",
        exclude={"fingerprint_sha256", "created_at", "expires_at"},
    )
    return _sha256_json(payload)


def _manifest_fingerprint(manifest: BackupManifest) -> str:
    payload = manifest.model_dump(
        mode="json",
        exclude={"manifest_checksum_sha256", "created_at"},
    )
    return _sha256_json(payload)


def _hash_file_no_follow(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    digest = hashlib.sha256()
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("not a regular file")
        while True:
            block = os.read(descriptor, _CHUNK_BYTES)
            if not block:
                break
            digest.update(block)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _progress(
    files_done: int,
    files_total: int,
    bytes_done: int,
    bytes_total: int,
    started: float,
) -> BackupProgress:
    percent = 100.0 if bytes_total == 0 else min(100.0, bytes_done * 100.0 / bytes_total)
    speed = _speed(bytes_done, started)
    remaining = max(0, bytes_total - bytes_done)
    eta = remaining / speed if speed is not None and speed > 0 else None
    return BackupProgress(
        files_completed=files_done,
        files_total=files_total,
        bytes_completed=bytes_done,
        bytes_total=bytes_total,
        percent=percent,
        speed_bytes_per_second=speed,
        eta_seconds=eta,
    )


def _speed(bytes_done: int, started: float) -> float | None:
    elapsed = monotonic() - started
    if elapsed <= 0 or bytes_done <= 0:
        return None
    return bytes_done / elapsed


def _path_token(relative_path: str) -> str:
    encoded = relative_path.encode("utf-8", errors="surrogatepass")
    return hashlib.sha256(encoded).hexdigest()[:24]


def _random_id() -> str:
    return os.urandom(16).hex()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _is_removable(device_id: str) -> bool:
    path = Path("/sys/dev/block") / device_id
    try:
        current = path.resolve(strict=True)
    except OSError:
        return False
    for candidate in (current, *current.parents):
        removable = candidate / "removable"
        try:
            if removable.is_file() and removable.read_text(encoding="ascii").strip() == "1":
                return True
        except OSError:
            continue
    return False


def _physical_device_id(device_id: str) -> str | None:
    path = Path("/sys/dev/block") / device_id
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return None
    candidate = resolved
    if (candidate / "partition").is_file():
        candidate = candidate.parent
    try:
        dev_file = candidate / "dev"
        physical_id = dev_file.read_text(encoding="ascii").strip()
    except OSError:
        physical_id = ""
    return physical_id or candidate.name or None


def _reject_same_storage(
    source_device_id: str,
    destination_device_id: str,
    source_physical: str | None,
    destination_physical: str | None,
) -> None:
    if source_device_id == destination_device_id:
        raise BackupToolError("BACKUP_SAME_FILESYSTEM")
    if (
        source_physical is not None
        and destination_physical is not None
        and source_physical == destination_physical
    ):
        raise BackupToolError("BACKUP_SAME_DEVICE")


def _prepare_backup_destination(ares_root: Path, final: Path, partial: Path) -> None:
    if _path_exists_or_symlink(final) or _path_exists_or_symlink(partial):
        raise BackupToolError("BACKUP_DESTINATION_EXISTS")
    ares_root.mkdir(mode=0o700, exist_ok=True)
    if ares_root.is_symlink() or ares_root.resolve(strict=True) != ares_root:
        raise BackupToolError("BACKUP_DESTINATION_UNSAFE")
    partial.mkdir(mode=0o700)


def _publish_backup(partial: Path, final: Path, ares_root: Path) -> None:
    os.replace(partial, final)
    _fsync_directory(ares_root)


def _remove_partial(partial: Path) -> None:
    try:
        info = os.lstat(partial)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
        shutil.rmtree(partial, ignore_errors=True)


def _safe_backup_root(root: Path) -> bool:
    try:
        info = os.lstat(root)
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode)


def _regular_file_exists_no_symlink(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode)


def _path_exists_or_symlink(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _destination_manifest_matches(root: Path, manifest: BackupManifest) -> bool:
    path = root / ".ares-manifest.json"
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return False
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_MANIFEST_BYTES:
            return False
        chunks: list[bytes] = []
        remaining = _MAX_MANIFEST_BYTES + 1
        while remaining > 0:
            block = os.read(descriptor, min(_CHUNK_BYTES, remaining))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        if remaining <= 0:
            return False
    finally:
        os.close(descriptor)
    try:
        stored = BackupManifest.model_validate_json(b"".join(chunks))
    except ValueError:
        return False
    return stored == manifest and stored.manifest_checksum_sha256 == _manifest_fingerprint(stored)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_DIRECTORY | os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _verification_failure(
    backup: Backup,
    manifest: BackupManifest,
    status: BackupVerificationStatus,
    *,
    missing_entries: tuple[str, ...] = (),
    message: str,
) -> BackupVerification:
    return BackupVerification(
        backup_id=backup.id,
        status=status,
        expected_file_count=manifest.file_count,
        verified_file_count=0,
        expected_size_bytes=manifest.total_size_bytes,
        verified_size_bytes=0,
        manifest_valid=False,
        missing_entries=missing_entries,
        message=message,
    )
