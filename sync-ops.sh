#!/usr/bin/env bash
# ============================================================
#  sync-ops.sh — Operaciones de SISTEMA pendientes por PC.
#  Syncthing sincroniza ARCHIVOS, pero NO cambios de sistema
#  (pacman install/remove, systemctl, etc). Este registro los
#  anota con en qué PC ya se aplicaron, y te muestra/aplica
#  lo que falta en la máquina actual.
#
#  Uso:  sync-ops                ver pendientes en esta PC
#        sync-ops --apply        aplicar pendientes (pregunta c/u)
#        sync-ops --done <id>... marcar una o varias ops como hechas acá
#        sync-ops --skip <id>... marcar como NO aplica en esta PC (no-<host>)
#        sync-ops --add <tipo> "<descripcion>" "<comando | ROLLBACK: ...>"
#                                registrar una op hecha acá (imprime el id)
#        sync-ops --check        validar el registro (formato, ids, hecho_en)
#  Registrar SIEMPRE con --add, nunca editando el TSV a mano: cada PC
#  numera en su rango (Santiago-PC de 999 para abajo, el resto para
#  arriba) y une antes las copias sync-conflict de Syncthing, así dos
#  sesiones en PCs distintas no chocan nunca.
#
#  El campo comando lleva "<comando> | ROLLBACK: <como deshacer>". Solo la
#  parte anterior a ROLLBACK se ejecuta con --apply; el rollback se muestra
#  y nunca se corre solo.
# ============================================================
set -uo pipefail
DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
TSV="$DIR/system-ops.tsv"
HOST="${SYNC_OPS_HOST:-$(hostname)}"

c_g='\033[1;32m'; c_y='\033[1;33m'; c_r='\033[1;31m'; c_b='\033[1;34m'; c_0='\033[0m'

[ -f "$TSV" ] || { echo "no existe $TSV"; exit 1; }

# Lógica del registro (unión de conflictos, escritura atómica con candado, validación).
py() {
  python3 -B - "$@" <<'PY'
import datetime, fcntl, glob, os, re, sys

sub, tsv, host, *rest = sys.argv[1:]
HOST_RX = re.compile(r"^(?:no-)?[A-Za-z0-9._-]+(?:,(?:no-)?[A-Za-z0-9._-]+)*$")
MAX_DESC = 300
HEADER = "id\tfecha\ttipo\tdescripcion\tcomando\thecho_en"


def leer(p):
    return [l.rstrip("\n") for l in open(p, encoding="utf-8") if l.strip()]


def conflict_copies():
    return sorted(glob.glob(os.path.join(os.path.dirname(tsv), "system-ops.sync-conflict-*.tsv")))


def merged(write=False):
    """Filas del principal + filas que solo están en copias de conflicto (rangos por PC disjuntos).
    Con write=True cada copia se RECLAMA con un renombre atómico antes de leerla: si Syncthing escribe
    después, crea un archivo nuevo en vez de pisar el que se está uniendo. Se borra recién cuando el
    principal quedó guardado (ver `liberar`)."""
    filas = leer(tsv)
    ids = {l.split("\t")[0]: l for l in filas[1:]}
    solo_copia, choques_all, reclamadas = set(), [], []
    for c in conflict_copies():
        src = c
        if write:
            src = c + ".reclamada"
            try:
                os.rename(c, src)
            except OSError:
                continue                  # otro proceso se la llevó: se verá en la próxima
        choques, nuevas = [], []
        for l in leer(src)[1:]:
            i = l.split("\t")[0]
            if i not in ids:
                ids[i] = l
                nuevas.append(l)
            elif ids[i] != l:
                choques.append(i)
        if choques:
            choques_all.append((os.path.basename(c), choques))
            if write:
                os.rename(src, c)         # no se une ni se pierde: queda para revisar a mano
            for l in nuevas:
                ids.pop(l.split("\t")[0], None)
            continue
        for l in nuevas:
            filas.append(l)
            solo_copia.add(l.split("\t")[0])
        if write:
            reclamadas.append((src, c))
    for name, ch in choques_all:
        print(f"AVISO: {name} difiere en ids {', '.join(ch)}; queda para revisar a mano", file=sys.stderr)
    return filas, solo_copia, reclamadas


def liberar(reclamadas, guardado):
    """Con el principal ya guardado se borran las copias unidas; si algo falló, vuelven a su nombre."""
    for src, orig in reclamadas:
        if guardado:
            os.remove(src)
            print(f"unida y borrada: {os.path.basename(orig)}", file=sys.stderr)
        else:
            os.rename(src, orig)


def guardar(filas):
    tmp = tsv + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(filas) + "\n")
    os.replace(tmp, tsv)


class Lock:
    def __enter__(self):
        self.fh = open(tsv + ".lock", "w")
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()


