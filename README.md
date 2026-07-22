# ARES — Autonomous Recovery and Expert System

ARES es una plataforma local y *offline-first* para diagnosticar, mantener y recuperar equipos desde un sistema Debian Live. Combina una API FastAPI, una interfaz React, modelos locales servidos por Ollama y un catálogo cerrado de herramientas del sistema.

> [!IMPORTANT]
> El modelo nunca recibe una terminal ni ejecuta Bash. Solo propone invocaciones tipadas. El servidor valida la política y la autorización, y un ejecutor aislado construye internamente los argumentos permitidos.

## Estado

El proyecto se construye por incrementos verificables. La iteración actual entrega:

- el diseño de arquitectura, seguridad, datos, API, agente, herramientas, frontend y sistema Live;
- las decisiones de arquitectura que bloquean atajos inseguros;
- el primer componente ejecutable del backend: configuración, ciclo de vida, SQLite, logging estructurado, identificadores de petición y endpoints de salud;
- la infraestructura de ARES OS sobre Debian 13 Live: build fijado, BIOS/UEFI, branding, servicios de plataforma e inventario de hardware;
- pruebas automáticas del componente fundacional.

Autenticación, catálogo de herramientas, agente y frontend de producto todavía no están implementados. El pipeline de ISO sí existe, pero no equivale a un release firmado: persistencia cifrada, cadena de integridad completa y matriz física conservan puertas pendientes. Consulta el [roadmap de ARES OS](docs/ares-os-roadmap.md).

## Principios no negociables

- Operación totalmente local; ninguna función esencial depende de Internet.
- API, frontend, Ollama y agente se ejecutan sin privilegios.
- No existe una herramienta genérica para ejecutar comandos.
- El modo operativo limita capacidades, pero nunca equivale a una aprobación.
- Toda mutación requiere una aprobación humana de un solo uso ligada a la acción y al destino exactos.
- Las evidencias, decisiones, aprobaciones, resultados y verificaciones se auditan.
- Una reparación no se declara exitosa hasta ejecutar su verificador.

## Inicio rápido del backend

Requisitos: Python 3.12 y [`uv`](https://docs.astral.sh/uv/).

```bash
cd backend
uv sync --dev
uv run uvicorn ares.main:create_app --factory --reload
```

La API queda disponible en `http://127.0.0.1:8000`. Comprobaciones:

```bash
curl http://127.0.0.1:8000/api/v1/health/live
curl http://127.0.0.1:8000/api/v1/health/ready
```

Ejecuta las verificaciones con:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

## Construcción de ARES OS

Requisito de desarrollo: Docker o Podman en un host Linux amd64. La configuración y el constructor Debian 13 están fijados en el repositorio.

```bash
make validate
make validate-config
make build-iso
```

La salida es `iso/ARES.iso`, acompañada por SHA-256, manifest de build y lista de paquetes. Consulte el [runbook](docs/live-build-runbook.md) antes de construir o probar la imagen.

## Documentación

- [Arquitectura](docs/architecture.md)
- [Modelo de seguridad](docs/security.md)
- [Agente y herramientas](docs/agent-and-tools.md)
- [Modelo de datos](docs/data-model.md)
- [Contrato de API](docs/api.md)
- [Reportes y evidencia](docs/reporting.md)
- [Frontend](docs/frontend.md)
- [Debian Live y despliegue offline](docs/live-system.md)
- [Runbook de construcción Live](docs/live-build-runbook.md)
- [Roadmap de ARES OS](docs/ares-os-roadmap.md)
- [Instalación](docs/installation.md)
- [Manual de desarrollo](docs/development.md)
- [Manual de usuario](docs/user-guide.md)
- [Roadmap](docs/roadmap.md)
- [Decisiones de arquitectura](docs/adr/)

## Licencia

Aún no se ha elegido una licencia. No distribuyas imágenes ISO ni modelos hasta definir las licencias del proyecto, de Debian, de Ollama y de cada modelo incluido.
