# ADR-0001: Monolito modular para el plano de control

- Estado: aceptada
- Fecha: 2026-07-22

## Contexto

ARES vive en un único equipo Live, debe funcionar offline y necesita consumo bajo, instalación reproducible y transacciones locales coherentes.

## Decisión

La API, los casos de uso, el agente y los adaptadores se entregan como un monolito modular Python. Las fronteras se imponen mediante paquetes, puertos y pruebas de arquitectura. El ejecutor privilegiado sí es otro proceso porque pertenece a una frontera de confianza distinta.

## Consecuencias

- Menor coste operativo y de empaquetado que microservicios.
- Transacciones y diagnósticos más fáciles de razonar.
- Los módulos deben respetar reglas de importación para no convertirse en una masa acoplada.
- Una futura separación de proceso es posible a través de los puertos existentes, pero no es un objetivo inicial.
