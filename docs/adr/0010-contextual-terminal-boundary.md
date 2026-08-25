# ADR-0010: Terminal contextual local con frontera broker y modo instalado de solo lectura

- Estado: aceptada para implementación incremental
- Fecha: 2026-08-25

## Contexto

ARES necesita permitir que una persona use una terminal manual durante la recuperación sin convertir al LLM, al navegador o a FastAPI en un ejecutor de comandos. La terminal debe poder operar sobre el entorno Live y, en una segunda variante, inspeccionar un sistema instalado en modo solo lectura.

El riesgo principal no es “abrir una terminal”; el riesgo es que la terminal se convierta en una shell remota, un canal de ejecución del modelo, un montaje escribible o una forma encubierta de ejecutar binarios del sistema instalado.

## Decisión

Se implementan dos contextos:

1. `ares_local`: terminal local del usuario `ares`, sin root, sin autorización de riesgo, sin acceso del LLM y sin ejecución automática.
2. `installed_system_read_only`: plan exacto, autorización textual, revalidación de huella del destino, broker privilegiado, sesión registrada y cleanup auditado.

La API solo crea planes, sesiones y estados. No ejecuta `mount`, `umount`, `chroot`, `unshare`, `nsenter`, `systemd-run`, shells ni comandos arbitrarios. Las operaciones privilegiadas viven en `ares-tool-broker` bajo acciones cerradas `terminal.*`.

La UI no crea grants. La autorización queda ligada a plan, sesión, contexto, destino, huella, política de montaje, riesgo y expiración. Un cambio de fingerprint invalida el arranque.

El broker rechaza campos de petición como `command`, `argv`, `shell`, `executable`, `script`, `device`, `mountpoint`, `flags` y `environment`. El contrato público no devuelve PTYs, FDs, sockets, namespace ids, contenidos de terminal ni rutas de montaje. Las rutas técnicas internas solo viajan por el canal API→broker y por archivos runtime root-owned necesarios para lanzar la terminal local.

Para `installed_system_read_only`, el broker prepara un directorio bajo `/run/ares/terminals/<session>`. Si el sistema ya está montado, usa ese origen como fuente de solo lectura. Si el origen es un dispositivo, el broker intenta montarlo como `ro,nosuid,nodev,noexec`. La terminal se lanza desde la sesión gráfica y entra a una vista con `bubblewrap`, exponiendo el sistema instalado en `/mnt/installed-system` como solo lectura y usando binarios del Live.

`installed_system_chroot_read_only` y `installed_system_admin` quedan bloqueados hasta demostrar aislamiento de red, `/proc`, `/dev`, `/run`, sockets ARES, herencia de credenciales, escape de namespace, cleanup y auditoría con pruebas privilegiadas.

## Amenazas cubiertas

- Reutilización de autorización: grants de un uso y expiran.
- Sustitución de destino: revalidación de fingerprint antes de iniciar.
- Shell genérica desde API/LLM: no existe endpoint de ejecución ni contrato con argv.
- Escritura accidental al sistema instalado: política read-only y mount options conservadoras.
- Terminal huérfana: sesión activa bloquea salida segura hasta cierre/cleanup.
- Exposición de terminal al navegador: el navegador solo observa estado, no contenidos.

## Limitaciones aceptadas

- Si el kernel o `bubblewrap` no permiten crear el namespace, el contexto instalado debe fallar cerrado.
- Si un dispositivo no puede montarse en solo lectura, no se degrada a modo escribible.
- El usuario conserva responsabilidad sobre comandos manuales dentro de la terminal.
- La terminal instalada no ejecuta binarios del sistema instalado; para chroot/admin se requiere otro ADR y otra suite de pruebas.

## Criterios de aceptación

- Tests de API para campos prohibidos, autorización exacta, redacción de rutas y bloqueo de salida con sesión activa.
- Tests del broker para rechazo de acciones/campos no permitidos y cleanup idempotente.
- Validación Live de tmpfiles, systemd, launcher y ausencia de endpoints `/terminal/execute`, `/shell`, `/commands` o equivalentes.
- Prueba VM/Live que demuestre apertura de `ares_local` y fallo cerrado o apertura sandboxed de `installed_system_read_only` según precondiciones del entorno.
