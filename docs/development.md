# Manual del desarrollador

## Preparación

```bash
cd backend
uv sync --dev
uv run pytest
uv run ruff check .
uv run mypy src
```

El proyecto requiere Python 3.12. El lockfile debe actualizarse intencionalmente y revisarse junto con cambios de dependencias.

## Reglas de diseño

- `api → services → core`; infraestructura implementa puertos de `core`.
- No importar FastAPI/SQLAlchemy/subprocess en el dominio.
- No serializar ORM directamente.
- No construir commands en endpoints, servicios o prompts.
- No introducir una herramienta genérica de shell, rutas arbitrarias o plugins dinámicos.
- Una mutación requiere preflight, aprobación exacta, lock y verificador.
- No ejecutar pruebas destructivas en el host. Usar VM, loop device o imagen descartable expresamente preparada.
- Los comentarios explican por qué, no repiten el código.

## Calidad

- Ruff para formato/lint e imports.
- mypy estricto para `src`.
- pytest y cobertura; meta ≥90 % en `core/services` y ≥80 % global, ponderada por riesgo.
- Tests de contrato comunes para cada herramienta.
- Fixtures/golden de salidas reales por versión/locale.
- Hypothesis para canonicalización, parsers y máquinas de estados cuando se incorporen.

Un cambio se considera terminado cuando código, migración, OpenAPI, pruebas, threat model y manual coinciden.

## Configuración

Las variables usan prefijo `ARES_`. Hoy se validan tipos, settings desconocidos y SQL echo en producción; el proceso web no expone modo debug. En la arquitectura objetivo, la precedencia será defaults seguros, `/etc/ares/ares.toml` y variables de entorno. Antes de fase 1 se añadirán y probarán las validaciones todavía no modeladas: bind externo, rutas de binario, permisos y CORS.

## Nuevas herramientas

Antes de escribir una implementación, añade spec, matriz riesgo/modo, modelos strict, identidades de destino, precondiciones, cancelación, lock, límites, verificador y estrategia de fixtures. La revisión debe demostrar que ninguna entrada del usuario o LLM llega a argv sin una transformación cerrada.

## Commits y secretos

No versionar `.env`, base de datos, artefactos, modelos, tokens, reportes ni salidas con seriales. Los cambios deben ser pequeños y dejar pruebas verdes; una fase no habilita capacidades de la siguiente a medias.
