"""Server-owned, bounded collectors for read-only diagnostic capabilities."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from ares.actions.base import ActionError
from ares.diagnostic_capabilities.models import (
    DiagnosticFinding,
    DiagnosticInput,
    DiagnosticScope,
    DiagnosticSeverity,
    EvidenceReference,
    FailedService,
    MemoryAnalysisResult,
    MemoryConsumer,
    PackageHealthResult,
    ReclaimableEstimate,
    ServiceFailureResult,
    SpaceAnalysisInput,
    SpaceAnalysisResult,
    SpaceConsumer,
)
from ares.storage.store import StorageSnapshotStore
from ares.tools.storage import (
    ProcessResult,
    ProcessRunner,
    SafeProcessRunner,
    ToolAvailability,
)

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\x1b]")
_OOM = re.compile(r"out of memory|oom-kill|killed process", re.IGNORECASE)
_SERVICE = re.compile(r"\b([A-Za-z0-9_.@-]{1,128}\.service)\b")
_PRESSURE = re.compile(r"^some\s+avg10=([0-9.]+)", re.MULTILINE)
_MAX_SMALL_FILE = 8 * 1024 * 1024
_MAX_LOG_BYTES = 512 * 1024
_SYSTEMCTL_FAILED_ARGS = ("--failed", "--no-legend", "--plain", "--no-pager")
_SAFE_TARGET_ROOTS = (Path("/mnt"), Path("/run"), Path("/media"), Path("/tmp"))  # noqa: S108
_TEMP_PATH = "/tmp"  # noqa: S108
_VAR_TEMP_PATH = "/var/tmp"  # noqa: S108
_SpaceCategory = Literal[
    "logs",
    "package_cache",
    "cache",
    "temporary",
    "crash_data",
    "personal_data",
    "system_data",
    "unknown",
]
_ReclaimRisk = Literal["low", "review"]


class ReadOnlyDiagnosticProcessRunner:
    """Permit only the exact passive runtime probe used by this diagnostic slice."""

    def __init__(self, delegate: ProcessRunner) -> None:
        self.delegate = delegate

    def inspect(self, tool: str) -> ToolAvailability:
        if tool != "systemctl":
            return ToolAvailability(tool=tool, available=False, reason="read_only_policy_rejected")
        return self.delegate.inspect(tool)

    async def run(
        self, tool: str, args: tuple[str, ...], *, timeout_seconds: float
    ) -> ProcessResult:
        if tool != "systemctl" or args != _SYSTEMCTL_FAILED_ARGS:
            raise PermissionError("read_only_policy_rejected")
        return await self.delegate.run(tool, args, timeout_seconds=timeout_seconds)


@dataclass(frozen=True, slots=True)
class _Target:
    resource_id: str
    fingerprint: str
    scope: DiagnosticScope
    root: Path
    snapshot_id: str | None


@dataclass(frozen=True, slots=True)
class _TreeMeasurement:
    size_bytes: int
    entries: int
    truncated: bool


class DiagnosticToolSuite:
    """Collect normalized evidence without accepting commands, argv or paths from clients."""

    def __init__(
        self,
        snapshots: StorageSnapshotStore,
        *,
        runner: ProcessRunner | None = None,
        max_entries: int = 50_000,
    ) -> None:
        self.snapshots = snapshots
        self.runner = runner or SafeProcessRunner()
        self.max_entries = max(1_000, min(max_entries, 200_000))

    async def space(self, payload: SpaceAnalysisInput) -> SpaceAnalysisResult:
        target = await self._resolve(payload)
        return await asyncio.to_thread(self._space_sync, target, payload.analysis_depth)

    async def memory(self, payload: DiagnosticInput) -> MemoryAnalysisResult:
        target = await self._resolve(payload)
        return await asyncio.to_thread(self._memory_sync, target)

    async def packages(self, payload: DiagnosticInput) -> PackageHealthResult:
        target = await self._resolve(payload)
        return await asyncio.to_thread(self._packages_sync, target)

    async def services(self, payload: DiagnosticInput) -> ServiceFailureResult:
        target = await self._resolve(payload)
        return await self._services(target)

    async def _resolve(self, payload: DiagnosticInput) -> _Target:
        snapshot = (
            await self.snapshots.get(payload.snapshot_id)
            if payload.snapshot_id is not None
            else await self.snapshots.latest()
        )
        requested = payload.target_resource_id
        if requested == "recovery:ares-live" or (snapshot is None and requested is None):
            return _runtime_target("recovery:ares-live", DiagnosticScope.ARES_LIVE_RUNTIME)
        if snapshot is None:
            raise ActionError("DIAGNOSTIC_SNAPSHOT_NOT_FOUND")

        selected = None
        if requested is None and len(snapshot.operating_systems) == 1:
            selected = snapshot.operating_systems[0]
        elif requested is None:
            return _runtime_target("recovery:ares-live", DiagnosticScope.ARES_LIVE_RUNTIME)
        else:
            selected = next(
                (item for item in snapshot.operating_systems if requested == f"res:{item.id}"),
                None,
            )
            if selected is None:
                selected = _operating_system_for_related_resource(snapshot, requested)
        if selected is None:
            raise ActionError("DIAGNOSTIC_TARGET_NOT_FOUND")

        root = _validated_root(Path(selected.mountpoint))
        scope = (
            DiagnosticScope.ACTIVE_SYSTEM_RUNTIME
            if root == Path("/")
            else DiagnosticScope.INSTALLED_SYSTEM_OFFLINE
        )
        identity = f"{selected.source}:{selected.name}:{selected.version or ''}"
        return _Target(
            resource_id=f"res:{selected.id}",
            fingerprint="os:" + hashlib.sha256(identity.encode()).hexdigest(),
            scope=scope,
            root=root,
            snapshot_id=snapshot.id,
        )

    def _space_sync(self, target: _Target, depth: str) -> SpaceAnalysisResult:
        try:
            usage = os.statvfs(target.root)
        except OSError as exc:
            raise ActionError("SPACE_USAGE_UNAVAILABLE") from exc
        total = usage.f_blocks * usage.f_frsize
        available = usage.f_bavail * usage.f_frsize
        free = usage.f_bfree * usage.f_frsize
        used = max(0, total - free)
        used_percent = _percent(used, total)
        inode_total = max(0, usage.f_files)
        inode_used = max(0, inode_total - usage.f_ffree)
        inode_percent = _percent(inode_used, inode_total)
        try:
            root_device = target.root.stat().st_dev
        except OSError as exc:
            raise ActionError("SPACE_TARGET_UNAVAILABLE") from exc

        categories: tuple[tuple[str, _SpaceCategory], ...] = (
            ("var/log", "logs"),
            ("var/log/journal", "logs"),
            ("var/cache/apt/archives", "package_cache"),
            ("var/cache", "cache"),
            ("tmp", "temporary"),
            ("var/tmp", "temporary"),
            ("var/crash", "crash_data"),
            ("var/lib/systemd/coredump", "crash_data"),
            ("home", "personal_data"),
            ("usr", "system_data"),
            ("var/lib", "system_data"),
        )
        entry_limit = self.max_entries if depth == "standard" else min(10_000, self.max_entries)
        consumers: list[SpaceConsumer] = []
        limitations: list[str] = []
        for relative, category in categories:
            category_path = target.root / relative
            measurement = _measure_tree(category_path, root_device, entry_limit)
            if measurement is None:
                continue
            consumers.append(
                SpaceConsumer(
                    path=_display_path(relative),
                    category=category,
                    size_bytes=measurement.size_bytes,
                    entries_scanned=measurement.entries,
                    truncated=measurement.truncated,
                )
            )
            if measurement.truncated:
                limitations.append(f"El recorrido de {_display_path(relative)} alcanzó su límite.")
        consumers.sort(key=lambda item: item.size_bytes, reverse=True)

        by_path = {item.path: item for item in consumers}
        reclaimable: list[ReclaimableEstimate] = []
        reclaim_candidates: tuple[tuple[str, str, _ReclaimRisk, str], ...] = (
            ("/var/cache/apt/archives", "package_cache", "low", "packages.cache-cleanup"),
            ("/var/log/journal", "journal", "low", "system.journal-vacuum"),
            (_TEMP_PATH, "temporary", "review", "system.temp-cleanup"),
            (_VAR_TEMP_PATH, "temporary", "review", "system.temp-cleanup"),
            ("/var/crash", "crash_data", "review", "storage.space-reclaim"),
            ("/var/lib/systemd/coredump", "crash_data", "review", "storage.space-reclaim"),
        )
        for display_path, reclaim_category, risk, capability in reclaim_candidates:
            consumer = by_path.get(display_path)
            if consumer and consumer.size_bytes:
                reclaimable.append(
                    ReclaimableEstimate(
                        category=reclaim_category,
                        estimated_bytes=consumer.size_bytes,
                        risk=risk,
                        path=display_path,
                        corrective_capability=capability,
                    )
                )

        findings: list[DiagnosticFinding] = []
        if used_percent >= 95:
            findings.append(
                _finding(
                    "storage.space.critical",
                    DiagnosticSeverity.CRITICAL,
                    0.98,
                    "Filesystem casi lleno",
                    f"El filesystem usa {used_percent:.1f}% de su capacidad.",
                    recommendation=(
                        "Revisar primero categorías recuperables; "
                        "no borrar datos personales."
                    ),
                    capability="storage.space-reclaim",
                )
            )
        elif used_percent >= 85:
            findings.append(
                _finding(
                    "storage.space.warning",
                    DiagnosticSeverity.WARNING,
                    0.96,
                    "Poco espacio disponible",
                    f"El filesystem usa {used_percent:.1f}% de su capacidad.",
                    recommendation="Preparar una limpieza limitada y verificable.",
                    capability="storage.space-reclaim",
                )
            )
        if inode_percent >= 90:
            findings.append(
                _finding(
                    "storage.inodes.warning",
                    DiagnosticSeverity.WARNING,
                    0.96,
                    "Uso elevado de inodos",
                    f"Se utiliza {inode_percent:.1f}% de los inodos disponibles.",
                    recommendation=(
                        "Localizar árboles con muchos archivos pequeños antes de limpiar."
                    ),
                )
            )
        if reclaimable:
            estimate = sum(item.estimated_bytes for item in reclaimable)
            findings.append(
                _finding(
                    "storage.reclaimable.detected",
                    DiagnosticSeverity.INFO,
                    0.78,
                    "Espacio potencialmente recuperable",
                    f"ARES clasificó de forma conservadora hasta {estimate} bytes para revisión.",
                    recommendation="Autorizar por separado una capability correctiva con límites.",
                    capability="storage.space-reclaim",
                )
            )
        if not findings:
            findings.append(
                _finding(
                    "storage.space.normal",
                    DiagnosticSeverity.INFO,
                    0.9,
                    "Sin presión evidente de espacio",
                    "La capacidad y los inodos no superan los umbrales conservadores.",
                )
            )
        normalized = {
            "total": total,
            "used": used,
            "available": available,
            "inode_used": inode_used,
            "consumers": [item.model_dump(mode="json") for item in consumers],
        }
        return SpaceAnalysisResult(
            target_resource_id=target.resource_id,
            target_fingerprint=target.fingerprint,
            scope=target.scope,
            total_bytes=total,
            used_bytes=used,
            available_bytes=available,
            used_percent=used_percent,
            inode_total=inode_total,
            inode_used=inode_used,
            inode_used_percent=inode_percent,
            consumers=tuple(consumers),
            reclaimable=tuple(reclaimable),
            findings=tuple(findings),
            evidence=(_evidence("filesystem-stat-and-bounded-walk", "storage.space", normalized),),
            limitations=tuple(dict.fromkeys(limitations)),
            summary=(
                f"Análisis de espacio read-only: {used_percent:.1f}% usado; "
                f"{len(reclaimable)} categoría(s) recuperable(s) requieren revisión."
            ),
        )

    def _memory_sync(self, target: _Target) -> MemoryAnalysisResult:
        if target.scope is DiagnosticScope.INSTALLED_SYSTEM_OFFLINE:
            log_data = _historical_logs(target.root)
            oom_events = len(_OOM.findall(log_data))
            findings = (
                (
                    _finding(
                        "memory.historical.oom",
                        DiagnosticSeverity.WARNING,
                        0.78,
                        "Eventos OOM históricos",
                        (
                            "La evidencia persistente contiene "
                            f"{oom_events} evento(s) relacionado(s) con OOM."
                        ),
                        recommendation=(
                            "Revisar servicios y consumidores antes de cambiar swap o límites."
                        ),
                        capability="services.failure-analysis",
                    ),
                )
                if oom_events
                else (
                    _finding(
                        "memory.offline.no_runtime",
                        DiagnosticSeverity.INFO,
                        1.0,
                        "El target está offline",
                        "ARES no confunde logs históricos con presión de memoria actual.",
                    ),
                )
            )
            evidence = (
                (_evidence("persistent-system-logs", "system.memory", log_data),)
                if log_data
                else ()
            )
            return MemoryAnalysisResult(
                target_resource_id=target.resource_id,
                target_fingerprint=target.fingerprint,
                scope=target.scope,
                status="historical_only" if log_data else "insufficient_evidence",
                oom_events=oom_events,
                findings=findings,
                evidence=evidence,
                limitations=(
                    "No es posible medir presión, procesos ni RAM actual "
                    "de un sistema instalado offline.",
                ),
                summary=(
                    "Análisis histórico de memoria; no representa el estado runtime del target."
                ),
            )

        meminfo_text = _read_bounded(Path("/proc/meminfo"), 256 * 1024)
        if not meminfo_text:
            return MemoryAnalysisResult(
                target_resource_id=target.resource_id,
                target_fingerprint=target.fingerprint,
                scope=target.scope,
                status="insufficient_evidence",
                findings=(
                    _finding(
                        "memory.runtime.unavailable",
                        DiagnosticSeverity.WARNING,
                        1.0,
                        "Métricas de memoria no disponibles",
                        "ARES no pudo leer /proc/meminfo.",
                    ),
                ),
                evidence=(),
                limitations=("No se obtuvieron métricas runtime.",),
                summary="No existe evidencia suficiente para evaluar la memoria.",
            )
        values = _parse_meminfo(meminfo_text)
        total = values.get("MemTotal", 0)
        available = values.get("MemAvailable", 0)
        cache = values.get("Cached", 0) + values.get("Buffers", 0)
        swap_total = values.get("SwapTotal", 0)
        swap_used = max(0, swap_total - values.get("SwapFree", 0))
        pressure_text = _read_bounded(Path("/proc/pressure/memory"), 64 * 1024)
        pressure_match = _PRESSURE.search(pressure_text)
        pressure = float(pressure_match.group(1)) if pressure_match else None
        consumers = _memory_consumers(Path("/proc"))
        low_available = total > 0 and available / total < 0.1
        high_pressure = pressure is not None and pressure >= 10
        status: Literal["healthy", "pressure"] = (
            "pressure" if low_available or high_pressure else "healthy"
        )
        if status == "pressure":
            findings = (
                _finding(
                    "memory.runtime.pressure",
                    DiagnosticSeverity.WARNING,
                    0.9,
                    "Presión de memoria detectada",
                    "La memoria disponible o PSI supera los umbrales conservadores.",
                    recommendation=(
                        "Correlacionar consumidores con servicios; "
                        "no vaciar cachés a ciegas."
                    ),
                    capability="services.failure-analysis",
                ),
            )
        else:
            findings = (
                _finding(
                    "memory.runtime.healthy",
                    DiagnosticSeverity.INFO,
                    0.9,
                    "Sin presión real evidente",
                    (
                        "La memoria disponible y PSI no muestran presión sostenida; "
                        "la caché es reutilizable."
                    ),
                ),
            )
        normalized = {
            "total": total,
            "available": available,
            "cache": cache,
            "swap_total": swap_total,
            "swap_used": swap_used,
            "pressure": pressure,
            "consumers": [item.model_dump(mode="json") for item in consumers],
        }
        return MemoryAnalysisResult(
            target_resource_id=target.resource_id,
            target_fingerprint=target.fingerprint,
            scope=target.scope,
            status=status,
            total_bytes=total,
            available_bytes=available,
            cache_bytes=cache,
            swap_total_bytes=swap_total,
            swap_used_bytes=swap_used,
            pressure_avg10=pressure,
            consumers=consumers,
            findings=findings,
            evidence=(_evidence("proc-memory-normalized", "system.memory", normalized),),
            limitations=(),
            summary=(
                f"Análisis runtime de memoria: estado {status}; "
                "caché contabilizada por separado."
            ),
        )

    def _packages_sync(self, target: _Target) -> PackageHealthResult:
        status_path = target.root / "var/lib/dpkg/status"
        status_text = _read_bounded(status_path, _MAX_SMALL_FILE)
        if not status_text:
            return PackageHealthResult(
                target_resource_id=target.resource_id,
                target_fingerprint=target.fingerprint,
                scope=target.scope,
                status="unsupported",
                findings=(
                    _finding(
                        "packages.manager.unsupported",
                        DiagnosticSeverity.INFO,
                        0.98,
                        "Base dpkg no detectada",
                        (
                            "Esta versión implementa diagnóstico real para Debian y Ubuntu "
                            "con dpkg/APT."
                        ),
                    ),
                ),
                evidence=(),
                limitations=("No se afirma compatibilidad con otro gestor de paquetes.",),
                summary="El target no expone una base dpkg compatible.",
            )
        installed, inconsistent, held = _parse_dpkg_status(status_text)
        updates = _safe_directory_count(target.root / "var/lib/dpkg/updates")
        extended = _read_bounded(target.root / "var/lib/apt/extended_states", _MAX_SMALL_FILE)
        auto_installed = extended.count("Auto-Installed: 1")
        metadata_age = _apt_metadata_age(target.root / "var/lib/apt/lists")
        repositories = _repository_count(target.root)
        if inconsistent or updates:
            health: Literal[
                "healthy", "warnings", "repair_required", "metadata_stale"
            ] = "repair_required"
            severity = DiagnosticSeverity.ERROR
            title = "Estado de paquetes inconsistente"
        elif metadata_age is not None and metadata_age > 14:
            health = "metadata_stale"
            severity = DiagnosticSeverity.WARNING
            title = "Metadatos locales de APT antiguos"
        elif held:
            health = "warnings"
            severity = DiagnosticSeverity.WARNING
            title = "Paquetes retenidos"
        else:
            health = "healthy"
            severity = DiagnosticSeverity.INFO
            title = "Base de paquetes coherente"
        findings = (
            _finding(
                f"packages.health.{health.replace('_', '-')}",
                severity,
                0.92,
                title,
                (
                    f"Se observaron {installed} paquetes instalados, {len(inconsistent)} "
                    f"inconsistentes y {updates} fragmento(s) de actualización pendiente(s)."
                ),
                recommendation=(
                    "Preparar packages.repair con autorización mutable independiente."
                    if health == "repair_required"
                    else None
                ),
                capability="packages.repair" if health == "repair_required" else None,
            ),
        )
        normalized = {
            "installed": installed,
            "inconsistent": inconsistent,
            "held": held,
            "updates": updates,
            "metadata_age": metadata_age,
            "repositories": repositories,
        }
        return PackageHealthResult(
            target_resource_id=target.resource_id,
            target_fingerprint=target.fingerprint,
            scope=target.scope,
            status=health,
            package_manager="dpkg/apt",
            installed_packages=installed,
            inconsistent_packages=tuple(inconsistent[:100]),
            held_packages=tuple(held[:100]),
            auto_installed_packages=auto_installed,
            interrupted_update_fragments=updates,
            metadata_age_days=metadata_age,
            repository_entries=repositories,
            findings=findings,
            evidence=(_evidence("dpkg-apt-metadata", "packages.health", normalized),),
            limitations=(
                "No se ejecutó apt update ni una resolución mutable de dependencias.",
                "Los metadatos de actualizaciones reflejan únicamente el cache local.",
            ),
            summary=f"Health-check dpkg/APT read-only: estado {health}.",
        )

    async def _services(self, target: _Target) -> ServiceFailureResult:
        failed: list[FailedService] = []
        log_data = ""
        limitations: list[str] = []
        evidence: list[EvidenceReference] = []
        if target.scope is not DiagnosticScope.INSTALLED_SYSTEM_OFFLINE:
            availability = self.runner.inspect("systemctl")
            if availability.available:
                result = await self.runner.run(
                    "systemctl",
                    _SYSTEMCTL_FAILED_ARGS,
                    timeout_seconds=4,
                )
                failed = _parse_failed_services(result.stdout)
                evidence.append(
                    _evidence(
                        "systemctl-failed-normalized",
                        "services.failure",
                        [item.model_dump(mode="json") for item in failed],
                    )
                )
            else:
                limitations.append("systemctl no está disponible o no es un ejecutable confiable.")
            log_data = _historical_logs(Path("/"))
            status: Literal[
                "healthy", "failures_detected", "historical_only", "insufficient_evidence"
            ] = "failures_detected" if failed else "healthy"
        else:
            log_data = _historical_logs(target.root)
            failed = _services_from_logs(log_data)
            status = "historical_only" if log_data else "insufficient_evidence"
            limitations.append(
                "Un target offline no tiene estado systemd actual; "
                "solo se analiza evidencia persistente."
            )
        error_events = _error_event_count(log_data)
        if log_data:
            evidence.append(_evidence("bounded-persistent-logs", "services.failure", log_data))
        findings: list[DiagnosticFinding] = []
        if failed:
            findings.append(
                _finding(
                    "services.failures.detected",
                    DiagnosticSeverity.ERROR,
                    0.86,
                    "Servicios fallidos detectados",
                    f"ARES observó {len(failed)} servicio(s) fallido(s) sin reiniciarlos.",
                    recommendation=(
                        "Correlacionar con memoria, espacio y paquetes antes de reiniciar."
                    ),
                    capability="services.restart",
                )
            )
        elif status == "healthy":
            findings.append(
                _finding(
                    "services.runtime.healthy",
                    DiagnosticSeverity.INFO,
                    0.9,
                    "Sin unidades fallidas",
                    "systemd no reportó unidades fallidas en el runtime analizado.",
                )
            )
        else:
            findings.append(
                _finding(
                    "services.offline.evidence",
                    DiagnosticSeverity.INFO,
                    0.7 if log_data else 1.0,
                    "Análisis histórico de servicios",
                    f"Se encontraron {error_events} evento(s) de error en evidencia persistente.",
                )
            )
        return ServiceFailureResult(
            target_resource_id=target.resource_id,
            target_fingerprint=target.fingerprint,
            scope=target.scope,
            status=status,
            service_manager="systemd",
            failed_services=tuple(failed[:100]),
            error_events=error_events,
            findings=tuple(findings),
            evidence=tuple(evidence),
            limitations=tuple(dict.fromkeys(limitations)),
            summary=(
                f"Análisis de servicios read-only: {len(failed)} servicio(s) fallido(s), "
                f"{error_events} evento(s) de error."
            ),
        )


def _runtime_target(resource_id: str, scope: DiagnosticScope) -> _Target:
    return _Target(
        resource_id=resource_id,
        fingerprint="recovery:" + hashlib.sha256(b"ares-live").hexdigest(),
        scope=scope,
        root=Path("/"),
        snapshot_id=None,
    )


def _operating_system_for_related_resource(snapshot: Any, resource_id: str) -> Any | None:
    raw_id = resource_id.removeprefix("res:")
    partitions = {item.id: item for item in snapshot.partitions}
    if raw_id in {item.id for item in snapshot.disks}:
        matches = [item for item in snapshot.operating_systems if item.disk_id == raw_id]
        return matches[0] if len(matches) == 1 else None
    partition = partitions.get(raw_id)
    if partition is None:
        partition = next(
            (item for item in snapshot.partitions if item.filesystem_id == raw_id),
            None,
        )
    if partition is None:
        return None
    return next(
        (item for item in snapshot.operating_systems if item.source == partition.path), None
    )


def _validated_root(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ActionError("DIAGNOSTIC_TARGET_UNAVAILABLE") from exc
    if not resolved.is_dir():
        raise ActionError("DIAGNOSTIC_TARGET_UNAVAILABLE")
    if resolved == Path("/"):
        return resolved
    if not any(resolved.is_relative_to(prefix) for prefix in _SAFE_TARGET_ROOTS):
        raise ActionError("DIAGNOSTIC_TARGET_OUTSIDE_SAFE_ROOT")
    return resolved


def _measure_tree(path: Path, root_device: int, max_entries: int) -> _TreeMeasurement | None:
    try:
        initial = path.stat(follow_symlinks=False)
    except (FileNotFoundError, PermissionError, OSError):
        return None
    if not stat.S_ISDIR(initial.st_mode) or initial.st_dev != root_device:
        return None
    total = 0
    entries = 0
    truncated = False
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            iterator = os.scandir(current)
        except OSError:
            continue
        with iterator:
            for entry in iterator:
                entries += 1
                if entries > max_entries:
                    truncated = True
                    stack.clear()
                    break
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if info.st_dev != root_device or stat.S_ISLNK(info.st_mode):
                    continue
                if stat.S_ISREG(info.st_mode):
                    total += max(0, info.st_size)
                elif stat.S_ISDIR(info.st_mode):
                    stack.append(Path(entry.path))
    return _TreeMeasurement(total, min(entries, max_entries), truncated)


def _parse_meminfo(text: str) -> dict[str, int]:
    output: dict[str, int] = {}
    for line in text.splitlines():
        key, separator, rest = line.partition(":")
        if not separator:
            continue
        parts = rest.strip().split()
        if not parts or not parts[0].isdigit():
            continue
        multiplier = 1024 if len(parts) > 1 and parts[1].casefold() == "kb" else 1
        output[key] = int(parts[0]) * multiplier
    return output


def _memory_consumers(proc: Path) -> tuple[MemoryConsumer, ...]:
    output: list[MemoryConsumer] = []
    try:
        candidates = tuple(proc.iterdir())[:4096]
    except OSError:
        return ()
    for candidate in candidates:
        if not candidate.name.isdigit():
            continue
        text = _read_bounded(candidate / "status", 128 * 1024)
        if not text:
            continue
        fields = dict(line.split(":", 1) for line in text.splitlines() if ":" in line)
        rss_text = fields.get("VmRSS", "").strip().split()
        if not rss_text or not rss_text[0].isdigit():
            continue
        name = _safe_label(fields.get("Name", "unknown"), "process")
        output.append(
            MemoryConsumer(
                pid=int(candidate.name),
                name=name or "unknown",
                resident_bytes=int(rss_text[0]) * 1024,
            )
        )
    return tuple(sorted(output, key=lambda item: item.resident_bytes, reverse=True)[:10])


def _parse_dpkg_status(text: str) -> tuple[int, list[str], list[str]]:
    installed = 0
    inconsistent: list[str] = []
    held: list[str] = []
    for paragraph in text.split("\n\n")[:200_000]:
        fields: dict[str, str] = {}
        for line in paragraph.splitlines():
            key, separator, value = line.partition(":")
            if separator and key in {"Package", "Status"}:
                fields[key] = value.strip()
        package = _safe_label(fields.get("Package", "unknown"), "package")
        state = fields.get("Status", "")
        if state == "install ok installed":
            installed += 1
        elif state:
            inconsistent.append(package)
        if state.startswith("hold "):
            held.append(package)
    return installed, inconsistent, held


def _safe_directory_count(path: Path) -> int:
    try:
        return sum(1 for item in path.iterdir() if not item.is_symlink())
    except OSError:
        return 0


def _apt_metadata_age(path: Path) -> int | None:
    try:
        timestamps = [
            item.stat(follow_symlinks=False).st_mtime for item in path.iterdir() if item.is_file()
        ]
    except OSError:
        return None
    if not timestamps:
        return None
    newest = max(timestamps)
    return max(0, int((datetime.now(UTC).timestamp() - newest) // 86_400))


def _repository_count(root: Path) -> int:
    paths = [root / "etc/apt/sources.list"]
    directory = root / "etc/apt/sources.list.d"
    with suppress(OSError):
        paths.extend(item for item in directory.iterdir() if item.suffix in {".list", ".sources"})
    count = 0
    for path in paths[:256]:
        text = _read_bounded(path, 512 * 1024)
        count += sum(
            1 for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")
        )
    return count


def _parse_failed_services(text: str) -> list[FailedService]:
    output: list[FailedService] = []
    for line in text.splitlines()[:200]:
        cleaned = _clean(line, 500)
        match = _SERVICE.search(cleaned)
        if match is None:
            continue
        parts = cleaned.split(None, 4)
        output.append(
            FailedService(
                unit=match.group(1),
                state=parts[3] if len(parts) > 3 else "failed",
                reason="Unidad reportada como fallida por systemd.",
            )
        )
    return output


def _services_from_logs(text: str) -> list[FailedService]:
    services: dict[str, FailedService] = {}
    for line in text.splitlines()[-5_000:]:
        if not re.search(r"failed|failure|start request repeated", line, re.IGNORECASE):
            continue
        match = _SERVICE.search(line)
        if match:
            services.setdefault(
                match.group(1),
                FailedService(unit=match.group(1), reason="Falla observada en logs persistentes."),
            )
    return list(services.values())


def _error_event_count(text: str) -> int:
    pattern = re.compile(r"failed|failure|segfault|out of memory|no space left", re.IGNORECASE)
    return min(10_000, sum(1 for line in text.splitlines() if pattern.search(line)))


def _historical_logs(root: Path) -> str:
    chunks: list[str] = []
    remaining = _MAX_LOG_BYTES
    for relative in ("var/log/syslog", "var/log/kern.log", "var/log/messages"):
        if remaining <= 0:
            break
        text = _read_bounded(root / relative, remaining)
        if text:
            chunks.append(text)
            remaining -= len(text.encode("utf-8", errors="ignore"))
    return _clean("\n".join(chunks), _MAX_LOG_BYTES)


def _read_bounded(path: Path, limit: int) -> str:
    try:
        info = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit or path.is_symlink():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _evidence(source: str, collector: str, value: object) -> EvidenceReference:
    if isinstance(value, str):
        encoded = value.encode("utf-8", errors="replace")
    else:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    return EvidenceReference(
        id=f"evidence:{digest}",
        source=source,
        collector=collector,
        sha256=f"sha256:{digest}",
    )


def _finding(
    identifier: str,
    severity: DiagnosticSeverity,
    confidence: float,
    title: str,
    detail: str,
    *,
    recommendation: str | None = None,
    capability: str | None = None,
) -> DiagnosticFinding:
    return DiagnosticFinding(
        id=identifier,
        severity=severity,
        confidence=confidence,
        title=title,
        detail=detail,
        recommendation=recommendation,
        recommended_capability=capability,
    )


def _display_path(relative: str) -> str:
    if relative == "home":
        return "/home/$USER"
    return "/" + relative.strip("/")


def _clean(value: str, limit: int) -> str:
    return _CONTROL.sub("", value).replace("\r", " ")[:limit]


def _safe_label(value: str, prefix: str) -> str:
    cleaned = _clean(value, 96).strip()
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@+-]{0,95}", cleaned):
        return cleaned
    digest = hashlib.sha256(cleaned.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"untrusted-{prefix}-{digest}"


def _percent(used: int, total: int) -> float:
    return min(100.0, max(0.0, (used / total * 100) if total else 0.0))
