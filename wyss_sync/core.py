"""Modelo de Wyss Sync: lee la API local de Syncthing y sondea la otra PC por ssh.

Solo lectura. La clave de la API se lee de config.xml en cada arranque y nunca se
imprime ni se guarda. Todo lo que toca red o disco entra por parámetros para poder
probarlo sin Syncthing.
"""
from __future__ import annotations

import inspect
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable

CONFIG_XML = Path("~/.local/state/syncthing/config.xml").expanduser()
LAPTOP_CHASSIS = {8, 9, 10, 11, 14, 30, 31, 32}


class ApiError(RuntimeError):
    """Syncthing no responde o rechazó la consulta."""


def read_config(path: Path = CONFIG_XML) -> tuple[str, str]:
    """Devuelve (base_url, api_key) de la config local de Syncthing."""
    try:
        gui = ET.parse(path).getroot().find("gui")
        key = gui.findtext("apikey") or ""
        addr = gui.findtext("address") or "127.0.0.1:8384"
        tls = gui.get("tls") == "true"
    except (OSError, ET.ParseError, AttributeError) as e:
        raise ApiError(f"no pude leer la config de Syncthing: {e}") from None
    if not key:
        raise ApiError("la config de Syncthing no tiene clave de API")
    return f"{'https' if tls else 'http'}://{addr}", key


class SyncthingApi:
    def __init__(self, base: str, key: str, timeout: float = 4.0,
                 opener: Callable[..., Any] = urllib.request.urlopen):
        self._base, self._key, self._timeout, self._opener = base, key, timeout, opener

    def _call(self, path: str, params: dict, data: bytes | None, timeout: float | None = None,
              method: str | None = None) -> Any:
        url = self._base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"X-API-Key": self._key}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener(req, timeout=timeout or self._timeout) as r:
                raw = r.read().decode()
                return json.loads(raw) if raw.strip() else {}
        except Exception as e:  # urllib, json y timeouts: el panel solo necesita saber que falló
            raise ApiError(f"{path}: {type(e).__name__}") from None

    def get(self, path: str, _timeout: float | None = None, **params: Any) -> Any:
        return self._call(path, params, None, timeout=_timeout)

    def put(self, path: str, body: Any, **params: Any) -> Any:
        return self._call(path, params, json.dumps(body).encode(), timeout=20.0, method="PUT")

    def post(self, path: str, body: Any = None, **params: Any) -> Any:
        return self._call(path, params, json.dumps(body).encode() if body is not None else b"", timeout=20.0)


def mount_table(path: str = "/proc/self/mountinfo") -> list[dict]:
    rows = []
    try:
        with open(path) as fh:
            for line in fh:
                left, _, right = line.partition(" - ")
                f, g = left.split(), right.split()
                if len(f) >= 5 and len(g) >= 2:
                    rows.append({"mount": f[4].replace("\\040", " "), "fstype": g[0], "source": g[1]})
    except OSError:
        pass
    return rows


def _sys(path: str) -> str:
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def physical_disk(source: str) -> dict:
    """Disco físico detrás de un dispositivo de bloque (partición -> disco), desde /sys."""
    if not source.startswith("/dev/"):
        return {}
    name = os.path.basename(source)
    real = os.path.realpath(f"/sys/class/block/{name}")
    parent = os.path.basename(os.path.dirname(real)) if os.path.exists(os.path.join(real, "partition")) else name
    base = f"/sys/block/{parent}"
    rota = _sys(base + "/queue/rotational")
    kind = "NVMe" if parent.startswith("nvme") else ("HDD" if rota == "1" else ("SSD" if rota == "0" else ""))
    try:
        size = int(_sys(base + "/size") or 0) * 512
    except ValueError:
        size = 0
    return {"device": parent, "model": _sys(base + "/device/model"), "kind": kind, "size": size}


