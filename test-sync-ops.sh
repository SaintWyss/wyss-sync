#!/usr/bin/env bash
# Test de sync-ops: --add (rangos y unión de conflictos), --apply (solo corre lo anterior a ROLLBACK),
# --done, --check y listado con copias de conflicto. Corre en un dir temporal.
set -euo pipefail
export SYNC_OPS_DESCENDING_HOST=pc-a
S="$(dirname "$(readlink -f "$0")")/sync-ops.sh"
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
cp "$S" "$T/"
printf 'id\tfecha\ttipo\tdescripcion\tcomando\thecho_en\n130\t2026-09-24\tconfig\tvieja\tx | ROLLBACK: y\tpc-b\n' > "$T/system-ops.tsv"
if env -u SYNC_OPS_DESCENDING_HOST SYNC_OPS_HOST=pc-a bash "$T/sync-ops.sh" --add config prueba 'x | ROLLBACK: y' >/dev/null 2>&1; then
  echo 'FALLA: se aceptó una operación sin configurar el rango'; exit 1
fi
add() { SYNC_OPS_HOST=$1 bash "$T/sync-ops.sh" --add config "$2" "c | ROLLBACK: r" 2>/dev/null; }
f() { [ "$1" = "$2" ] || { echo "FALLA: $3 — esperaba $2, dio $1"; exit 1; }; }
n=0; ok() { n=$((n+1)); }

f "$(add pc-a a)" 999 "primera del escritorio"; ok
f "$(add pc-a b)" 998 "escritorio baja"; ok
f "$(add pc-b c)" 131 "acer sube desde la última baja"; ok
# Syncthing: la Acer escribió 132 en su copia mientras el escritorio escribía 997
C="$T/system-ops.sync-conflict-20260924-000000-XXXX.tsv"
cp "$T/system-ops.tsv" "$C"
printf '132\t2026-09-24\tconfig\tdesde acer\tc | ROLLBACK: r\tpc-b\n' >> "$C"
# el listado ve la fila que solo está en la copia, sin borrarla
SYNC_OPS_HOST=pc-a bash "$T/sync-ops.sh" | grep -q 'solo en copia de conflicto' || { echo "FALLA: el listado no ve la fila de la copia"; exit 1; }; ok
[ -f "$C" ] || { echo "FALLA: listar no debe borrar la copia"; exit 1; }; ok
f "$(add pc-a d)" 997 "escritorio con conflicto pendiente"; ok
f "$(ls "$T" | grep -c sync-conflict || true)" 0 "copia en conflicto borrada"; ok
f "$(grep -c '^132	' "$T/system-ops.tsv")" 1 "fila de la acer rescatada"; ok
f "$(add pc-b e)" 133 "acer sigue después de la rescatada"; ok
f "$(cut -f1 "$T/system-ops.tsv" | sort | uniq -d | wc -l)" 0 "ids únicos"; ok
if SYNC_OPS_HOST=x bash "$T/sync-ops.sh" --add config d "comando sin reversa" 2>/dev/null; then
  echo "FALLA: aceptó sin ROLLBACK"; exit 1
fi; ok
LONG=$(printf 'x%.0s' $(seq 1 301))
if SYNC_OPS_HOST=x bash "$T/sync-ops.sh" --add config "$LONG" "c | ROLLBACK: r" 2>/dev/null; then
  echo "FALLA: aceptó una descripción de 301 caracteres"; exit 1
fi; ok

