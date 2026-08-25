"""Private read-only actions for boot diagnostics."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ares.actions.base import ActionContext, ActionError
from ares.boot import (
    BootDiagnosticResult,
    BootFinding,
    BootFindingSeverity,
    BootInstalledSystem,
    FirmwareMode,
)
from ares.events import AresEvent
from ares.storage.models import SystemStorageSnapshot
from ares.storage.store import StorageSnapshotStore


class DiagnoseBootAction:
    """Inspect boot evidence that is already visible; never mounts or repairs."""

    id = "boot.diagnose-read-only"
    idempotent = True

    def __init__(self, snapshots: StorageSnapshotStore) -> None:
        self.snapshots = snapshots

    async def run(self, inputs: dict[str, Any], context: ActionContext) -> dict[str, Any]:
        snapshot_id = inputs.get("snapshot_id")
        if snapshot_id is not None:
            if not isinstance(snapshot_id, str):
                raise ActionError("BOOT_SNAPSHOT_INVALID")
            snapshot = await self.snapshots.get(snapshot_id)
        else:
            snapshot = await self.snapshots.latest()
        if snapshot is None:
            raise ActionError("BOOT_SNAPSHOT_NOT_FOUND")
        result = _diagnose(snapshot)
        await context.event_bus.publish(
            AresEvent(
                name="boot.diagnostic.completed",
                source=self.id,
                correlation_id=context.execution_id,
                payload={
                    "snapshot_id": snapshot.id,
                    "firmware": result.firmware.value,
                    "installed_system_count": len(result.installed_systems),
                    "bootloader_verified": result.bootloader_verified,
                    "finding_count": len(result.findings),
                },
            )
        )
        return result.model_dump(mode="json")

    async def compensate(self, output: dict[str, Any], context: ActionContext) -> None:
        del output, context


def _diagnose(snapshot: SystemStorageSnapshot) -> BootDiagnosticResult:
    firmware = _firmware()
    filesystems = {item.id: item for item in snapshot.filesystems}
    partitions_by_path = {item.path: item for item in snapshot.partitions}
    findings: list[BootFinding] = []
    evidence: list[str] = [
        f"snapshot:{snapshot.id}",
        f"firmware:{firmware.value}",
        f"disk_count:{snapshot.summary.disk_count}",
        f"partition_count:{snapshot.summary.partition_count}",
    ]
    limitations: list[str] = []

    installed: list[BootInstalledSystem] = []
    for os_item in snapshot.operating_systems:
        grub_config = _grub_config_visible(Path(os_item.mountpoint))
        installed.append(
            BootInstalledSystem(
                id=os_item.id,
                name=os_item.name,
                version=os_item.version,
                source=os_item.source,
                mountpoint=os_item.mountpoint,
                disk_id=os_item.disk_id,
                bootloader_checked=Path(os_item.mountpoint).exists(),
                grub_config_found=grub_config,
            )
        )
        evidence.append(f"installed-os:{os_item.id}:{os_item.name}")
        if grub_config:
            evidence.append(f"grub-config-visible:{os_item.id}")

    efi_partitions: list[str] = []
    for partition in snapshot.partitions:
        filesystem = filesystems.get(partition.filesystem_id or "")
        if filesystem is None:
            continue
        fs_type = (filesystem.filesystem_type or "").casefold()
        label = (filesystem.label or "").casefold()
        mounted_paths = [
            mount.path
            for mount in snapshot.mounts
            if mount.id in partition.mount_point_ids
        ]
        if fs_type in {"vfat", "fat32"} and (
            "efi" in label or "/boot/efi" in mounted_paths or partition.size_bytes <= 2 * 1024**3
        ):
            efi_partitions.append(partition.id)
            evidence.append(f"efi-candidate:{partition.id}:{partition.path}")

    if not snapshot.disks:
        findings.append(
            BootFinding(
                id="boot.no_disks",
                severity=BootFindingSeverity.ERROR,
                title="No se detectaron discos",
                detail="El diagnóstico de arranque no puede continuar sin evidencia de almacenamiento.",
                evidence=("hardware.block-devices",),
            )
        )
    if not installed:
        findings.append(
            BootFinding(
                id="boot.no_installed_system",
                severity=BootFindingSeverity.WARNING,
                title="No se detectó sistema instalado",
                detail=(
                    "ARES no encontró una instalación Linux en los filesystems ya visibles. "
                    "Puede requerirse montar de forma segura candidatos en una fase posterior."
                ),
                evidence=("storage.operating_systems",),
            )
        )
    elif len(installed) > 1:
        findings.append(
            BootFinding(
                id="boot.multiple_systems",
                severity=BootFindingSeverity.WARNING,
                title="Se detectaron varios sistemas instalados",
                detail="ARES debe pedir selección explícita antes de cualquier diagnóstico dirigido.",
                evidence=tuple(item.id for item in installed),
            )
        )

    if firmware is FirmwareMode.UEFI and not efi_partitions:
        findings.append(
            BootFinding(
                id="boot.uefi_without_efi_partition",
                severity=BootFindingSeverity.WARNING,
                title="UEFI detectado sin partición EFI verificada",
                detail=(
                    "El firmware actual es UEFI, pero el snapshot no contiene una partición EFI "
                    "claramente identificada."
                ),
                evidence=("firmware.uefi",),
            )
        )
    if firmware is FirmwareMode.UNKNOWN:
        limitations.append("No se pudo determinar si el equipo arrancó en UEFI o BIOS.")

    bootloader_verified = any(item.grub_config_found for item in installed)
    if bootloader_verified:
        findings.append(
            BootFinding(
                id="boot.grub_config_visible",
                severity=BootFindingSeverity.INFO,
                title="Configuración GRUB visible",
                detail="ARES encontró grub.cfg en un sistema ya montado; no se modificó.",
                evidence=("grub.cfg",),
            )
        )
    else:
        limitations.append(
            "GRUB no fue verificado porque ARES no monta sistemas automáticamente en boot.diagnose."
        )

    for partition in snapshot.partitions:
        filesystem = filesystems.get(partition.filesystem_id or "")
        if filesystem is None:
            findings.append(
                BootFinding(
                    id=f"boot.partition_without_filesystem.{partition.name}".replace("_", "-"),
                    severity=BootFindingSeverity.INFO,
                    title="Partición sin filesystem identificado",
                    detail=f"{partition.path} no tiene filesystem identificado en el snapshot.",
                    resource_id=partition.id,
                    evidence=(partition.id,),
                )
            )

    if not findings:
        findings.append(
            BootFinding(
                id="boot.no_obvious_issue",
                severity=BootFindingSeverity.INFO,
                title="Sin problema de arranque evidente en evidencia disponible",
                detail=(
                    "La evidencia pasiva no mostró una falla concluyente. Esto no confirma que "
                    "el sistema arranque; solo indica que se requiere evidencia adicional."
                ),
                evidence=tuple(evidence),
            )
        )
    summary = (
        f"Diagnóstico de arranque read-only: {len(installed)} sistema(s) instalado(s), "
        f"{len(efi_partitions)} candidato(s) EFI, firmware {firmware.value}."
    )
    return BootDiagnosticResult(
        snapshot_id=snapshot.id,
        firmware=firmware,
        installed_systems=tuple(installed),
        efi_partitions=tuple(efi_partitions),
        bootloader_verified=bootloader_verified,
        bootloader_name="GRUB" if bootloader_verified else None,
        findings=tuple(findings),
        evidence=tuple(dict.fromkeys(evidence)),
        limitations=tuple(dict.fromkeys(limitations)),
        repair_capability_available=False,
        summary=summary,
    )


def _firmware() -> FirmwareMode:
    if Path("/sys/firmware/efi").exists():
        return FirmwareMode.UEFI
    if Path("/sys/firmware").exists():
        return FirmwareMode.BIOS
    return FirmwareMode.UNKNOWN


def _grub_config_visible(mountpoint: Path) -> bool:
    try:
        resolved = mountpoint.resolve(strict=False)
    except OSError:
        return False
    if not str(resolved).startswith(("/mnt", "/run", "/media", "/tmp")):
        return False
    return any(
        (resolved / relative).is_file()
        for relative in ("boot/grub/grub.cfg", "boot/grub2/grub.cfg", "grub/grub.cfg")
    )