def disk_info(path: str, mounts: list[dict] | None = None) -> dict | None:
    """Dónde vive realmente `path`: montaje, sistema de archivos, disco físico y uso.
    Sigue enlaces simbólicos: un enlace de Home hacia el HDD se resuelve al HDD."""
    try:
        rp = os.path.realpath(path)
        os.stat(rp)          # tocar la ruta dispara el automount antes de leer la tabla
    except OSError:
        return None
    mounts = mounts if mounts is not None else mount_table()

    def best_for(table: list[dict]) -> dict | None:
        best = None
        for m in table:
            mp = m["mount"]
            if m["fstype"] != "autofs" and (mp == "/" or rp == mp or rp.startswith(mp.rstrip("/") + "/")):
                if best is None or len(mp) >= len(best["mount"]):
                    best = m
        return best

    best = best_for(mounts)
    if best is None or best["mount"] == "/":
        fresh = mount_table()      # un automount recién disparado no estaba en la tabla leída antes
        best = best_for(fresh) or best
    if best is None:
        return None
    try:
        du = shutil.disk_usage(rp)
        total, used, free = du.total, du.used, du.free
    except OSError:
        total = used = free = 0
    return {"mount": best["mount"], "fstype": best["fstype"], "source": best["source"],
            "total": total, "used": used, "free": free, "real": rp, **physical_disk(best["source"])}


def probe() -> dict:
    """Datos de esta máquina. Se ejecuta acá y, enviado por ssh (módulo entero), en la otra PC."""
    import socket
    import xml.etree.ElementTree as ET

    out = {"host": socket.gethostname(), "os": "", "chassis": None, "folders": {}, "disks": {}}
    try:
        with open("/etc/os-release") as fh:
            for line in fh:
                if line.startswith("PRETTY_NAME="):
                    out["os"] = line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    try:
        with open("/sys/class/dmi/id/chassis_type") as fh:
            out["chassis"] = int(fh.read())
    except (OSError, ValueError):
        pass
    for p in ("~/.local/state/syncthing/config.xml", "~/.config/syncthing/config.xml"):
        p = os.path.expanduser(p)
        if os.path.exists(p):
            for f in ET.parse(p).getroot().findall("folder"):
                out["folders"][f.get("id")] = f.get("path")
            break
    table = mount_table()
    for fid, fpath in out["folders"].items():
        out["disks"][fid] = disk_info(fpath, table)
    return out


def probe_script() -> str:
    """Este mismo módulo (solo stdlib) más la llamada: lo que corre en la otra PC por ssh."""
    return inspect.getsource(sys.modules[__name__]) + "\nprint(json.dumps(probe()))\n"


def probe_remote(host: str, runner: Callable[..., Any] = subprocess.run,
                 timeout: float = 8.0) -> dict | None:
    """Corre probe() en la otra PC por ssh (sin contraseña). None si no responde."""
    script = probe_script()
    try:
        r = runner(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=4", host, "python3", "-"],
                   input=script, capture_output=True, text=True, timeout=timeout)
        return json.loads(r.stdout) if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def kind_of(chassis: int | None) -> str:
    return "laptop" if chassis in LAPTOP_CHASSIS else "desktop"


def folder_state(*, paused: bool, status: dict | None, completion: dict | None,
                 remote_connected: bool, remote_paused: bool) -> tuple[str, float | None]:
    """Estado visible de un vínculo y, si aplica, el avance en %."""
    status = status or {}
    state = status.get("state", "")
    if paused or remote_paused:
        return "paused", None
    if state == "error" or status.get("errors") or status.get("pullErrors"):
        return "error", None
    if not remote_connected:
        return "offline", None
    if state in ("scanning", "scan-waiting"):
        return "scanning", None
    pct = None
    if completion and completion.get("completion") is not None:
        pct = float(completion["completion"])
    if pct is None and status.get("globalBytes"):
        pct = 100.0 * (1 - status.get("needBytes", 0) / status["globalBytes"])
    working = state in ("syncing", "sync-preparing", "sync-waiting", "cleaning")
    behind = (pct is not None and pct < 100) or status.get("needTotalItems", 0) > 0
    if working or behind:
        return "syncing", None if pct is None else max(0.0, min(99.9, pct))
    return "synced", None


def disk_summary(disks: dict | None) -> list[dict]:
    """Discos distintos que usan las carpetas, sin repetir (varias carpetas pueden compartir uno)."""
    seen, out = set(), []
    for d in (disks or {}).values():
        if d and (d["source"], d["mount"]) not in seen:
            seen.add((d["source"], d["mount"]))
            out.append(d)
    return out


