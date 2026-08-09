# ADR-0008 — Probes de almacenamiento read-only y frontera de dispositivos

- Estado: Accepted
- Fecha: 2026-08-08

## Contexto

ARES necesita demostrar un vertical slice real de diagnóstico de almacenamiento antes de habilitar mutaciones. La arquitectura existente ya establece en ADR-0002 que las operaciones privilegiadas deben atravesar `ares-tool-broker` y que FastAPI no debe convertirse en un ejecutor root.

Sin embargo, parte de la evidencia de almacenamiento puede obtenerse mediante utilidades pasivas sin privilegios (`lsblk`, `findmnt`, `df`), mientras que otras utilidades (`smartctl`, ciertos usos de `blkid`) pueden abrir dispositivos y requerir permisos adicionales según plataforma/configuración.

Había que decidir si el primer vertical slice debía:

1. esperar a que el broker estuviera terminado;
2. ejecutar todas las utilidades directamente desde FastAPI;
3. permitir únicamente probes pasivos con una frontera explícita y degradar la evidencia privilegiada.

## Decisión

Se adopta la opción 3.

`storage.disk-analysis` ejecuta desde el proceso no privilegiado únicamente una allowlist exacta de probes pasivos:

```text
lsblk   --json --bytes --output <columnas enumeradas>
findmnt --json --bytes --output SOURCE,TARGET,FSTYPE,OPTIONS
df      -B1 --output=source,size,used,avail,pcent,target
```

La composición usa dos capas:

- `SafeProcessRunner`: ejecuta sin shell, con entorno/cwd/stdin/timeout/output acotados y verifica ownership/permisos del binario;
- `ReadOnlyStorageProcessRunner`: rechaza cualquier Tool o argv que no coincida exactamente con la allowlist antes del subprocess.

`smartctl` y `blkid` pueden inspeccionarse para reportar disponibilidad, y existen parsers tipados para su formato, pero no se ejecutan sobre dispositivos desde FastAPI en esta fase.

Estados de degradación:

```text
tool ausente     -> tool_not_installed
tool disponible  -> privileged_broker_required
```

La ausencia de SMART no se convierte en un fallo de disco.

## Razones

- Mantiene intacta ADR-0002.
- Permite validar Capability Manager, Workflow, Tool Layer, Snapshot, KG, Reasoning, API, CLI, UI y Event Bus sin introducir escritura ni privilegio.
- Reduce la superficie de comando: no hay shell ni argumentos controlados por usuario/LLM.
- Permite pruebas deterministas con runners falsos y sin hardware real.
- Prepara los modelos de SMART/blkid para una futura integración con broker sin cambiar el contrato de dominio.

## Consecuencias positivas

- El primer vertical slice funciona offline y en modo de solo lectura.
- `wipefs`, `parted`, `fdisk`, `mkfs`, `fsck`, `mount`, `umount` y cualquier argv alternativo quedan fuera de la ruta de ejecución.
- Un sistema sin `smartctl` sigue produciendo snapshot/diagnóstico con limitación explícita.
- La futura Tool privilegiada puede reutilizar `SmartProbe`, `BlkidProbe` y `SystemStorageSnapshot`.

## Consecuencias negativas / deuda

- La cobertura SMART puede ser `unavailable` aunque `smartctl` esté instalado.
- La identidad de hardware todavía no recibe toda la revalidación udev/by-id/major:minor necesaria antes de una mutación.
- Existen dos niveles de ejecución read-only: probes no privilegiados directos y futuras observaciones privilegiadas por broker. La documentación debe mantener la distinción para no interpretar “read-only” como “sin privilegios”.

## Alternativas rechazadas

### Esperar al broker

Habría retrasado la validación de toda la arquitectura v2 y acoplado el primer caso útil a una frontera de privilegio aún no necesaria para `lsblk/findmnt/df`.

### Ejecutar smartctl/blkid directamente con privilegios desde FastAPI

Rechazado porque viola ADR-0002, aumenta impacto de compromiso de API y abre un precedente incorrecto para futuras Capabilities.

### Usar un comando genérico read-only

Rechazado. No existe una forma segura de aceptar executable/argv libres y demostrar que no hay efectos laterales. ARES registra operaciones semánticas y allowlists exactas.

## Verificación

La decisión se prueba con casos que intentan ejecutar:

- `smartctl --all /dev/sda`;
- `wipefs --all /dev/sda`;
- `lsblk` con argv alternativo.

Todos deben fallar `read_only_policy_rejected` antes de alcanzar el ProcessRunner delegado.

## Próxima revisión

Revisar este ADR cuando `ares-tool-broker` implemente la primera observación privilegiada. La expectativa es conservar la decisión: probes pasivos no privilegiados pueden permanecer locales; device-opening privilegiado se mueve al broker.
