# Wyss Sync

Panel y herramientas locales para supervisar Syncthing, comparar aplicaciones y registrar operaciones entre las PCs del propietario.

## Contenido

- `wyss-sync.py` y `wyss_sync/`: panel gráfico.
- `sync_status.py` y scripts `sync-*.sh`: estado y herramientas de sincronización.
- Tests Python y shell existentes.

## Uso

Requiere Python 3, PyQt6 para la interfaz y Syncthing configurado localmente. Los scripts conservan rutas específicas de las máquinas originales; revisalas antes de usarlos en otro entorno.

```bash
# Abrir el panel gráfico
python3 wyss-sync.py

# Consultar el estado desde la terminal
bash sync-status.sh
```

Para ejecutar las pruebas del panel en un entorno sin pantalla:

```bash
QT_QPA_PLATFORM=offscreen python3 -m unittest -v test_wyss_sync
```

Este repositorio conserva el código. No incluye credenciales, estado de Syncthing, registros de operaciones, inventarios de paquetes, respaldos ni el historial operativo.
