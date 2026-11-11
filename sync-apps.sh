#!/usr/bin/env bash
# ============================================================
#  sync-apps.sh — Instala en ESTA PC las apps de la wishlist
#  que todavía no estén. No toca nada de hardware.
#  Uso:  ~/Data/00_System/sync-control/sync-apps.sh        (interactivo)
#        ~/Data/00_System/sync-control/sync-apps.sh --dry  (solo muestra)
# ============================================================
set -uo pipefail
DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
WISH="$DIR/apps-wishlist.txt"
DRY=0; [ "${1:-}" = "--dry" ] && DRY=1

c_g='\033[1;32m'; c_y='\033[1;33m'; c_r='\033[1;31m'; c_0='\033[0m'

[ -f "$WISH" ] || { echo -e "${c_r}no existe $WISH${c_0}"; exit 1; }

# AUR helper
AUR=""
for h in paru yay; do command -v "$h" &>/dev/null && { AUR="$h"; break; }; done

mapfile -t WANT < <(sed 's/#.*//' "$WISH" | awk 'NF{print $1}')
MISSING=()
for p in "${WANT[@]}"; do
  pacman -Qq "$p" &>/dev/null || MISSING+=("$p")
done

if [ "${#MISSING[@]}" -eq 0 ]; then
  echo -e "${c_g}✓ Nada que instalar — todas las apps de la wishlist ya están.${c_0}"
  exit 0
fi

echo -e "${c_y}Faltan en esta PC (${#MISSING[@]}):${c_0}"
printf '  %s\n' "${MISSING[@]}"

if [ "$DRY" -eq 1 ]; then echo -e "\n(--dry: no instalo nada)"; exit 0; fi

echo
read -rp "¿Instalar todas? [s/N] " ans
[ "$ans" = "s" ] || [ "$ans" = "S" ] || { echo "cancelado"; exit 0; }

# Separar repo oficial vs AUR
REPO=(); AURPKGS=()
for p in "${MISSING[@]}"; do
  if pacman -Si "$p" &>/dev/null; then REPO+=("$p"); else AURPKGS+=("$p"); fi
done

[ "${#REPO[@]}" -gt 0 ] && { echo -e "${c_y}Repos oficiales:${c_0} ${REPO[*]}"; sudo pacman -S --needed "${REPO[@]}"; }
if [ "${#AURPKGS[@]}" -gt 0 ]; then
  if [ -n "$AUR" ]; then echo -e "${c_y}AUR ($AUR):${c_0} ${AURPKGS[*]}"; "$AUR" -S --needed "${AURPKGS[@]}";
  else echo -e "${c_r}No hay helper AUR (paru/yay). Instalá a mano:${c_0} ${AURPKGS[*]}"; fi
fi
echo -e "${c_g}Listo.${c_0}"
