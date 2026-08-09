# Roadmap incremental

Una fase solo se cierra cuando pasa su puerta de salida. El orden está condicionado por seguridad y evidencia verificable, no por apariencia visual.

El workstream ARES OS permanece desglosado en [ares-os-roadmap.md](ares-os-roadmap.md). La existencia de una Capability funcional no declara implementadas las futuras fronteras privilegiadas ni la release ISO completa.

## Incremento completado — primer vertical slice funcional

`storage.disk-analysis@1.1.0` demuestra el recorrido:

```text
API/CLI/UI
-> Application Service
-> Reasoning Engine
-> Capability Manager
-> Workflow Engine
-> Tool Layer
-> SystemStorageSnapshot
-> Knowledge Graph
-> DiagnosticResult
```

Incluye probes pasivos `lsblk`, `findmnt` y `df`; degradación estructurada de `smartctl`/`blkid`; snapshot persistido; Knowledge Graph; Event Bus; diagnóstico determinista; API Storage; CLI; UI mínima y pruebas sin hardware real.

No incluye mutaciones, broker privilegiado operativo, consentimiento independiente ni ledger criptográfico.

## Fase 0 — Fundamentos

Entregables existentes:

- arquitectura, threat model, API, datos y Live OS;
- FastAPI/SQLite, configuración, request IDs, Problem Details y health;
- CI Python 3.12/3.13, Ruff, mypy estricto y build;
- plataforma v2 de Capabilities, Workflow Engine, Event Bus y Knowledge Graph.

Puerta pendiente para cerrar formalmente la fase global: alinear toda la documentación histórica y los paquetes de distribución con el runtime v2.

## Fase 1 — Identidad y auditoría autoritativa

- Bootstrap local sin credenciales predeterminadas.
- Sesiones opacas, CSRF, roles y reautenticación.
- Migraciones Alembic y repositorios de historial.
- `ares-audit-writer`, ledger HMAC, proyección SQLite y checkpoints exportables.

Puerta: matriz de autenticación/RBAC/CSRF, revocación y atomicidad cubierta; ningún evento crítico fuera del writer autoritativo.

## Fase 2 — Observación Storage segura

Estado: **parcialmente implementada mediante `storage.disk-analysis@1.1.0`**.

Implementado:

- Capability registration/discovery y output tipado;
- Tool Layer read-only con argv fijo;
- parsers `lsblk`, `findmnt`, `df`, `blkid` y SMART JSON;
- snapshot, KG, eventos, diagnóstico, API, CLI y UI;
- fixtures/mocks; no dependencia de disco real;
- cobertura global superior al gate de 85%.

Pendiente para cerrar la fase:

- integrar lectura SMART/blkid privilegiada a través de `ares-tool-broker` sin cambiar el contrato de `storage.disk-analysis`;
- identidad estable con udev/by-id/major:minor y protección explícita del medio Live;
- historial/retención de snapshots y diagnósticos;
- SSE/progreso si se necesita una ejecución más larga;
- pruebas de imagen/VM que demuestren ausencia de escrituras a block devices.

Puerta: la Capability funciona offline sobre la imagen ARES, la identidad se revalida y una suite de sistema confirma cero operaciones de escritura.

## Fase 3 — Agente local sobre evidencia

- Perfiles Ollama por digest y structured outputs.
- Orquestador acotado con presupuesto, hipótesis, evidence IDs y modelo falso determinista.
- Chat que consume `DiagnosticResult`/Knowledge Graph sin acceso directo a Tools.

Estado parcial: ya existe adaptador loopback y chat sin Tools; el vertical slice añade razonamiento determinista de Storage. Falta integrar el agente conversacional con evidencia versionada sin permitir autoejecución.

Puerta: suites deterministas demuestran que el modelo no ejecuta, aprueba ni afirma resultados sin evidencia; ARES sigue siendo útil con Ollama caído.

## Fase 4 — Broker y consentimiento independiente

- Socket Unix autenticado, peer credentials y manifiesto root-owned.
- Implementar primero una **lectura privilegiada** de SMART/blkid para validar la frontera sin introducir escritura.
- Luego desafíos del broker, locks, cancelación y `ares-consent-agent` para mutaciones futuras.
- Reparación simulada con verificador antes de tocar dispositivos reales.

Puerta: replay, sustitución de parámetros/destino, hotplug, concurrencia y fallos de auditoría cierran de forma segura.

## Fase 5 — Historial y frontend operativo

- Historial de snapshots/diagnósticos y comparación temporal.
- Reportes técnicos/ejecutivos con evidence IDs.
- Exportación local redactada.
- Responsive, i18n y WCAG 2.2 AA.

Puerta: pruebas E2E de flujos críticos y trazabilidad completa desde conclusión a evidencia.

## Fase 6 — ISO integrada

- Paquetes del producto y APT local firmado sobre Debian Live.
- Runtime Python aislado y modelo local pequeño.
- Persistencia cifrada opcional.

Puerta: BIOS/UEFI, operación offline, no automount/escritura de discos anfitriones y smoke/security checks en QEMU.

## Fase 7 — Capabilities de recuperación

Una familia por incremento, sin convertir `storage.disk-analysis` en una Capability mutable:

1. SMART privilegiado e inventario de identidad;
2. backup con destino administrado;
3. filesystem check read-only;
4. filesystem repair con verifier;
5. recuperación de archivos;
6. boot repair;
7. memoria/red.

Puerta por Capability: modelos tipados, target identity, policy, broker, preflight, lock, cancelación, rollback/backup cuando aplique, verifier, auditoría, docs y pruebas en VM/imágenes desechables.

## Fase 8 — Release candidate

- Secure Boot, SBOM, firmas y procedencia.
- Matriz de hardware físico y recuperación ante corte/desconexión.
- Auditoría de seguridad, licencias y rollback de release.

Puerta: sin riesgos críticos abiertos y documentación alineada con el artefacto firmado.

## Siguiente incremento recomendado

**Integrar SMART/blkid mediante la frontera privilegiada existente, todavía en modo OBSERVE/READ_ONLY.**

Es el siguiente paso más seguro porque valida `ares-tool-broker`, identidad de dispositivos y evidencia privilegiada sin introducir ninguna mutación. Debe reutilizar `SmartProbe`, `BlkidProbe`, `SystemStorageSnapshot` y `DiagnosticResult`, no crear un camino paralelo.
