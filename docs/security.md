# Modelo de seguridad

> Estado: arquitectura objetivo. El slice fundacional solo implementa defaults locales básicos, configuración validada, errores HTTP redactados, logs con campos curados, correlación y readiness de SQLite; identidad, policy, consentimiento independiente por TTY, broker y ledger llegan en fases siguientes.

## 1. Objetivos

ARES controla operaciones que pueden destruir datos. El objetivo no es confiar en que el modelo “se comporte”, sino hacer que una salida incorrecta o maliciosa no pueda saltarse controles deterministas.

Propiedades buscadas:

- el LLM no tiene credenciales, acceso a dispositivos ni capacidad de ejecutar procesos;
- una API comprometida no obtiene una shell root;
- cada operación se limita por identidad, argumentos, modo, rol, riesgo y tiempo;
- las mutaciones requieren intención humana verificable;
- todos los resultados se sustentan con evidencias y quedan auditados;
- el comportamiento por defecto es local, sin red y de solo lectura.

## 2. Activos y fronteras

Activos: datos de los discos anfitriones, credenciales, historial, artefactos, modelos, manifiesto de herramientas, ejecutables y cadena de arranque.

Fronteras:

1. Navegador ↔ API: sesión, CSRF, validación y escape de contenido.
2. API/agente ↔ Ollama: toda salida se trata como propuesta no confiable.
3. API ↔ broker: socket Unix, credenciales del peer, protocolo cerrado y autenticado.
4. Broker ↔ agente de consentimiento: desafío/decisión fuera del proceso web para mutaciones.
5. API/broker/consentimiento ↔ audit writer: emisores separados y ledger no escribible por la API.
6. Broker ↔ sistema anfitrión: herramientas con privilegios mínimos y destino revalidado.
7. Sistema Live ↔ persistencia: por defecto efímera; persistencia únicamente explícita, cifrada y ligada al mismo medio físico.

La persistencia genérica de `live-boot` permanece deshabilitada. Una etiqueta o nombre GPT no basta: el activador exige LUKS2, PARTUUID/UUID registrados, marcador ARES, mismo ancestro físico que el medio Live y coincidencia única. Solo persiste datos ARES seleccionados, nunca el overlay raíz completo.

## 3. Amenazas y controles

| Amenaza | Control principal |
|---|---|
| Prompt injection en logs o nombres de volumen | Datos marcados como no confiables, parser tipado, contexto mínimo; policy y broker independientes del LLM |
| Comando o flags inyectados | No hay `run_command`; argv interno, `shell=False`, modelos estrictos y enums |
| Path traversal o symlink | IDs opacos; raíces controladas; `openat2`/`O_NOFOLLOW` cuando aplique; revalidación |
| Reparar otro disco tras hotplug | WWN/serial/by-id + major:minor + snapshot; resolver inmediatamente antes de ejecutar |
| Reutilizar una aprobación | Nonce, hash del plan y argumentos, destino, expiración, consumo atómico |
| Sesión robada/CSRF | Token opaco aleatorio, cookie HttpOnly/SameSite, Origin y CSRF, expiración y revocación |
| API o parser comprometido | API sin root; broker mínimo con protocolo cerrado |
| DoS por salida o proceso colgado | Límites de bytes, CPU/memoria, deadlines, grupo de procesos y estado `HUNG` |
| Dos operaciones sobre el mismo disco | Locks por recurso y matriz de compatibilidad lectura/escritura |
| API reescribe auditoría | Audit writer separado, HMAC y eventos críticos emitidos también por broker/consent agent |
| Escritura accidental por el Live | Sin automount, swap ni resume; montaje `ro,nosuid,nodev,noexec`; modo READ_ONLY |

El ledger objetivo detecta reescritura por la API, no es *tamper-proof* frente a root. Sin un checkpoint firmado fuera del medio o sellado en TPM, tampoco puede demostrar que no se revirtió/truncó el almacenamiento completo. La cadena simple de hashes de la fase inicial solo detectará corrupción accidental y no se presentará como una garantía de seguridad.

