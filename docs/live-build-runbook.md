# Runbook de construcción de ARES OS

## Requisitos

- host Linux amd64;
- GNU Make;
- Docker o Podman;
- espacio libre para paquetes, chroot y SquashFS;
- red solo durante la resolución del snapshot;
- QEMU, OVMF y xorriso para pruebas posteriores.

El contenedor de build requiere privilegios para chroot/mount. En release ejecútelo dentro de una VM Debian 13 desechable y no sobre código no confiable.

## Construcción

```bash
make validate
make validate-config
make build-iso
```

Para descartar el cache de `live-build`:

```bash
make build-iso-clean
```

La caché persistente se prepara dentro del workspace de cada ejecución y se
sincroniza al terminar. Esto mantiene el chroot y la caché en el mismo montaje,
requisito de `live-build` para sus enlaces duros, sin compartir un workspace
mutable entre compilaciones concurrentes.

Para escribir la salida en otro directorio:

```bash
ARES_OUTPUT_DIR=/ruta/segura make build-iso
```

Un release exige un árbol Git limpio:

```bash
ARES_REQUIRE_CLEAN=1 make build-iso-clean
```

Si el constructor fijado ya existe localmente, puede reutilizarse sin crear
capas nuevas:

```bash
ARES_REUSE_BUILDER=1 make build-iso
```

Esta ruta comprueba la etiqueta OCI y la versión real de `live-build`; no
acepta silenciosamente una imagen incompatible con el mismo nombre. El flujo
normal mantiene el valor `0` y reconstruye el constructor desde el Dockerfile.

## Verificación

```bash
sha256sum --check iso/ARES.iso.sha256
make inspect-iso
make test-iso
make verify-reproducible
```

`make test-iso` arranca cuatro perfiles: BIOS y UEFI Secure Boot, cada uno
desde medio óptico y desde la imagen híbrida tratada como disco USB. La prueba
solo pasa cuando el guest publica
`ARES_BOOT_READY ... api=READY ui=READY hardware=READY`; en UEFI utiliza
OVMF con claves Microsoft inscritas y exige `trust=ENFORCED_PARTIAL`. TCG es el
acelerador predeterminado y reproducible. En un host con acceso a `/dev/kvm`
puede reducirse el tiempo sin cambiar la cobertura:

```bash
ARES_QEMU_ACCEL=kvm make test-iso
```

Los discos híbridos se abren mediante snapshots efímeros de QEMU, por lo que
las escrituras del firmware o del guest nunca modifican `iso/ARES.iso`.

`make verify-reproducible` siempre usa dos workspaces nuevos y compara las ISO
byte por byte. Por defecto reutiliza la caché local cuyos paquetes siguen
siendo autenticados por APT; esto evita descargar dos veces el mismo snapshot.
El saneamiento y la lista de exclusión final de SquashFS eliminan del root Live
el `hostid` aleatorio creado por `nvme-cli` y los caches binarios de APT. La
exclusión sigue aplicándose aunque `live-build` regenere APT después de los
hooks; esos datos de build no sirven en la operación offline ni tienen una
representación byte a byte estable.
La sal y el UUID de dm-verity se derivan del SHA-256 del SquashFS terminado. Así conservan
32 bytes de sal vinculados al contenido, pero evita la sal aleatoria que
`veritysetup format` generaría en cada build. El inspector comprueba ambos
relación antes de aceptar la ISO.
La puerta de release puede además exigir dos descargas en frío:

```bash
ARES_REPRODUCIBLE_COLD_CACHE=1 make verify-reproducible
```

Para diagnosticar una diferencia, puede conservarse de forma explícita el
par fallido; en ejecuciones normales se elimina para no consumir espacio:

```bash
ARES_KEEP_REPRO_EVIDENCE=1 make verify-reproducible
```

## Escritura a USB

La generación del ISO no requiere USB ni pasos manuales. Escribir el artefacto sí es destructivo y debe hacerse fuera del build tras resolver el dispositivo exacto con `lsblk --paths --output NAME,SIZE,MODEL,SERIAL,TRAN,MOUNTPOINTS` y verificar el SHA-256. No se proporciona una orden que elija automáticamente “el primer USB”: una identificación ambigua debe fallar y pedir intervención.

## Artefactos

- `ARES.iso`: imagen híbrida;
- `ARES.iso.sha256`: checksum externo;
- `ARES.build.json`: procedencia mínima y claim de integridad;
- `ARES.packages.txt`: inventario de paquetes.

Firmas, SBOM SPDX/CycloneDX y attestations son puertas de OS5/OS7. No se crean archivos vacíos que aparenten esas garantías.

## Diagnóstico

- fallo de snapshot: compruebe timestamps de `live/release.env`, nunca cambie a `stable`;
- paquete ausente: corrija la lista o el snapshot; no use `apt-secure false`;
- Secure Boot ausente: el build debe fallar porque se usa `enable`, no `auto`;
- espacio insuficiente: limpie solo con `make clean`; no borre rutas amplias;
- diferencias reproducibles: conserve ambos workspaces y use `diffoscope` antes de firmar;
- fallo gráfico: pruebe la entrada “Gráficos seguros” y conserve logs de boot.

## Limpieza

```bash
make clean
```

Solo elimina `live/.build`, `live/.cache` y los artefactos ARES conocidos de `iso/`.
