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

`make test-iso` comprueba que QEMU puede iniciar BIOS y UEFI; no sustituye los asserts dentro del guest ni la prueba Secure Boot con OVMF_VARS que contenga claves inscritas.

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
