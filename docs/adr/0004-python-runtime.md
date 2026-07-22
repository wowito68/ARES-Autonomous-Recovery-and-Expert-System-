# ADR-0004: Runtime Python 3.12 aislado en Debian 13

- Estado: aceptada
- Fecha: 2026-07-22

## Contexto

El requisito fija Python 3.12. Debian 13 `trixie`, base estable elegida para la imagen Live, distribuye Python 3.13 como runtime del sistema. Sustituir `/usr/bin/python3` rompería expectativas de herramientas y paquetes Debian; cambiar ARES silenciosamente a 3.13 incumpliría el requisito.

## Decisión

Los paquetes de ARES instalarán un CPython 3.12 autocontenido y versionado bajo `/opt/ares/runtime`. No modificarán el Python del sistema. El runtime se construirá desde una fuente fijada, tendrá lockfile, hashes, SBOM y un proceso explícito para incorporar parches de seguridad.

## Consecuencias

- Se respeta Python 3.12 sin interferir con Debian.
- La imagen crece y ARES asume el mantenimiento de ese runtime.
- Dependencias con extensiones nativas se construyen y prueban contra el ABI 3.12.
- Migrar a Python 3.13 exigirá compatibilidad, pruebas y otro ADR.

Referencias: [releases de Debian](https://www.debian.org/releases/) y [Python 3.13 en trixie](https://packages.debian.org/trixie/python/python3.13).
