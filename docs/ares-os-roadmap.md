# Roadmap de ARES OS

Este workstream corresponde a **ARES — Fase 2: construcción del sistema operativo Live**. Los hitos son acumulativos; una ISO que arranca no equivale a un release. Cada hito se cierra solo con su puerta de aceptación.

## OS0 — Contrato de plataforma y threat model

**Objetivos:** fijar alcance, invariantes, cadena de boot, ejes de modo, fronteras de privilegio y estados honestos de integridad.

**Entregables:** arquitectura Live, diagramas Mermaid, ADR, árbol de repositorio, catálogo de servicios, modelo offline y matriz de riesgos.

**Aceptación:** BIOS/UEFI, overlay, persistencia, cuentas, red y degradación tienen una autoridad única y no existen contradicciones entre docs de arquitectura/seguridad.

**Riesgos:** confundir Secure Boot con integridad completa; llamar forense a una protección solo software; mezclar modo, retención y autorización.

**Dependencias:** arquitectura y threat model de ARES Fase 1.

**Estado:** implementado en documentación; revisión continua.

## OS1 — Build automatizado y reproducible

**Objetivos:** generar `ARES.iso` con una sola orden desde entradas fijadas y sin modificar manualmente el chroot.

**Entregables:** `Makefile`, constructor Debian por digest, snapshots APT, `auto/*`, package lists, hooks, includes, manifest, checksum y lista de paquetes.

**Aceptación:** `make validate-config` pasa; `make build-iso` genera los cuatro artefactos; dos builds limpios son byte-idénticos; se registran hashes de índices y `.deb`; build de release opera desde mirror firmado sin egress.

**Riesgos:** timestamps residuales, mirror incompleto, host contaminando el build, contenedor privilegiado sobre código no confiable.

**Dependencias:** snapshot Debian disponible y entorno amd64 con virtualización/contenedores.

**Estado:** infraestructura implementada; igualdad bit a bit y mirror local son puertas pendientes.

## OS2 — Boot híbrido, sesión y branding

**Objetivos:** arrancar la misma imagen como ISO/USB en BIOS y UEFI, mostrar identidad ARES y abrir una sesión local sin pasos manuales.

**Entregables:** ISOLINUX/SYSLINUX, shim/GRUB EFI, kernel/initramfs, Plymouth, LightDM, Xfce, wallpaper, icono, MOTD, hostname, `os-release`, pantalla provisional.

**Aceptación:** SeaBIOS y OVMF llegan a UI; opción segura usa `nomodeset`; prompts de error/LUKS siguen visibles; todos los textos muestran versión/build correctos; no hay consola root/autologin TTY.

**Riesgos:** divergencia de menús, Plymouth ocultando prompts, gráficos incompatibles, CA Secure Boot ausente en firmware.

**Dependencias:** OS1 y paquetes firmados de shim/GRUB/kernel.

**Estado:** configurado; pruebas de ISO y hardware pendientes.

## OS3 — Inventario y compatibilidad de hardware

**Objetivos:** publicar un perfil inicial útil en menos de 15 segundos, sin escribir ni despertar dispositivos.

**Entregables:** schema v1, vistas privada/redactada, CPU/RAM/GPU/storage/network/USB/EFI/radios/power/sensors/drivers, estados parciales y timeouts; hot-plug incremental; helper privilegiado separado para SMART/DMI/NVMe.

**Aceptación:** JSON válido/atómico, salida acotada, redacción estable por boot, API arranca con inventario parcial, ningún probe monta/escanea Wi-Fi/inicia GPU/despierta discos; hot-plug actualiza generación.

**Riesgos:** firmware no confiable, puentes USB bloqueados, comandos lentos, seriales filtrados, cobertura de hardware nuevo.

**Dependencias:** OS2, udev y matriz de firmware/licencias.

**Estado:** snapshot inicial implementado; helper y hot-plug pendientes.

## OS4 — Modos, protección del host y persistencia

**Objetivos:** hacer verificables Live efímero, forensic write-minimized, recovery y persistencia cifrada selectiva.

