# Wyss Sync

Panel y herramientas locales para supervisar Syncthing, comparar aplicaciones y registrar operaciones entre las PCs del propietario.

## Contenido

- `wyss-sync.py` y `wyss_sync/`: panel gráfico.
- `sync_status.py` y scripts `sync-*.sh`: estado y herramientas de sincronización.
- Tests Python y shell existentes.

Requiere Python 3, PyQt6 para la interfaz y Syncthing configurado localmente. Los scripts conservan las rutas específicas de las máquinas originales; revisar antes de usarlos en otro entorno.

Este repositorio conserva el código. No incluye credenciales, estado de Syncthing, registros de operaciones, inventarios de paquetes, respaldos ni el historial operativo.
