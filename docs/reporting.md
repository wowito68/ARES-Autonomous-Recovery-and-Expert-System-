# Reportes y trazabilidad de evidencia

> Estado: diseño objetivo; la generación de reportes llega después de identidad, herramientas y auditoría.

## Tipos

El mismo snapshot inmutable alimenta dos presentaciones:

- **Reporte técnico**: equipo/fingerprint, inventario, caso, timeline, herramientas/versiones, parámetros redactados, evidencias, resultados, errores, verificaciones, duraciones, hashes de artefactos y recomendaciones técnicas.
- **Resumen ejecutivo**: estado general, problemas confirmados, impacto, acciones autorizadas/realizadas, resultado verificable, riesgos pendientes y próximos pasos en lenguaje claro.

Ambos incluyen:

1. estado del equipo al capturar el reporte;
2. problemas confirmados, hipótesis no confirmadas y datos desconocidos en secciones separadas;
3. acciones propuestas, aprobadas, rechazadas y ejecutadas;
4. evidencia y `invocation_id` detrás de cada afirmación;
5. recomendaciones, prioridad y condiciones;
6. fecha, versión de ARES, perfil/digest del modelo y versión del schema;
7. aviso de sesión efímera/persistente y límites del diagnóstico.

## Generación

El backend construye primero un `ReportSnapshot` estructurado desde la base y ArtifactStore. El LLM puede redactar una explicación usando únicamente ese snapshot, pero no añadir hechos. Un validador comprueba que las referencias existen. Toda inferencia se etiqueta y cita las evidencias de las que deriva; una frase sin base se elimina.

Las acciones y resultados no se regeneran desde la conversación. Se obtienen de registros inmutables. Si el modelo no está disponible, ARES produce igualmente un reporte determinista.

## Formatos y seguridad

Formato canónico JSON versionado; vistas HTML imprimible y, posteriormente, PDF. No se inserta HTML del modelo. Antes de exportar, el usuario elige perfil de redacción: completo local, soporte técnico o compartible. Seriales, usernames, rutas, IP y fragmentos de logs se redactan según el perfil.

Cada exportación incluye SHA-256 y lista de artefactos. Una futura firma usa una clave local explícitamente creada por el usuario; el hash por sí solo detecta corrupción accidental, no autentica al autor.

No hay subida, correo ni telemetría automática. Guardar en otro disco es una acción de escritura con selección y autorización explícitas.

## Disparadores

ARES crea automáticamente una nueva versión inmutable cuando un diagnóstico pasa a `DIAGNOSIS_READY`, cuando una reparación alcanza un estado terminal después de verificación y al cerrar el caso. El usuario también puede solicitar una versión manual. Un cambio posterior nunca sobrescribe un reporte: crea otro snapshot con su propio digest y referencias.