Broker y consent agent esperan un ACK del audit writer emitido solo después de `fdatasync` antes de conceder autoridad o ejecutar una tool. Si el writer no responde, aplican backpressure y fallan cerrado. Si cae después de un efecto, el broker mantiene lock y `RECONCILIATION_REQUIRED` hasta persistir/reconciliar el outcome.

## 4. Autenticación y autorización

### Autenticación local

La identidad humana canónica será PAM/cuentas y grupos root-owned. La API conserva perfil, sesión y proyecciones de rol, pero no puede elevar la autoridad que el broker obtiene directamente del sistema. Si una fase transitoria conserva credenciales Argon2id propias de la UI, se consideran una identidad no autoritativa: cada mutación vuelve a autenticar por PAM, el ledger registra ambas identidades y cualquier discrepancia bloquea.

- Sin usuario ni contraseña predeterminados reutilizables.
- Bootstrap de un solo uso en TTY confiable; se deshabilita tras crear el primer administrador.
- Contraseñas PAM con política del sistema; cualquier hash Argon2id de UI queda limitado al login web y versionado.
- Sesiones opacas de 256 bits. Solo el SHA-256 del token se persiste.
- Cookie `HttpOnly`, `SameSite=Strict` y `Secure` cuando haya TLS.
- CSRF vinculado a sesión y comprobación estricta de `Origin` en mutaciones.
- Expiración por inactividad y absoluta, revocación y reautenticación reciente para alto riesgo.
- En la imagen Live/v1 el servidor escucha exclusivamente en `127.0.0.1`; no existe un modo remoto.

Roles iniciales:

- `VIEWER`: consulta casos, evidencias y reportes.
- `OPERATOR`: inicia diagnósticos y autoriza acciones dentro de política.
- `ADMIN`: administra usuarios, modelos, configuración y acceso a modo avanzado.

El agente es un principal interno, no un usuario. Hereda una delegación acotada del usuario y nunca puede aprobar, reautenticar ni elevar el modo.

### Modos

El modo es un techo de capacidad, no una autorización:

- `READ_ONLY`: solo observación. Un consentimiento en el canal confiable crea un `capability_grant` autoritativo en el broker, ligado a identidad autenticada, sesión/caso, tools/versiones exactas, máximo de usos y caducidad; SQLite solo proyecta el grant.
- `REPAIR`: permite proponer cambios. Cada cambio necesita aprobación individual; alto riesgo también exige reautenticación, preflight, respaldo cuando sea viable y verificador.
- `ADVANCED`: expone herramientas críticas a administradores. Expira pronto y usa confirmación reforzada; no desactiva ningún control.

La capacidad efectiva es la intersección entre techo root-owned del broker, rol obtenido directamente de PAM/grupos del sistema, modo solicitado (que solo puede restringir), grant del broker, política/manifiesto root-owned y estado del recurso. Los roles/modos enviados por la API nunca amplían autoridad. `ALLOW` es imposible si no existe el grant aplicable y su uso no se consume/contabiliza atómicamente en el broker.

### Consentimiento independiente para mutaciones

El navegador puede solicitar un grant o confirmación, pero no producirlos. El broker crea el desafío exacto. `ares-consent-agent` corre con UID/socket distintos, autentica directamente mediante PAM/FIDO2 contra estado root-owned y obtiene rol/grupos sin consultar FastAPI.

La versión v1 no confía en un diálogo X11 superpuesto. Tras preparar el desafío, exige que el usuario pulse una combinación de VT reservada (secure-attention gestionada fuera del navegador) para entrar al TTY de consent agent; allí ve plan, destino, riesgo y código. Logind retira input/DRM al kiosco mientras ese VT está activo. El usuario nunca introduce credenciales de reparación en una página web. Esta atención segura y `SO_PEERCRED`/permisos de socket separan `ares-api`, `ares-consent`, `ares-broker` y `ares-audit`; un prompt dibujado dentro de Firefox no es válido.

