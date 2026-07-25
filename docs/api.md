# Contrato HTTP

## Convenciones

- Prefijo: `/api/v1`.
- JSON UTF-8; fechas RFC 3339 UTC; IDs UUID.
- Recursos directos en éxito y `application/problem+json` en error.
- Paginación por cursor `(created_at, id)`.
- `Idempotency-Key` obligatorio en mutaciones críticas.
- `X-Request-ID` validado o generado por el servidor.
- REST para recursos y SSE para progreso. `Last-Event-ID` permite reanudar.
- La especificación OpenAPI será fuente para el cliente TypeScript y pruebas de contrato.

## Recursos previstos

```text
POST /auth/bootstrap
POST /auth/login
POST /auth/logout
POST /auth/reauthenticate
GET  /auth/me

GET/POST       /users
GET/PATCH      /users/{id}
GET             /machines/current
GET             /machines/{id}/snapshots

GET             /capabilities
GET             /capabilities/{capability_id}
POST            /capabilities/{capability_id}/executions
GET             /capabilities/executions/{execution_id}
POST            /reasoning/assess
GET             /knowledge/graph

GET/POST        /cases
GET/PATCH       /cases/{id}
PUT             /cases/{id}/mode
GET/POST        /cases/{id}/capability-grants
POST            /capability-grants/{id}/revoke
GET/POST        /cases/{id}/messages
GET/POST        /cases/{id}/agent-runs
GET             /agent-runs/{id}

GET             /cases/{id}/events

GET/POST        /cases/{id}/reports
GET             /reports/{id}
GET             /reports/{id}/export
GET             /audit-events

GET /health/live
GET /health/ready
```

No existirán endpoints públicos de Tools ni Actions. Una operación se solicita
por identificador de Capability y entrada semántica. Las futuras Capabilities
mutables quedarán `AWAITING_APPROVAL` hasta que el broker reciba prueba del
consent agent en un TTY confiable; ningún endpoint web podrá fabricar esa
decisión ni sustituir argumentos internos.

`POST /cases/{id}/capability-grants` solicitará al broker un desafío en el TTY
confiable; no crea directamente el grant. Allí se eligen
`capability@version`, efecto máximo, identidad, caso/sesión, `max_uses` y
expiración. El broker emite, revoca y consume la autoridad; no existe un grant
implícito.

## Problem Details

```json
{
  "type": "urn:ares:error:mode-violation",
  "title": "Operation not permitted in current mode",
  "status": 403,
  "code": "MODE_VIOLATION",
  "detail": "This capability requires REPAIR mode.",
  "instance": "/api/v1/capabilities/recovery.boot-repair/executions",
  "request_id": "01..."
}
```

Códigos estables iniciales: `AUTH_INVALID_CREDENTIALS`, `FORBIDDEN`,
`MODE_VIOLATION`, `CAPABILITY_NOT_FOUND`, `CAPABILITY_EXECUTION_NOT_FOUND`,
`APPROVAL_REQUIRED`, `TARGET_CHANGED`, `RESOURCE_BUSY`, `ACTION_TIMEOUT`,
`POSTCHECK_FAILED`, `DEPENDENCY_UNAVAILABLE` e `INTERNAL_ERROR`.

Una Capability fallida produce una ejecución con estado `failed`; consultar
ese recurso es una respuesta HTTP exitosa. Nunca se retornan Actions, comandos,
stack traces, secretos ni stdout.

## Componente implementado ahora

| Endpoint | Semántica |
|---|---|
| `GET /api/v1/health/live` | El proceso y event loop responden; no consulta dependencias |
| `GET /api/v1/health/ready` | Comprueba una consulta mínima a SQLite; responde 503 si no está disponible |
| `GET /api/v1/system/overview` | Lee el estado público y acotado generado durante el boot |
| `GET /api/v1/ai/status` | Distingue runtime ausente, modelo ausente y modelo preparado |
| `POST /api/v1/assistant/chat` | Conversación local acotada; sin tools, ejecución ni privilegios |
| `GET /api/v1/capabilities` | Busca metadata pública en el registro sellado |
| `GET /api/v1/capabilities/{id}` | Describe una Capability sin revelar Actions privadas |
| `POST /api/v1/capabilities/{id}/executions` | Ejecuta un workflow server-owned con input semántico |
| `GET /api/v1/capabilities/executions/{id}` | Lee el registro público de una ejecución del arranque |
| `POST /api/v1/reasoning/assess` | Genera hipótesis, pide evidencia o selecciona una Capability |
| `GET /api/v1/knowledge/graph` | Devuelve el snapshot versionado del equipo |

Salud incluye estado y versión. El endpoint de chat acepta como máximo 12
mensajes, conserva el prompt de sistema bajo control del servidor y traduce
fallos del runtime a códigos estables `AI_RUNTIME_UNAVAILABLE`,
`AI_MODEL_MISSING` y `AI_INVALID_RESPONSE`. El contrato completo se habilitará
por fases conforme existan sus políticas, persistencia y pruebas.

### Ejecución implementada

`storage.disk-analysis` acepta exclusivamente:

```json
{"scope": "all_detected"}
```

No admite rutas de dispositivo, comandos ni argumentos. La ejecución devuelve resumen,
hallazgos, evidencia, métricas por paso y revisión del Knowledge Graph. Los nombres de
Actions no forman parte de la respuesta HTTP. Un fallo de la capability queda
registrado con estado `failed` y un `error_code` estable dentro de una respuesta HTTP
exitosa.

El diseño y la secuencia se describen en
[ARES v2: arquitectura basada en Capabilities](architecture-v2-capabilities.md).
