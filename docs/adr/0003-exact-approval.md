# ADR-0003: Aprobaciones de un solo uso vinculadas al plan exacto

- Estado: aceptada
- Fecha: 2026-07-22

## Contexto

Una confirmación genérica como “permitir reparación de disco” puede reutilizarse, sufrir cambios de parámetros o terminar apuntando a otro dispositivo después de un evento hotplug.

## Decisión

El broker crea el desafío y conserva nonce/estado. La aprobación se origina en `ares-consent-agent` sobre un TTY seguro con autenticación PAM/FIDO2 directa, no en FastAPI, y liga identidad/rol del sistema, sesión, invocación, herramienta/versión, manifiesto, argumentos, plan reconstruido, destinos, riesgo, efecto, techo de modo y caducidad. Es atómica y de un solo uso. SQLite recibe una proyección; no es autoridad.

Antes de ejecutar se repiten autenticación del proof del TTY, policy, identidad y precondiciones. CSRF protege la petición web de preparar/rechazar, pero no sustituye el proof. Cualquier diferencia invalida la aprobación. El TTY se genera desde manifiesto/plan del broker, nunca desde texto libre del LLM/API.

## Consecuencias

- Se evitan reutilización, sustitución de destino y cambios posteriores a la aprobación.
- Una desconexión/reconexión o cambio de precondiciones obliga a preparar y aprobar otro plan.
- El modo avanzado no elude esta regla.
- La UI web no puede crear grants ni completar una mutación si consent agent/TTY no están disponibles.
