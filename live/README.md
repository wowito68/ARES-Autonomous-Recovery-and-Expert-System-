# Infraestructura ARES OS Live

Este directorio es la fuente versionada de la imagen Debian 13 Live.

```bash
make validate
make build-iso
```

`config/` es el árbol canónico de `live-build`. El build lo copia a un workspace temporal, renderiza metadata/artwork y nunca usa el checkout como chroot. Consulte `docs/live-system.md` y `docs/live-build-runbook.md`.
