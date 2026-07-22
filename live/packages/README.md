# Paquetes offline

`debs/` puede contener paquetes locales solo en builds de desarrollo. Releases usarán un repositorio APT local, firmado y fijado, para resolver dependencias y procedencia. `models/` alojará packs por digest/licencia cuando OS6 los habilite. No se guarda ninguna clave privada aquí.
