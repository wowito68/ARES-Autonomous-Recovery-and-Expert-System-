# Datos del vertical slice Storage

> Estado: modelos implementados para `storage.disk-analysis@1.1.0`. El modelo relacional objetivo general continúa documentado en [data-model.md](data-model.md).

## 1. Flujo de datos

```text
salida de probes
  -> StorageEvidence
  -> SystemStorageSnapshot
  -> Knowledge Graph
  -> DiagnosticResult
```

El backend no expone stdout crudo como contrato de negocio. Cada frontera normaliza a modelos Pydantic `extra="forbid"`.

## 2. StorageEvidence

La evidencia de bajo nivel puede contener:

- `BlockDeviceProbe`;
- `MountProbe`;
- `UsageProbe`;
- `SmartProbe`;
- `OperatingSystemProbe`;
- `ToolAvailability`;
- warnings/errors de recolección.

Se usa únicamente como entrada normalizada del workflow. El snapshot almacena su SHA-256 canónico en `evidence_sha256` para permitir trazabilidad/reproducción.

## 3. SystemStorageSnapshot

Campos de primer nivel:

```text
id
timestamp
hostname
session_id
evidence_sha256
summary
disks
partitions
filesystems
mounts
operating_systems
smart
warnings
errors
tool_availability
```

Entidades principales:

```text
DiskSnapshot
  id, name, path, size_bytes, model, vendor, transport,
  read_only, removable, hardware_identity, partition_ids, smart_id

PartitionSnapshot
  id, disk_id, name, path, size_bytes, read_only,
  filesystem_id, mount_point_ids

FilesystemSnapshot
  id, device_path, filesystem_type, version, uuid, label

MountPointSnapshot
  id, source, path, filesystem_type, options,
  total_bytes, used_bytes, available_bytes, used_percent

SmartSnapshot
  id, disk_id, status, passed, temperature_celsius, reason

OperatingSystemSnapshot
  id, name, version, os_id, source, mountpoint, disk_id
```

`StorageSummary` contabiliza discos, particiones, filesystems, montajes y capacidad total.

## 4. Identidad

Los IDs de recursos derivados usan hashes deterministas del tipo/identidad. El ID del snapshot es único por ejecución.

Los identificadores de hardware sensibles no se publican como serial/WWN crudo. Si existen, contribuyen a `hardware_identity` mediante SHA-256 truncado. Esto permite correlación local sin conservar innecesariamente el identificador original en el snapshot.

## 5. Knowledge Graph

El store interno conserva `GraphNode`, `GraphEdge` y `GraphSnapshot` versionado por `revision`.

Nuevos kinds para Storage:

```text
disk
partition
filesystem
mount_point
smart_status
operating_system
```

Relaciones:

```text
contains
formatted_as
mounted_at
has_health_status
```

Los `attributes` del grafo siguen siendo un mapa heterogéneo porque el grafo almacena múltiples clases de recursos. Esto es una excepción deliberada al uso de modelos rígidos para entidades de dominio; los datos entran al grafo únicamente después de validarse como snapshots tipados.

## 6. DiagnosticResult

El diagnóstico persistido contiene:

```text
id
created_at
snapshot_id
summary
severity
confidence
findings
evidence
affected_resources
recommendations
suggested_capabilities
warnings
limitations
needs_more_evidence
```

Cada `DiagnosticFinding` identifica resource, type, status, message y evidencia asociada. La salida textual de usuario se renderiza a partir de este modelo; no es una fuente independiente de verdad.

## 7. Tool availability

La disponibilidad es un dato de evidencia separado del estado de salud del disco:

```json
{
  "tool": "smartctl",
  "available": false,
  "reason": "tool_not_installed"
}
```

`tool_not_installed`, `privileged_broker_required`, `timeout`, `permission_denied` y `command_failed` describen capacidad de observación, no salud de almacenamiento.

## 8. Persistencia

En este incremento:

```text
<capability_state_dir>/events.jsonl
<capability_state_dir>/knowledge-graph.json
<capability_state_dir>/storage/snapshots/<snapshot_id>.json
<capability_state_dir>/diagnostics/<diagnostic_id>.json
```

Los stores aplican límite de bytes, directorio `0700`, archivo `0600` y reemplazo atómico. El Event Bus conserva un journal operacional JSONL.

El futuro `ares-audit-writer` y la persistencia relacional consolidada no están implementados por esta Capability.

## 9. Retención y datos no almacenados

No se almacenan intencionalmente:

- stdout/stderr completos de probes en el snapshot;
- secretos;
- credenciales;
- razonamiento interno del LLM;
- comandos construidos por usuario (no existen en el contrato);
- serial/WWN crudos en el snapshot público.

La política de retención/limpieza de múltiples snapshots y diagnósticos queda como deuda técnica para el incremento de persistencia/historial.
