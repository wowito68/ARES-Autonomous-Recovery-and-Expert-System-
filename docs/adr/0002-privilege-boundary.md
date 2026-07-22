# ADR-0002: Separar el ejecutor privilegiado

- Estado: aceptada
- Fecha: 2026-07-22

## Contexto

Algunas reparaciones necesitan acceso a dispositivos, montaje, `fsck` o instalación de GRUB. Ejecutar FastAPI, el parser de Markdown o el LLM como root convertiría cualquier fallo en compromiso total.

## Decisión

`ares-api` y Ollama se ejecutan sin privilegios. Un broker pequeño (`ares-tool-broker`) escucha únicamente en un socket Unix con permisos restringidos y valida credenciales del peer. La API envía `tool@version`, argumentos canónicos y destinos propuestos; el broker revalida schemas/identidad y construye su propio plan. No acepta argv, scripts, entorno, rutas de binarios ni un plan “firmado” por la API.

Para grants y mutaciones, el broker crea/conserva un desafío con nonce. El usuario pulsa una combinación de VT reservada que el navegador no puede consumir; `ares-consent-agent`, con UID/socket/TTY propios, autentica directamente por PAM/FIDO2. Roles/grupos y techo proceden de estado root-owned, no de DB/API. Devuelve la decisión al broker con peer credentials. FastAPI solo solicita/observa y no puede producir un grant o `APPROVED`.

Los binarios se referencian por ruta absoluta, se verifica que sean propiedad de root y no escribibles, y se invocan sin shell, con entorno mínimo, límites y timeout.

## Consecuencias

- Una explotación de la UI, API o modelo no concede una shell privilegiada.
- Una API comprometida puede proponer tools, causar prompts y abusar de grants de lectura ya vigentes, pero no crearlos/ampliarlos ni forjar mutaciones. Comprometer broker, consent agent, PAM/root o el canal TTY rompe esa garantía.
- El protocolo, manifiesto y empaquetado del broker requieren pruebas propias.
- Las herramientas que interactúan con el kernel o dispositivos siguen siendo peligrosas; la separación reduce superficie, no sustituye la política y las aprobaciones.
