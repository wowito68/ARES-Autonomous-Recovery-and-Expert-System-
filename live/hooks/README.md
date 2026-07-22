# Hooks

Los hooks canónicos están en `live/config/hooks/live/`. Deben ser idempotentes, `set -eu`, sin red, sin lógica de negocio y con sufijo `.hook.chroot`. No se admiten hooks binary salvo ADR porque correrían como root sobre el entorno de build.
