# Diseño del frontend

> Estado: existe una primera interfaz estática sin dependencias externas,
> servida por FastAPI y con resumen, estado de IA/chat local y ejecución
> explícita de la capability pasiva Disk Analysis. La SPA
> React/TypeScript descrita abajo sigue siendo el diseño objetivo.

## Enfoque

La interfaz será una consola de recuperación guiada, no un chat genérico. El chat explica y coordina; la evidencia, el plan, los riesgos y el estado de cada acción son elementos de primer nivel.

La SPA React/TypeScript/Tailwind se compila a assets estáticos que FastAPI sirve bajo el mismo origen en producción. No usa CDN, fuentes remotas, telemetría ni service worker. Esto simplifica CORS, sesión y funcionamiento offline.

La interfaz incorporada en la ISO actual usa HTML/CSS/JavaScript nativo para
evitar Node y dependencias de red en esta etapa. Arranca directamente desde el
autostart XDG de Xfce, espera readiness del backend y abre Firefox en kiosco. Si
el API no arranca, muestra una página local degradada en vez de dejar una
pantalla vacía. El texto del modelo se inserta únicamente con `textContent`.
La vista ARES v2 consulta el catálogo, inicia únicamente
`storage.disk-analysis` con un input cerrado y presenta resumen, hallazgos y
revisión del Knowledge Graph. No acepta rutas o comandos.

## Layout

En escritorio:

- navegación izquierda;
- conversación/línea temporal central;
- evidencia, plan y aprobación pendiente a la derecha.

En pantallas pequeñas, el panel derecho se presenta como drawer. La cabecera mantiene visibles estado local/offline, modo operativo, modelo/Ollama y operación activa.

Rutas previstas:

```text
/setup                 /login
/overview              /assistant
/cases/:caseId         /hardware
/storage               /storage/:deviceId
/logs                  /actions/:actionId
/history/:runId        /reports/:reportId
/settings              /settings/models
/support
```

## Organización

```text
frontend/src/
├── app/                 # router, providers, layout, error boundaries
├── api/                 # cliente OpenAPI, SSE y Problem Details
├── components/ui/       # primitivas accesibles
├── features/
│   ├── auth/
│   ├── assistant/
│   ├── inventory/
│   ├── storage/
│   ├── logs/
│   ├── actions/
│   ├── history/
│   ├── reports/
│   └── settings/
├── hooks/
├── lib/
├── styles/
└── test/
```

React Router gestiona rutas y filtros persistibles en URL. TanStack Query mantiene estado remoto; Context/useReducer se reserva para sesión visual, tema e idioma. No se duplica el estado del servidor en un store global. Un cliente TypeScript se genera desde OpenAPI.

REST cubre consultas/mutaciones; SSE transmite mensajes completados, progreso y eventos. Se reanuda con `Last-Event-ID` y se rehidrata desde el historial. No hay actualizaciones optimistas para aprobación, reparación o cancelación. Evidencias y tokens no se guardan en `localStorage`.

## Aprobaciones

`ApprovalSheet` muestra información generada desde el plan confiable:

- herramienta y versión;
- identidad estable y legible del destino; `/dev/sdX` es secundario;
- parámetros normalizados;
- datos afectados y posible pérdida;
- evidencia que motiva la propuesta;
- duración, fase cancelable y verificador;
- backup/rollback disponible o su ausencia;
- caducidad del desafío.

El botón web de una mutación dice “Preparar confirmación segura”: solicita el desafío y enseña al usuario a pulsar la combinación de VT reservada, que Firefox no puede consumir. La aprobación/reautenticación y, en alto riesgo, el identificador corto se introducen en ese TTY; logind retira input al kiosco. El navegador nunca recibe el nonce/clave. El grant inicial de lectura usa el mismo canal.

El control destructivo no recibe autofoco y Enter accidental no confirma. Rechazar/cancelar es tan visible como continuar. El frontend no decide si una acción está permitida; backend, consent agent y broker revalidan. La UI advierte que las credenciales solo se introducen en el TTY seguro de ARES, nunca en el navegador.

## Estados y degradación

La UI representa todos los estados de la máquina de invocación y no traduce “proceso terminó” como “reparación exitosa”. Ollama, API, SQLite y broker tienen indicadores separados. Si Ollama falla, inventario, historial, reportes y herramientas manuales autorizadas continúan disponibles.

El Markdown del modelo se sanitiza, HTML crudo está deshabilitado y una respuesta del chat nunca genera navegación o mutación automática.

## Accesibilidad

Objetivo WCAG 2.2 AA:

- landmarks, enlace para saltar al contenido y teclado completo;
- foco visible y restaurado al cerrar diálogos;
- objetivos táctiles de al menos 44 px;
- riesgo expresado por texto e icono, no solo color;
- mapa de particiones acompañado de tabla textual;
- `aria-live` para estados agregados, no cada token del streaming;
- `prefers-reduced-motion`, alto contraste y zoom al 200 %;
- errores persistentes junto al control; los toasts no son el único canal;
- español e inglés, tamaños IEC y fechas localizadas.

## Pruebas

Vitest + Testing Library cubren unidades e integración; MSW simula la API; axe comprueba accesibilidad; Playwright recorre setup, login, diagnóstico, aprobación/rechazo, expiración y reporte. El contrato OpenAPI se valida en CI y los casos de doble clic, replay, reconexión SSE y cambio de dispositivo son obligatorios.
