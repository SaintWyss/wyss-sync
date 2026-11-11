#!/usr/bin/env bash
# apps-diff.sh — Paquetes explícitos de ESTA PC contra la otra, en vivo por ssh.
# Si la otra PC no responde, usa su último snapshot (pkgs-<host>.txt, que sync-status refresca en cada corrida).
# Uso: apps-diff.sh
set -uo pipefail
DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
HOST="$(hostname)"
if [ "$HOST" = "Santiago-PC" ]; then OTHER="saint-acer"; ALIAS=(saint-acer 192.168.100.5)
else OTHER="Santiago-PC"; ALIAS=(santiago-pc 192.168.100.18); fi
MINE="$DIR/pkgs-$HOST.txt"; THEIRS="$DIR/pkgs-$OTHER.txt"

pacman -Qqe | sort > "$MINE.tmp"
if ! cmp -s "$MINE.tmp" "$MINE" 2>/dev/null; then mv "$MINE.tmp" "$MINE"; date -Iseconds > "$DIR/pkgs-$HOST.stamp"; else rm -f "$MINE.tmp"; fi
echo "snapshot: $(wc -l < "$MINE") paquetes explícitos ($HOST)"

LIVE="$(mktemp)"; trap 'rm -f "$LIVE"' EXIT
SRC=""
for h in "${ALIAS[@]}"; do
  if ssh -o BatchMode=yes -o ConnectTimeout=4 "$h" 'pacman -Qqe' 2>/dev/null | sort > "$LIVE" && [ -s "$LIVE" ]; then SRC="en vivo ($h)"; THEIRS="$LIVE"; break; fi
done
if [ -z "$SRC" ]; then
  [ -f "$THEIRS" ] || { echo "no alcanzo a $OTHER y no hay snapshot ($THEIRS)"; exit 0; }
  SRC="snapshot del $(cat "$DIR/pkgs-$OTHER.stamp" 2>/dev/null || echo 'fecha desconocida')"
fi
echo "otra PC: $SRC"

# ponytail: filtro = apps-ignore.txt, misma fuente de verdad que sync-apps y sync-status
IGN="$DIR/apps-ignore.txt"
flt() { [ -f "$IGN" ] && grep -vEf <(grep -vE '^\s*#|^\s*$' "$IGN") || cat; }

echo -e "\n== solo en $HOST (falta en $OTHER) =="
comm -23 "$MINE" "$THEIRS" | flt
echo -e "\n== solo en $OTHER (falta en $HOST) =="
comm -13 "$MINE" "$THEIRS" | flt
