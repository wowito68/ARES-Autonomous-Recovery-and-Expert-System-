# Manual de instalación

## Estado actual: entorno de desarrollo

La infraestructura Debian Live ya está implementada, pero todavía no existe un release público firmado. Se pueden construir y verificar imágenes de desarrollo; persistencia cifrada, integridad completa y pruebas físicas siguen abiertas.

Requisitos:

- Linux;
- Python 3.12;
- `uv`;
- Git.

```bash
# Desde una copia autorizada del repositorio ARES:
cd backend
uv sync --dev
uv run uvicorn ares.main:create_app --factory --host 127.0.0.1 --port 8000
```

Prueba liveness/readiness:

```bash
curl http://127.0.0.1:8000/api/v1/health/live
curl http://127.0.0.1:8000/api/v1/health/ready
```

Por defecto la base de desarrollo se crea bajo `backend/data/`. Puede cambiarse con `ARES_DATABASE_URL`. No expongas esta API a otra interfaz de red; autenticación todavía pertenece a la siguiente fase.

## Construir una imagen Live de desarrollo

Requisitos adicionales: Docker o Podman, Make, espacio para el chroot y acceso al snapshot Debian durante el build.

```bash
make validate
make validate-config
make build-iso
sha256sum --check iso/ARES.iso.sha256
make inspect-iso
make test-iso
```

El constructor usa un contenedor privilegiado para las operaciones de chroot/mount. Ejecútalo en una VM Debian 13 desechable para releases y nunca sobre código no confiable.

## Imagen Live pública futura

El flujo previsto será:

1. descargar la ISO, checksums, firma y SBOM;
2. verificar la firma y SHA-256 antes de escribirla;
3. escribir la imagen híbrida en un USB identificado con certeza;
4. arrancar desde UEFI/BIOS;
5. mantener READ_ONLY durante el diagnóstico inicial;
6. elegir explícitamente si el historial será efímero o persistente/cifrado.

No se publicarán instrucciones definitivas de distribución, flasheo o activación de Secure Boot hasta disponer de una ISO firmada, licencias aprobadas y matriz física superada. El procedimiento técnico de build está en [live-build-runbook.md](live-build-runbook.md).
