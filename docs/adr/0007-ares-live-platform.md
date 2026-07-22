# ADR-0007: plataforma Debian Live y ejes de ejecución

- Estado: aceptado
- Fecha: 2026-07-22

## Contexto

ARES necesita arrancar desde USB, operar offline y proteger discos anfitriones antes de incorporar agente o reparación. La palabra “modo” estaba mezclando operación, persistencia, red, autorización e integridad. También era fácil afirmar Secure Boot completo cuando solo estaban firmados shim, GRUB y kernel.

## Decisión

1. Construir con Debian 13 `trixie`, `live-build` fijado, snapshots APT y un entorno OCI por digest.
2. Usar ISO híbrida: ISOLINUX/SYSLINUX para BIOS y shim + GRUB para UEFI.
3. Usar SquashFS/dm-verity como lower y tmpfs/OverlayFS como upper predeterminado.
4. Separar operación (`live`, `forensic`, `recovery`), retención (`ephemeral`, `persistent`), red (`off`, `scoped`), confianza de boot y techo de autorización.
5. Deshabilitar persistencia genérica. Solo se admitirán datos ARES en LUKS2 inequívocamente ligada al mismo USB y creada por el provisionador oficial.
6. Mantener red, NTP, swap, resume, GPT auto-discovery y automount apagados por defecto.
7. Declarar la cadena Debian actual `ENFORCED_PARTIAL`; `ENFORCED_FULL` requiere UKI y root hash autenticado.
8. Definir servicios futuros con condiciones de artefacto, sin procesos stub que simulen producto implementado.
9. Ejecutar builds de release en una VM desechable: el contenedor privilegiado es una comodidad reproducible, no un sandbox.

## Consecuencias

- El reinicio restaura una raíz limpia y la persistencia no puede secuestrarse por una etiqueta de disco.
- La primera ISO funcional puede existir antes que backend/agente/frontend, sin confundir plataforma con producto.
- Persistencia, UKI, mirror offline y matriz física exigen hitos independientes.
- “Forensic” significa escritura minimizada por software; evidencia certificada exige write blocker físico y procedimiento pericial.
- Incluir firmware no libre activa revisión de licencias y tamaño.