# --apply: corre el comando y NO el rollback, ni con "|" ni con ";"
M="$T/marca"; mkdir "$M"
printf 'id\tfecha\ttipo\tdescripcion\tcomando\thecho_en\n' > "$T/system-ops.tsv"
printf '1\t2026-10-10\tconfig\tcon pipe\ttouch %s/hecho1 | ROLLBACK: touch %s/rollback1\tpc-b\n' "$M" "$M" >> "$T/system-ops.tsv"
printf '2\t2026-10-10\tconfig\tcon punto y coma\ttouch %s/hecho2 ; ROLLBACK: touch %s/rollback2 ; touch %s/rollback2b\tpc-b\n' "$M" "$M" "$M" >> "$T/system-ops.tsv"
printf '3\t2026-10-10\tconfig\tsolo registro\t# ya aplicado | ROLLBACK: nada\tpc-b\n' >> "$T/system-ops.tsv"
printf 's\ns\n' > "$T/resp"
SYNC_OPS_HOST=pc-a SYNC_OPS_TTY="$T/resp" bash "$T/sync-ops.sh" --apply >/dev/null 2>&1
[ -f "$M/hecho1" ] && [ -f "$M/hecho2" ] || { echo "FALLA: --apply no corrió los comandos"; ls "$M"; exit 1; }; ok
[ ! -e "$M/rollback1" ] && [ ! -e "$M/rollback2" ] && [ ! -e "$M/rollback2b" ] || { echo "FALLA: --apply ejecutó un ROLLBACK"; ls "$M"; exit 1; }; ok
f "$(awk -F'\t' '$1==1||$1==2||$1==3{print $6}' "$T/system-ops.tsv" | grep -c 'pc-a')" 3 "las tres quedaron marcadas en esta PC"; ok
f "$(SYNC_OPS_HOST=pc-a bash "$T/sync-ops.sh" | grep -c 'Nada pendiente')" 1 "sin pendientes tras aplicar"; ok

# --apply: responder que no no corre nada, y la respuesta no consume filas del registro
printf '4\t2026-10-10\tconfig\tno aplicar\ttouch %s/no | ROLLBACK: x\tpc-b\n' "$M" >> "$T/system-ops.tsv"
printf '5\t2026-10-10\tconfig\totra\ttouch %s/otra | ROLLBACK: x\tpc-b\n' "$M" >> "$T/system-ops.tsv"
printf 'n\ns\n' > "$T/resp"
SYNC_OPS_HOST=pc-a SYNC_OPS_TTY="$T/resp" bash "$T/sync-ops.sh" --apply >/dev/null 2>&1
[ ! -e "$M/no" ] && [ -f "$M/otra" ] || { echo "FALLA: la confirmación se mezcló con las filas"; ls "$M"; exit 1; }; ok

# --done y --check
f "$(SYNC_OPS_HOST=pc-a bash "$T/sync-ops.sh" --done 4 2>&1 | grep -c 'Marcado')" 1 "--done marca"; ok
f "$(awk -F'\t' '$1==4{print $6}' "$T/system-ops.tsv")" "pc-b,pc-a" "--done agrega el host"; ok
if SYNC_OPS_HOST=x bash "$T/sync-ops.sh" --done 999999 >/dev/null 2>&1; then echo "FALLA: --done aceptó un id inexistente"; exit 1; fi; ok
printf '9\t2026-10-10\tconfig\tmal\tx | ROLLBACK: y\tlas 2 PC (nota)\n' >> "$T/system-ops.tsv"
if SYNC_OPS_HOST=x bash "$T/sync-ops.sh" --check >/dev/null 2>&1; then echo "FALLA: --check no detectó hecho_en inválido"; exit 1; fi; ok
SYNC_OPS_HOST=pc-a bash "$T/sync-ops.sh" | grep -q 'hecho_en inválido' || { echo "FALLA: el listado no avisa la fila inválida"; exit 1; }; ok
# --- revisión externa: lo que se ve es lo que se ejecuta
ESC=$(printf '\033')
printf 'id\tfecha\ttipo\tdescripcion\tcomando\thecho_en\n' > "$T/system-ops.tsv"
printf '20\t2026-10-10\tconfig\tdesc con escape literal \\033[31mROJO\\033[0m\ttrue | ROLLBACK: x\tpc-b\n' >> "$T/system-ops.tsv"
printf '21\t2026-10-10\tconfig\tcon control\ttouch %s/escapado%s[8m | ROLLBACK: x\tpc-b\n' "$M" "$ESC" >> "$T/system-ops.tsv"
OUT=$(SYNC_OPS_HOST=pc-a bash "$T/sync-ops.sh" 2>&1)
printf '%s' "$OUT" | grep -qF '\033[31mROJO' || { echo "FALLA: la descripción se interpretó como escape en vez de mostrarse literal"; exit 1; }; ok
printf '%s' "$OUT" | grep -q 'caracteres de control' || { echo "FALLA: la fila con ESC no se rechazó"; exit 1; }; ok
printf 's\ns\n' > "$T/resp"
SYNC_OPS_HOST=pc-a SYNC_OPS_TTY="$T/resp" bash "$T/sync-ops.sh" --apply >/dev/null 2>&1
ls "$M" | grep -q escapado && { echo "FALLA: se ejecutó una fila con caracteres de control"; exit 1; }; ok
if SYNC_OPS_HOST=x bash "$T/sync-ops.sh" --check >/dev/null 2>&1; then echo "FALLA: --check no marcó los caracteres de control"; exit 1; fi; ok

