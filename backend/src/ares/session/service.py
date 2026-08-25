"""Safe closed operations for ending the ARES Live session."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

from ares.config import Environment, Settings
from ares.resources.models import ResourceKind
from ares.resources.service import ResourceResolver
from ares.session.models import (
    BootReturnCapability,
    FirmwareKind,
    SessionActionRequest,
    SessionActionResult,
    SessionOperation,
    SessionPreflight,
    TerminalContext,
    TerminalContextCollection,
    TerminalContextKind,
    TerminalOpenRequest,
    TerminalOpenResult,
)
from ares.terminal.service import TerminalService


class SystemSessionService:
    """Preflight and execute only fixed power/session transitions."""

    def __init__(
        self, settings: Settings, resources: ResourceResolver, terminals: TerminalService | None = None
    ) -> None:
        self.settings = settings
        self.resources = resources
        self.terminals = terminals

    async def preflight(self, operation: SessionOperation) -> SessionPreflight:
        catalog = await self.resources.catalog()
        installed = tuple(
            item for item in catalog.resources if item.kind is ResourceKind.OPERATING_SYSTEM
        )
        firmware = _firmware()
        bootloader_detected = _bootloader_detected()
        capabilities = _capabilities(firmware, bootloader_detected, bool(installed))
        active_operations = _active_operations(self.settings)
        if self.terminals is not None:
            active_operations = tuple(
                sorted({*active_operations, *(await self.terminals.blocking_sessions())})
            )
        prepared_terminals = _prepared_terminals(self.settings)
        mounted = tuple(
            item.human_name
            for item in catalog.resources
            if item.kind in {ResourceKind.MOUNT, ResourceKind.FILESYSTEM}
            and item.technical_path
            and item.technical_path.startswith("/run/ares/mounts/")
        )
        mounted = tuple(sorted({*mounted, *_ares_mounts_from_mountinfo()}))
        requested = next((item for item in capabilities if item.operation is operation), None)
        allowed = bool(
            requested
            and requested.available
            and not active_operations
            and not prepared_terminals
            and not mounted
        )
        message = _message(
            operation,
            firmware,
            bootloader_detected,
            bool(installed),
            allowed,
            active_operations=active_operations,
            prepared_terminals=prepared_terminals,
            mounted_resources=mounted,
        )
        return SessionPreflight(
            requested_operation=operation,
            allowed=allowed,
            firmware=firmware,
            active_operations=active_operations,
            prepared_terminals=prepared_terminals,
            mounted_resources=mounted,
            installed_systems=installed,
            bootloader_detected=bootloader_detected,
            bootloader_name="GRUB" if bootloader_detected else None,
            capabilities=capabilities,
            steps_before_exit=(
                "Comprobar que no haya operaciones ARES activas.",
                "Sincronizar escrituras pendientes con sync.",
                "Cerrar terminales preparadas por ARES si existen.",
                "Limpiar montajes gestionados por ARES cuando existan.",
                "Abortar la salida si queda una operación, terminal o montaje activo.",
                "Informar si se debe retirar el USB de recuperación.",
            ),
            message=message,
        )

    async def execute(self, request: SessionActionRequest) -> SessionActionResult:
        preflight = await self.preflight(request.operation)
        if not request.confirm or not preflight.allowed:
            return SessionActionResult(
                accepted=False,
                executed=False,
                operation=request.operation,
                preflight=preflight,
                message="No se ejecutó ninguna transición; falta confirmación o preflight seguro.",
            )
        if request.understood != "ENTIENDO":
            return SessionActionResult(
                accepted=False,
                executed=False,
                operation=request.operation,
                preflight=preflight,
                message="La confirmación contextual no coincide. Escribe ENTIENDO.",
            )
        await asyncio.to_thread(os.sync)
        _discard_pending_terminal_requests(self.settings)
        if self.settings.environment is Environment.TEST:
            return SessionActionResult(
                accepted=True,
                executed=False,
                operation=request.operation,
                preflight=preflight,
                message="Preflight aceptado en modo TEST; no se reinició ni apagó el sistema.",
            )
        args = _systemctl_args(request.operation, preflight)
        if args is None:
            return SessionActionResult(
                accepted=False,
                executed=False,
                operation=request.operation,
                preflight=preflight,
                message="No existe una transición systemctl verificada para esta operación.",
            )
        process = await asyncio.create_subprocess_exec(
            "/usr/bin/systemctl",
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            cwd="/",
            env={"LANG": "C", "LC_ALL": "C", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
        )
        code = await process.wait()
        return SessionActionResult(
            accepted=code == 0,
            executed=code == 0,
            operation=request.operation,
            preflight=preflight,
            message="Transición solicitada a systemd." if code == 0 else "systemctl rechazó la transición.",
        )

    async def terminal_contexts(self) -> TerminalContextCollection:
        catalog = await self.resources.catalog()
        installed = tuple(
            item for item in catalog.resources if item.kind is ResourceKind.OPERATING_SYSTEM
        )
        contexts = [
            TerminalContext(
                id=TerminalContextKind.ARES_RECOVERY,
                label="Terminal del entorno ARES",
                available=True,
                requires_authorization=False,
                privilege="usuario ares sin privilegios",
                explanation="Abre una terminal manual en el sistema Live. El LLM no puede leerla ni controlarla.",
            ),
            TerminalContext(
                id=TerminalContextKind.READ_ONLY,
                label="Terminal de solo lectura",
                available=True,
                requires_authorization=False,
                privilege="usuario ares sin privilegios",
                explanation="Abre una terminal manual con encabezado de solo lectura; no prepara chroot ni privilegios.",
            ),
            TerminalContext(
                id=TerminalContextKind.INSTALLED_DIRECTORY,
                label="Terminal en directorio del sistema detectado",
                available=bool(installed),
                requires_authorization=True,
                privilege="usuario ares; montajes no preparados por esta iteración",
                explanation="Visible para selección, pero todavía no prepara mount namespace.",
                resource_id=installed[0].resource_id if len(installed) == 1 else None,
                limitations=("Preparación automática de montajes/chroot pendiente.",),
            ),
            TerminalContext(
                id=TerminalContextKind.INSTALLED_CHROOT,
                label="Terminal dentro del sistema instalado",
                available=False,
                requires_authorization=True,
                privilege="bloqueado",
                explanation="Requiere resolver raíz, EFI, /dev, /proc, /sys, /run y limpieza de namespace.",
                limitations=("No implementado aún; no se simula chroot.",),
            ),
            TerminalContext(
                id=TerminalContextKind.ADMINISTRATIVE,
                label="Terminal administrativa",
                available=False,
                requires_authorization=True,
                privilege="bloqueado",
                explanation="Requiere una frontera privilegiada separada y autorización explícita.",
                limitations=("No se abre terminal privilegiada silenciosamente.",),
            ),
        ]
        return TerminalContextCollection(contexts=tuple(contexts), count=len(contexts))

    async def open_terminal(self, request: TerminalOpenRequest) -> TerminalOpenResult:
        contexts = await self.terminal_contexts()
        context = next((item for item in contexts.contexts if item.id is request.context), None)
        if context is None:
            return TerminalOpenResult(
                accepted=False,
                context=request.context,
                message="Contexto de terminal desconocido.",
            )
        if not context.available:
            return TerminalOpenResult(
                accepted=False,
                context=request.context,
                message="Ese contexto de terminal no está implementado de forma segura.",
                limitations=context.limitations,
            )
        if context.requires_authorization and (
            not request.confirm or request.understood != "ENTIENDO"
        ):
            return TerminalOpenResult(
                accepted=False,
                context=request.context,
                message="Este contexto requiere autorización explícita y confirmación ENTIENDO.",
                limitations=context.limitations,
            )
        if request.context not in {TerminalContextKind.ARES_RECOVERY, TerminalContextKind.READ_ONLY}:
            return TerminalOpenResult(
                accepted=False,
                context=request.context,
                message="ARES no abrirá este contexto hasta implementar preparación y limpieza reales.",
                limitations=context.limitations,
            )
        request_id = uuid4().hex
        payload = {
            "request_id": request_id,
            "context": request.context.value,
            "label": context.label,
            "privilege": context.privilege,
            "resource_id": request.resource_id,
        }
        terminal_dir = self.settings.runtime_state_dir / "terminal"
        terminal_dir.mkdir(mode=0o770, parents=True, exist_ok=True)
        target = terminal_dir / f"{request_id}.json"
        await asyncio.to_thread(
            target.write_text,
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        return TerminalOpenResult(
            accepted=True,
            request_id=request_id,
            context=request.context,
            message="Solicitud enviada a la sesión gráfica ARES. El usuario controla la terminal manual.",
        )


def _firmware() -> FirmwareKind:
    if Path("/sys/firmware/efi").exists():
        return FirmwareKind.UEFI
    if Path("/sys/firmware").exists():
        return FirmwareKind.BIOS
    return FirmwareKind.UNKNOWN


def _bootloader_detected() -> bool:
    candidates = (
        Path("/boot/grub/grub.cfg"),
        Path("/boot/grub2/grub.cfg"),
        Path("/run/live/medium/boot/grub/grub.cfg"),
    )
    return any(path.exists() for path in candidates)


def _capabilities(
    firmware: FirmwareKind, bootloader_detected: bool, installed_system: bool
) -> tuple[BootReturnCapability, ...]:
    return (
        BootReturnCapability(
            operation=SessionOperation.POWEROFF,
            available=True,
            verified=True,
            label="Apagar el equipo",
            explanation="Apaga ARES mediante systemd después de sincronizar escrituras.",
        ),
        BootReturnCapability(
            operation=SessionOperation.REBOOT,
            available=True,
            verified=True,
            label="Reiniciar el equipo",
            explanation="Reinicia el equipo. Si arrancaste desde USB, retíralo cuando el firmware apague la pantalla.",
        ),
        BootReturnCapability(
            operation=SessionOperation.REBOOT_TO_INSTALLED_SYSTEM,
            available=installed_system,
            verified=installed_system,
            label="Reiniciar hacia el sistema instalado",
            explanation=(
                "ARES detectó al menos un sistema instalado. No modifica GRUB ni BootNext; "
                "reinicia y te indica retirar el USB o cambiar prioridad de arranque."
            ),
            warnings=()
            if installed_system
            else ("No se detectó un sistema instalado en el snapshot actual.",),
        ),
        BootReturnCapability(
            operation=SessionOperation.REBOOT_TO_BOOT_MENU,
            available=firmware is FirmwareKind.UEFI,
            verified=firmware is FirmwareKind.UEFI,
            label="Reiniciar al menú de firmware",
            explanation=(
                "En UEFI se solicita firmware-setup mediante systemd. Esto no equivale a reparar GRUB."
                if firmware is FirmwareKind.UEFI
                else "En BIOS Legacy no hay una llamada estándar verificable al menú de firmware."
            ),
            warnings=()
            if bootloader_detected
            else ("GRUB no fue verificado; se ofrecerá menú de firmware, no 'volver a GRUB'.",),
        ),
    )


def _message(
    operation: SessionOperation,
    firmware: FirmwareKind,
    bootloader_detected: bool,
    installed: bool,
    allowed: bool,
    *,
    active_operations: tuple[str, ...] = (),
    prepared_terminals: tuple[str, ...] = (),
    mounted_resources: tuple[str, ...] = (),
) -> str:
    if active_operations:
        return "No se permite salir: ARES detectó operaciones en curso o no reconciliadas."
    if prepared_terminals:
        return "No se permite salir: existen terminales ARES pendientes de abrir o cerrar."
    if mounted_resources:
        return "No se permite salir: existen montajes gestionados por ARES que requieren limpieza."
    if not allowed:
        return "La operación solicitada no está verificada como segura en el estado actual."
    if operation is SessionOperation.REBOOT_TO_BOOT_MENU and not bootloader_detected:
        return "Se puede solicitar menú de firmware UEFI, pero ARES no verificó GRUB."
    if operation is SessionOperation.REBOOT_TO_INSTALLED_SYSTEM and installed:
        return "ARES reiniciará; retira el USB de recuperación cuando el equipo vuelva a arrancar."
    return f"Operación {operation.value} disponible en firmware {firmware.value}."


def _systemctl_args(
    operation: SessionOperation, preflight: SessionPreflight
) -> tuple[str, ...] | None:
    if operation is SessionOperation.POWEROFF:
        return ("poweroff",)
    if operation in {SessionOperation.REBOOT, SessionOperation.REBOOT_TO_INSTALLED_SYSTEM}:
        return ("reboot",)
    if (
        operation is SessionOperation.REBOOT_TO_BOOT_MENU
        and preflight.firmware is FirmwareKind.UEFI
    ):
        return ("reboot", "--firmware-setup")
    return None


def _active_operations(settings: Settings) -> tuple[str, ...]:
    root = settings.capability_state_dir or settings.runtime_state_dir / "capabilities"
    candidates = (
        root / "agent/runs",
        root / "backups",
        root / "filesystems/repairs",
        root / "storage-operations/operations",
    )
    terminal_states = {
        "COMPLETED",
        "PARTIAL",
        "FAILED",
        "CANCELLED",
        "INVALIDATED",
        "ABORTED",
        "succeeded",
        "failed",
        "cancelled",
        "completed",
        "partial",
    }
    active: list[str] = []
    for directory in candidates:
        try:
            files = tuple(directory.glob("*.json"))
        except OSError:
            continue
        for file in files:
            try:
                if file.stat().st_size > 1_000_000:
                    continue
                payload = json.loads(file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            state = str(payload.get("state") or payload.get("status") or "")
            if state and state not in terminal_states:
                active.append(f"{file.parent.name}:{payload.get('id', file.stem)}:{state}")
    return tuple(sorted(active))


def _prepared_terminals(settings: Settings) -> tuple[str, ...]:
    terminal_dir = settings.runtime_state_dir / "terminal"
    try:
        entries = tuple(terminal_dir.glob("*.json")) + tuple(terminal_dir.glob("*.processing"))
    except OSError:
        return ()
    return tuple(sorted(path.name for path in entries if path.is_file()))


def _discard_pending_terminal_requests(settings: Settings) -> None:
    terminal_dir = settings.runtime_state_dir / "terminal"
    try:
        entries = tuple(terminal_dir.glob("*.json"))
    except OSError:
        return
    for path in entries:
        try:
            path.unlink()
        except OSError:
            continue


def _ares_mounts_from_mountinfo() -> tuple[str, ...]:
    try:
        lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError:
        return ()
    mounts: list[str] = []
    for line in lines:
        fields = line.split()
        if len(fields) >= 5 and fields[4].startswith("/run/ares/mounts/"):
            mounts.append(fields[4])
    return tuple(sorted(set(mounts)))
