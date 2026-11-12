#!/usr/bin/env python3
"""Estado de sincronización entre las 2 PC: apps · archivos · ajustes.

Una sola fuente para `sync-status` (texto) y `sync-data` (JSON de la GUI). Lo que antes eran tres
comprobaciones sueltas (md5 del .stignore, `find $HOME`, wishlist) ahora usa la API de Syncthing, las
reglas efectivas del .stignore y los paquetes reales de la otra PC.
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(DIR))
sys.dont_write_bytecode = True

from wyss_sync import core, manage  # noqa: E402

HOME = Path.home()
CACHE = HOME / ".cache" / "wyss-sync" / "conflictos.json"
TTL_HOME, TTL_DATA = 60, 600
ARCHIVED_DIRS = ("conflicts-resolved", "descartados")
ARCHIVED_PREFIX = ("state-sync-conflicts",)


# ---------------------------------------------------------------- piezas puras (con pruebas)
def effective_rules(text: str) -> set[str]:
    """Reglas de un .stignore sin comentarios ni líneas vacías."""
    return {ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("//")}


def rules_diff(here: str, there: str) -> tuple[list[str], list[str]]:
    a, b = effective_rules(here), effective_rules(there)
    return sorted(a - b), sorted(b - a)


def is_archived(rel: str) -> bool:
    """Copias que alguien guardó a propósito: no cuentan como conflicto sin resolver."""
    parts = rel.split("/")
    return any(p in ARCHIVED_DIRS or p.startswith(ARCHIVED_PREFIX) for p in parts[:-1])


def conflict_breakdown(rels: list[str], top: int = 6) -> dict:
    active = [r for r in rels if not is_archived(r)]
    counts: dict[str, int] = {}
    for r in active:
        parts = r.split("/")[:-1]
        key = "/".join(parts[:2]) or "(raíz)"
        counts[key] = counts.get(key, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return {"active": len(active), "archived": len(rels) - len(active), "sources": ranked[:top],
            "sources_total": len(ranked)}


def read_names(path: Path) -> list[str]:
    """Primer campo de cada línea, sin comentarios (formato de apps-wishlist.txt)."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    names = []
    for ln in lines:
        fields = ln.split("#")[0].split()
        if fields:
            names.append(fields[0])
    return names


def read_patterns(path: Path) -> list[re.Pattern]:
    try:
        return [re.compile(ln.strip()) for ln in path.read_text(encoding="utf-8").splitlines()
                if ln.strip() and not ln.lstrip().startswith("#")]
    except (OSError, re.error):
        return []


def pkg_diff(mine: set[str], theirs: set[str], ignore: list[re.Pattern]) -> tuple[list[str], list[str]]:
    keep = lambda names: sorted(n for n in names if not any(p.search(n) for p in ignore))  # noqa: E731
    return keep(mine - theirs), keep(theirs - mine)


# ---------------------------------------------------------------- entorno
def run(cmd: list[str], timeout: float = 8.0) -> str | None:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def ssh(hosts, argv: list[str], timeout: float = 8.0) -> str | None:
    for h in hosts:
        out = run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=4", h, *argv], timeout)
        if out is not None:
            return out
    return None


def configured_peers() -> tuple[str, ...]:
    """Read peer aliases without publishing machine-specific defaults."""
    values = (os.environ.get("WYSS_SYNC_PEER", ""), os.environ.get("WYSS_SYNC_PEER_FALLBACK", ""))
    if any(value and not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@-]*", value) for value in values):
        raise ValueError("Alias de equipo inválido en la configuración")
    return tuple(value for value in values if value)


def conflicts(api, folders: list[dict], deep: bool, now=time.time) -> tuple[dict, dict]:
    """(desglose, antigüedad por carpeta). Home se recorre casi siempre; HDD Data usa caché de 10 min
    salvo que `deep` pida recorrerlo (tarda ~1 minuto en el disco rotacional)."""
    try:
        cache = json.loads(CACHE.read_text())
    except (OSError, ValueError):
        cache = {}
    rels, ages, fresh = [], {}, False
    for f in folders:
        fid, root = f["id"], f["local_path"]
        entry = cache.get(fid)
        ttl = TTL_HOME if root == str(HOME) else TTL_DATA
        old = entry is None or now() - entry["ts"] > ttl
        if old and (deep or root == str(HOME)):
            try:
                lines = api.get("/rest/db/ignores", folder=fid).get("ignore") or []
                top = os.listdir(root)
            except (core.ApiError, OSError):
                lines, top = [], []
            rules = manage.Rules(lines)
            skip = {d for d in top if rules.ignored(d) or os.path.islink(os.path.join(root, d))}
            found, done = manage.find_conflicts(root, fid, skip=skip, budget=300 if deep else 25)
            entry = {"ts": now(), "rels": [c.rel for c in found], "complete": done}
            cache[fid] = entry
            fresh = True
        if entry:
            rels += [f["label"] + "/" + r for r in entry["rels"]]
            ages[f["label"]] = int(now() - entry["ts"])
        else:
            ages[f["label"]] = None     # nunca se recorrió: no se afirma que esté limpia
    if fresh:
        try:
            CACHE.parent.mkdir(parents=True, exist_ok=True)
            CACHE.write_text(json.dumps(cache))
        except OSError:
            pass
    return conflict_breakdown(rels), ages


