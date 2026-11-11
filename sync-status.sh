#!/usr/bin/env bash
# ============================================================
#  sync-status.sh — Dashboard de sincronización entre las 2 PCs
#  Muestra 3 capas: APPS · ARCHIVOS · AJUSTES
#  Uso:  sync-status          (HDD Data usa la caché de 10 min)
#        sync-status --deep   (recorre HDD Data: ~1 min)
#  Toda la lógica vive en sync_status.py (con pruebas en test_sync_status.py).
# ============================================================
exec python3 -B "$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)/sync_status.py" "$@"
