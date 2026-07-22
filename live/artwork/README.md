# Artwork generado

Las fuentes viven en `live/branding/*.svg`. `scripts/render_branding.sh` genera PNG con `librsvg2-bin` dentro del constructor fijado. `generated/` nunca se versiona para evitar divergencias entre fuente y salida.
