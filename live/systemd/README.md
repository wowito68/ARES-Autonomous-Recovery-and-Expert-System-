# systemd

Las unidades canónicas están bajo `config/includes.chroot/usr/lib/systemd/`. `ares.target` separa preflight, autoridad y UI. Binarios futuros usan condiciones de existencia y no se reemplazan por stubs que simulen funcionalidad.
