# Manual de usuario

## Capacidades disponibles hoy

La versión actual es una fundación técnica, no una herramienta de recuperación utilizable. Solo expone comprobaciones de salud del backend. No puede inspeccionar ni reparar discos todavía.

## Experiencia prevista

1. Arrancar ARES desde USB.
2. Elegir idioma y almacenamiento efímero o persistente cifrado.
3. Crear/iniciar la sesión local y conceder herramientas de lectura seleccionadas.
4. Describir el problema o abrir inventario, discos, SMART y logs.
5. Revisar evidencias e hipótesis del asistente.
6. Leer cualquier propuesta, destino, riesgo y verificador.
7. Pedir la confirmación segura y aprobar/reautenticar solo en el TTY dedicado de ARES; también puedes rechazar o dejar expirar.
8. Seguir ejecución y verificación.
9. Exportar reporte técnico o resumen ejecutivo.

## Significado de los modos

- **Solo lectura**: ARES observa; es el modo inicial recomendado.
- **Reparación**: permite proponer cambios, pero cada uno requiere aprobación.
- **Avanzado**: expone operaciones críticas a administradores y exige controles reforzados.

Cambiar de modo no autoriza una acción. El usuario conserva siempre la decisión por operación.

## Cómo leer una aprobación

Comprueba en el TTY seguro identidad del disco (marca/modelo, capacidad, serial/WWN), acción y versión, parámetros, datos potencialmente afectados, backup/rollback, cancelación y verificación. `/dev/sda` no basta para identificar un disco. Si un dispositivo se desconecta o los datos cambian, ARES invalida la propuesta. El panel web informa, pero no puede emitir por sí mismo una aprobación de escritura. Nunca escribas credenciales de reparación en una página web.

Nunca apruebes una acción que no comprendas. Rechazar no borra el diagnóstico; permite pedir una explicación o alternativa.

## Límites

ARES reduce riesgo, no garantiza recuperar datos ni sustituye un backup. Fallos físicos, cortes de energía y filesystems dañados pueden empeorar durante una reparación. Para datos irremplazables, prioriza una imagen/copia y ayuda profesional antes de modificar el original.

La sesión Live será efímera por defecto. Exporta reportes o activa persistencia cifrada explícitamente si necesitas conservar el historial.
