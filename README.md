# Wyss Sync

Panel y herramientas locales para supervisar Syncthing, comparar aplicaciones y registrar operaciones entre las PCs del propietario.

## Tecnologías y arquitectura

El panel está construido en **Python 3** y **PyQt6**. El paquete `wyss_sync/` separa la interfaz de `core.py`, que consulta la configuración y la API local de Syncthing y reúne información de estado. Las herramientas de terminal usan **Bash** y **SSH** para operaciones y consultas entre equipos; requieren configuración local previa.

La interfaz presenta información de sincronización y operaciones existentes; no reemplaza a Syncthing ni transporta por sí misma los archivos. Las pruebas del repositorio cubren componentes Python y scripts de shell.

## Contenido

- `wyss-sync.py` y `wyss_sync/`: panel gráfico.
- `sync_status.py` y scripts `sync-*.sh`: estado y herramientas de sincronización.
- Tests Python y shell existentes.

## Uso

Requiere Python 3, PyQt6 para la interfaz y Syncthing configurado localmente. Los scripts conservan rutas específicas de las máquinas originales; revisalas antes de usarlos en otro entorno.

La comparación entre equipos necesita un alias SSH configurado en cada máquina:

```bash
export WYSS_SYNC_PEER=otro-equipo
# Opcional: dirección o alias alternativo del mismo equipo.
export WYSS_SYNC_PEER_FALLBACK=192.0.2.10
```

Para agregar operaciones con `sync-ops.sh --add`, definí además `SYNC_OPS_DESCENDING_HOST` con el nombre del equipo que utiliza el rango descendente. Usá el mismo valor en ambos equipos. Sin esa configuración, el alta se rechaza para evitar IDs duplicados. La modificación requiere configurar de nuevo el entorno local antes de usar estas funciones.

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