def gather(deep: bool = False) -> dict:
    host = socket.gethostname()
    peers = configured_peers()
    peer = os.environ.get("WYSS_SYNC_PEER", "")
    out = {"host": host, "other_name": peer or "Otro equipo", "other_online": False,
           "apps": {}, "files": {}, "settings": {}}

    # ---- archivos (API de Syncthing)
    files = {"syncthing": run(["systemctl", "--user", "is-active", "syncthing"]) is not None, "folders": [],
             "conflicts": 0, "conflicts_archived": 0, "conflict_sources": [], "conflict_age_s": {}}
    model = None
    try:
        api = core.SyncthingApi(*core.read_config())
        model = core.Collector(api, remote_probe=lambda h: None).collect()
        out["other_online"] = bool(model["remote"]["online"])
        out["other_name"] = model["remote"]["name"] or out["other_name"]
        for f in model["folders"]:
            files["folders"].append({k: f[k] for k in ("id", "label", "state", "percent", "need_items", "need_bytes",
                                                        "sync_bytes", "sync_files")})
        br, ages = conflicts(api, model["folders"], deep)
        files.update(conflicts=br["active"], conflicts_archived=br["archived"],
                     conflict_sources=br["sources"], conflict_age_s=ages)
    except (core.ApiError, OSError, KeyError) as e:
        files["error"] = str(e)
    out["files"] = files

    # ---- ajustes: reglas efectivas del .stignore en las dos PC
    mine = (HOME / ".stignore").read_text(encoding="utf-8", errors="replace") if (HOME / ".stignore").exists() else ""
    theirs = ssh(peers, ["cat", ".stignore"])
    if theirs is None:
        out["settings"] = {"rules": len(effective_rules(mine)), "stignore": "offline", "only_here": [], "only_there": []}
    else:
        a, b = rules_diff(mine, theirs)
        out["settings"] = {"rules": len(effective_rules(mine)), "stignore": "match" if not (a or b) else "diff",
                           "only_here": a, "only_there": b}
        out["other_online"] = True

    # ---- apps: wishlist + diferencia real de paquetes explícitos con la otra PC (en vivo)
    wish = read_names(DIR / "apps-wishlist.txt")
    installed = set((run(["pacman", "-Qq"]) or "").split())
    missing = [p for p in wish if p not in installed]
    mine_pk = set((run(["pacman", "-Qqe"]) or "").split())
    live = ssh(peers, ["pacman", "-Qqe"], timeout=12)
    source = "en vivo"
    if live is None:
        snap = DIR / f"pkgs-{peer}.txt" if peer else None
        theirs_pk = set(snap.read_text().split()) if snap and snap.exists() else set()
        source = "snapshot" if theirs_pk else "sin datos"
    else:
        theirs_pk = set(live.split())
    only_here, only_there = (pkg_diff(mine_pk, theirs_pk, read_patterns(DIR / "apps-ignore.txt"))
                             if theirs_pk else ([], []))
    out["apps"] = {"wishlist_total": len(wish), "missing": missing, "only_here": only_here,
                   "only_there": only_there, "source": source}
    # el snapshot propio queda al día para que la otra PC pueda comparar aunque esta esté apagada
    snap_mine = DIR / f"pkgs-{host}.txt"
    try:
        body = "\n".join(sorted(mine_pk)) + "\n"
        if mine_pk and (not snap_mine.exists() or snap_mine.read_text() != body):
            snap_mine.write_text(body)
            (DIR / f"pkgs-{host}.stamp").write_text(time.strftime("%Y-%m-%dT%H:%M:%S%z") + "\n")
    except OSError:
        pass
    return out


# ---------------------------------------------------------------- texto
C = {"g": "\033[1;32m", "y": "\033[1;33m", "r": "\033[1;31m", "b": "\033[1;34m", "0": "\033[0m"}
STATE = {"synced": ("ok", "al día"), "syncing": ("y", "sincronizando"), "scanning": ("y", "escaneando"),
         "paused": ("y", "pausada"), "offline": ("y", "sin conexión"), "error": ("r", "con errores")}


