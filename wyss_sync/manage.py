"""Subcarpetas y reglas de ignorado (fase 1 de la gestión).

Lo que está o no ignorado lo dice la API de Syncthing (`local.ignored`); el matcher de
este módulo solo explica qué regla lo causa. Todo cambio pasa por la API, que reescribe
el .stignore y lo recarga, deja backup del contenido anterior y un registro en jsonl.
Las rutas son entrada no confiable: se validan antes de formar cualquier regla.
"""
from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import urllib.parse
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from .core import ApiError, SyncthingApi, disk_info, mount_table, read_config

MARK_BEGIN = "// >>> Wyss Sync (gestionado): reglas agregadas desde el panel"
MARK_END = "// <<< Wyss Sync"
STATE_DIR = Path("~/.local/state/wyss-sync").expanduser()
REMOTE_ENTRY = "~/Data/00_System/wyss-sync/wyss-sync.py"
MAX_CHILDREN = 600
SPECIAL = set("\\*?[]{}")


# ---------------------------------------------------------------- rutas y reglas
# Carpetas que, ignoradas enteras, dejan de sincronizar configuraciones, credenciales o datos del usuario.
CRITICAL_TOP = (".claude", ".codex", ".config", ".ssh", ".gnupg", ".local/share", ".local/state", ".mozilla",
                "Data", "Documentos", "Escritorio", "Descargas", "Imágenes", "Música", "Vídeos", "Github_repos")


def is_critical(rel: str) -> bool:
    return rel.strip("/") in CRITICAL_TOP


def valid_rel(rel: str) -> str:
    """Ruta relativa a la raíz de una carpeta de Syncthing, o ValueError."""
    if not isinstance(rel, str) or not rel.strip("/"):
        raise ValueError("ruta vacía")
    if any(c in rel for c in ("\0", "\n", "\r")):
        raise ValueError("la ruta tiene caracteres de control")
    rel = rel.strip("/")
    parts = rel.split("/")
    if rel.startswith("~") or any(p in ("", ".", "..") for p in parts):
        raise ValueError("ruta no permitida")
    if parts[-1] == ".stignore" or parts[0] == ".stfolder":
        raise ValueError("ruta reservada de Syncthing")
    return rel


def rule_for(rel: str) -> str:
    """Regla anclada a la raíz para una ruta: '/' + ruta con los comodines escapados."""
    rel = valid_rel(rel)
    return "/" + "".join("\\" + c if c in SPECIAL else c for c in rel)


def _translate(body: str) -> str:
    out, i, n = [], 0, len(body)
    while i < n:
        c = body[i]
        if c == "\\" and i + 1 < n:
            out.append(re.escape(body[i + 1]))
            i += 2
        elif c == "*":
            if body[i:i + 2] == "**":
                out.append(".*")
                i += 2
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = body.find("]", i + 1)
            if j == -1:
                out.append(re.escape(c))
                i += 1
            else:
                cls = body[i + 1:j]
                neg = cls[:1] == "!"
                out.append("[" + ("^" if neg else "") + cls[1 if neg else 0:].replace("\\", "\\\\") + "]")
                i = j + 1
        elif c == "{":
            j = body.find("}", i + 1)
            if j == -1:
                out.append(re.escape(c))
                i += 1
            else:
                out.append("(?:" + "|".join(_translate(x) for x in body[i + 1:j].split(",")) + ")")
                i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)


def parse_rule(line: str) -> tuple[bool, re.Pattern] | None:
    """(es_negación, regex) de una línea del .stignore, o None si es comentario o directiva."""
    s = line.strip()
    if not s or s.startswith("//") or s.startswith("#include"):
        return None
    neg = ci = False
    while True:
        if s.startswith("!"):
            neg, s = True, s[1:]
        elif s.startswith("(?i)"):
            ci, s = True, s[4:]
        elif s.startswith("(?d)"):
            s = s[4:]
        else:
            break
    if not s:
        return None
    anchored = s.startswith("/")
    body = _translate(s[1:] if anchored else s)
    rx = ("^" if anchored else "^(?:.*/)?") + body + "$"
    try:
        return neg, re.compile(rx, re.IGNORECASE if ci else 0)
    except re.error:
        return None


class Rules:
    """Reglas del .stignore listas para evaluar muchas rutas. Las reglas literales (la mayoría) se
    resuelven por diccionario; el resto, por regex en orden. La primera coincidencia manda, igual que
    en Syncthing. Se usa sobre rutas cuyos padres ya se sabe que no están ignorados."""

    def __init__(self, lines: list[str]):
        self.exact: dict[str, int] = {}
        self.names: dict[str, int] = {}
        self.rx: list[tuple[int, bool, re.Pattern]] = []
        self.neg: dict[int, bool] = {}
        for i, ln in enumerate(lines):
            pr = parse_rule(ln)
            if not pr:
                continue
            self.neg[i] = pr[0]
            raw = ln.strip().lstrip("!")
            if not raw or raw.startswith("(?") or any(c in raw for c in "*?[{\\"):
                self.rx.append((i, pr[0], pr[1]))
            elif raw.startswith("/"):
                self.exact.setdefault(raw[1:], i)
            elif "/" not in raw:
                self.names.setdefault(raw, i)
            else:
                self.rx.append((i, pr[0], pr[1]))

    def ignored(self, rel: str) -> bool:
        best = self.exact.get(rel)
        name = rel.rsplit("/", 1)[-1]
        n = self.names.get(name)
        if n is not None and (best is None or n < best):
            best = n
        for i, _neg, rx in self.rx:
            if best is not None and i > best:
                break
            if rx.match(rel):
                best = i
                break
        return best is not None and not self.neg[best]


def match_rule(lines: list[str], rel: str) -> tuple[str | None, bool]:
    """(regla que decide, ignorada). Una carpeta padre ignorada cubre a todo lo que contiene."""
    parts = rel.strip("/").split("/")
    parsed = [(ln, parse_rule(ln)) for ln in lines]
    decided: str | None = None
    for depth in range(1, len(parts) + 1):
        sub = "/".join(parts[:depth])
        for ln, pr in parsed:
            if pr and pr[1].match(sub):
                decided = ln
                if not pr[0]:
                    return ln, True
                break
    return decided, False


