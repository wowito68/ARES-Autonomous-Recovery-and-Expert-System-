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
GET             /system/capabilities
GET             /machines/current
GET             /machines/{id}/snapshots

GET/POST        /cases
GET/PATCH       /cases/{id}
PUT             /cases/{id}/mode
GET/POST        /cases/{id}/capability-grants
POST            /capability-grants/{id}/revoke
GET/POST        /cases/{id}/messages
GET/POST        /cases/{id}/agent-runs
GET             /agent-runs/{id}

GET             /tools
GET             /tools/{name}
POST            /cases/{id}/actions
GET             /actions/{id}
POST            /actions/{id}/approval-challenges
POST            /actions/{id}/reject
POST            /actions/{id}/cancel

GET             /executions/{id}
GET             /executions/{id}/events
GET             /cases/{id}/events

GET/POST        /cases/{id}/reports
GET             /reports/{id}
GET             /reports/{id}/export
GET             /audit-events

GET /health/live
GET /health/ready
```

`POST /cases/{id}/actions` devuelve `202 Accepted`. Una lectura con grant puede pasar a `QUEUED`; una mutación queda `AWAITING_APPROVAL`. `POST /actions/{id}/approval-challenges` solo pide al broker preparar el TTY seguro; no aprueba. La decisión llega de `ares-consent-agent` al broker y pone en cola la acción ya preparada. Ningún endpoint web puede fabricar la decisión ni sustituir argumentos.

`POST /cases/{id}/capability-grants` solicita al broker un desafío en el TTY confiable; no crea directamente el grant. Allí se eligen `tool@version`, efecto máximo `OBSERVE`, identidad, caso/sesión, `max_uses` y expiración. El broker emite/revoca/consume la autoridad y SQLite recibe una proyección. CSRF protege las solicitudes web, pero solo el proof del consent agent cambia el grant; no existe uno implícito.

## Problem Details

```json
{
  "type": "urn:ares:error:mode-violation",
  "title": "Operation not permitted in current mode",
  "status": 403,
  "code": "MODE_VIOLATION",
  "detail": "This tool requires REPAIR mode.",
  "instance": "/api/v1/actions/01...",
  "request_id": "01..."
}
```

Códigos estables iniciales: `AUTH_INVALID_CREDENTIALS`, `FORBIDDEN`, `MODE_VIOLATION`, `TOOL_NOT_FOUND`, `TOOL_ARGS_INVALID`, `APPROVAL_REQUIRED`, `ACTION_STATE_CONFLICT`, `TARGET_CHANGED`, `RESOURCE_BUSY`, `TOOL_TIMEOUT`, `DEPENDENCY_UNAVAILABLE` e `INTERNAL_ERROR`.

Una herramienta fallida produce una ejecución persistida con estado `FAILED`; consultar ese recurso es una respuesta HTTP exitosa. Nunca se retornan stack traces, secretos ni stdout sin límites.

## Componente implementado ahora

| Endpoint | Semántica |
|---|---|
| `GET /api/v1/health/live` | El proceso y event loop responden; no consulta dependencias |
| `GET /api/v1/health/ready` | Comprueba una consulta mínima a SQLite; responde 503 si no está disponible |

Ambos incluyen el estado y la versión del servicio. El contrato completo se habilitará por fases conforme existan sus políticas, persistencia y pruebas.
