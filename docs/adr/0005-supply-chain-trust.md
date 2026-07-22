# ADR-0005: Raíz de confianza y firma de releases offline

- Estado: aceptada como diseño; claves aún no creadas
- Fecha: 2026-07-22

## Contexto

Una ISO, paquete APT, runtime Python o modelo modificado puede anular todas las fronteras de ARES. Publicar hashes junto al artefacto no sirve si ambos son sustituidos. Además se necesita rotación y recuperación ante compromiso sin depender de un servicio online durante el uso.

## Decisión

ARES usará una raíz Ed25519 offline con delegación y expiración inspiradas en TUF. La raíz será 2-de-3 y solo firmará/rotará claves de release y metadata de revocación. Una clave de release protegida firma un manifiesto versionado que incluye hashes de ISO, repositorio APT, `.deb`, SBOM, runtime, binario Ollama y packs/modelos con su licencia.

El fingerprint público de bootstrap se distribuirá por al menos dos canales controlados (repositorio fuente y sitio/documentación de release). La primera verificación exige compararlos. Una imagen ya instalada incorpora la raíz pública, nunca una clave privada.

La metadata incluye versión monotónica y expiración. ARES conserva `highest_seen_version`, roots aceptadas y `last_trusted_time` en persistencia cifrada autenticada; cuando existe TPM, sella además un digest/contador para detectar rollback de ese estado. El reloj aceptado nunca retrocede respecto de `last_trusted_time`; RTC anterior o estado inconsistente bloquean instalación y piden corrección/verificación manual.

En una sesión efímera o primera verificación totalmente offline, firma e integridad sí pueden comprobarse, pero **no** la frescura frente a replay de una release antigua válidamente firmada: no existe estado ni tiempo confiable. La UI lo muestra como `FRESHNESS_UNVERIFIED` y no afirma “última versión”. Para instalar un pack/update se exige persistencia previa, contador TPM o que el usuario compare versión/fecha/fingerprint con un canal externo confiable. La ISO base puede arrancar en READ_ONLY con esa advertencia.

Una rotación normal se firma con la raíz vigente; ante compromiso se publica revocación/root nueva con quorum. No se publicarán instrucciones que digan “verificado” hasta crear claves, ceremonia, backups offline y ensayo documentado de recuperación.

## Consecuencias

- El funcionamiento y la verificación siguen siendo offline después de obtener artefactos/metadata.
- APT puede conservar su firma delegada, pero el manifiesto ARES enlaza también la ISO y los packs que APT no cubre.
- Perder quorum de raíz puede impedir nuevas releases confiables; backups y custodios son parte del procedimiento.
- Un usuario que no verifica el fingerprint inicial sigue expuesto a sustitución del primer artefacto.
- Sin estado persistente/TPM ni referencia externa, un atacante puede reproducir una release antigua firmada; la expiración por sí sola no resuelve el primer arranque offline.
