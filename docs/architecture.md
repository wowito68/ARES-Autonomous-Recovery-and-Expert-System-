# Arquitectura de ARES

> Estado: arquitectura objetivo con un primer vertical slice funcional implementado. El backend ya dispone de la plataforma v2 de Capabilities, Workflow Engine, Event Bus, Knowledge Graph, Reasoning determinista y la Capability `storage.disk-analysis@1.1.0`. Las fronteras privilegiadas, consentimiento independiente y ledger criptográfico siguen siendo arquitectura objetivo y no deben considerarse implementadas.

## 1. Principios vigentes

ARES se ejecuta localmente, puede operar sin Internet y mantiene un monolito modular con adaptadores reemplazables. La arquitectura evita que el LLM, el frontend o un endpoint HTTP puedan seleccionar procesos del sistema operativo.

Las reglas autoritativas son:

1. El LLM razona sobre contratos de ARES; no ejecuta Tools ni Actions.
2. Los endpoints traducen HTTP a servicios de aplicación; no contienen lógica de diagnóstico.
3. Capability Manager valida metadata, compatibilidad, permisos y modelos Pydantic de entrada/salida.
4. Las Capabilities construyen workflows server-owned a partir de entradas semánticas.
5. Las Actions implementan pasos privados del workflow.
6. El Tool Layer es la única frontera que puede observar el sistema operativo.
7. Una operación privilegiada sobre dispositivos debe atravesar `ares-tool-broker`; el vertical slice de Storage no debilita esa decisión.
8. Toda mutación de almacenamiento permanece fuera del alcance de `storage.disk-analysis`.

Consulta los ADR existentes y [ADR-0008](adr/0008-read-only-storage-probes.md), que formaliza el límite entre probes pasivos sin privilegios y herramientas que abren dispositivos.

## 2. Vertical slice implementado

El primer flujo funcional es:

```mermaid
flowchart TD
    U[Usuario] --> API[ARES API / CLI / Storage UI]
    API --> SVC[StorageAnalysisService]
    SVC --> RE1[Reasoning Engine: selección]
    RE1 --> CM[Capability Manager]
    CM --> CAP[storage.disk-analysis@1.1.0]
    CAP --> WF[Workflow Engine]
    WF --> A1[collect-evidence]
    A1 --> TL[Tool Layer read-only]
    TL --> OS[lsblk / findmnt / df]
    TL -. broker requerido .-> PRIV[blkid / smartctl sobre dispositivo]
    A1 --> A2[build-snapshot]
    A2 --> A3[persist-snapshot]
    A3 --> A4[project-knowledge-graph]
    A4 --> KG[Knowledge Graph]
    A4 --> RE2[Reasoning Engine: diagnóstico]
    RE2 --> DR[DiagnosticResult]
    DR --> U
```

`POST /api/v1/storage/analyze` y `ares storage analyze` llaman al mismo `StorageAnalysisService`. La UI no reconstruye hechos ni inventa estados; solo proyecta snapshot y diagnóstico recibidos del backend.

## 3. Capability SDK

Una Capability registrada declara de forma tipada:

- `id`, `version`, nombre, descripción, objetivo y categoría;
- plataformas compatibles;
- `risk`, `operation` y `mode`;
- permisos y dependencias;
- modelo Pydantic de inputs y outputs;
- Actions internas y workflow;
- postchecks/verificación;
- política de rollback;
- eventos, métricas y política de auditoría.

El modo `read_only` exige `operation=observe`. El Capability Manager rechaza registros incompatibles y valida el `output_model` después de una ejecución exitosa. Los esquemas JSON publicados en el catálogo son una proyección de los modelos Pydantic; no sustituyen los modelos del dominio.

## 4. Storage workflow

`storage.disk-analysis` utiliza cuatro pasos secuenciales:

```text
collect-evidence
  -> build-snapshot
  -> persist-snapshot
  -> project-knowledge-graph
```

La recolección puede degradarse por herramienta. La ausencia de `smartctl`, falta de privilegio o requisito de broker se representa como disponibilidad estructurada; no implica automáticamente fallo del disco.

El snapshot se persiste antes de proyectarse al Knowledge Graph. Esto permite reconstruir qué evidencia normalizada produjo una revisión del grafo y un diagnóstico posterior.

## 5. Tool Layer y frontera de privilegios