El broker conserva la clave/estado de los desafíos y consume el nonce de un solo uso. Una API comprometida puede proponer operaciones, provocar solicitudes molestas y abusar de lecturas dentro de un grant ya concedido, pero no crear/ampliar grants ni forjar una aprobación de mutación. Consent agent aplica rate limit. Si broker, consent agent, PAM/root o el compositor/TTY confiable se comprometen, la garantía deja de existir. Esta frontera se probará antes de habilitar tools.

## 5. Ejecución segura

El broker recibe `tool@version`, argumentos canónicos y destinos propuestos. Revalida schema, manifest, policy e identidad, y reconstruye internamente el plan/argv; nunca acepta una línea de comandos ni un plan construido por frontend, API o modelo.

Para cada proceso:

- binario absoluto, propiedad de root y no escribible por otros;
- `create_subprocess_exec`, nunca shell ni `sudo`;
- stdin cerrado, descriptores cerrados, directorio fijo y entorno mínimo (`LC_ALL=C`);
- timeout de cola, inicio, inactividad y total;
- stdout y stderr drenados simultáneamente con límite;
- grupo de procesos y política de cancelación declarada por herramienta;
- límites de CPU, memoria, archivos, procesos y descriptores;
- unidad systemd/perfil AppArmor/seccomp/capabilities adaptado a la operación.

No se mata a ciegas una reparación después de su punto de commit. Si un proceso queda en espera de E/S no interrumpible, la invocación se marca `HUNG`, conserva el lock del dispositivo y bloquea otras acciones hasta intervención segura.

## 6. Identidad de dispositivos

La identidad se construye con una jerarquía: WWN/EUI/NGUID cuando existe; si no, combinación de serial, modelo, transporte, capacidad y ruta física; UUID/GPT GUID y `major:minor` solo complementan el snapshot porque pueden clonarse o cambiar. La topología de device-mapper, LVM, RAID y particiones se conserva hasta el dispositivo físico.

El disco físico que contiene root/medio Live se determina desde `findmnt` + udev; él, sus ancestros y descendientes quedan excluidos de **toda mutación de diagnóstico/recuperación**. Solo herramientas de persistencia/exportación ARES pueden escribir una partición preprovisionada y reconocida de ese medio, sin tocar tabla de particiones, bootloader ni otros volúmenes. Si faltan atributos, hay duplicados o más de un dispositivo coincide, ARES permite observación pero falla cerrado para mutaciones. Justo antes de ejecutar se vuelve a resolver toda la identidad y se compara con el snapshot aprobado.

## 7. Privacidad y red

ARES no transmite automáticamente prompts, inventario, logs, telemetría ni reportes. Los artefactos se crean con permisos `0600`; directorios, `0700`. Seriales, nombres de usuario, rutas, IP y contenido de logs se clasifican y redactan al generar reportes. Tokens, contraseñas, CSRF, hashes de sesión y razonamiento interno del modelo nunca entran en logs o auditoría.

La API y Ollama tienen egress denegado. `network.diagnose` es la única excepción prevista: requiere grant explícito, muestra destino/protocolo, usa una unidad aislada con allowlist y regla temporal, limita paquetes/bytes y no incluye contenido del diagnóstico. Por tanto puede generar tráfico de prueba consentido, pero no subir datos de ARES.

No se carga Markdown/HTML sin sanitizar, no se usan CDN, analítica o fuentes remotas, y la UI no ejecuta acciones como resultado de texto del modelo.

## 8. Reglas de lanzamiento

Una herramienta de mutación no se habilita hasta contar con:

1. schema estricto y fixtures de salida;
2. identificación estable del destino y protección TOCTOU;
3. preflight y consecuencias mostrables;
4. política, riesgo y permisos documentados;
5. lock de recurso y cancelación segura;
6. backup/rollback cuando sea posible;
7. verificador independiente;
8. pruebas en VM/imagen desechable;
9. auditoría y redacción;
10. manual de recuperación si ARES se interrumpe.

`mount`, `parted`, `fdisk`, `testdisk`, reparaciones `fsck`, `grub-install` y destinos libres de `rsync` permanecen deshabilitados hasta cumplir esta puerta.