# --- revisión externa: unir una copia de conflicto no pierde nada ni la deja a medias
printf 'id\tfecha\ttipo\tdescripcion\tcomando\thecho_en\n130\t2026-09-24\tconfig\tvieja\tx | ROLLBACK: y\tpc-b\n' > "$T/system-ops.tsv"
C1="$T/system-ops.sync-conflict-20260925-000000-AAAA.tsv"
cp "$T/system-ops.tsv" "$C1"; printf '140\t2026-09-25\tconfig\tnueva\tc | ROLLBACK: r\tpc-b\n' >> "$C1"
C2="$T/system-ops.sync-conflict-20260925-000001-BBBB.tsv"
cp "$T/system-ops.tsv" "$C2"; printf '130\t2026-09-24\tconfig\tDISTINTA\tx | ROLLBACK: y\tpc-b\n' | tail -1 >> /dev/null
sed -i 's/vieja/DISTINTA/' "$C2"          # mismo id, contenido distinto: choque
add pc-a z >/dev/null
f "$(grep -c '^140	' "$T/system-ops.tsv")" 1 "fila de la copia sin choque entró"; ok
[ ! -e "$C1" ] || { echo "FALLA: la copia unida no se borró"; exit 1; }; ok
[ -e "$C2" ] || { echo "FALLA: la copia con choque se perdió"; exit 1; }; ok
f "$(grep -c 'DISTINTA' "$T/system-ops.tsv")" 0 "la copia con choque no se mezcló"; ok
f "$(ls "$T" | grep -c 'reclamada' || true)" 0 "sin archivos reclamados colgados"; ok
# --- --skip y varios ids (todo o nada)
printf 'id\tfecha\ttipo\tdescripcion\tcomando\thecho_en\n' > "$T/system-ops.tsv"
for i in 31 32 33; do printf '%s\t2026-10-10\tconfig\top %s\t# x | ROLLBACK: y\tpc-b\n' $i $i >> "$T/system-ops.tsv"; done
SYNC_OPS_HOST=pc-a bash "$T/sync-ops.sh" --skip 31 32 >/dev/null 2>&1
f "$(awk -F'\t' '$1==31{print $6}' "$T/system-ops.tsv")" "pc-b,no-pc-a" "--skip agrega no-<host>"; ok
f "$(awk -F'\t' '$1==32{print $6}' "$T/system-ops.tsv")" "pc-b,no-pc-a" "--skip con varios ids"; ok
f "$(SYNC_OPS_HOST=pc-a bash "$T/sync-ops.sh" | grep -c '\[3[12]\]')" 0 "lo omitido no figura como pendiente"; ok
f "$(SYNC_OPS_HOST=pc-a bash "$T/sync-ops.sh" | grep -c '\[33\]')" 1 "lo no marcado sigue pendiente"; ok
if SYNC_OPS_HOST=pc-a bash "$T/sync-ops.sh" --skip 33 999 >/dev/null 2>&1; then echo "FALLA: aceptó un id inexistente"; exit 1; fi; ok
f "$(awk -F'\t' '$1==33{print $6}' "$T/system-ops.tsv")" "pc-b" "todo o nada: 33 quedó intacta"; ok
SYNC_OPS_HOST=pc-a bash "$T/sync-ops.sh" --done 33 >/dev/null 2>&1
if SYNC_OPS_HOST=pc-a bash "$T/sync-ops.sh" --skip 33 >/dev/null 2>&1; then echo "FALLA: marcó como no aplicable una op ya hecha"; exit 1; fi; ok
SYNC_OPS_HOST=pc-a bash "$T/sync-ops.sh" --done 31 >/dev/null 2>&1
f "$(awk -F'\t' '$1==31{print $6}' "$T/system-ops.tsv")" "pc-b,pc-a" "--done reemplaza un no-<host> anterior"; ok
echo "OK sync-ops: $n checks"
