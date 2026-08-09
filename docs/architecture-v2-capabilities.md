# ARES v2 — Arquitectura basada en Capabilities

> Estado: plataforma v2 implementada y validada con `storage.disk-analysis@1.1.0` como primer vertical slice funcional.

## Principio

ARES no expone Tools ni Actions al LLM, planner, frontend o API pública. La unidad pública/razonable es una **Capability** con contrato semántico tipado.

```text
Reasoning / Planner
  -> Capability ID
Capability Manager
  -> input Pydantic
Capability
  -> WorkflowDefinition server-owned
Workflow Engine
  -> Actions privadas
Tool Layer
  -> SO / broker
```

## SDK

Una Capability declara `CapabilityMetadata`, `input_model`, `output_model` y `build_workflow()`.

Metadata incluye:

- identidad y versión semántica;
- categoría y objetivo;
- plataforma;
- `risk`, `operation`, `mode`;
- permisos y dependencias;
- Actions internas;
- postchecks;
- rollback;
- evidencia requerida;
- eventos, métricas y política de auditoría.

`CapabilityDescriptor` publica JSON Schema de input/output generado desde Pydantic, además de plugin/version/estado activo. Los schemas son documentación/proyección; la validación autoritativa sigue siendo el modelo Pydantic.

## Registry

`CapabilityManager` carga plugins confiables durante startup, valida:

- Core API version;
- compatibilidad OS/arch/live mode;
- permisos declarados;
- IDs/versiones duplicados;
- dependencias y ciclos;
- input/output models;
- coherencia `read_only -> observe`.

Después `seal()` congela la resolución. La versión activa es la semánticamente más reciente instalada.

## Ejecución

`execute()` valida input, solicita el workflow a la Capability, comprueba que sus Actions coincidan con metadata, ejecuta Workflow Engine y valida el resultado contra `output_model` antes de devolverlo.

No acepta un Action ID ni Tool ID desde el cliente.

## `storage.disk-analysis@1.1.0`

Input:

```json
{"scope":"all_detected"}
```

Output:

```text
StorageCapabilityResult
  snapshot: SystemStorageSnapshot
  knowledge_graph: GraphUpdateSummary
```

Workflow actual:

```text
collect-evidence
  -> build-snapshot
  -> persist-snapshot
  -> project-knowledge-graph
```

El anterior prototipo basado únicamente en inventario de boot queda reemplazado como implementación principal. El inventario de boot permanece como **fallback** cuando `lsblk` no está disponible/falla y contiene evidencia válida.

## Tool Layer

El Tool Layer es una abstracción distinta de Capability. `StorageToolSuite` devuelve `StorageEvidence` tipado. En la composición oficial está protegido por `ReadOnlyStorageProcessRunner`.

Probes ejecutables actuales: `lsblk`, `findmnt`, `df` con argv exacto. `smartctl`/`blkid` no abren dispositivos desde FastAPI; véase [ADR-0008](adr/0008-read-only-storage-probes.md).

## Knowledge Graph

La proyección interna modela System, Disk, Partition, Filesystem, MountPoint, OperatingSystem y SMARTStatus. El grafo avanza revisión mediante escritura atómica.

No se introduce un motor externo de grafos en este incremento.

## Reasoning y Planner

Reasoning selecciona Capabilities por objetivo/evidencia y puede pedir evidencia faltante. Planner produce una secuencia de Capability IDs/versiones/dependencias; no produce Tools, Actions ni comandos.

Después del vertical slice, Reasoning también interpreta `SystemStorageSnapshot` para producir `DiagnosticResult`. Esa función no ejecuta Tools.

## Eventos

Workflow, Capability, Actions, Tool Layer, Storage, Knowledge Graph y diagnóstico emiten eventos en un Event Bus durable local. El envelope v2 añade `event_id`, `timestamp`, `session_id` y mantiene aliases del journal anterior durante la migración.

## Seguridad

`storage.disk-analysis` es `low / observe / read_only`. La entrada no permite target, command ni argv. La allowlist de proceso rechaza Tools o flags no previstos antes del subprocess.

No se habilitan reparticionado, formateo, mount/unmount, reparación, recovery, backup ni acciones destructivas.

## Extensión futura

Las próximas Capabilities deben reutilizar estos contratos y no crear caminos paralelos. En particular, SMART privilegiado deberá integrar `ares-tool-broker` y proyectar sus resultados a `SmartProbe`/snapshot existentes.

Detalles operativos del primer slice: [storage-disk-analysis.md](capabilities/storage-disk-analysis.md).
