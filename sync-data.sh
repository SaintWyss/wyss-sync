#!/usr/bin/env bash
# ============================================================
#  sync-data.sh — Devuelve el estado de sync en JSON (lo consume sync-gui.py).
#  Mismo motor que sync-status.sh; sin colores, solo datos.
# ============================================================
exec python3 -B "$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)/sync_status.py" --json "$@"