**Entregables:** protección swap/resume/GPT/automount, reglas RO para hot-plug, monitor de overlay, provisionador USB, LUKS2, descriptor ligado al medio, cuotas, fallback y desconexión segura.

**Aceptación:** discos canario conservan hash en Live/forensic; labels/UUID duplicados fallan cerrado; clave incorrecta, volumen lleno/corrupto/futuro no se reparan; solo rutas ARES persisten; retirada del USB detiene operaciones.

**Riesgos:** journal replay, firmware que escribe internamente, selección ambigua, pérdida de recovery key, desconexión durante `fsync`.

**Dependencias:** OS2, OS3 e identidad estable del medio Live.

**Estado:** efímero y write-minimized inicial implementados; persistencia permanece bloqueada hasta el provisionador.

## OS5 — Plano systemd y paquetes offline

**Objetivos:** convertir la plataforma en paquetes `.deb` firmados y levantar servicios con degradación controlada.

**Entregables:** `ares-live-config.deb`, APT local firmado, cuentas estáticas, sockets, targets, backend/frontend empaquetados, runtime Python 3.12, docs, SBOM y licencias.

**Aceptación:** `systemd-analyze verify/security`; kill/restart de cada servicio no eleva privilegios; API/UI útiles sin LLM; cero listeners externos; ninguna instalación o descarga en boot.

**Riesgos:** includes sobrescribiendo dpkg, UID drift, permisos de sockets, runtime duplicado, dependencias no redistribuibles.

**Dependencias:** OS1–OS4 y artefactos bloqueados de cada componente.

**Estado:** grafo/unidades preparados; binarios de producto fuera del alcance actual.

## OS6 — Packs de modelos y operación offline completa

**Objetivos:** integrar LLM local sin convertirlo en dependencia crítica ni introducir egress.

**Entregables:** runtime fijado, catálogo firmado, modelo CPU pequeño, packs opcionales, validación de licencia/digest/RAM/VRAM, carga diferida y límites OOM.

**Aceptación:** operación útil sin NIC; captura de cero egress; modelo corrupto/no licenciado no carga; UI funciona con runtime caído; RAM limitada mata LLM antes que auditoría/broker.

**Riesgos:** tamaño ISO, licencias, instrucciones no confiables del modelo, drivers GPU, tiempo de carga.

**Dependencias:** OS5 y agente determinista probado en el roadmap de aplicación.

**Estado:** diseñado; no implementado en Fase 2.

## OS7 — Integridad completa y Secure Boot ARES

**Objetivos:** autenticar kernel, initrd, cmdline, root hash y userspace como una cadena verificable.

**Entregables:** UKI ARES firmada, política de claves/MOK/plataforma, dm-verity root hash firmado, estados `ENFORCED_FULL/FAILED`, firma de ISO, SBOM y procedencia.

**Aceptación:** alterar shim, loader, UKI, cmdline, rootfs, manifest o modelo provoca el estado/bloqueo esperado; pruebas con dbx y variantes de CA; ninguna autoridad inicia ante fallo.

**Riesgos:** rotación/revocación de claves, firmware sin CA de terceros, initrd mutable, recuperación de claves.

**Dependencias:** OS5, PKI de release y revisión externa de seguridad.

**Estado:** Secure Boot Debian y verity parcial configurados; cadena completa pendiente.

## OS8 — Rendimiento, fault injection y release candidate

**Objetivos:** demostrar comportamiento profesional en VM y hardware diverso.

**Entregables:** presupuestos boot/RAM/CPU/ISO, perfiles XZ/Zstd medidos, matriz física, pruebas USB 2/3, fault injection, runbooks, rollback/revocación y notas de release.

**Aceptación:** puertas OS0–OS7 cerradas; tiempos y memoria dentro del presupuesto publicado; cortes/desconexiones no reintentan escrituras; cero riesgos críticos; documentación coincide con artefacto firmado.

**Riesgos:** hardware de cola larga, regresiones de firmware, baja RAM, USB lento/defectuoso, diferencias BIOS/UEFI.

**Dependencias:** todos los hitos anteriores y laboratorio de hardware.

**Estado:** pendiente.
