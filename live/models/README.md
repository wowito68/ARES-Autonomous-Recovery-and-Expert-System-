# Paquete offline de IA

ARES puede construir la ISO sin un modelo: la interfaz y el backend quedan
operativos y reportan `runtime_unavailable`. Para una variante con IA, este
directorio acepta un paquete previamente revisado y versionado:

```text
live/models/
├── BUNDLE.json
├── runtime/
│   └── bin/ollama
├── store/
│   ├── blobs/
│   └── manifests/
└── SHA256SUMS
```

`runtime/` y `store/` se ignoran en Git porque contienen binarios y pesos de
gran tamaño. `BUNDLE.json` conserva procedencia, versión, arquitectura y
licencias. `SHA256SUMS` debe enumerar `BUNDLE.json` y todos los archivos
aceptados con rutas relativas a este directorio. El constructor:

1. rechaza el paquete si faltan metadatos o manifiesto;
2. valida campos mínimos de fuente, versión, licencia y arquitectura;
3. rechaza enlaces simbólicos;
4. verifica todos los hashes antes de copiar;
5. exige un `runtime/bin/ollama` ELF amd64 ejecutable;
6. exige el manifiesto Ollama del modelo configurado;
7. instala el runtime como `root` en `/opt/ares/llm/runtime`;
8. instala el almacén como `ares-llm` en `/var/lib/ares/models`;
9. incluye los metadatos auditables en `/usr/share/ares/ai/BUNDLE.json`.

Esquema mínimo:

```json
{
  "schema_version": 1,
  "architecture": "amd64",
  "runtime": {
    "name": "ollama",
    "version": "VERSION_FIJADA",
    "source": "https://FUENTE_PRIMARIA/ARTEFACTO",
    "license": "LICENCIA_SPDX"
  },
  "model": {
    "name": "qwen2.5:1.5b-instruct-q4_K_M",
    "source": "https://FUENTE_PRIMARIA/MODELO",
    "license": "LICENCIA_SPDX"
  }
}
```

El modelo configurado actualmente es
`qwen2.5:1.5b-instruct-q4_K_M`. Su licencia, procedencia, hash y prueba de
compatibilidad deben aprobarse antes de incluir pesos en una imagen de
distribución. No se descarga nada durante el arranque ni durante la
construcción normal.

Para preparar el paquete de desarrollo reproducible:

```sh
make prepare-ai-bundle
```

El comando descarga el runtime Ollama fijado, materializa el modelo en
`store/`, escribe `BUNDLE.json` y regenera `SHA256SUMS`. Los directorios
`runtime/` y `store/` permanecen fuera de Git por su tamaño.

El runtime se ejecuta como `ares-llm`, escucha solo en `127.0.0.1:11434` y la
unidad systemd bloquea cualquier dirección que no sea loopback. El backend no
envía definiciones de herramientas al modelo.