if sub == "view":
    filas, solo, _ = merged()
    print("\n".join([filas[0]] + [
        (l + "") if l.split("\t")[0] not in solo else
        "\t".join(l.split("\t")[:3] + [l.split("\t")[3] + " [solo en copia de conflicto]"] + l.split("\t")[4:])
        for l in filas[1:]]))

elif sub == "add":
    tipo, desc, cmd = rest
    if "\t" in tipo + desc + cmd or "\n" in tipo + desc + cmd:
        sys.exit("sin tabs ni saltos de línea en los campos")
    if "ROLLBACK" not in cmd.upper():
        sys.exit('el comando tiene que declarar "| ROLLBACK: ..." (lo exige claude-doctor)')
    if len(desc) > MAX_DESC:
        sys.exit(f"la descripción tiene {len(desc)} caracteres (máximo {MAX_DESC}): la prosa larga va al CHANGELOG.md")
    with Lock():
        filas, _, reclamadas = merged(write=True)
        guardado = False
        try:
            nums = [int(m.group(1)) for l in filas[1:] if (m := re.fullmatch(r"(\d+)b?", l.split("\t")[0]))]
            # corte fijo en 500; si alguna PC llega ahí, los rangos se cruzan y hay que moverlo
            if host == "Santiago-PC":
                nuevo = min([n for n in nums if n >= 500], default=1000) - 1
            else:
                nuevo = max([n for n in nums if n < 500], default=0) + 1
            filas.append("\t".join([str(nuevo), datetime.date.today().isoformat(), tipo, desc, cmd, host]))
            guardar(filas)
            guardado = True
        finally:
            liberar(reclamadas, guardado)
    print(nuevo)

elif sub in ("done", "skip"):
    ops = rest
    if not ops:
        sys.exit("faltan ids")
    token = host if sub == "done" else "no-" + host
    with Lock():
        filas, _, reclamadas = merged(write=True)
        guardado = False
        try:
            by_id = {l.split("\t")[0]: k for k, l in enumerate(filas) if k > 0}
            for op in ops:
                if op not in by_id:
                    sys.exit(f"no existe la op {op}")        # todo o nada: no se escribe nada
            for op in ops:
                k = by_id[op]
                f = filas[k].split("\t")
                hs = [x for x in f[5].split(",") if x]
                if sub == "skip" and host in hs:
                    sys.exit(f"la op {op} figura como hecha en {host}: no se puede marcar como no aplicable")
                if token not in hs:
                    hs = [x for x in hs if x != ("no-" + host if sub == "done" else host)] + [token]
                f[5] = ",".join(hs)
                filas[k] = "\t".join(f)
            guardar(filas)
            guardado = True
        finally:
            liberar(reclamadas, guardado)

elif sub == "check":
    filas, solo, _ = merged()
    errores = avisos = 0
    CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
    ids = [l.split("\t")[0] for l in filas[1:]]
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        print(f"ERROR id duplicado: {dup}")
        errores += 1
    for l in filas[1:]:
        f = l.split("\t")
        if CTRL.search(l):
            print(f"ERROR {CTRL.sub('?', f[0])}: la fila tiene caracteres de control (no se muestra ni se ejecuta)")
            errores += 1
            continue
        if len(f) != 6:
            print(f"ERROR {f[0]}: {len(f)} columnas (esperaba 6)")
            errores += 1
            continue
        if not HOST_RX.match(f[5]):
            print(f"ERROR {f[0]}: hecho_en inválido {f[5][:50]!r}: tiene que ser hosts separados por coma")
            errores += 1
        if len(f[3]) > MAX_DESC:
            print(f"AVISO {f[0]}: descripción de {len(f[3])} caracteres (máximo {MAX_DESC})")
            avisos += 1
    for i in sorted(solo):
        print(f"AVISO {i}: solo existe en una copia de conflicto (se une con --add o --done)")
        avisos += 1
    print(f"{len(filas) - 1} filas · {errores} errores · {avisos} avisos")
    sys.exit(1 if errores else 0)

else:
    sys.exit(f"subcomando desconocido: {sub}")
PY
}

mode="${1:-list}"

case "$mode" in
  --add)
    [ $# -eq 4 ] || { echo "uso: sync-ops --add <tipo> \"<descripcion>\" \"<comando | ROLLBACK: ...>\""; exit 2; }
    py add "$TSV" "$HOST" "$2" "$3" "$4"
    exit $? ;;
  --done|--skip)
    [ $# -ge 2 ] || { echo "uso: sync-ops $mode <id> [<id>...]"; exit 2; }
    sub="${mode#--}"; shift
    py "$sub" "$TSV" "$HOST" "$@" || exit $?
    for i in "$@"; do
      if [ "$sub" = "done" ]; then printf '%bMarcado [%s] hecho en %s%b\n' "$c_g" "$i" "$HOST" "$c_0"
      else printf '%bMarcado [%s] como no aplicable en %s%b\n' "$c_g" "$i" "$HOST" "$c_0"; fi
    done
    exit 0 ;;
  --check)
    py check "$TSV" "$HOST"
    exit $? ;;
  list|--apply) ;;
  *) echo "uso: sync-ops [--apply | --done <id>... | --skip <id>... | --add ... | --check]"; exit 2 ;;