La composición oficial envuelve el runner de procesos con una allowlist exacta. En este incremento solo son ejecutables por esa ruta:

```text
lsblk   --json --bytes --output <columnas fijas>
findmnt --json --bytes --output SOURCE,TARGET,FSTYPE,OPTIONS
df      -B1 --output=source,size,used,avail,pcent,target
```

No hay shell, interpolación de strings ni argv procedente del cliente. El proceso recibe stdin cerrado, cwd fijo, entorno mínimo, timeout y límites de salida. El ejecutable debe ser un archivo regular propiedad de root y no escribible por grupo/otros.

`blkid` y `smartctl` tienen parsers tipados, pero su ejecución sobre dispositivos no se realiza desde FastAPI en esta fase. Si están instalados se reportan como `privileged_broker_required`; si no existen se reportan como `tool_not_installed`. Esta decisión mantiene [ADR-0002](adr/0002-privilege-boundary.md).

## 6. Snapshot y Knowledge Graph

`SystemStorageSnapshot` contiene ID, timestamp UTC, hostname, session ID, `evidence_sha256`, resumen, discos, particiones, filesystems, montajes, sistemas operativos, SMART, warnings, errors y disponibilidad de herramientas.

Serial/WWN no se persisten como texto crudo en el snapshot. Cuando aportan identidad se incorporan a un hash de identidad de hardware.

El Knowledge Graph interno añade estos tipos y relaciones:

```text
System --contains--> Disk
Disk --contains--> Partition
Partition --formatted_as--> Filesystem
Partition --mounted_at--> MountPoint
Disk --contains--> OperatingSystem
Disk --has_health_status--> SMARTStatus
```

El grafo sigue siendo un store interno atómico y versionado; no se introduce todavía una base de grafos externa.

## 7. Reasoning y diagnóstico

El Reasoning Engine conserva dos responsabilidades separadas:

1. seleccionar una Capability a partir de objetivo y evidencia disponible;
2. interpretar un snapshot ya obtenido para producir `DiagnosticResult`.

No recibe un ejecutable ni llama directamente al Tool Layer. Para Storage aplica reglas deterministas iniciales: uso elevado de filesystem, SMART fallido cuando hay evidencia, filesystem no identificado, evidencia ausente y disco no observado.

La ausencia de SMART se convierte en `limitations` y reduce confianza; nunca se transforma por sí sola en un finding de disco fallido.

## 8. Eventos y auditoría operacional

El Event Bus serializa el sobre canónico:

```text
event_id, timestamp, correlation_id, session_id,
source, event_type, payload, severity?
```

Durante la migración v2 también conserva aliases serializados `id`, `name` y `occurred_at` para lectores del journal anterior.

El vertical slice genera, entre otros:

- `capability.started` / `capability.completed`;
- `tool.execution.started` / `tool.execution.completed`;
- `storage.disk.detected`;
- `storage.partition.detected`;
- `storage.smart.analyzed`;
- `storage.snapshot.created`;
- `knowledge.graph.updated`;
- `diagnostic.generated`.

Los eventos de Tool contienen actor, motivo, recurso, tool, resultado y decisión de observación. El journal JSONL permite reconstrucción operacional, pero **no se presenta como ledger tamper-evident**. Ese atributo sigue reservado al futuro `ares-audit-writer` de [ADR-0006](adr/0006-audit-ledger.md).

## 9. Persistencia local del slice

Los snapshots, diagnósticos, Event Bus y Knowledge Graph se guardan bajo el directorio privado de estado de Capabilities. Los stores crean directorios `0700`, documentos `0600`, límites de tamaño y reemplazos atómicos.

El slice no requiere red ni servicio externo. SQLite continúa siendo la base transaccional general de ARES, pero estos artefactos v2 se mantienen como documentos locales tipados hasta que una migración posterior consolide retención e índices.

## 10. Fronteras no implementadas todavía

No están implementados por este incremento:

- reparticionado, formateo o reparación de filesystem;
- recuperación de archivos, clonación o backup;
- montaje/desmontaje gestionado;
- instalación de paquetes, firewall o cambios de Windows;
- ejecución privilegiada de SMART mediante broker;
- consentimiento independiente para mutaciones;
- ledger HMAC/tamper-evident definitivo;
- autonomía completa del agente.

Estas capacidades deben incorporarse por vertical slices independientes y no como flags de `storage.disk-analysis`.
