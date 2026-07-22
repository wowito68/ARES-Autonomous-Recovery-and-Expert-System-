# ARES backend

Plano de control local de ARES construido con Python 3.12, FastAPI, SQLAlchemy y SQLite.

La entrega actual contiene la base ejecutable y los endpoints de liveness/readiness. Autenticación, auditoría, agente y herramientas se incorporan de acuerdo con el [roadmap del repositorio](../docs/roadmap.md); todavía no hay operaciones de diagnóstico o reparación habilitadas.

```bash
uv sync --dev
uv run uvicorn ares.main:create_app --factory --reload
uv run pytest
```

Consulta el [README principal](../README.md) y la [arquitectura](../docs/architecture.md) para conocer el alcance y las fronteras de seguridad.
