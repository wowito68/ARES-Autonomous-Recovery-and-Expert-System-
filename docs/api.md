# Contrato HTTP

## Convenciones

- Prefijo: `/api/v1`.
- JSON UTF-8; timestamps RFC 3339 UTC.
- `application/problem+json` para errores HTTP.
- `X-Request-ID` validado o generado por middleware.
- No existen endpoints públicos de Tools ni Actions.
- Las operaciones se solicitan mediante Capabilities o servicios de aplicación con entradas semánticas.
- La UI de producción usa same-origin y no requiere recursos remotos.

## Endpoints implementados

| Endpoint | Semántica |
|---|---|
| `GET /api/v1/health/live` | Liveness del proceso/event loop |
| `GET /api/v1/health/ready` | Readiness con consulta mínima a SQLite |
| `GET /api/v1/system/overview` | Estado público acotado generado en boot |
| `GET /api/v1/ai/status` | Estado de runtime/modelo local |
| `POST /api/v1/assistant/chat` | Chat local sin Tools/privilegios |
| `GET /api/v1/capabilities` | Catálogo sellado y buscable |
| `GET /api/v1/capabilities/{id}` | Metadata + input/output schema públicos |
| `GET /api/v1/capabilities/{id}/versions` | Versiones instaladas |
| `POST /api/v1/capabilities/{id}/executions` | Ejecuta workflow server-owned |
| `GET /api/v1/capabilities/executions/{id}` | Registro público de ejecución del boot |
| `POST /api/v1/reasoning/assess` | Hipótesis, solicitud de evidencia o selección de Capability |
| `POST /api/v1/planner/plan` | Plan inmutable de Capabilities sin ejecutar |
| `GET /api/v1/knowledge/graph` | Snapshot versionado del Knowledge Graph |
| `GET /api/v1/storage/disks` | Discos del último snapshot Storage persistido |
| `POST /api/v1/storage/analyze` | Vertical slice completo `storage.disk-analysis` + diagnóstico |

Diagnósticos read-only adicionales instalados en el catálogo:

- `storage.space-analysis`
- `system.memory-analysis`
- `packages.health-check`
- `services.failure-analysis`
- `boot.diagnose`

Los contratos, alcances y límites se documentan en
[`capabilities/read-only-diagnostic-analysis.md`](capabilities/read-only-diagnostic-analysis.md).
| `GET /api/v1/storage/snapshots/{id}` | Recupera un `SystemStorageSnapshot` |
| `GET /api/v1/diagnostics/{id}` | Recupera un `DiagnosticResult` |

## Capability `storage.disk-analysis`

El endpoint genérico admite exclusivamente la entrada semántica:

```http
POST /api/v1/capabilities/storage.disk-analysis/executions
Content-Type: application/json
```

```json
{
  "scope": "all_detected"
}
```

Campos como `command`, `tool`, `action`, `device`, flags o rutas adicionales son rechazados por validación (`extra="forbid"`).

La respuesta de una ejecución exitosa contiene un `result` conforme a `StorageCapabilityResult`:

```json
{
  "snapshot": {
    "id": "...",
    "summary": {
      "disk_count": 2,
      "partition_count": 3,
      "filesystem_count": 2,
      "mounted_filesystem_count": 2,
      "total_capacity_bytes": 1000000000000
    },
    "disks": [],
    "partitions": [],
    "filesystems": [],
    "mounts": [],
    "operating_systems": [],
    "smart": [],
    "warnings": [],
    "errors": [],
    "tool_availability": []
  },
  "knowledge_graph": {
    "revision": 1,
    "node_count": 8,
    "edge_count": 7
  }
}
```

Los valores anteriores son ilustrativos; el backend no inventa recursos ausentes.

## Endpoint de aplicación Storage

### `POST /api/v1/storage/analyze`

No recibe dispositivo ni comando. Coordina el flujo:

```text
Reasoning selection
-> Capability Manager
-> storage.disk-analysis
-> snapshot / Knowledge Graph
-> Reasoning diagnosis
-> DiagnosticStore
```

Respuesta:

```json
{
  "execution_id": "...",
  "snapshot_id": "...",
  "diagnostic_id": "...",
  "diagnostic": {
    "summary": "Se analizaron ...",
    "severity": "warning",
    "confidence": 0.9,
    "findings": [],
    "evidence": [],
    "affected_resources": [],
    "recommendations": [],
    "suggested_capabilities": [],
    "warnings": [],
    "limitations": [],
    "needs_more_evidence": false
  },
  "message": "Texto renderizado exclusivamente desde DiagnosticResult"
}
```

Si no existe evidencia mínima para ejecutar la Capability, responde 503 Problem Details con código estable `STORAGE_EVIDENCE_UNAVAILABLE`.

### `GET /api/v1/storage/disks`

Devuelve los `DiskSnapshot` del último snapshot persistido. Antes del primer análisis válido puede devolver `count=0`.

### `GET /api/v1/storage/snapshots/{id}`

Devuelve el snapshot inmutable persistido o 404 `STORAGE_SNAPSHOT_NOT_FOUND`.

### `GET /api/v1/diagnostics/{id}`

Devuelve el diagnóstico estructurado o 404 `DIAGNOSTIC_NOT_FOUND`.

## Problem Details

Formato base:

```json
{
  "type": "urn:ares:error:storage-evidence-unavailable",
  "title": "Storage analysis unavailable",
  "status": 503,
  "code": "STORAGE_EVIDENCE_UNAVAILABLE",
  "detail": "ARES could not produce a verified storage analysis from current evidence.",
  "instance": "/api/v1/storage/analyze",
  "request_id": "..."
}
```

Un workflow de Capability que inicia correctamente pero falla durante una Action continúa representándose como un recurso de ejecución HTTP válido con `status="failed"` y `error_code`. El endpoint de aplicación `/storage/analyze` traduce la imposibilidad de producir diagnóstico a Problem Details.

Nunca se retornan stack traces, secretos, stdout crudo, executable paths internos ni Action IDs como autoridad de ejecución.

## CLI equivalente

La CLI no implementa un flujo alternativo; usa el mismo `StorageAnalysisService`:

```bash
ares storage analyze
ares storage snapshot
ares storage snapshot <snapshot_id>
```

## Recursos futuros

Autenticación, casos, grants, SSE durable, approvals, reportes y audit ledger continuarán incorporándose conforme existan sus políticas y procesos autoritativos. Ningún endpoint futuro de mutación podrá fabricar una aprobación: esa autoridad permanece en broker/consent agent según los ADR vigentes.
