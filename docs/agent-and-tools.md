# Agente, Capabilities y Tool Layer

> Estado: la separación Agent/Reasoning → Capabilities → Workflows → Actions → Tools ya tiene un vertical slice funcional mediante `storage.disk-analysis@1.1.0`. El broker privilegiado, grants autoritativos y consentimiento TTY siguen siendo arquitectura objetivo.

## 1. Regla principal

El LLM es una fuente no confiable de propuestas. No puede ejecutar procesos, seleccionar argv, abrir dispositivos, aprobar mutaciones ni declarar éxito por sí mismo.

ARES expone al razonamiento **Capabilities**, no Tools ni Actions. Una Capability recibe entrada semántica validada y construye un workflow privado propiedad del servidor.

```text
LLM / Agent
   ↓ objetivo + evidencia
Reasoning Engine
   ↓ capability_id
Capability Manager
   ↓ input Pydantic validado
Capability
   ↓ WorkflowDefinition server-owned
Workflow Engine
   ↓ Actions privadas
Tool Layer
   ↓ procesos fijos / broker cuando corresponda
Sistema operativo
```

## 2. Contrato actual de Capability

El SDK exige metadata inmutable, modelo Pydantic de entrada y modelo Pydantic de salida. Para `storage.disk-analysis`:

```text
id: storage.disk-analysis
version: 1.1.0
category: storage
operation: observe
mode: read_only
risk: low
input: DiskAnalysisInput(scope="all_detected")
output: StorageCapabilityResult
rollback: unsupported / no hay efecto sobre discos
```

El cliente no puede proporcionar dispositivo, ruta, executable, tool, action, flags ni command. `extra="forbid"` hace que esos campos fallen validación HTTP/SDK.

El Capability Manager verifica:

- compatibilidad de plataforma;
- permisos declarados por plugin;
- dependencias;
- coherencia `read_only -> observe`;
- modelo de entrada/salida;
- que las Actions reales del workflow coincidan con `internal_actions`;
- output tipado tras una ejecución exitosa.

## 3. Workflow de Disk Analysis

```text
storage.collect-evidence
storage.build-snapshot
storage.persist-snapshot
knowledge.project-storage-snapshot
```

Los postchecks verifican que la evidencia sea tipada/no vacía, que exista fingerprint SHA-256 del snapshot y que el Knowledge Graph avance de revisión.

El diagnóstico se calcula después de la Capability, en Reasoning Engine, para no mezclar observación con inferencia.

## 4. Tool Layer implementado

Los adaptadores de bajo nivel son independientes de la Capability. Sus resultados son modelos tipados (`BlockDeviceProbe`, `MountProbe`, `UsageProbe`, `SmartProbe`, etc.).

La ruta de proceso de producción usa `SafeProcessRunner` envuelto por `ReadOnlyStorageProcessRunner`. La segunda capa aplica una allowlist exacta antes de crear cualquier subprocess:

- `lsblk` JSON/bytes con columnas enumeradas;
- `findmnt` JSON/bytes con columnas enumeradas;
- `df -B1` con columnas enumeradas.

No se usa shell ni interpolación. Los ejecutables se resuelven desde PATH mínimo, deben ser archivos regulares root-owned y no escribibles por otros. Hay timeout y límites de stdout/stderr.

Cualquier argv distinto, tool no allowlisted o intento de `wipefs`, `smartctl`, `blkid`, etc. por esta ruta produce `read_only_policy_rejected` antes del subprocess.

## 5. SMART y blkid

ARES incluye parsers tipados para salida de `smartctl --json` y `blkid -o export` porque forman parte del contrato futuro, pero este vertical slice **no abre dispositivos mediante esos binarios desde FastAPI**.

Se distinguen dos degradaciones:

```text
smartctl ausente      -> unavailable / tool_not_installed
smartctl instalado    -> unavailable / privileged_broker_required
```

Ninguna de las dos equivale a un fallo SMART. Cuando el broker privilegiado exista se añadirá una Action/Tool dedicada que preserve el mismo modelo estructurado y las reglas de identidad de dispositivo.

## 6. Reasoning Engine

El Reasoning Engine actual puede:

1. recibir un objetivo/observación;
2. identificar la Capability compatible;
3. pedir `hardware.block-devices` si no hay evidencia mínima;
4. seleccionar `storage.disk-analysis` cuando la evidencia existe;
5. interpretar el snapshot resultante;
6. producir findings, confidence, warnings, limitations y necesidad de más evidencia.

El Reasoning Engine no tiene referencia al `ProcessRunner`. El servicio de aplicación coordina la ejecución a través de Capability Manager.

## 7. Datos hostiles

Toda salida del sistema operativo se trata como no confiable. Los parsers:

- limitan tamaño;
- rechazan estructuras JSON inesperadas;
- filtran paths de dispositivo;
- limitan strings y caracteres no imprimibles;
- no conservan serial/WWN crudos en snapshots públicos;
- normalizan a modelos Pydantic con `extra="forbid"`.

Un nombre o label malicioso sigue siendo un dato; nunca se evalúa como instrucción del agente.

## 8. Auditoría de Tool execution

Cada intento de Tool genera eventos estructurados. Los eventos de ejecución incluyen, como mínimo en el payload operacional:

```text
actor
reason
resource
tool
result
decision=observe_only
```

La fecha, correlación y sesión viven en el envelope del Event Bus. No se persisten stdout completos, secretos ni argumentos libres del usuario.

## 9. Contrato objetivo para Tools mutables

La arquitectura futura sigue separando preparación, broker y verificación. Una mutación deberá cumplir policy, identidad estable, lock, consentimiento independiente y verifier. Ninguna reparación se añadirá como opción booleana de una Tool de observación.

Familias previstas continúan separadas:

| Capability/Tool futura | Efecto inicial |
|---|---|
| SMART privilegiado ampliado | OBSERVE |
| `filesystem.check` | OBSERVE |
| backup administrado | CHANGE |
| recuperación de archivos | CHANGE |
| reparación de filesystem | CHANGE/HIGH |
| reparación de boot | CRITICAL |

`parted`, `fdisk`, `mkfs`, `fsck` con flags de reparación, `wipefs`, `mount`, `umount` y herramientas equivalentes permanecen fuera de `storage.disk-analysis`.
