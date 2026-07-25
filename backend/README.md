# ARES backend

Plano de control local de ARES construido con Python 3.12/3.13, FastAPI,
SQLAlchemy y SQLite.

La entrega actual contiene:

- endpoints de liveness/readiness;
- resumen de modo, integridad, red, persistencia y hardware;
- assets estáticos servidos bajo el mismo origen;
- adaptador estricto a Ollama por loopback;
- chat acotado, sin tools ni privilegios;
- núcleo v2 de Capabilities, Workflows, Event Bus, Reasoning Engine y Knowledge Graph;
- capability pasiva `storage.disk-analysis` de extremo a extremo.

La operación disponible sólo analiza el inventario público ya recolectado: no abre
dispositivos y no repara almacenamiento. Autenticación, consentimiento, agente y
capabilities mutables se incorporan de acuerdo con el
[roadmap del repositorio](../docs/roadmap.md).

```bash
uv sync --dev
uv run uvicorn ares.main:create_app --factory --reload
uv run pytest
```

Consulta el [README principal](../README.md), la
[arquitectura](../docs/architecture.md) y el
[contrato de IA local](../docs/local-ai.md). La evolución de Tools a Capabilities,
incluyendo el flujo Disk Analysis, se documenta en
[ARES v2](../docs/architecture-v2-capabilities.md).