# ---------------------------------------------------------------- bloque gestionado
def split_managed(lines: list[str]) -> tuple[list[str], list[str]]:
    """(reglas gestionadas, resto del archivo). El bloque, si existe, está entre las marcas."""
    if MARK_BEGIN in lines and MARK_END in lines:
        a, b = lines.index(MARK_BEGIN), lines.index(MARK_END)
        if a < b:
            return lines[a + 1:b], lines[:a] + lines[b + 1:]
    return [], list(lines)


def compose(managed: list[str], rest: list[str]) -> list[str]:
    """El bloque va arriba: la primera regla que coincide manda, así una negación suya gana."""
    return ([MARK_BEGIN, *managed, MARK_END] if managed else []) + rest


def plan_change(lines: list[str], rel: str, action: str, *, negate: bool = False) -> list[str]:
    """Líneas nuevas del .stignore. 'ignore' agrega la regla; 'unignore' la quita y, si
    `negate`, agrega una negación (cuando una regla propia del archivo sigue ignorándola)."""
    rule = rule_for(rel)
    managed, rest = split_managed(lines)
    managed = [m for m in managed if m not in (rule, "!" + rule)]
    if action == "ignore":
        managed.append(rule)
    elif action == "unignore":
        if negate:
            managed.append("!" + rule)
    else:
        raise ValueError(f"acción desconocida: {action}")
    return compose(managed, rest)


# ---------------------------------------------------------------- disco
@dataclass
class Child:
    name: str
    rel: str
    kind: str                      # dir | link
    mtime: float | None
    ignored: bool | None = None    # None: Syncthing todavía no la indexó
    rule: str | None = None
    exists: bool = True            # solo aplica al lado remoto
    stale: bool = False            # la entrada de Syncthing contradice a las reglas: falta reescanear
    disk: dict | None = None       # dónde vive de verdad (un enlace a otro disco se resuelve a ese disco)


def entry_ignored(info: dict) -> bool | None:
    """Estado `ignored` de una entrada real del índice, o None si Syncthing no la tiene.
    Para rutas que nunca indexó, la API devuelve un `local` vacío con sequence 0."""
    local = (info or {}).get("local") or {}
    if (local.get("sequence") or 0) <= 0:
        return None
    return bool(local.get("ignored"))


def dir_size(path: str, budget: float = 3.0, clock: Callable[[], float] = time.monotonic) -> tuple[int, int, bool]:
    """(bytes, archivos, completo). No sigue enlaces y corta al agotar el presupuesto."""
    deadline, total, files, stack = clock() + budget, 0, 0, [path]
    while stack:
        if clock() > deadline:
            return total, files, False
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    try:
                        if e.is_symlink():
                            continue
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        else:
                            total += e.stat(follow_symlinks=False).st_size
                            files += 1
                    except OSError:
                        continue
        except OSError:
            continue
    return total, files, True


def index_state(api: SyncthingApi, folder_id: str, rel: str) -> bool | None:
    """`ignored` de la entrada del índice, o None si no la hay (la API responde 404 o un objeto vacío)."""
    try:
        return entry_ignored(api.get("/rest/db/file", folder=folder_id, file=rel))
    except ApiError:
        return None


def dir_sizes(path: str, rel: str, rules: Rules, budget: float = 4.0,
              clock: Callable[[], float] = time.monotonic) -> dict:
    """Tamaño en disco y tamaño que sincroniza (lo que no ignora ninguna regla) bajo `path`.
    No sigue enlaces. Corta al agotar el presupuesto y lo avisa en `complete`."""
    deadline = clock() + budget
    disk = synced = files = synced_files = 0
    stack = [(path, rel, rules.ignored(rel))]
    while stack:
        if clock() > deadline:
            return {"disk": disk, "synced": synced, "files": files, "synced_files": synced_files, "complete": False}
        cur, cur_rel, cur_ign = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    child_rel = f"{cur_rel}/{e.name}" if cur_rel else e.name
                    ign = cur_ign or rules.ignored(child_rel)
                    try:
                        if e.is_symlink():
                            continue
                        if e.is_dir(follow_symlinks=False):
                            stack.append((e.path, child_rel, ign))
                        else:
                            size = e.stat(follow_symlinks=False).st_size
                            disk += size
                            files += 1
                            if not ign:
                                synced += size
                                synced_files += 1
                    except OSError:
                        continue
        except OSError:
            continue
    return {"disk": disk, "synced": synced, "files": files, "synced_files": synced_files, "complete": True}


def human_rate(bps: float) -> str:
    return human_size(int(bps)) + "/s"


def folder_root(api: SyncthingApi, folder_id: str) -> str:
    for f in api.get("/rest/config/folders"):
        if f["id"] == folder_id:
            return f["path"]
    raise ApiError(f"carpeta desconocida: {folder_id}")


def children(api: SyncthingApi, folder_id: str, rel: str = "", root: str | None = None,
             limit: int = MAX_CHILDREN) -> list[Child]:
    """Subcarpetas directas de `rel` con su estado según Syncthing y la regla que la decide."""
    root = root or folder_root(api, folder_id)
    base = os.path.join(root, rel) if rel else root
    lines = (api.get("/rest/db/ignores", folder=folder_id).get("ignore") or [])
    table = mount_table()
    out: list[Child] = []
    try:
        entries = sorted(os.scandir(base), key=lambda e: e.name.lower())
    except OSError:
        return out
    for e in entries:
        if e.name in (".stfolder", ".stversions"):
            continue
        try:
            link = e.is_symlink()
            if not (e.is_dir(follow_symlinks=False) or (link and e.is_dir())):
                continue
            mtime = e.stat(follow_symlinks=False).st_mtime
        except OSError:
            continue
        child_rel = f"{rel}/{e.name}" if rel else e.name
        indexed = index_state(api, folder_id, child_rel)
        rule, matched = match_rule(lines, child_rel)
        # con entrada en el índice manda Syncthing; sin entrada, la regla (nunca la indexó por ignorada)
        ignored = indexed if indexed is not None else (True if matched else None)
        stale = indexed is not None and indexed != matched
        out.append(Child(e.name, child_rel, "link" if link else "dir", mtime, ignored,
                         rule if (ignored or matched) else None, stale=stale,
                         disk=disk_info(e.path, table)))
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- escritura
@dataclass
class ApplyResult:
    ok: bool
    action: str
    rel: str
    rules_before: int = 0
    rules_after: int = 0
    changed: list[str] = field(default_factory=list)
    backup: str = ""
    verified: bool | None = None   # True: Syncthing lo confirmó · False: lo contradice · None: no se pudo confirmar
    backup_sha256: str = ""
    ignored_after: bool | None = None
    note: str = ""
    host: str = ""


