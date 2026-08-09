# Modelo de seguridad

> Estado: `storage.disk-analysis@1.1.0` implementa la primera frontera de observación read-only. Identidad humana autoritativa, grants del broker, consentimiento TTY y ledger HMAC siguen siendo arquitectura objetivo y no se consideran implementados.

## 1. Objetivo

ARES debe asumir que el modelo, el navegador, los nombres de dispositivos y la salida de utilidades pueden ser incorrectos o maliciosos. La seguridad no depende de prompts: depende de contratos tipados, separación de privilegios y policy determinista.

Propiedades vigentes para Storage:

- el LLM no posee referencia al Tool Layer ni al ProcessRunner;
- la API no acepta command, executable, flags, path de dispositivo ni Action ID para `storage.disk-analysis`;
- la Capability declara `risk=low`, `operation=observe` y `mode=read_only`;
- la composición de producción permite únicamente tres invocaciones de proceso pasivas y exactas;
- no hay shell, `sudo`, red ni instalación de paquetes;
- los tests de integración deshabilitan probes de proceso y usan fixtures/mocks;
- no existe operación de escritura sobre dispositivos en este vertical slice.

## 2. Garantía read-only de `storage.disk-analysis`

La garantía se aplica en varias capas:

1. **Contrato HTTP/Capability:** `DiskAnalysisInput` solo permite `scope="all_detected"` y usa `extra="forbid"`.
2. **Capability Manager:** `mode=read_only` solo puede registrarse con `operation=observe`.
3. **Workflow:** las únicas Actions son recolección, construcción/persistencia de snapshot y proyección del Knowledge Graph.
4. **Tool policy:** `ReadOnlyStorageProcessRunner` rechaza cualquier invocación que no coincida exactamente con la allowlist.
5. **Process runner:** `create_subprocess_exec`, stdin cerrado, cwd `/`, entorno mínimo, timeout y output acotado.
6. **Privilegios:** tools que abren dispositivos (`smartctl`, `blkid`) no se ejecutan desde FastAPI; quedan `privileged_broker_required` cuando están instaladas.

Allowlist actual:

```text
lsblk   --json --bytes --output NAME,PATH,TYPE,SIZE,RO,RM,MODEL,VENDOR,TRAN,PKNAME,FSTYPE,FSVER,UUID,LABEL,MOUNTPOINTS,SERIAL,WWN
findmnt --json --bytes --output SOURCE,TARGET,FSTYPE,OPTIONS
df      -B1 --output=source,size,used,avail,pcent,target
```

Un intento de ejecutar `wipefs`, `smartctl`, `blkid`, `parted`, `fdisk`, `mkfs`, `fsck`, `mount`, `umount` o una variación de argumentos por esta ruta produce `read_only_policy_rejected` antes de crear un subprocess.

## 3. Frontera privilegiada

[ADR-0002](adr/0002-privilege-boundary.md) sigue vigente: operaciones que requieran abrir dispositivos o privilegio elevado pertenecen a `ares-tool-broker`, que deberá reconstruir argv desde una invocación canónica y revalidar identidad/policy.

Este slice no ejecuta SMART activo si eso requiere cruzar esa frontera. La degradación es explícita:

- herramienta ausente: `tool_not_installed`;
- herramienta presente pero broker requerido: `privileged_broker_required`;
- permiso/timeout/error en probes permitidos: motivo estructurado y fallback cuando existe evidencia de boot.

La ausencia de SMART nunca se interpreta automáticamente como disco dañado.

## 4. Datos hostiles y privacidad

La salida de utilidades se parsea a modelos conocidos. Se limitan paths, tamaño, strings y caracteres. No se persiste stdout completo en el snapshot ni en el Event Bus.

Serial/WWN pueden contribuir a una identidad estable, pero el snapshot guarda un hash de identidad de hardware, no esos identificadores crudos. Los stores de snapshot/diagnóstico usan directorios `0700`, archivos `0600`, límites de tamaño y escritura atómica.

## 5. Eventos y auditoría

Cada evento v2 incluye `event_id`, `timestamp`, `correlation_id`, `session_id`, `source`, `event_type`, `payload` y severity opcional. Los eventos de Tool contienen actor, motivo, recurso, tool, resultado y decisión.

El journal JSONL actual es reconstruible operacionalmente, pero **no es una garantía tamper-evident**. [ADR-0006](adr/0006-audit-ledger.md) reserva esa propiedad para un `ares-audit-writer` separado con HMAC/checkpoints. No se debe describir el journal de esta fase como ledger de seguridad.

## 6. Red y operación offline

El vertical slice no necesita Internet. Los probes se ejecutan localmente y el snapshot/diagnóstico se persisten localmente. La UI usa same-origin `/api/v1`.

La arquitectura general de ARES mantiene egress denegado por defecto; una futura `network.diagnose` será una Capability independiente con policy propia.

## 7. Amenazas cubiertas en esta iteración

| Amenaza | Control implementado |
|---|---|
| Inyección de comando/flags | input semántico + argv exacto + sin shell |
| LLM ejecuta una tool | no existe referencia Tool/ProcessRunner en Reasoning |
| Escritura accidental por Storage | allowlist solo lectura + ninguna tool mutable registrada |
| `smartctl` ausente | limitación explícita, no diagnóstico falso |
| Tool ausente | disponibilidad estructurada y fallback controlado |
| Timeout/permisos/comando fallido | resultado degradado y tests deterministas |
| Output hostil | parser tipado, límites y saneamiento |
| Traversal al recuperar snapshot/diagnóstico | IDs restringidos y raíces privadas |
| Secretos/identidad hardware | no stdout completo; identidad hardware hasheada |

## 8. Fuera de alcance

No se habilitan todavía reparticionado, formateo, reparación de filesystem, recuperación, backup, clonación, firewall, instalación de paquetes, cambios de Windows ni acciones destructivas.

Antes de incorporar una Capability mutable se mantienen las puertas existentes: target identity estable, preflight, lock, consentimiento independiente, broker, verifier, rollback/backup cuando aplique, auditoría durable y pruebas con imágenes/VM desechables.