class Collector:
    """Arma el modelo que dibuja la ventana. La sonda remota se cachea `ttl` segundos."""

    def __init__(self, api: SyncthingApi, local_probe: Callable[[], dict] = probe,
                 remote_probe: Callable[[str], dict | None] = probe_remote,
                 ttl: float = 60.0, clock: Callable[[], float] = time.monotonic):
        self._api, self._local_probe, self._remote_probe = api, local_probe, remote_probe
        self._ttl, self._clock = ttl, clock
        self._remote: dict | None = None
        self._remote_at = -1e9
        self._net: tuple[float, int, int] | None = None   # (instante, bytes recibidos, bytes enviados)

    @property
    def api(self) -> SyncthingApi:
        return self._api

    def _remote_info(self, hosts: list[str]) -> dict | None:
        """Prueba cada nombre en orden (alias de ssh primero, IP de la conexión después)."""
        if not hosts:
            return None
        if self._clock() - self._remote_at >= self._ttl:
            fresh = next((r for h in hosts if (r := self._remote_probe(h))), None)
            self._remote = fresh or self._remote  # sin ssh se conserva la última sonda buena
            self._remote_at = self._clock()
        return self._remote

    def collect(self) -> dict:
        api = self._api
        me = api.get("/rest/system/status")["myID"]
        devices = {d["deviceID"]: d for d in api.get("/rest/config/devices")}
        folders = api.get("/rest/config/folders")
        remote_id = next((d["deviceID"] for f in folders for d in f.get("devices", [])
                          if d["deviceID"] != me), None)
        conn = (api.get("/rest/system/connections").get("connections", {}).get(remote_id, {})
                if remote_id else {})
        connected = bool(conn.get("connected"))
        now, rate_in, rate_out = self._clock(), 0.0, 0.0
        tin, tout = int(conn.get("inBytesTotal") or 0), int(conn.get("outBytesTotal") or 0)
        if self._net and connected and now > self._net[0] and tin >= self._net[1] and tout >= self._net[2]:
            dt = now - self._net[0]
            rate_in, rate_out = (tin - self._net[1]) / dt, (tout - self._net[2]) / dt
        self._net = (now, tin, tout) if connected else None
        address = (conn.get("address") or "").split("://")[-1].rsplit(":", 1)[0] or None
        # con la otra PC apagada no hay dirección de conexión: se reintenta la última sonda
        alias = devices.get(remote_id, {}).get("name")
        remote = (self._remote_info([h for h in (alias, address) if h])
                  if connected else self._remote)
        local = self._local_probe()

        rows = []
        for f in folders:
            if remote_id not in [d["deviceID"] for d in f.get("devices", [])]:
                continue
            fid = f["id"]
            try:
                status = api.get("/rest/db/status", folder=fid)
            except ApiError:
                status = None
            completion = None
            if connected and not f.get("paused"):
                try:
                    completion = api.get("/rest/db/completion", folder=fid, device=remote_id)
                except ApiError:
                    completion = None
            state, pct = folder_state(paused=bool(f.get("paused")), status=status,
                                      completion=completion, remote_connected=connected,
                                      remote_paused=bool(conn.get("paused")))
            need = (status or {}).get("needTotalItems", 0) or 0
            rows.append({
                "id": fid, "label": f.get("label") or fid, "type": f.get("type", ""),
                "local_path": f.get("path", ""),
                "remote_path": ((remote or {}).get("folders") or {}).get(fid),
                "local_disk": (local.get("disks") or {}).get(fid),
                "remote_disk": ((remote or {}).get("disks") or {}).get(fid),
                "state": state, "percent": pct, "need_items": need,
                "sync_bytes": (status or {}).get("globalBytes", 0) or 0,
                "sync_files": (status or {}).get("globalFiles", 0) or 0,
                "need_bytes": (status or {}).get("needBytes", 0) or 0,
            })
        return {
            "local": {"name": devices.get(me, {}).get("name") or local["host"],
                      "os": local["os"], "kind": kind_of(local["chassis"]), "online": True, "id": me,
                      "disks": disk_summary(local.get("disks"))},
            "remote": {"name": devices.get(remote_id, {}).get("name") or (remote or {}).get("host", "—"),
                       "os": (remote or {}).get("os", ""), "kind": kind_of((remote or {}).get("chassis")),
                       "online": connected, "address": address, "ssh": remote is not None, "id": remote_id,
                       "rate_in": rate_in, "rate_out": rate_out, "bytes_in": tin, "bytes_out": tout,
                       "disks": disk_summary((remote or {}).get("disks"))},
            "folders": rows,
            "fetched_at": time.time(),
        }