def _log(entry: dict, state_dir: Path) -> None:
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        with open(state_dir / f"cambios-{socket.gethostname()}.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


@contextlib.contextmanager
def ignores_lock(folder_id: str, state_dir: Path = STATE_DIR):
    """Candado entre procesos (panel, CLI remoto, historiador) para leer-modificar-escribir el .stignore:
    sin él, dos escritores se pisan reglas el uno al otro."""
    state_dir.mkdir(parents=True, exist_ok=True)
    with open(state_dir / f"ignores-{folder_id}.lock", "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _sha(lines: list[str]) -> str:
    return hashlib.sha256(("\n".join(lines) + "\n").encode("utf-8")).hexdigest()


def _backup(lines: list[str], folder_id: str, state_dir: Path, now: float) -> str:
    d = state_dir / "backups"
    d.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    base = f"{folder_id}-{socket.gethostname()}-{stamp}"
    n = 0
    while True:   # dos cambios en el mismo segundo no pueden pisarse el backup
        p = d / (f"{base}.stignore" if n == 0 else f"{base}-{n}.stignore")
        try:
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            n += 1
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return str(p)


def _is_ignored(api: SyncthingApi, folder_id: str, rel: str, want: bool, tries: int = 12,
                sleep: Callable[[float], None] = time.sleep) -> bool | None:
    """Reescanea solo esa subcarpeta y espera a que Syncthing refleje el estado pedido."""
    try:
        api.post("/rest/db/scan", folder=folder_id, sub=rel)
    except ApiError:
        pass
    seen = None
    for _ in range(tries):
        try:
            seen = index_state(api, folder_id, rel)
            if seen is None:   # Syncthing no la indexa (ignorada de origen): decide la regla vigente
                lines = api.get("/rest/db/ignores", folder=folder_id).get("ignore") or []
                seen = match_rule(lines, rel)[1]
            if seen == want:
                return seen
        except ApiError:
            pass
        sleep(0.5)
    return seen


def apply_change(api: SyncthingApi, folder_id: str, rel: str, action: str, *,
                 state_dir: Path = STATE_DIR, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, tries: int = 12) -> ApplyResult:
    rel = valid_rel(rel)
    with ignores_lock(folder_id, state_dir):
        return _apply_locked(api, folder_id, rel, action, state_dir, clock, sleep, tries)


def _apply_locked(api: SyncthingApi, folder_id: str, rel: str, action: str, state_dir: Path,
                  clock: Callable[[], float], sleep: Callable[[float], None], tries: int = 12) -> ApplyResult:
    current = list(api.get("/rest/db/ignores", folder=folder_id).get("ignore") or [])
    new = plan_change(current, rel, action)
    res = ApplyResult(True, action, rel, rules_before=len(current), rules_after=len(current),
                      host=socket.gethostname())
    if action == "ignore" and new == current:
        res.note, res.verified = "ya estaba ignorada por una regla del panel", True
        return res
    if action == "unignore":
        state = index_state(api, folder_id, rel)
        if state is None:
            state = match_rule(current, rel)[1]
        if state is False and not match_rule(current, rel)[1] and new == current:
            # nada que quitar: ni estaba ignorada ni hay una regla del panel (o una excepción vieja) para esa ruta
            res.ignored_after, res.verified, res.note = False, True, "no estaba ignorada"
            return res
    res.backup = _backup(current, folder_id, state_dir, clock())
    res.backup_sha256 = _sha(current)
    api.post("/rest/db/ignores", folder=folder_id, body={"ignore": new})
    res.changed = [ln for ln in new if ln not in current]
    want = action == "ignore"
    seen = _is_ignored(api, folder_id, rel, want, tries=tries, sleep=sleep)
    if action == "unignore" and seen is True and match_rule(new, rel)[1]:
        # una regla propia del archivo (no la del panel) sigue ignorándola: se la niega.
        # Solo si de verdad la cubre una regla: un índice de Syncthing todavía viejo no justifica una excepción.
        new = plan_change(current, rel, "unignore", negate=True)
        api.post("/rest/db/ignores", folder=folder_id, body={"ignore": new})
        res.changed = [ln for ln in new if ln not in current]
        seen = _is_ignored(api, folder_id, rel, False, tries=tries, sleep=sleep)
    res.rules_after = len(new)
    res.ignored_after = seen
    res.verified = None if seen is None else (seen == want)
    # lo que quedó escrito tiene que ser lo que se mandó: si no, alguien más escribió en el medio
    try:
        written = list(api.get("/rest/db/ignores", folder=folder_id).get("ignore") or [])
    except ApiError:
        written = None
    if written is not None and written != new:
        res.verified = False
        res.note = "otro proceso cambió el .stignore mientras se escribía; volvé a intentar"
    res.ok = res.verified is True       # sin confirmación no se afirma éxito
    if res.note == "" and res.verified is False:
        res.note = ("la regla quedó escrita pero Syncthing todavía no la refleja (carpeta ocupada): mirá de nuevo en un "
                    "minuto; si sigue igual, deshacelo desde el backup")
    elif res.note == "" and res.verified is None:
        res.note = "no pude confirmar con Syncthing si el cambio quedó aplicado; revisalo antes de darlo por hecho"
    _log({"ts": clock(), **asdict(res), "folder": folder_id}, state_dir)
    return res


def _registered_backups(state_dir: Path) -> dict[str, str]:
    """{ruta del backup: sha256 o ''} de lo que este panel registró en su log. Un archivo que solo
    tiene el nombre correcto, pero que el panel nunca escribió, no es restaurable."""
    out: dict[str, str] = {}
    path = state_dir / f"cambios-{socket.gethostname()}.jsonl"
    try:
        for ln in path.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(ln)
            except ValueError:
                continue
            if e.get("backup"):
                out[e["backup"]] = e.get("backup_sha256", "")
    except OSError:
        pass
    return out


def restore_backup(api: SyncthingApi, folder_id: str, backup: str, *, state_dir: Path = STATE_DIR,
                   clock: Callable[[], float] = time.time) -> int:
    """Vuelve el .stignore al contenido de un backup propio del panel. Devuelve cantidad de líneas."""
    p = Path(backup).resolve()
    if p.parent != (state_dir / "backups").resolve() or not p.name.startswith(folder_id + "-"):
        raise ValueError("el backup no es de este panel")
    registered = _registered_backups(state_dir)
    if str(p) not in registered:
        raise ValueError("el backup no figura en el registro del panel")
    lines = p.read_text(encoding="utf-8").splitlines()
    if registered[str(p)] and registered[str(p)] != _sha(lines):
        raise ValueError("el contenido del backup cambió desde que el panel lo guardó")
    with ignores_lock(folder_id, state_dir):
        current = list(api.get("/rest/db/ignores", folder=folder_id).get("ignore") or [])
        before = _backup(current, folder_id, state_dir, clock() + 1)   # lo previo a deshacer también se guarda
        _log({"ts": clock(), "action": "pre-restore", "folder": folder_id, "backup": before,
              "backup_sha256": _sha(current)}, state_dir)
        api.post("/rest/db/ignores", folder=folder_id, body={"ignore": lines})
    _log({"ts": clock(), "action": "restore", "folder": folder_id, "restored_from": str(p)}, state_dir)
    return len(lines)


# ---------------------------------------------------------------- la otra PC (ssh)
def b64(obj: Any) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode()


def remote_cli(hosts: list[str], sub: str, payload: dict, runner: Callable[..., Any] = subprocess.run,
               timeout: float = 40.0) -> dict | None:
    """Corre el CLI del panel en la otra PC. El payload viaja como base64 url-safe: sin shell."""
    for h in hosts:
        try:
            r = runner(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=4", h,
                        "python3", "-B", REMOTE_ENTRY, "--cli", sub, b64(payload)],
                       capture_output=True, text=True, timeout=timeout)
            if r.returncode == 0:
                return json.loads(r.stdout)
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
    return None


def cli(argv: list[str], api: SyncthingApi | None = None, out=print) -> int:
    """Punto de entrada de `wyss-sync.py --cli <subcomando> <payload-b64>` (lo corre la otra PC por ssh)."""
    try:
        sub, raw = argv[0], argv[1]
        p = json.loads(base64.urlsafe_b64decode(raw.encode()))
        api = api or SyncthingApi(*read_config())
        fid = str(p.get("folder", ""))
        if sub == "children":
            rel = valid_rel(p["rel"]) if p.get("rel") else ""
            out(json.dumps({"ok": True, "host": socket.gethostname(),
                            "children": [asdict(c) for c in children(api, fid, rel)]}))
        elif sub == "apply":
            out(json.dumps({"ok": True, "result": asdict(apply_change(api, fid, p["rel"], p["action"]))}))
        elif sub == "restore":
            out(json.dumps({"ok": True, "lines": restore_backup(api, fid, p["backup"])}))
        elif sub == "history":
            harvest(api)
            out(json.dumps({"ok": True, "host": socket.gethostname(),
                            "rows": read_history(limit=int(p.get("limit", 300)), folder=p.get("folder") or None)}))
        elif sub == "settings":
            cfg = api.get(f"/rest/config/folders/{urllib.parse.quote(fid, safe='')}")
            out(json.dumps({"ok": True, "host": socket.gethostname(), "settings": folder_settings(cfg),
                            "path": cfg.get("path", "")}))
        elif sub == "apply-settings":
            out(json.dumps({"ok": True, "result": apply_settings(api, fid, p["changes"])}))
        elif sub == "link-scan":
            from . import links
            links.scan_side(api, str(p["fid"]))
            out(json.dumps({"ok": True, "result": {"ok": True}}))
        elif sub in ("link-inspect", "link-apply", "link-remove"):
            from . import links
            folders = [{"id": f["id"], "path": f["path"], "label": f.get("label") or f["id"]}
                       for f in api.get("/rest/config/folders")]
            home = os.path.expanduser("~")
            if sub == "link-inspect":
                info = links.inspect_path(str(p["path"]), folders, home, must_exist=False)
                out(json.dumps({"ok": True, "host": socket.gethostname(), "info": info}))
            elif sub == "link-apply":
                res = links.apply_side(api, fid=str(p["fid"]), label=str(p["label"])[:80], path=str(p["path"]),
                                       peer=str(p["peer"]), rules=list(p["rules"]), create_dir=bool(p["create_dir"]),
                                       folders=folders)
                out(json.dumps({"ok": True, "result": res}))
            else:
                res = links.remove_side(api, fid=str(p["fid"]), rules=list(p["rules"]))
                out(json.dumps({"ok": True, "result": res}))
        elif sub == "restore-settings":
            out(json.dumps({"ok": restore_settings(api, fid, p["backup"])}))
        else:
            out(json.dumps({"ok": False, "error": f"subcomando desconocido: {sub}"}))
            return 2
        return 0
    except (ValueError, KeyError, IndexError, ApiError, OSError) as e:
        out(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
        return 1


# ---------------------------------------------------------------- filas para la vista
@dataclass
class Row:
    name: str
    rel: str
    kind: str
    mtime: float | None
    here: str          # sync | ignored | stale | unindexed | missing
    there: str         # sync | ignored | unindexed | missing | unknown
    rule: str | None = None
    remote_rule: str | None = None
    disk_here: dict | None = None
    disk_there: dict | None = None


def disk_label(d: dict | None) -> str:
    """'NVMe nvme0n1 · btrfs' / 'HDD sda · ntfs3'. Vacío si no se pudo resolver."""
    if not d:
        return ""
    kind = d.get("kind") or "Disco"
    dev = d.get("device") or ""
    return f"{kind} {dev}".strip() + f" · {d.get('fstype', '')}"


def _state(ignored: bool | None, stale: bool = False) -> str:
    if stale:
        return "stale"
    return "unindexed" if ignored is None else ("ignored" if ignored else "sync")


def merge_rows(local: list[Child], remote: list[dict] | None) -> list[Row]:
    """Une el listado de esta PC con el de la otra. `remote=None`: sin datos de la otra PC."""
    rmap = {c["name"]: c for c in (remote or [])}
    rows: dict[str, Row] = {}
    for c in local:
        r = rmap.get(c.name)
        there = ("unknown" if remote is None else
                 "missing" if r is None else _state(r.get("ignored"), r.get("stale", False)))
        rows[c.name] = Row(c.name, c.rel, c.kind, c.mtime, _state(c.ignored, c.stale), there, c.rule,
                           (r or {}).get("rule"), c.disk, (r or {}).get("disk"))
    for name, r in rmap.items():
        if name not in rows:
            rows[name] = Row(name, r["rel"], r.get("kind", "dir"), r.get("mtime"), "missing",
                             _state(r.get("ignored"), r.get("stale", False)), None, r.get("rule"),
                             None, r.get("disk"))
    return sorted(rows.values(), key=lambda x: x.name.lower())


def human_size(n: int | None, complete: bool = True) -> str:
    if n is None:
        return "…"
    v, unit = float(n), "B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < 1024 or unit == "TB":
            break
        v /= 1024
    s = f"{v:.0f} {unit}" if unit == "B" else f"{v:.1f} {unit}".replace(".", ",")
    return s if complete else "≥ " + s


# ================================================================ historial de sincronización
HISTORY_EVENTS = "ItemFinished,LocalChangeDetected,RemoteChangeDetected"
HISTORY_MAX_BYTES = 5_000_000
ACTIONS = {"update": "recibido", "delete": "borrado", "metadata": "metadatos"}
CHANGES = {"modified": "modificado", "deleted": "borrado", "added": "creado", "new": "creado"}


def host_name() -> str:
    return socket.gethostname()


SELF_PATHS = (".local/state/wyss-sync/", ".local/state/wyss-sync")


def event_to_record(e: dict, host: str) -> dict | None:
    """Un evento de Syncthing -> una fila de historial, o None si no interesa.
    Los archivos del propio panel se descartan: están en una carpeta que se sincroniza, y registrar
    sus cambios generaría cambios nuevos sin fin."""
    d, t = e.get("data") or {}, e.get("type")
    ts = e.get("time", "")
    if str(d.get("item") or d.get("path") or "").startswith(SELF_PATHS):
        return None
    if t == "ItemFinished":
        what = ACTIONS.get(d.get("action"), d.get("action") or "")
        return {"ts": ts, "host": host, "folder": d.get("folder", ""), "path": d.get("item", ""),
                "kind": d.get("type", ""), "what": f"{what} aquí" if not d.get("error") else f"{what} (falló)",
                "error": d.get("error") or ""}
    if t in ("LocalChangeDetected", "RemoteChangeDetected"):
        act = CHANGES.get(d.get("action"), d.get("action") or "")
        side = "en esta PC" if t == "LocalChangeDetected" else "en la otra PC"
        return {"ts": ts, "host": host, "folder": d.get("folderID") or d.get("folder", ""),
                "path": d.get("path", ""), "kind": d.get("type", ""), "what": f"{act} {side}", "error": ""}
    return None


def harvest(api: SyncthingApi, state_dir: Path = STATE_DIR, wait: int = 1, host: str | None = None) -> int:
    """Trae los eventos nuevos de Syncthing y los agrega al historial local. Devuelve cuántos guardó."""
    host = host or host_name()
    state_dir.mkdir(parents=True, exist_ok=True)
    sp = state_dir / f"historial-estado-{host}.json"
    start = api.get("/rest/system/status").get("startTime")
    try:
        st = json.loads(sp.read_text())
    except (OSError, ValueError):
        st = {}
    last = st.get("last", 0) if st.get("start") == start else 0   # al reiniciar Syncthing los ids vuelven a 1
    events = api.get("/rest/events", _timeout=wait + 10, since=last, limit=1000, timeout=wait, events=HISTORY_EVENTS)
    recs = [r for r in (event_to_record(e, host) for e in events) if r]
    path = state_dir / f"historial-{host}.jsonl"
    if recs:
        with open(path, "a", encoding="utf-8") as fh:
            for r in recs:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        if path.stat().st_size > HISTORY_MAX_BYTES:
            lines = path.read_text(encoding="utf-8").splitlines()[-20000:]
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if events:
        last = max(e["id"] for e in events)
    new_state = {"start": start, "last": last}
    if new_state != st:        # reescribirlo en cada ciclo generaba tráfico de sincronización sin fin
        sp.write_text(json.dumps(new_state))
    return len(recs)


def historian(make_api: Callable[[], SyncthingApi], state_dir: Path = STATE_DIR, wait: int = 60,
              stop: Callable[[], bool] = lambda: False, sleep: Callable[[float], None] = time.sleep) -> None:
    """Registro continuo: espera eventos de Syncthing y los agrega al historial. Si Syncthing no
    responde reintenta con espera creciente (5 s hasta 2 min) y vuelve a leer la configuración."""
    backoff, api, last_purge = 5.0, None, -1e9
    while not stop():
        try:
            api = api or make_api()
            harvest(api, state_dir, wait=wait)
            backoff = 5.0
            if time.monotonic() - last_purge > 86400:      # una vez por día
                purge_descartados(state_dir)
                last_purge = time.monotonic()
        except (ApiError, OSError):
            api = None
            sleep(backoff)
            backoff = min(backoff * 2, 120.0)


def read_history(state_dir: Path = STATE_DIR, host: str | None = None, limit: int = 500,
                 folder: str | None = None, text: str = "", only_errors: bool = False) -> list[dict]:
    path = state_dir / f"historial-{host or host_name()}.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    needle = text.lower()
    for ln in reversed(lines):
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if str(r.get("path", "")).startswith(SELF_PATHS):   # registros viejos de antes del filtro
            continue
        if folder and r.get("folder") != folder:
            continue
        if only_errors and not r.get("error"):
            continue
        if needle and needle not in (r.get("path", "") + " " + r.get("what", "")).lower():
            continue
        out.append(r)
        if len(out) >= limit:
            break
    return out


# ================================================================ diferencias entre las 2 PC
def _items(raw: Any, key: str | None = None, cap: int = 200) -> list[dict]:
    rows = raw.get(key, []) if key and isinstance(raw, dict) else raw
    out = []
    for r in rows[:cap]:
        out.append({"name": r.get("name", ""), "size": r.get("size", 0), "modified": r.get("modified", ""),
                    "type": str(r.get("type", "")).replace("FILE_INFO_TYPE_", "").lower(),
                    "deleted": bool(r.get("deleted"))})
    return out


def folder_diff(api: SyncthingApi, folder_id: str, remote_id: str, cap: int = 200) -> dict:
    """Qué le falta a cada lado. Solo consulta; las listas se piden únicamente si hay algo pendiente."""
    st = api.get("/rest/db/status", _timeout=20, folder=folder_id)
    comp = api.get("/rest/db/completion", _timeout=20, folder=folder_id, device=remote_id)
    here = {"items": st.get("needTotalItems", 0) or 0, "bytes": st.get("needBytes", 0) or 0, "list": []}
    there = {"items": comp.get("needItems", 0) or 0, "bytes": comp.get("needBytes", 0) or 0, "list": []}
    if here["items"]:
        raw = api.get("/rest/db/need", _timeout=25, folder=folder_id, page=1, perpage=cap)
        rows = []
        for part in ("progress", "queued", "rest"):
            rows += raw.get(part) or []
        here["list"] = _items(rows, cap=cap)
    if there["items"]:
        raw = api.get("/rest/db/remoteneed", _timeout=25, folder=folder_id, device=remote_id, page=1, perpage=cap)
        there["list"] = _items(raw, "files", cap)
    return {"folder": folder_id, "here": here, "there": there,
            "completion": float(comp.get("completion", 100) or 0), "state": st.get("state", ""),
            "global_items": st.get("globalFiles", 0), "global_bytes": st.get("globalBytes", 0),
            "local_changed": st.get("receiveOnlyTotalItems", 0) or 0}


# ================================================================ conflictos
CONFLICT_RE = re.compile(r"^(?P<base>.+)\.sync-conflict-(?P<date>\d{8})-(?P<time>\d{6})-(?P<dev>[A-Z0-9]{7})(?P<ext>\..*)?$")
SKIP_DIRS = {".cache", ".git", "node_modules", ".stversions", ".Trash", "__pycache__", ".stfolder",
             "descartados"}   # descartados: la papelera del propio panel


@dataclass
class Conflict:
    folder: str
    path: str
    original: str
    rel: str
    when: str
    by: str
    size: int
    orig_exists: bool


def parse_conflict_name(name: str) -> tuple[str, str, str] | None:
    """(nombre original, fecha ISO, dispositivo) de un archivo de conflicto, o None."""
    m = CONFLICT_RE.match(name)
    if not m:
        return None
    d, t = m["date"], m["time"]
    return (m["base"] + (m["ext"] or ""), f"{d[:4]}-{d[4:6]}-{d[6:]} {t[:2]}:{t[2:4]}:{t[4:]}", m["dev"])


def find_conflicts(root: str, folder_id: str, skip: set[str] | None = None, budget: float = 300.0,
                   limit: int = 500, clock: Callable[[], float] = time.monotonic) -> tuple[list[Conflict], bool]:
    """Busca copias *.sync-conflict-* bajo `root`. `skip`: nombres de primer nivel que no se recorren
    (los ignorados por Syncthing). Devuelve (conflictos, completo)."""
    deadline, found, skip = clock() + budget, [], skip or set()
    for dirpath, dirs, files in os.walk(root):
        if clock() > deadline:
            return found, False
        top = dirpath == root
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not (top and d in skip)]
        for f in files:
            parsed = parse_conflict_name(f)
            if not parsed:
                continue
            orig_name, when, by = parsed
            full = os.path.join(dirpath, f)
            try:
                size = os.stat(full).st_size
            except OSError:
                continue
            orig = os.path.join(dirpath, orig_name)
            found.append(Conflict(folder_id, full, orig, os.path.relpath(full, root), when, by, size,
                                  os.path.exists(orig)))
            if len(found) >= limit:
                return found, True
    return found, True


def diff_files(a: str, b: str, max_bytes: int = 1_000_000, max_lines: int = 1500) -> dict:
    """Compara dos archivos. Texto -> diff unificado; binario -> tamaños, fechas y hash."""
    import difflib
    import hashlib

    def read(p: str) -> bytes | None:
        try:
            with open(p, "rb") as fh:
                return fh.read(max_bytes + 1)
        except OSError:
            return None

    def digest(p: str) -> str:
        h = hashlib.sha256()
        with open(p, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
        return h.hexdigest()[:16]

    da, db = read(a), read(b)
    meta = {}
    for tag, p in (("a", a), ("b", b)):
        try:
            st = os.stat(p)
            meta[tag] = {"size": st.st_size, "mtime": st.st_mtime,
                         "sha256": digest(p) if st.st_size <= 200_000_000 else ""}
        except OSError:
            meta[tag] = None
    if da is None or db is None:
        return {"kind": "missing", "meta": meta}
    if meta["a"] and meta["b"] and meta["a"]["sha256"] and meta["a"]["sha256"] == meta["b"]["sha256"]:
        return {"kind": "same", "meta": meta}
    if len(da) > max_bytes or len(db) > max_bytes or b"\0" in da[:8192] or b"\0" in db[:8192]:
        return {"kind": "binary", "meta": meta}
    try:
        ta, tb = da.decode("utf-8").splitlines(), db.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return {"kind": "binary", "meta": meta}
    lines = list(difflib.unified_diff(ta, tb, "original", "copia en conflicto", lineterm="", n=3))
    return {"kind": "text", "meta": meta, "lines": lines[:max_lines], "truncated": len(lines) > max_lines,
            "added": sum(1 for x in lines if x.startswith("+") and not x.startswith("+++")),
            "removed": sum(1 for x in lines if x.startswith("-") and not x.startswith("---"))}


def _safe_move(src: str, dst_dir: Path, tag: str, clock: Callable[[], float]) -> str:
    dst_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(clock()))
    n, name = 0, os.path.basename(src)
    while True:
        dst = dst_dir / (f"{stamp}-{tag}-{name}" if n == 0 else f"{stamp}-{tag}-{n}-{name}")
        if not dst.exists():
            break
        n += 1
    shutil.move(src, dst)
    return str(dst)


DESCARTADOS_DAYS = 30
_DISCARD_RX = re.compile(r"^(\d{8})-(\d{6})-(?:conflicto|original)-")


def _discard_time(name: str) -> float | None:
    """Cuándo se descartó, según el prefijo que pone `_safe_move`. None si el nombre no es del panel."""
    m = _DISCARD_RX.match(name)
    if not m:
        return None
    try:
        return time.mktime(time.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S"))
    except ValueError:
        return None


def _real_descartados(d: Path, state_dir: Path) -> bool:
    """`descartados/` tiene que ser una carpeta real dentro del estado del panel. Si es un enlace (o apunta
    afuera) no se lista ni se borra nada: seguirlo sacaría la purga de su carpeta."""
    try:
        return (not d.is_symlink() and d.is_dir()
                and d.resolve() == (state_dir.resolve() / "descartados"))
    except OSError:
        return False


def descartados_summary(state_dir: Path = STATE_DIR, days: int = DESCARTADOS_DAYS,
                        now: Callable[[], float] = time.time) -> dict:
    d = state_dir / "descartados"
    out = {"files": 0, "bytes": 0, "due_files": 0, "due_bytes": 0}
    cutoff = now() - days * 86400
    if not _real_descartados(d, state_dir):
        return out
    try:
        entries = list(d.iterdir())
    except OSError:
        return out
    for p in entries:
        try:
            if p.is_symlink() or not p.is_file():
                continue
            size = p.stat().st_size
        except OSError:
            continue
        out["files"] += 1
        out["bytes"] += size
        t = _discard_time(p.name)
        if t is not None and t <= cutoff:
            out["due_files"] += 1
            out["due_bytes"] += size
    return out


def purge_descartados(state_dir: Path = STATE_DIR, days: int = DESCARTADOS_DAYS,
                      now: Callable[[], float] = time.time) -> dict:
    """Borra de `descartados/` lo que tiene más de `days` días (`days=0`: todo). Solo archivos regulares
    directamente dentro de esa carpeta; nunca sigue enlaces ni toca nombres que el panel no escribió."""
    d = (state_dir / "descartados")
    out = {"removed": 0, "bytes": 0, "kept": 0}
    if not _real_descartados(d, state_dir):
        return out
    try:
        entries = list(d.iterdir())
    except OSError:
        return out
    cutoff = now() - days * 86400
    for p in entries:
        try:
            if p.is_symlink() or not p.is_file():
                continue
            t = _discard_time(p.name)
            if t is None or (days > 0 and t > cutoff):
                out["kept"] += 1
                continue
            size = p.stat().st_size
            p.unlink()
            out["removed"] += 1
            out["bytes"] += size
        except OSError:
            out["kept"] += 1
    if out["removed"]:
        _log({"ts": now(), "action": "purge-descartados", "days": days, **out}, state_dir)
    return out


def resolve_conflict(c: Conflict, action: str, *, state_dir: Path = STATE_DIR,
                     clock: Callable[[], float] = time.time) -> dict:
    """'discard': guarda la copia en conflicto en descartados/ y la quita. 'use': guarda el original
    en descartados/ y deja la copia en su lugar. Nada se borra sin pasar antes por descartados/."""
    if action not in ("discard", "use"):
        raise ValueError("acción desconocida")
    if parse_conflict_name(os.path.basename(c.path)) is None:
        raise ValueError("no es una copia de conflicto")
    if os.path.dirname(c.path) != os.path.dirname(c.original):
        raise ValueError("ruta de original inconsistente")
    if not os.path.isfile(c.path):
        raise ValueError("la copia en conflicto ya no existe")
    dump = state_dir / "descartados"
    saved = []
    if action == "discard":
        saved.append(_safe_move(c.path, dump, "conflicto", clock))
    else:
        if os.path.exists(c.original):
            saved.append(_safe_move(c.original, dump, "original", clock))
        os.replace(c.path, c.original)
    _log({"ts": clock(), "action": f"conflict-{action}", "folder": c.folder, "rel": c.rel, "saved": saved}, state_dir)
    return {"ok": True, "saved": saved}


# ================================================================ versiones anteriores
def list_versions(api: SyncthingApi, folder_id: str, text: str = "", limit: int = 300) -> list[dict]:
    raw = api.get("/rest/folder/versions", _timeout=30, folder=folder_id)
    needle = text.lower()
    rows = []
    for path, vs in raw.items():
        if needle and needle not in path.lower():
            continue
        vs = sorted(vs, key=lambda v: v["versionTime"], reverse=True)
        rows.append({"path": path, "versions": vs, "latest": vs[0]["versionTime"] if vs else ""})
    rows.sort(key=lambda r: r["latest"], reverse=True)
    return rows[:limit]


def version_file(root: str, rel: str, version_time: str) -> str:
    """Dónde guardó Syncthing esa versión (`.stversions/dir/nombre~AAAAMMDD-HHMMSS.ext`)."""
    stamp = re.sub(r"[-:]", "", version_time[:19]).replace("T", "-")
    d, name = os.path.split(rel)
    base, ext = os.path.splitext(name)
    return os.path.join(root, ".stversions", d, f"{base}~{stamp}{ext}")


def restore_version(api: SyncthingApi, folder_id: str, rel: str, version_time: str, *,
                    state_dir: Path = STATE_DIR, clock: Callable[[], float] = time.time) -> dict:
    rel = valid_rel(rel)
    if not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d|\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", version_time):
        raise ValueError("fecha de versión inválida")
    res = api.post("/rest/folder/versions", body={rel: version_time}, folder=folder_id)
    err = (res or {}).get(rel, "") if isinstance(res, dict) else ""
    _log({"ts": clock(), "action": "restore-version", "folder": folder_id, "rel": rel,
          "version": version_time, "error": err}, state_dir)
    return {"ok": not err, "error": err}


# ================================================================ ajustes por carpeta
FOLDER_TYPES = ("sendreceive", "sendonly", "receiveonly")
VERSIONING = {"": (), "simple": ("keep",), "trashcan": ("cleanoutDays",), "staggered": ("maxAge",)}
TYPE_TEXT = {"sendreceive": "Envía y recibe", "sendonly": "Solo envía", "receiveonly": "Solo recibe"}


def folder_settings(cfg: dict) -> dict:
    v = cfg.get("versioning") or {}
    return {"paused": bool(cfg.get("paused")), "type": cfg.get("type", "sendreceive"),
            "rescanIntervalS": int(cfg.get("rescanIntervalS", 3600) or 0),
            "fsWatcherEnabled": bool(cfg.get("fsWatcherEnabled")), "ignorePerms": bool(cfg.get("ignorePerms")),
            "maxConflicts": int(cfg.get("maxConflicts", 10)),
            "versioning": v.get("type") or "", "versioning_params": dict(v.get("params") or {})}


def validate_changes(ch: dict) -> dict:
    """Solo se aceptan estos campos y valores. Cualquier otra cosa es un error, no se ignora."""
    out: dict = {}
    for k, v in ch.items():
        if k in ("paused", "fsWatcherEnabled", "ignorePerms"):
            if not isinstance(v, bool):
                raise ValueError(f"{k}: se esperaba sí/no")
            out[k] = v
        elif k == "type":
            if v not in FOLDER_TYPES:
                raise ValueError("tipo de carpeta inválido")
            out[k] = v
        elif k == "rescanIntervalS":
            if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 2_592_000:
                raise ValueError("el intervalo de escaneo va de 0 a 2.592.000 s")
            out[k] = v
        elif k == "maxConflicts":
            if not isinstance(v, int) or isinstance(v, bool) or not -1 <= v <= 100:
                raise ValueError("máximo de conflictos: de -1 a 100")
            out[k] = v
        elif k == "versioning":
            if v not in VERSIONING:
                raise ValueError("tipo de versionado inválido")
            out[k] = v
        elif k == "versioning_params":
            if not isinstance(v, dict) or any(not isinstance(x, str) or not x.isdigit() for x in v.values()):
                raise ValueError("los parámetros de versionado son números")
            out[k] = {str(a): b for a, b in v.items()}
        else:
            raise ValueError(f"campo no permitido: {k}")
    return out


def merged_config(cfg: dict, changes: dict) -> dict:
    new = json.loads(json.dumps(cfg))
    for k in ("paused", "type", "rescanIntervalS", "fsWatcherEnabled", "ignorePerms", "maxConflicts"):
        if k in changes:
            new[k] = changes[k]
    if "versioning" in changes or "versioning_params" in changes:
        cur = new.get("versioning") or {}
        vtype = changes.get("versioning", cur.get("type") or "")
        params = changes.get("versioning_params", cur.get("params") or {}) if vtype else {}
        allowed = VERSIONING[vtype]
        new["versioning"] = {"type": vtype, "params": {k: v for k, v in params.items() if k in allowed},
                             "cleanupIntervalS": cur.get("cleanupIntervalS", 3600),
                             "fsPath": cur.get("fsPath", ""), "fsType": cur.get("fsType", "basic")}
    return new


def describe_changes(before: dict, changes: dict) -> list[str]:
    cur = folder_settings(before)
    names = {"paused": "Pausada", "type": "Tipo", "rescanIntervalS": "Escaneo cada (s)",
             "fsWatcherEnabled": "Vigilancia en vivo", "ignorePerms": "Ignorar permisos",
             "maxConflicts": "Máx. copias de conflicto", "versioning": "Versionado",
             "versioning_params": "Parámetros de versionado"}
    return [f"{names[k]}: {cur.get(k)!r} → {v!r}" for k, v in changes.items() if cur.get(k) != v]


def apply_settings(api: SyncthingApi, folder_id: str, changes: dict, *, state_dir: Path = STATE_DIR,
                   clock: Callable[[], float] = time.time) -> dict:
    changes = validate_changes(changes)
    if not folder_id or "/" in folder_id:
        raise ValueError("carpeta inválida")
    path = f"/rest/config/folders/{urllib.parse.quote(folder_id, safe='')}"
    before = api.get(path)
    after = merged_config(before, changes)
    if after == before:
        return {"ok": True, "changed": [], "note": "sin cambios", "backup": ""}
    d = state_dir / "backups"
    d.mkdir(parents=True, exist_ok=True)
    bp = d / f"config-{folder_id}-{host_name()}-{time.strftime('%Y%m%d-%H%M%S', time.localtime(clock()))}.json"
    fd = os.open(bp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(before, fh)
    api.put(path, after)
    got = api.get(path)
    ok = all(folder_settings(got).get(k) == v for k, v in changes.items())
    out = {"ok": ok, "changed": describe_changes(before, changes), "backup": str(bp), "host": host_name(),
           "note": "" if ok else "Syncthing no reflejó todos los cambios"}
    _log({"ts": clock(), "action": "settings", "folder": folder_id, **out}, state_dir)
    return out


def restore_settings(api: SyncthingApi, folder_id: str, backup: str, *, state_dir: Path = STATE_DIR) -> bool:
    p = Path(backup).resolve()
    if p.parent != (state_dir / "backups").resolve() or not p.name.startswith(f"config-{folder_id}-"):
        raise ValueError("el backup no es de este panel")
    if str(p) not in _registered_backups(state_dir):
        raise ValueError("el backup no figura en el registro del panel")
    cfg = json.loads(p.read_text(encoding="utf-8"))
    if cfg.get("id") != folder_id:
        raise ValueError("el backup es de otra carpeta")
    api.put(f"/rest/config/folders/{urllib.parse.quote(folder_id, safe='')}", cfg)
    return True
