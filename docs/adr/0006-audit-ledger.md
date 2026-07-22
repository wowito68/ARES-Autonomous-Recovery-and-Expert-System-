# ADR-0006: Ledger de auditoría fuera de la API

- Estado: aceptada como diseño; aún no implementada
- Fecha: 2026-07-22

## Contexto

Una cadena SHA almacenada y escrita por la misma API no prueba integridad: una API comprometida puede reescribir todo y recalcularla. También puede truncarse o revertirse el medio completo.

## Decisión

`ares-audit-writer` será un proceso/usuario separado. Posee una clave HMAC no legible por FastAPI y un archivo append-only. Acepta eventos por sockets Unix y usa peer credentials para distinguir API, broker y consent agent. Cada registro incluye secuencia, MAC previo, emisor, timestamp monotónico/civil, correlación y payload redactado.

Antes de emitir un grant/aprobación o iniciar cualquier tool, consent agent/broker envían el evento de intención y esperan un ACK que incluye secuencia/MAC **después de `fdatasync`**. Sin ACK no hay side effect: la cola aplica backpressure acotado y luego falla cerrado, aunque salud, historial y exportación sigan disponibles. Después del efecto el broker espera otro ACK para el resultado. Si ese segundo ACK falla, conserva lock, guarda un journal de emergencia root-owned y pasa a `RECONCILIATION_REQUIRED` hasta reconciliar y persistir el outcome; nunca declara éxito.

Broker y consent agent emiten directamente ejecución, destino y decisión, de modo que una API comprometida no puede reescribir u omitir esos efectos críticos. SQLite conserva una proyección reconstruible para búsqueda. Un checkpoint puede exportarse/firmarse en persistencia externa o sellarse con TPM cuando exista.

## Consecuencias

- La API no puede modificar eventos ya aceptados ni falsificar el emisor del broker.
- La caída/saturación del writer bloquea nuevas tools y consentimientos en vez de crear huecos silenciosos.
- La API todavía puede omitir eventos que solo ella conoce; los efectos críticos tienen emisores independientes.
- Comprometer root/audit writer rompe la garantía.
- Sin ancla externa/TPM no se detecta con certeza rollback o truncación de todo el almacenamiento.
- La fase inicial no se anunciará como tamper-evident de seguridad hasta implementar y probar este proceso.
