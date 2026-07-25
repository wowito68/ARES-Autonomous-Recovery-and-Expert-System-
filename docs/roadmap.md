# Roadmap incremental

Una fase solo se cierra cuando pasa su puerta de salida. Las fechas se decidirán tras medir la fase anterior; el orden está condicionado por seguridad, no por apariencia visual.

> Este archivo conserva el roadmap funcional de la aplicación. La solicitud “ARES — Fase 2” abrió en paralelo el workstream del sistema operativo, desglosado como OS0–OS8 en [ares-os-roadmap.md](ares-os-roadmap.md). Implementar el sustrato Live ahora no declara cerradas las fases funcionales ni la futura integración completa de producto.

## Fase 0 — Fundamentos (en curso)

Entregables:

- arquitectura, threat model, datos, API, agente/tools, frontend y Live;
- ADR de monolito, privilegios y aprobaciones;
- backend arrancable con configuración, SQLite, request IDs, errores y salud;
- CI/calidad base.

Puerta: tests y análisis estático verdes; documentación diferencia claramente diseño e implementación.

## Fase 1 — Identidad y auditoría

- Bootstrap local sin credenciales predeterminadas.
- Sesiones opacas, Argon2id, CSRF, roles y reautenticación.
- Migraciones Alembic y repositorios.
- `ares-audit-writer`, ledger HMAC, proyección SQLite y checkpoints exportables.

Puerta: matriz de autenticación/RBAC/CSRF, revocación y atomicidad cubierta; ninguna acción importante fuera de auditoría.

## Fase 2 — Vertical slice de solo lectura

- Contrato, registry, grants y policy engine.
- `storage.list_block_devices@1` end-to-end.
- Inventario/snapshot, evidencias, timeouts, límites y SSE.
- UI mínima de login, overview y discos, sin Ollama.

Puerta: funciona con backend real; inyección, herramienta desconocida, falta de grant, salida hostil y cambio de identidad se bloquean.

## Fase 3 — Agente local

- Perfiles Ollama por digest y detección de capacidades.
- Orquestador acotado, structured outputs, citas de evidencia y modelo falso.
- Chat, hipótesis y diagnóstico; nunca reparación.

Puerta: suites deterministas demuestran que el modelo no ejecuta, aprueba ni afirma resultados sin evidencia; operación útil con Ollama caído.

Estado parcial: ya existe el adaptador loopback, estado de runtime/modelo, chat
sin tools y UI degradable. Todavía faltan el pack redistribuible, modelo por
digest, orquestador/evidencias y la puerta determinista completa.

## Fase 4 — Broker y consentimiento independiente

- Protocolo por socket Unix, peer credentials y manifiesto.
- Desafíos autenticados por el broker, locks, cancelación y unidades aisladas.
- `ares-consent-agent` por TTY seguro; la API no puede fabricar grants ni `APPROVED`.
- Una reparación simulada con aprobación exacta y verificador.
- UI completa de riesgo/rechazo/expiración.

Puerta: replay, CSRF, doble clic, sustitución de parámetros/destino, hotplug y concurrencia fallan cerrado.

## Fase 5 — Reportes y frontend operativo

- Historial, logs, hardware, acciones y reportes técnico/ejecutivo.
- Exportación local redactada.
- Responsive, oscuro, i18n y WCAG 2.2 AA.

Puerta: Playwright y axe pasan flujos críticos; cada conclusión de reporte conserva evidencia.

## Fase 6 — Primera ISO integrada de producto

- integrar los `.deb` del producto y el APT local firmado sobre la base Debian Live iniciada en el workstream ARES OS;
- Runtime Python 3.12 aislado y modelo pequeño.
- Sesión efímera y persistencia cifrada opcional.

Puerta: arranca BIOS/UEFI sin red, no monta/escribe discos anfitriones y pasa QEMU/smoke/security checks.

## Fase 7 — Herramientas de recuperación

Incorporar una familia por iteración:

1. inventario, SMART y logs;
2. copias/rsync con destinos internos;
3. comprobación y reparación de filesystems;
4. GRUB BIOS/UEFI;
5. recuperación con testdisk;
6. memoria y red.

Puerta por herramienta: contrato, parser/fixtures, política, target identity, preflight, sandbox, lock, cancelación, verificador, auditoría, documentación y pruebas VM con imágenes desechables.

## Fase 8 — Release candidate

- Secure Boot, packs de modelos, SBOM, firmas y procedencia.
- Matriz de hardware físico y recuperación ante corte/desconexión.
- Auditoría de seguridad, licencias y proceso de rollback de release.

Puerta: no hay riesgos críticos abiertos y la documentación de instalación, uso, desarrollo y recuperación coincide con el artefacto firmado.
