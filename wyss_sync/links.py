"""Vínculos (fase 2): guardar la carpeta X de esta PC en la ruta Y de la otra PC.

Un vínculo es una carpeta nueva de Syncthing, compartida entre las dos PC, con una ruta distinta en cada
una. Como X (y Y) ya forman parte de `Home` o de `HDD Data`, hay que sacarlas de esas carpetas en las dos
PC (regla de ignorado) para que no se sincronicen dos veces. Quitar el vínculo deshace todo eso y nunca
borra archivos. La comprobación previa (`plan`) no escribe nada.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import manage
from .core import ApiError, SyncthingApi

LINK_PREFIX = "wyss-"
ALLOWED_ROOTS = ("HOME", "/mnt/hdd")
RESERVED = {".stfolder", ".stversions", "descartados"}
MAX_INDEX = 60000
# Un vínculo vive dentro de otra carpeta de Syncthing (Home o HDD Data). En la prueba en vivo el vigilante de cambios
# de una carpeta anidada dejó de ver un archivo nuevo y con el escaneo completo cada 24 h habría tardado un día:
# los vínculos reescanean cada 10 minutos como red de seguridad.
LINK_RESCAN_S = 600


# ---------------------------------------------------------------- rutas
def expand(path: str, home: str) -> str:
    """`~` y `~/x` se resuelven contra el HOME de la PC donde corre; el resto debe ser absoluto."""
    p = (path or "").strip()
    if p == "~":
        return home
    if p.startswith("~/"):
        return os.path.join(home, p[2:])
    return p


def containing_folder(path: str, folders: list[dict]) -> tuple[str, str] | None:
    """(id de la carpeta de Syncthing más específica que contiene la ruta, ruta relativa) o None."""
    best = None
    for f in folders:
        root = f["path"].rstrip("/")
        if path == root or path.startswith(root + "/"):
            if best is None or len(root) > len(best[1]):
                best = (f["id"], root)
    if best is None:
        return None
    return best[0], os.path.relpath(path, best[1]) if path != best[1] else ""


def validate_path(path: str, folders: list[dict], home: str, *, must_exist: bool) -> list[str]:
    """Bloqueos (lista vacía = la ruta sirve). No toca nada."""
    out: list[str] = []
    if not path:
        return ["falta la ruta"]
    if any(c in path for c in ("\0", "\n", "\r")):
        return ["la ruta tiene caracteres de control"]
    if not os.path.isabs(path):
        return ["la ruta tiene que ser absoluta (o empezar con ~/)"]
    norm = os.path.normpath(path)
    if norm != path.rstrip("/") or ".." in path.split("/"):
        out.append("la ruta no está normalizada (sin '..', '.' ni '//')")
    roots = [home if r == "HOME" else r for r in ALLOWED_ROOTS]
    if not any(norm == r or norm.startswith(r.rstrip("/") + "/") for r in roots):
        out.append(f"solo se admiten rutas bajo {' o '.join(roots)}")
    if any(norm == r for r in roots):
        out.append("no se puede vincular una raíz entera")
    if set(norm.split("/")) & RESERVED:
        out.append("la ruta pasa por una carpeta reservada de Syncthing o del panel")
    real = os.path.realpath(norm)      # resuelve los enlaces del tramo que existe, aunque el final no exista
    if real != norm:
        out.append(f"la ruta pasa por un enlace simbólico; la ruta real es {real}")
    if must_exist and not os.path.isdir(norm):
        out.append("la carpeta no existe" if not os.path.exists(norm) else "no es una carpeta")
    if os.path.exists(norm) and not os.path.isdir(norm):
        out.append("la ruta existe pero no es una carpeta")
    for f in folders:
        root = f["path"].rstrip("/")
        if norm == root:
            out.append(f"es la raíz de la carpeta de Syncthing «{f.get('label', f['id'])}»")
        elif root.startswith(norm + "/"):
            out.append(f"contiene a la carpeta de Syncthing «{f.get('label', f['id'])}» ({root})")
        elif norm.startswith(root + "/") and f["id"].startswith(LINK_PREFIX):
            out.append(f"ya está dentro del vínculo «{f.get('label', f['id'])}»")
    return out


_FID_RX = re.compile(r"^wyss-[a-z0-9-]{1,60}$")


def check_fid(fid: str) -> str:
    """Un vínculo solo puede crear o quitar carpetas cuyo id empiece con `wyss-`: así una orden mal
    armada (o remota) jamás toca `home-saintwyss`, `mnt-hdd-data` ni ninguna carpeta propia del usuario."""
    if not isinstance(fid, str) or not _FID_RX.match(fid):
        raise ValueError(f"id de vínculo inválido: {fid!r}")
    return fid


def safe_makedirs(path: str, home: str) -> list[str]:
    """Crea `path` componente por componente, sin seguir enlaces, y devuelve lo que creó. Si en algún paso el
    componente resulta ser un enlace (o la ruta real difiere), deshace lo creado y falla."""
    norm = os.path.normpath(path)
    roots = [os.path.realpath(r) for r in (home, "/mnt/hdd")]
    base = next((r for r in roots if norm == r or norm.startswith(r.rstrip("/") + "/")), None)
    if base is None:
        raise ValueError("ruta fuera de las raíces permitidas")
    cur, created = base, []
    try:
        for part in [x for x in os.path.relpath(norm, base).split("/") if x not in ("", ".")]:
            cur = os.path.join(cur, part)
            if os.path.islink(cur):
                raise ValueError(f"enlace simbólico en la ruta: {cur}")
            if not os.path.isdir(cur):
                os.mkdir(cur)
                created.append(cur)
                if os.path.islink(cur) or os.path.realpath(cur) != cur:
                    raise ValueError(f"la carpeta creada no es la esperada: {cur}")
    except Exception:
        for c in reversed(created):
            try:
                os.rmdir(c)
            except OSError:
                pass
        raise
    return created


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return (s or "carpeta")[:24]


def new_id(x_path: str, y_path: str) -> str:
    h = hashlib.sha1(f"{x_path}\0{y_path}".encode()).hexdigest()[:6]
    return f"{LINK_PREFIX}{slug(os.path.basename(x_path))}-{h}"


# ---------------------------------------------------------------- comparación X contra Y
def tree_index(root: str, cap: int = MAX_INDEX, budget: float = 90.0,
               clock: Callable[[], float] = time.monotonic) -> tuple[dict[str, list[float]], bool]:
    """{ruta relativa: [tamaño, mtime]} de los archivos bajo `root`. No sigue enlaces. (índice, truncado)"""
    deadline, idx = clock() + budget, {}
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in RESERVED]
        # os.walk no baja por enlaces a carpetas pero los lista entre las carpetas: Syncthing sincroniza el enlace
        files = list(files) + [d for d in dirs if os.path.islink(os.path.join(dirpath, d))]
        for f in files:
            if clock() > deadline or len(idx) >= cap:
                return idx, True
            full = os.path.join(dirpath, f)
            try:
                st = os.lstat(full)
                target = os.readlink(full) if os.path.islink(full) else None
            except OSError:
                continue
            idx[os.path.relpath(full, root)] = ([st.st_size, int(st.st_mtime)] if target is None
                                                else [0, int(st.st_mtime), target])
    return idx, False


def diff_indexes(here: dict, there: dict, examples: int = 8) -> dict:
    """Qué pasaría al unir las dos carpetas. mtime con 2 s de tolerancia (NTFS)."""
    only_here = sorted(set(here) - set(there))
    only_there = sorted(set(there) - set(here))
    same, different = [], []
    for k in set(here) & set(there):
        a, b = here[k], there[k]
        if len(a) == 3 or len(b) == 3:          # enlace simbólico: cuenta a dónde apunta, no el tamaño ni la fecha
            (same if len(a) == len(b) and a[2] == b[2] else different).append(k)
        else:
            (same if a[0] == b[0] and abs(a[1] - b[1]) <= 2 else different).append(k)
    size = lambda idx, keys: sum(idx[k][0] for k in keys)  # noqa: E731
    return {
        "only_here": {"n": len(only_here), "bytes": size(here, only_here), "examples": only_here[:examples]},
        "only_there": {"n": len(only_there), "bytes": size(there, only_there), "examples": only_there[:examples]},
        "same": {"n": len(same), "bytes": size(here, same)},
        "different": {"n": len(different), "bytes": size(here, different), "examples": sorted(different)[:examples],
                      "same_size_other_time": sum(1 for k in different if here[k][0] == there[k][0])},
    }


# ---------------------------------------------------------------- lado remoto (también corre acá)
def inspect_path(path: str, folders: list[dict], home: str, *, must_exist: bool, with_index: bool = True,
                 cap: int = MAX_INDEX) -> dict:
    p = expand(path, home)
    blockers = validate_path(p, folders, home, must_exist=must_exist)
    info: dict[str, Any] = {"path": p, "home": home, "blockers": blockers, "exists": os.path.isdir(p),
                            "folder": None, "rel": None, "files": 0, "bytes": 0, "truncated": False}
    c = containing_folder(os.path.normpath(p), folders) if os.path.isabs(p) else None
    if c:
        info["folder"], info["rel"] = c
    if info["exists"] and not blockers and with_index:
        idx, trunc = tree_index(p, cap=cap)
        info.update(files=len(idx), bytes=sum(v[0] for v in idx.values()), truncated=trunc, index=idx)
    return info


def folder_defaults(api: SyncthingApi) -> dict:
    try:
        return api.get("/rest/config/defaults/folder")
    except ApiError:
        return {}


def add_link_folder(api: SyncthingApi, *, fid: str, label: str, path: str, me: str, peer: str) -> dict:
    """Agrega la carpeta compartida. Si el id ya existe, no la pisa."""
    check_fid(fid)
    if any(f["id"] == fid for f in api.get("/rest/config/folders")):
        raise ValueError(f"ya existe una carpeta con id {fid}")
    cfg = folder_defaults(api)
    cfg.update({"id": fid, "label": label, "path": path, "type": "sendreceive", "paused": False,
                "rescanIntervalS": LINK_RESCAN_S, "fsWatcherEnabled": True,
                "devices": [{"deviceID": me, "introducedBy": "", "encryptionPassword": ""},
                            {"deviceID": peer, "introducedBy": "", "encryptionPassword": ""}]})
    api.post("/rest/config/folders", body=cfg)
    return cfg


def apply_side(api: SyncthingApi, *, fid: str, label: str, path: str, peer: str,
               rules: list[dict], create_dir: bool, folders: list[dict], home: str | None = None,
               verify_tries: int = 120, sleep: Callable[[float], None] = time.sleep) -> dict:
    """Pasos de UNA PC, en orden y con deshacer propio si algo falla a mitad:
    1) reglas de ignorado (confirmadas por Syncthing), 2) crear la carpeta si falta, 3) alta de la carpeta."""
    check_fid(fid)
    home = home or os.path.expanduser("~")
    me = api.get("/rest/system/status")["myID"]
    done: list[tuple[str, Any]] = []
    added: list[dict] = []
    try:
        bad = validate_path(path, folders, home, must_exist=False)
        if bad:
            raise ValueError("ruta no válida: " + "; ".join(bad))
        for r in rules:
            res = manage.apply_change(api, r["folder"], r["rel"], "ignore", tries=verify_tries, sleep=sleep)
            if res.changed:                      # solo se deshace lo que ESTE intento agregó
                done.append(("ignore", (r["folder"], r["rel"], res.backup)))
                added.append({"folder": r["folder"], "rel": r["rel"]})
            if not res.ok:
                raise RuntimeError(f"no pude confirmar que Syncthing ignora «{r['rel']}»: {res.note}")
        if create_dir and not os.path.isdir(path):
            created = safe_makedirs(path, home)
            if created:
                done.append(("mkdir", created))
        add_link_folder(api, fid=fid, label=label, path=path, me=me, peer=peer)
        done.append(("folder", fid))
        try:
            scan_side(api, fid)              # que no dependa del vigilante para ver lo que ya hay
        except ApiError:
            pass
        return {"ok": True, "steps": [d[0] for d in done], "added": added, "host": manage.host_name()}
    except Exception as e:      # deshacer lo que ya se hizo en esta PC, en orden inverso
        undo_steps(api, done)
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "undone": [d[0] for d in reversed(done)],
                "host": manage.host_name()}


def scan_side(api: SyncthingApi, fid: str) -> None:
    """Pide a Syncthing que reescanee la carpeta del vínculo ahora."""
    check_fid(fid)
    api.post("/rest/db/scan", folder=fid)


def rescan_link(api: SyncthingApi, fid: str, remote_call: Callable[[str, dict], dict | None]) -> dict:
    """Reescanea el vínculo en las dos PC. No cambia configuración ni toca archivos."""
    check_fid(fid)
    out = {"local": False, "remote": False}
    try:
        scan_side(api, fid)
        out["local"] = True
    except ApiError:
        pass
    r = remote_call("link-scan", {"fid": fid})
    out["remote"] = bool(r and r.get("ok") and (r.get("result") or {}).get("ok"))
    return out


def undo_steps(api: SyncthingApi, done: list[tuple[str, Any]]) -> None:
    for kind, arg in reversed(done):
        try:
            if kind == "folder":
                api._call(f"/rest/config/folders/{urllib.parse.quote(arg, safe='')}", {}, None, timeout=20.0,
                          method="DELETE")
            elif kind == "ignore":
                fid, rel, _backup = arg
                manage.apply_change(api, fid, rel, "unignore")
            elif kind == "mkdir":
                for d in reversed(arg if isinstance(arg, list) else [arg]):
                    os.rmdir(d)         # solo si quedó vacía: nunca borra contenido
        except (ApiError, OSError, ValueError):
            continue


def remove_side(api: SyncthingApi, *, fid: str, rules: list[dict],
                sleep: Callable[[float], None] = time.sleep) -> dict:
    """Quita la carpeta compartida (no borra archivos) y las reglas que agregó el vínculo. Idempotente: se puede
    repetir; `ok` solo es verdadero si la carpeta ya no está y todas las reglas quedaron confirmadas."""
    check_fid(fid)
    out: dict[str, Any] = {"folder_removed": False, "unignored": [], "errors": [], "host": manage.host_name()}
    try:
        api._call(f"/rest/config/folders/{urllib.parse.quote(fid, safe='')}", {}, None, timeout=20.0, method="DELETE")
        out["folder_removed"] = True
    except ApiError as e:
        try:
            gone = all(f["id"] != fid for f in api.get("/rest/config/folders"))
        except ApiError:
            gone = False
        out["folder_removed"] = gone
        if not gone:
            out["errors"].append(f"carpeta: {e}")
    for r in rules:
        try:
            res = manage.apply_change(api, r["folder"], r["rel"], "unignore", tries=60, sleep=sleep)
        except (ApiError, ValueError) as e:
            out["errors"].append(f"{r['rel']}: {e}")
            continue
        if res.ok:
            out["unignored"].append(r["rel"])
        else:
            out["errors"].append(f"{r['rel']}: {res.note}")
    out["ok"] = out["folder_removed"] and not out["errors"]
    return out


# ---------------------------------------------------------------- plan (solo lectura) y registro
@dataclass
class Plan:
    fid: str
    label: str
    x: dict
    y: dict
    rules: list[dict] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    diff: dict | None = None
    y_has_data: bool = False
    truncated: bool = False


def build_plan(x_path: str, y_path: str, local_folders: list[dict], local_home: str,
               remote_info: dict | None, *, local_cap: int = MAX_INDEX) -> Plan:
    """Combina la inspección local de X y la remota de Y. No escribe nada."""
    x = inspect_path(x_path, local_folders, local_home, must_exist=True, cap=local_cap)
    y = remote_info or {"path": y_path, "blockers": ["no pude consultar la otra PC por ssh"], "exists": False,
                        "folder": None, "rel": None, "files": 0, "bytes": 0}
    p = Plan(new_id(x["path"], y["path"]), os.path.basename(x["path"]) or "carpeta", x, y)
    p.blockers += [f"origen: {b}" for b in x["blockers"]] + [f"destino: {b}" for b in y.get("blockers", [])]
    if x["path"] == y["path"] and not p.blockers:
        p.blockers.append("origen y destino son la misma ruta en las dos PC: ya se sincronizan por la carpeta que los "
                          "contiene, un vínculo no cambiaría nada")
    # reglas: cada ruta se saca de la carpeta de Syncthing que hoy la contiene, en las dos PC
    seen = set()
    for side, label in ((x, "origen"), (y, "destino")):
        if side.get("folder") and side.get("rel") and (side["folder"], side["rel"]) not in seen:
            seen.add((side["folder"], side["rel"]))
            p.rules.append({"folder": side["folder"], "rel": side["rel"], "why": label})
    p.y_has_data = bool(y.get("exists") and y.get("files", 0) > 0)
    if not y.get("exists") and not y.get("blockers"):
        p.warnings.append("la carpeta de destino no existe: se va a crear")
    if y.get("truncated") or x.get("truncated"):
        p.truncated = True
        p.warnings.append("alguna de las carpetas tiene más de 60.000 archivos: la comparación es parcial")
    if "index" in x and "index" in y:
        p.diff = diff_indexes(x["index"], y["index"])
    # los índices completos no viajan al resumen
    x.pop("index", None)
    y.pop("index", None)
    return p


def record_path(state_dir: Path = manage.STATE_DIR) -> Path:
    return state_dir / "vinculos.json"


def load_links(state_dir: Path = manage.STATE_DIR) -> dict:
    try:
        return json.loads(record_path(state_dir).read_text())
    except (OSError, ValueError):
        return {}


def save_link(rec: dict, state_dir: Path = manage.STATE_DIR) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    data = load_links(state_dir)
    data[rec["id"]] = rec
    tmp = record_path(state_dir).with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    os.replace(tmp, record_path(state_dir))


def forget_link(fid: str, state_dir: Path = manage.STATE_DIR) -> None:
    data = load_links(state_dir)
    if data.pop(fid, None) is not None:
        tmp = record_path(state_dir).with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
        os.replace(tmp, record_path(state_dir))


def create_link(api: SyncthingApi, plan: Plan, local_folders: list[dict], remote_call: Callable[[str, dict], dict | None],
                peer_id: str, *, state_dir: Path = manage.STATE_DIR, clock: Callable[[], float] = time.time,
                home: str | None = None) -> dict:
    """Aplica el plan: primero la otra PC, después esta; si esta falla, deshace la otra. La intención queda
    registrada ANTES de cambiar nada: si algo se interrumpe, el vínculo a medias se puede quitar desde el panel."""
    if plan.blockers:
        raise ValueError("el plan tiene bloqueos: " + "; ".join(plan.blockers))
    check_fid(plan.fid)
    me = api.get("/rest/system/status")["myID"]
    rec = {"id": plan.fid, "label": plan.label, "x": plan.x["path"], "y": plan.y["path"], "rules": plan.rules,
           "rules_local": plan.rules, "rules_remote": plan.rules, "created": clock(), "peer": peer_id, "state": "creando"}
    save_link(rec, state_dir)                    # si esto falla, todavía no se cambió nada
    remote_payload = {"fid": plan.fid, "label": plan.label, "path": plan.y["path"], "peer": me,
                      "rules": plan.rules, "create_dir": not plan.y["exists"]}
    remote_done = False
    try:
        rres = remote_call("link-apply", remote_payload)
        rok = bool(rres and rres.get("ok") and (rres.get("result") or {}).get("ok"))
        if not rok:
            err = (rres or {}).get("error") or (rres or {}).get("result", {}).get("error") or "sin respuesta de la otra PC"
            forget_link(plan.fid, state_dir)
            return {"ok": False, "stage": "remoto", "error": err, "remote": (rres or {}).get("result")}
        remote_done = True
        rec["rules_remote"] = rres["result"].get("added", plan.rules)
        lres = apply_side(api, fid=plan.fid, label=plan.label, path=plan.x["path"], peer=peer_id, rules=plan.rules,
                          create_dir=False, folders=local_folders, home=home)
        if not lres["ok"]:
            back = remote_call("link-remove", {"fid": plan.fid, "rules": rec["rules_remote"]})
            if back and back.get("ok") and (back.get("result") or {}).get("ok"):
                forget_link(plan.fid, state_dir)
                return {"ok": False, "stage": "local", "error": lres["error"], "undone_remote": True}
            rec["state"] = "a-medias"            # la otra PC no pudo volver atrás: queda registrado para quitarlo
            save_link(rec, state_dir)
            return {"ok": False, "stage": "local", "error": lres["error"], "undone_remote": False,
                    "note": "la otra PC quedó con el vínculo a medias; quitalo desde Vínculos"}
        rec["rules_local"] = lres.get("added", plan.rules)
        rec["state"] = "activo"
        try:
            save_link(rec, state_dir)
        except OSError:
            pass                                  # sigue el registro "creando", que alcanza para quitarlo
        _log({"ts": clock(), "action": "link-create", **rec}, state_dir)
        return {"ok": True, "id": plan.fid, "record": rec}
    except Exception as e:
        if remote_done:
            rec["state"] = "a-medias"
            try:
                save_link(rec, state_dir)
            except OSError:
                pass
        else:
            forget_link(plan.fid, state_dir)
        return {"ok": False, "stage": "excepción", "error": f"{type(e).__name__}: {e}"}


def drop_link(api: SyncthingApi, fid: str, remote_call: Callable[[str, dict], dict | None], *,
              state_dir: Path = manage.STATE_DIR, clock: Callable[[], float] = time.time,
              sleep: Callable[[float], None] = time.sleep) -> dict:
    """Quita el vínculo en las dos PC. El registro solo se olvida si TODO quedó confirmado: si algo falló
    se conserva para poder repetir (la operación es idempotente)."""
    rec = load_links(state_dir).get(fid)
    if not rec:
        raise ValueError("ese vínculo no está registrado en esta PC")
    local = remove_side(api, fid=fid, rules=rec.get("rules_local", rec.get("rules", [])), sleep=sleep)
    remote = remote_call("link-remove", {"fid": fid, "rules": rec.get("rules_remote", rec.get("rules", []))})
    rres = (remote or {}).get("result") or {}
    remote_ok = bool(remote and remote.get("ok") and rres.get("ok"))
    ok = bool(local["ok"]) and remote_ok
    if ok:
        forget_link(fid, state_dir)
    else:
        rec["state"] = "quitando-incompleto"
        save_link(rec, state_dir)
    _log({"ts": clock(), "action": "link-remove", "id": fid, "ok": ok, "local": local, "remote": rres}, state_dir)
    return {"ok": ok, "local": local, "remote": rres or None}


def _log(entry: dict, state_dir: Path) -> None:
    manage._log(entry, state_dir)