def render(d: dict) -> str:
    c, L = C, []
    L += [f"{c['b']}╔══════════════════════════════════════════════╗{c['0']}",
          f"{c['b']}║  SYNC STATUS — {d['host']}  <->  {d['other_name']}{c['0']}",
          f"{c['b']}╚══════════════════════════════════════════════╝{c['0']}"]
    a = d["apps"]
    L += ["", f"{c['b']}[1] APPS{c['0']}"]
    if a["missing"]:
        L += [f"  {c['y']}falta acá (wishlist):{c['0']} {p}" for p in a["missing"]]
    else:
        L.append(f"  {c['g']}✓ todas las apps de la wishlist están instaladas acá{c['0']}")
    if a["source"] == "sin datos":
        L.append(f"  {c['y']}○ no pude comparar paquetes con la otra PC{c['0']}")
    else:
        for title, names, col in (("solo acá", a["only_here"], "y"), ("solo en la otra PC", a["only_there"], "y")):
            if names:
                more = f" … (+{len(names) - 12})" if len(names) > 12 else ""
                L.append(f"  {c[col]}{title} ({len(names)}):{c['0']} {', '.join(names[:12])}{more}")
        if not (a["only_here"] or a["only_there"]):
            L.append(f"  {c['g']}✓ mismos paquetes en las dos PC{c['0']} ({a['source']})")
        else:
            L.append(f"  (comparado {a['source']}; instalar faltantes: sync-apps)")
    f = d["files"]
    L += ["", f"{c['b']}[2] ARCHIVOS (Syncthing){c['0']}"]
    L.append(f"  {c['g']}✓ syncthing activo{c['0']}" if f.get("syncthing") else f"  {c['r']}✗ syncthing NO está activo{c['0']}  (systemctl --user start syncthing)")
    if f.get("error"):
        L.append(f"  {c['r']}✗ la API de Syncthing no responde:{c['0']} {f['error']}")
    for fo in f.get("folders", []):
        col, txt = STATE.get(fo["state"], ("y", fo["state"]))
        extra = f" {int(fo['percent'])} %" if fo.get("percent") is not None else ""   # hacia abajo: 99,9 no es 100
        gb = manage.human_size(fo["sync_bytes"])
        pend = f" · faltan {manage.human_size(fo['need_bytes'])}" if fo["need_bytes"] else ""
        mark = "✓" if col == "ok" else "⚠"
        files_n = f"{fo['sync_files']:,}".replace(",", ".")
        L.append(f"  {c['g' if col == 'ok' else col]}{mark} {fo['label']}: {txt}{extra}{c['0']} — sincroniza {gb} en {files_n} archivos{pend}")
    unscanned = [k for k, v in f.get("conflict_age_s", {}).items() if v is None]
    if unscanned:
        L.append(f"  {c['y']}○ conflictos de {', '.join(unscanned)}: sin recorrer (sync-status --deep, ~1 min){c['0']}")
    if f["conflicts"] == 0:
        L.append(f"  {c['g']}✓ 0 conflictos sin resolver{c['0']}" + (f" ({f['conflicts_archived']} copias archivadas a propósito)" if f["conflicts_archived"] else ""))
    else:
        L.append(f"  {c['r']}⚠ {f['conflicts']} conflictos sin resolver{c['0']}" + (f" (+{f['conflicts_archived']} archivados)" if f["conflicts_archived"] else ""))
        for origin, n in f["conflict_sources"]:
            L.append(f"      {n:>3}  {origin}")
        L.append("  (resolverlos: wyss-sync → Conflictos y versiones)")
    s = d["settings"]
    L += ["", f"{c['b']}[3] AJUSTES (.stignore — qué NO se sincroniza){c['0']}",
          f"  reglas efectivas en este .stignore: {s['rules']}"]
    if s["stignore"] == "offline":
        L.append(f"  {c['y']}({d['other_name']} no alcanzable por ssh, no comparo .stignore){c['0']}")
    elif s["stignore"] == "match":
        L.append(f"  {c['g']}✓ mismas reglas efectivas en ambas PCs{c['0']}")
    else:
        L.append(f"  {c['y']}⚠ reglas que difieren (puede ser a propósito, per-PC):{c['0']}")
        L += [f"    solo acá:  {r}" for r in s["only_here"]] + [f"    solo allá: {r}" for r in s["only_there"]]
        L.append("  (NUNCA scp el archivo entero: pisa reglas propias del otro; ver op 047)")
    return "\n".join(L) + "\n"


def main(argv: list[str]) -> int:
    deep = "--deep" in argv
    d = gather(deep=deep)
    if "--json" in argv:
        print(json.dumps(d, ensure_ascii=False))
    else:
        sys.stdout.write(render(d))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