esac

echo -e "${c_b}═══ Operaciones de sistema — $HOST ═══${c_0}"

pending=0
bad_rows=0
# las respuestas salen de la terminal (fd 3), no del flujo que lee las filas del registro
if [ "$mode" = "--apply" ]; then exec 3<"${SYNC_OPS_TTY:-/dev/tty}"; fi
host_rx='^(no-)?[A-Za-z0-9._-]+(,(no-)?[A-Za-z0-9._-]+)*$'
while IFS=$'\t' read -r id fecha tipo desc cmd done_in; do
  [ "$id" = "id" ] && continue
  [ -z "$id" ] && continue
  # un campo con caracteres de control (ESC, \r…) puede hacer que la pantalla muestre otra cosa que lo que
  # se ejecuta: esa fila ni se muestra ni se aplica
  if [[ "$id$tipo$desc$cmd$done_in" =~ [[:cntrl:]] ]]; then
    bad_rows=$((bad_rows+1))
    printf '  %b[%s]%b caracteres de control en la fila: no se muestra ni se ejecuta (sync-ops --check)\n' \
      "$c_r" "${id//[^0-9b]/?}" "$c_0"
    continue
  fi
  # hecho acá, o marcado no-<host> = no aplica a esta PC
  if [[ "$done_in" =~ $host_rx ]]; then
    if echo ",$done_in," | grep -qE ",(no-)?$HOST,"; then
      continue
    fi
  else
    bad_rows=$((bad_rows+1))
    printf '  %b[%s]%b hecho_en inválido: se ignora hasta corregirlo (sync-ops --check)\n' "$c_r" "$id" "$c_0"
    continue
  fi
  pending=$((pending+1))
  case "$tipo" in
    remove) icon="🗑 ";;
    install) icon="⬇ ";;
    cleanup) icon="🧹";;
    *) icon="• ";;
  esac
  # solo lo anterior a ROLLBACK se ejecuta; el rollback se muestra aparte
  run="$(printf '%s' "$cmd" | sed -E 's/[[:space:]]*[|;]?[[:space:]]*ROLLBACK[[:space:]]*:.*$//')"
  rb="$(printf '%s' "$cmd" | sed -nE 's/^.*ROLLBACK[[:space:]]*:[[:space:]]*//p')"
  printf '  %b[%s]%b %s %s\n' "$c_y" "$id" "$c_0" "$icon" "$desc"
  printf '      %b$%b %s\n' "$c_b" "$c_0" "${run:-(sin comando: solo registro)}"
  [ -n "$rb" ] && printf '      ↩ rollback (no se ejecuta solo): %s\n' "$rb"
  printf '      (hecho en: %s)\n' "${done_in:-nadie}"

  if [ "$mode" = "--apply" ]; then
    if [ -z "$run" ] || [[ "$run" == \#* ]]; then
      echo -e "      ${c_g}→ sin comando que correr, marco hecho${c_0}"
      py done "$TSV" "$HOST" "$id"
      echo
      continue
    fi
    if [[ "$run" == *$'\n'* ]]; then
      echo -e "      ${c_r}✗ el comando tiene saltos de línea: no se ejecuta${c_0}"
      echo
      continue
    fi
    # la respuesta sale de la terminal, no del flujo que está leyendo las filas del registro
    read -rp "      ¿Aplicar [$id]? [s/N] " -u 3 a
    if [ "$a" = "s" ] || [ "$a" = "S" ]; then
      if eval "$run"; then
        py done "$TSV" "$HOST" "$id"
        echo -e "      ${c_g}✓ hecho y marcado${c_0}"
      else
        echo -e "      ${c_r}✗ falló (no marco)${c_0}"
      fi
    fi
  fi
  echo
done < <(py view "$TSV" "$HOST")

if [ "$pending" -eq 0 ]; then
  echo -e "  ${c_g}✓ Nada pendiente. Esta PC está al día.${c_0}"
else
  [ "$mode" != "--apply" ] && echo -e "\n  ${c_y}$pending pendiente(s).${c_0} Aplicar: ${c_b}sync-ops --apply${c_0}"
fi
[ "$bad_rows" -gt 0 ] && echo -e "  ${c_r}$bad_rows fila(s) con hecho_en inválido: corré sync-ops --check${c_0}"
exit 0
