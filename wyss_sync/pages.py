"""Pestañas de Wyss Sync: historial, diferencias, conflictos y versiones, ajustes por carpeta."""
from __future__ import annotations

import html
import os
import subprocess
import time
from dataclasses import asdict

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
                             QLineEdit, QPlainTextEdit, QPushButton, QRadioButton, QSpinBox, QSplitter,
                             QTabBar, QTextEdit, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from . import links, manage
from .core import ApiError, SyncthingApi
from .detail import BTN, BTN_PRIMARY, MUTED, TEXT, Task, style_tree

OK, WARN, BAD, INFO = "#34d399", "#fbbf24", "#f87171", "#38bdf8"


class Context:
    """Lo que las pestañas necesitan saber del último modelo: carpetas, equipos y cómo llegar a la otra PC."""

    def __init__(self, api: SyncthingApi | None):
        self.api, self.model = api, None

    def update(self, model: dict) -> None:
        self.model = model

    @property
    def ready(self) -> bool:
        return bool(self.api and self.model)

    @property
    def folders(self) -> list[dict]:
        return (self.model or {}).get("folders", [])

    @property
    def local_name(self) -> str:
        return (self.model or {}).get("local", {}).get("name", "esta PC")

    @property
    def remote(self) -> dict:
        return (self.model or {}).get("remote", {})

    @property
    def remote_online(self) -> bool:
        return bool(self.remote.get("online"))

    @property
    def hosts(self) -> list[str]:
        r = self.remote
        return [h for h in (r.get("name"), r.get("address")) if h]


def tree(headers: list[str], widths: dict[int, int] | None = None) -> QTreeWidget:
    t = QTreeWidget()
    t.setColumnCount(len(headers))
    t.setHeaderLabels(headers)
    t.setUniformRowHeights(True)
    t.setRootIsDecorated(False)
    style_tree(t)
    hdr = t.header()
    hdr.setStretchLastSection(True)
    for c, w in (widths or {}).items():
        hdr.setSectionResizeMode(c, hdr.ResizeMode.Interactive)
        t.setColumnWidth(c, w)
    return t


def button(text: str, primary: bool = False) -> QPushButton:
    b = QPushButton(text)
    b.setStyleSheet(BTN_PRIMARY if primary else BTN)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    return b


def note(text: str = "") -> QLabel:
    l = QLabel(text)
    l.setWordWrap(True)
    l.setStyleSheet(f"color:{MUTED};font-size:12px;")
    return l


def field_style() -> str:
    return (f"QLineEdit,QComboBox,QSpinBox{{color:{TEXT};background:rgba(255,255,255,18);"
            "border:1px solid rgba(255,255,255,46);border-radius:4px;padding:5px 8px;font-size:12px;}"
            "QLineEdit:focus,QComboBox:focus,QSpinBox:focus{border:1px solid #ffffff;}"
            f"QComboBox QAbstractItemView{{background:#0f2447;color:{TEXT};selection-background-color:#1d4ed8;}}"
            f"QCheckBox{{color:{TEXT};font-size:13px;spacing:8px;}}")


def human_time(ts: str) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return ts[:19].replace("T", " ")


class Confirm(QDialog):
    """Diálogo de confirmación: qué se va a cambiar, a qué PC, y los avisos. Nada corre sin 'Aplicar'."""

    def __init__(self, parent, title: str, lines: list[str], warnings: list[str], local_name: str,
                 remote_name: str | None, ok_text: str = "Aplicar", choose_scope: bool = True):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(560)
        self.setStyleSheet(f"QDialog{{background:#0f2447;}} QLabel{{color:{TEXT};font-size:13px;}}"
                           f"QRadioButton{{color:{TEXT};font-size:13px;spacing:8px;}}"
                           "QRadioButton::indicator{width:14px;height:14px;border-radius:9px;"
                           "border:2px solid #7dd3fc;background:transparent;}"
                           "QRadioButton::indicator:checked{background:qradialgradient(cx:0.5,cy:0.5,radius:0.5,"
                           "fx:0.5,fy:0.5,stop:0 #38bdf8,stop:0.55 #38bdf8,stop:0.6 transparent);}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(10)
        h = QLabel(title)
        h.setStyleSheet(f"color:{TEXT};font-size:18px;font-weight:700;")
        lay.addWidget(h)
        body = QLabel("<br>".join(f"<span style='font-family:monospace;color:#7dd3fc'>{html.escape(x)}</span>"
                                  for x in lines[:14]) + (f"<br>y {len(lines) - 14} más…" if len(lines) > 14 else ""))
        body.setTextFormat(Qt.TextFormat.RichText)
        body.setWordWrap(True)
        lay.addWidget(body)
        self.both = QRadioButton(f"Las dos PC ({local_name} y {remote_name})" if remote_name else "")
        self.here = QRadioButton(f"Solo esta PC ({local_name})")
        if choose_scope:
            if remote_name:
                self.both.setChecked(True)
                lay.addWidget(self.both)
            else:
                self.here.setChecked(True)
            lay.addWidget(self.here)
        for w in warnings:
            l = QLabel("⚠ " + w)
            l.setWordWrap(True)
            l.setStyleSheet("color:#fbbf24;font-size:12px;")
            lay.addWidget(l)
        row = QHBoxLayout()
        row.addStretch()
        c, ok = button("Cancelar"), button(ok_text, primary=True)
        c.clicked.connect(self.reject)
        ok.clicked.connect(self.accept)
        ok.setDefault(True)
        row.addWidget(c)
        row.addWidget(ok)
        lay.addLayout(row)

    def both_pcs(self) -> bool:
        return self.both.isChecked()


class Page(QWidget):
    """Base: carga al mostrarse y se refresca solo mientras está a la vista."""
    refresh_ms = 0

    def __init__(self, ctx: Context):
        super().__init__()
        self.ctx, self._tasks = ctx, []
        self._timer = QTimer(self)
        if self.refresh_ms:
            self._timer.setInterval(self.refresh_ms)
            self._timer.timeout.connect(self.load)

    def showEvent(self, e) -> None:
        super().showEvent(e)
        self.load()
        if self.refresh_ms:
            self._timer.start()

    def hideEvent(self, e) -> None:
        self._timer.stop()
        super().hideEvent(e)

    def load(self) -> None:  # pragma: no cover - lo define cada página
        pass

    def run(self, fn, done) -> None:
        t = Task(fn)
        t.done.connect(done)
        self._tasks = [x for x in self._tasks if x.isRunning()] + [t]
        t.start()


# ================================================================ Historial
class HistoryPage(Page):
    refresh_ms = 15000

    def __init__(self, ctx: Context):
        super().__init__(ctx)
        self.setStyleSheet(field_style())
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 0)
        row = QHBoxLayout()
        self.folder = QComboBox()
        self.folder.addItem("Todas las carpetas", "")
        self.who = QComboBox()
        for label, v in (("Las dos PC", ""), ("Esta PC", "here"), ("La otra PC", "there")):
            self.who.addItem(label, v)
        self.text = QLineEdit()
        self.text.setPlaceholderText("Buscar por ruta o por acción…")
        self.errors = QCheckBox("Solo errores")
        self.btn = button("Actualizar")
        for w in (self.folder, self.who, self.text, self.errors, self.btn):
            row.addWidget(w)
        row.setStretch(2, 1)
        lay.addLayout(row)
        self.table = tree(["Fecha", "PC", "Carpeta", "Qué pasó", "Ruta", "Detalle"], {0: 150, 1: 110, 2: 110, 3: 190, 4: 380})
        lay.addWidget(self.table, 1)
        self.info = note()
        lay.addWidget(self.info)
        self.folder.currentIndexChanged.connect(self.load)
        self.who.currentIndexChanged.connect(self.load)
        self.errors.toggled.connect(self.load)
        self.text.returnPressed.connect(self.load)
        self.btn.clicked.connect(self.load)
        self._seen_folders: list[str] = []

    def load(self) -> None:
        if not self.ctx.ready:
            return
        folders = [f["id"] for f in self.ctx.folders]
        if folders != self._seen_folders:
            self._seen_folders = folders
            cur = self.folder.currentData()
            self.folder.blockSignals(True)
            self.folder.clear()
            self.folder.addItem("Todas las carpetas", "")
            for f in self.ctx.folders:
                self.folder.addItem(f["label"], f["id"])
            i = self.folder.findData(cur)
            self.folder.setCurrentIndex(max(i, 0))
            self.folder.blockSignals(False)
        fid, who = self.folder.currentData() or None, self.who.currentData()
        text, errs, api = self.text.text(), self.errors.isChecked(), self.ctx.api
        hosts, online = self.ctx.hosts, self.ctx.remote_online

        def work():
            try:
                manage.harvest(api)
            except (ApiError, OSError):
                pass
            rows = [] if who == "there" else manage.read_history(folder=fid, text=text, only_errors=errs, limit=600)
            remote_ok = None
            if who != "here" and online:
                r = manage.remote_cli(hosts, "history", {"folder": fid or "", "limit": 600})
                remote_ok = bool(r and r.get("ok"))
                if remote_ok:
                    rows += [x for x in r["rows"]
                             if (not text or text.lower() in (x.get("path", "") + x.get("what", "")).lower())
                             and (not errs or x.get("error"))]
            rows.sort(key=lambda x: x.get("ts", ""), reverse=True)
            return rows[:800], remote_ok

        self.run(work, self._loaded)

    def _loaded(self, res) -> None:
        if isinstance(res, Exception):
            self.info.setText(f"No pude leer el historial: {res}")
            return
        rows, remote_ok = res
        labels = {f["id"]: f["label"] for f in self.ctx.folders}
        self.table.clear()
        for r in rows:
            it = QTreeWidgetItem(self.table)
            for c, v in enumerate((human_time(r.get("ts", "")), r.get("host", ""), labels.get(r.get("folder"), r.get("folder", "")),
                                   r.get("what", ""), r.get("path", ""), r.get("error", ""))):
                it.setText(c, v)
            it.setToolTip(4, r.get("path", ""))
            if r.get("error"):
                for c in range(6):
                    it.setForeground(c, QColor(BAD))
        svc = subprocess.run(["systemctl", "--user", "is-active", "wyss-sync-historian"],
                             capture_output=True, text=True).stdout.strip()
        mode = ("registro continuo activo" if svc == "active"
                else "se registra mientras el panel está abierto; el servicio de registro continuo no está instalado")
        extra = "" if remote_ok in (None, True) else " · sin respuesta de la otra PC (ssh)"
        self.info.setText(f"{len(rows)} registros · {mode}{extra}")


# ================================================================ Diferencias
class DiffsPage(Page):
    refresh_ms = 10000

    def __init__(self, ctx: Context):
        super().__init__(ctx)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 0)
        self.summary = tree(["Carpeta", "Falta acá", "Falta allá", "Completo", "Estado"], {0: 200, 1: 190, 2: 190, 3: 100})
        self.summary.setMaximumHeight(170)
        lay.addWidget(self.summary)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.left = tree(["Falta acá", "Tamaño", "Tipo"], {1: 90})
        self.right = tree(["Falta allá", "Tamaño", "Tipo"], {1: 90})
        split.addWidget(self.left)
        split.addWidget(self.right)
        lay.addWidget(split, 1)
        self.info = note("Cuando las dos PC están al día, las listas quedan vacías: es lo normal.")
        lay.addWidget(self.info)
        self.summary.itemSelectionChanged.connect(self._show)
        self._data: dict[str, dict] = {}

    def load(self) -> None:
        if not self.ctx.ready or not self.ctx.remote.get("id"):
            return
        api, rid = self.ctx.api, self.ctx.remote["id"]
        ids = [f["id"] for f in self.ctx.folders]

        def work():
            out = {}
            for fid in ids:
                try:
                    out[fid] = manage.folder_diff(api, fid, rid)
                except ApiError as e:
                    out[fid] = {"error": str(e)}
            return out

        self.run(work, self._loaded)

    def _loaded(self, res) -> None:
        if isinstance(res, Exception):
            self.info.setText(f"No pude consultar a Syncthing: {res}")
            return
        self._data = res
        sel = (self.summary.selectedItems() or [None])[0]
        keep = sel.data(0, Qt.ItemDataRole.UserRole) if sel else None
        self.summary.clear()
        labels = {f["id"]: f["label"] for f in self.ctx.folders}
        for fid, d in res.items():
            it = QTreeWidgetItem(self.summary)
            it.setData(0, Qt.ItemDataRole.UserRole, fid)
            it.setText(0, labels.get(fid, fid))
            if "error" in d:
                it.setText(4, "sin datos")
                continue
            it.setText(1, f"{d['here']['items']} · {manage.human_size(d['here']['bytes'])}")
            it.setText(2, f"{d['there']['items']} · {manage.human_size(d['there']['bytes'])}")
            it.setText(3, f"{d['completion']:.0f} %")
            behind = d["here"]["items"] or d["there"]["items"]
            if behind:
                it.setText(4, "con pendientes")
            elif d["completion"] < 100:
                it.setText(4, f"sincronizando {d['completion']:.0f} %")   # Syncthing aún no da la lista
            else:
                it.setText(4, "al día")
            it.setForeground(4, QColor(OK if it.text(4) == "al día" else (WARN if behind else INFO)))
            if fid == keep:
                it.setSelected(True)
        if not self.summary.selectedItems() and self.summary.topLevelItemCount():
            self.summary.topLevelItem(0).setSelected(True)
        self._show()

    def _show(self) -> None:
        sel = (self.summary.selectedItems() or [None])[0]
        for t in (self.left, self.right):
            t.clear()
        if not sel:
            return
        d = self._data.get(sel.data(0, Qt.ItemDataRole.UserRole), {})
        for t, side in ((self.left, "here"), (self.right, "there")):
            for r in (d.get(side) or {}).get("list", []):
                it = QTreeWidgetItem(t)
                it.setText(0, r["name"] + ("  (borrado)" if r["deleted"] else ""))
                it.setText(1, manage.human_size(r["size"]))
                it.setText(2, r["type"])
        if d and "error" not in d:
            cap = 200
            extra = [f"{x} de {d[s]['items']} (se muestran {cap})" for s, x in (("here", len(d["here"]["list"])),
                     ("there", len(d["there"]["list"]))) if d[s]["items"] > len(d[s]["list"])]
            self.info.setText("Se muestran los primeros 200 de cada lado. " + " · ".join(extra) if extra else
                              f"{d['global_items']} elementos en total · {manage.human_size(d['global_bytes'])}"
                              + (f" · {d['local_changed']} cambios locales sin enviar" if d["local_changed"] else ""))


# ================================================================ Conflictos y versiones
class ConflictsPage(Page):
    def __init__(self, ctx: Context):
        super().__init__(ctx)
        self.setStyleSheet(field_style())
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 0)
        self.sub = QTabBar()
        self.sub.addTab("Conflictos")
        self.sub.addTab("Versiones anteriores")
        self.sub.setDrawBase(False)
        self.sub.setStyleSheet(
            f"QTabBar::tab{{color:{MUTED};padding:6px 12px;border:none;border-bottom:2px solid transparent;font-size:12px;}}"
            f"QTabBar::tab:selected{{color:{TEXT};border-bottom:2px solid #38bdf8;}}")
        lay.addWidget(self.sub)

        # ---- conflictos / versiones comparten el visor de diferencias
        bar = QHBoxLayout()
        self.folder = QComboBox()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filtrar por nombre…")
        self.scan = button("Buscar conflictos", primary=True)
        for w in (self.folder, self.search, self.scan):
            bar.addWidget(w)
        bar.setStretch(1, 1)
        lay.addLayout(bar)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.list = tree(["Archivo", "Fecha", "Tamaño", "Original"], {0: 380, 1: 130, 2: 72, 3: 72})
        self.list.setRootIsDecorated(True)
        self.list.setTextElideMode(Qt.TextElideMode.ElideMiddle)   # se ve el comienzo y el nombre del archivo
        self.list.setSelectionMode(QTreeWidget.SelectionMode.SingleSelection)
        split.addWidget(self.list)
        self.viewer = QTextEdit()
        self.viewer.setReadOnly(True)
        mono = QFont("Hack")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        mono.setPixelSize(12)
        self.viewer.setFont(mono)
        self.viewer.setStyleSheet(f"QTextEdit{{background:rgba(0,0,0,70);color:{TEXT};border:1px solid rgba(255,255,255,46);"
                                  "border-radius:6px;}}")
        split.addWidget(self.viewer)
        split.setSizes([680, 380])
        lay.addWidget(split, 1)
        acts = QHBoxLayout()
        self.b_use = button("Quedarme con la copia…")
        self.b_drop = button("Descartar la copia…")
        self.b_restore = button("Restaurar esta versión…", primary=True)
        self.b_purge = button("Vaciar descartados…")
        for b in (self.b_use, self.b_drop, self.b_restore, self.b_purge):
            acts.addWidget(b)
        acts.addStretch()
        self.info = note()
        lay.addLayout(acts)
        lay.addWidget(self.info)

        self.sub.currentChanged.connect(self._mode)
        self.scan.clicked.connect(self._go)
        self.folder.currentIndexChanged.connect(self._go_if_versions)
        self.search.returnPressed.connect(self._go_if_versions)
        self.list.itemSelectionChanged.connect(self._selected)
        self.b_use.clicked.connect(lambda: self._resolve("use"))
        self.b_drop.clicked.connect(lambda: self._resolve("discard"))
        self.b_restore.clicked.connect(self._restore)
        self.b_purge.clicked.connect(self._purge)
        self._conflicts: dict[str, manage.Conflict] = {}
        self._folders_key: list[str] = []
        self._mode(0)

    # ---------- comunes
    def load(self) -> None:
        if not self.ctx.ready:
            return
        ids = [f["id"] for f in self.ctx.folders]
        if ids != self._folders_key:
            self._folders_key = ids
            self.folder.blockSignals(True)
            self.folder.clear()
            self.folder.addItem("Todas las carpetas", "")
            for f in self.ctx.folders:
                self.folder.addItem(f["label"], f["id"])
            self.folder.blockSignals(False)

    def _mode(self, i: int) -> None:
        conflicts = i == 0
        self.scan.setText("Buscar conflictos" if conflicts else "Buscar versiones")
        self.b_use.setVisible(conflicts)
        self.b_drop.setVisible(conflicts)
        self.b_restore.setVisible(not conflicts)
        self.list.clear()
        self.viewer.clear()
        self.list.setHeaderLabels(["Archivo", "Fecha", "Tamaño", "Original"] if conflicts
                                 else ["Archivo / versión", "Modificado", "Tamaño", "Versiones"])
        self.list.setRootIsDecorated(not conflicts)
        self.info.setText("Busca copias *.sync-conflict-* en las carpetas (puede tardar en Home)." if conflicts else
                          "Versiones que Syncthing guardó antes de pisar o borrar un archivo. Solo las carpetas con versionado activo.")
        self._selected()

    def _go(self) -> None:
        (self._scan_conflicts if self.sub.currentIndex() == 0 else self._scan_versions)()

    def _go_if_versions(self) -> None:
        if self.sub.currentIndex() == 1:
            self._scan_versions()

    def _folder_rows(self) -> list[dict]:
        fid = self.folder.currentData()
        return [f for f in self.ctx.folders if not fid or f["id"] == fid]

    def _selected(self) -> None:
        it = (self.list.selectedItems() or [None])[0]
        is_conf = self.sub.currentIndex() == 0
        data = it.data(0, Qt.ItemDataRole.UserRole) if it else None
        self.b_use.setEnabled(bool(is_conf and data))
        self.b_drop.setEnabled(bool(is_conf and data))
        self.b_restore.setEnabled(bool(not is_conf and data and data.get("version")))
        if not data:
            return
        self.run(lambda: self._make_diff(data), self._show_diff)

    # ---------- conflictos
    def _scan_conflicts(self) -> None:
        rows, api = self._folder_rows(), self.ctx.api
        needle = self.search.text().lower()
        self.scan.setEnabled(False)
        self.info.setText("Buscando… Home tarda segundos; HDD Data puede tardar 1 o 2 minutos. Podés seguir usando el panel.")

        def work():
            out, complete = [], True
            for f in rows:
                root = f["local_path"]
                lines = (api.get("/rest/db/ignores", folder=f["id"]).get("ignore") or [])
                try:
                    top = [d for d in os.listdir(root)]
                except OSError:
                    continue
                skip = {d for d in top if manage.match_rule(lines, d)[1]}
                found, done = manage.find_conflicts(root, f["id"], skip=skip)
                complete = complete and done
                out += [c for c in found if needle in c.rel.lower()]
            return out, complete

        self.run(work, self._conflicts_found)

    def _conflicts_found(self, res) -> None:
        self.scan.setEnabled(True)
        if isinstance(res, Exception):
            self.info.setText(f"No pude buscar: {res}")
            return
        found, complete = res
        self.list.clear()
        self._conflicts = {}
        for c in found:
            it = QTreeWidgetItem(self.list)
            it.setText(0, c.rel)
            it.setText(1, c.when)
            it.setText(2, manage.human_size(c.size))
            it.setText(3, "existe" if c.orig_exists else "falta")
            it.setToolTip(0, c.path)
            it.setData(0, Qt.ItemDataRole.UserRole, {"conflict": asdict(c)})
            self._conflicts[c.path] = c
        self.info.setText(f"{len(found)} conflictos" + ("" if complete else " (búsqueda incompleta: se agotó el tiempo)")
                          + ". Elegí uno para ver qué cambia.")

    def _make_diff(self, data: dict) -> dict:
        if "conflict" in data:
            c = data["conflict"]
            return manage.diff_files(c["original"], c["path"])
        root = data["root"]
        cur = os.path.join(root, data["rel"])
        ver = manage.version_file(root, data["rel"], data["version"])
        d = manage.diff_files(cur, ver)
        d["version_label"] = data["version"]
        return d

    def _show_diff(self, d) -> None:
        if isinstance(d, Exception):
            self.viewer.setPlainText(f"No pude comparar: {d}")
            return
        k = d["kind"]
        if k == "text":
            out = []
            for ln in d["lines"]:
                color = "#34d399" if ln.startswith("+") and not ln.startswith("+++") else (
                    "#f87171" if ln.startswith("-") and not ln.startswith("---") else (
                        "#7dd3fc" if ln.startswith("@@") else MUTED))
                out.append(f"<span style='color:{color}'>{html.escape(ln) or '&nbsp;'}</span>")
            head = f"<b>+{d['added']} / −{d['removed']} líneas</b>" + (" (recortado)" if d.get("truncated") else "")
            self.viewer.setHtml(f"<pre style='font-family:monospace'>{head}\n" + "\n".join(out) + "</pre>")
        elif k == "same":
            self.viewer.setPlainText("Los dos archivos son idénticos.")
        elif k == "missing":
            self.viewer.setPlainText("Falta uno de los dos archivos: no hay con qué comparar.")
        else:
            m = d["meta"]
            f = lambda x: (f"{manage.human_size(x['size'])} · modificado "  # noqa: E731
                           f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(x['mtime']))} · sha {x['sha256'] or '—'}") if x else "no existe"
            self.viewer.setPlainText("Archivo binario o muy grande: sin diff de texto.\n\n"
                                     f"Actual / original: {f(m['a'])}\nOtra versión:      {f(m['b'])}")

    def _resolve(self, action: str) -> None:
        it = (self.list.selectedItems() or [None])[0]
        if not it:
            return
        c = manage.Conflict(**it.data(0, Qt.ItemDataRole.UserRole)["conflict"])
        title = "Quedarme con la copia en conflicto" if action == "use" else "Descartar la copia en conflicto"
        warn = ["El archivo original se guarda en ~/.local/state/wyss-sync/descartados antes de reemplazarlo."
                if action == "use" else
                "La copia se guarda en ~/.local/state/wyss-sync/descartados antes de quitarla; Syncthing "
                "propaga la baja a la otra PC."]
        dlg = Confirm(self, title, [c.rel], warn, self.ctx.local_name, None, "Aplicar", choose_scope=False)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self.run(lambda: manage.resolve_conflict(c, action), lambda r: self._resolved(r, it))

    def _resolved(self, r, it: QTreeWidgetItem) -> None:
        if isinstance(r, Exception):
            self.info.setText(f"No se pudo: {r}")
            return
        self.list.takeTopLevelItem(self.list.indexOfTopLevelItem(it))
        self.viewer.clear()
        self.info.setText(f"Hecho. Copia de seguridad en {r['saved'][0] if r['saved'] else '—'}")

    def _purge(self) -> None:
        sm = manage.descartados_summary()
        if not sm["files"]:
            self.info.setText("La carpeta de descartados está vacía.")
            return
        dlg = Confirm(self, "Vaciar descartados",
                      [f"{sm['files']} archivos · {manage.human_size(sm['bytes'])} en ~/.local/state/wyss-sync/descartados",
                       f"{sm['due_files']} tienen más de {manage.DESCARTADOS_DAYS} días "
                       f"({manage.human_size(sm['due_bytes'])}) y se borran solos"],
                      ["Se borran DEFINITIVAMENTE: son las copias que el panel guardó antes de quitar o reemplazar archivos."],
                      self.ctx.local_name, None, "Borrar todo", choose_scope=False)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self.run(lambda: manage.purge_descartados(days=0),
                 lambda r: self.info.setText(f"Se borraron {r['removed']} archivos ({manage.human_size(r['bytes'])})."
                                             if not isinstance(r, Exception) else f"No se pudo: {r}"))

    # ---------- versiones
    def _scan_versions(self) -> None:
        rows, api, needle = self._folder_rows(), self.ctx.api, self.search.text()
        self.scan.setEnabled(False)
        self.info.setText("Leyendo versiones…")

        def work():
            cfgs = {c["id"]: c for c in api.get("/rest/config/folders")}
            out, without = [], []
            for f in rows:
                if not (cfgs.get(f["id"], {}).get("versioning") or {}).get("type"):
                    without.append(f["label"])
                    continue
                out += [(f, r) for r in manage.list_versions(api, f["id"], needle)]
            return out, without

        self.run(work, self._versions_found)

    def _versions_found(self, res) -> None:
        self.scan.setEnabled(True)
        if isinstance(res, Exception):
            self.info.setText(f"No pude leer versiones: {res}")
            return
        found, without = res
        self.list.clear()
        for f, r in found:
            top = QTreeWidgetItem(self.list)
            top.setText(0, r["path"])
            top.setText(3, str(len(r["versions"])))
            top.setToolTip(0, f"{f['label']}: {r['path']}")
            for v in r["versions"]:
                ch = QTreeWidgetItem(top)
                ch.setText(0, "  " + human_time(v["versionTime"]))
                ch.setText(1, human_time(v.get("modTime", "")))
                ch.setText(2, manage.human_size(v.get("size", 0)))
                ch.setData(0, Qt.ItemDataRole.UserRole, {"root": f["local_path"], "rel": r["path"], "folder": f["id"],
                                                         "version": v["versionTime"]})
        msg = f"{len(found)} archivos con versiones"
        if without:
            msg += f" · sin versionado: {', '.join(without)} (se activa en Ajustes)"
        self.info.setText(msg)

    def _restore(self) -> None:
        it = (self.list.selectedItems() or [None])[0]
        data = it.data(0, Qt.ItemDataRole.UserRole) if it else None
        if not data or not data.get("version"):
            return
        dlg = Confirm(self, "Restaurar versión anterior",
                      [data["rel"], f"versión del {human_time(data['version'])}"],
                      ["El archivo actual se guarda como una versión nueva antes de reemplazarlo (Syncthing).",
                       "Syncthing propaga el cambio a la otra PC."], self.ctx.local_name, None, "Restaurar",
                      choose_scope=False)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        api = self.ctx.api
        self.run(lambda: manage.restore_version(api, data["folder"], data["rel"], data["version"]),
                 lambda r: self.info.setText("Restaurada." if not isinstance(r, Exception) and r["ok"]
                                             else f"No se pudo restaurar: {r if isinstance(r, Exception) else r['error']}"))


# ================================================================ Ajustes por carpeta
class SettingsPage(Page):
    def __init__(self, ctx: Context):
        super().__init__(ctx)
        self.setStyleSheet(field_style())
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 0)
        self.table = tree(["Carpeta", "Ajuste", "Esta PC", "La otra PC"], {0: 150, 1: 220, 2: 220})
        self.table.setRootIsDecorated(True)
        lay.addWidget(self.table, 1)
        row = QHBoxLayout()
        self.b_edit = button("Editar carpeta…", primary=True)
        self.b_undo = button("Deshacer último cambio")
        self.b_reload = button("Actualizar")
        for b in (self.b_edit, self.b_undo, self.b_reload):
            row.addWidget(b)
        row.addStretch()
        lay.addLayout(row)
        self.info = note("Cada PC tiene sus propios ajustes. Los cambios guardan un backup y se pueden deshacer.")
        lay.addWidget(self.info)
        self.b_edit.clicked.connect(self._edit)
        self.b_undo.clicked.connect(self._undo)
        self.b_reload.clicked.connect(self.load)
        self.table.itemSelectionChanged.connect(self._sel)
        self._cur: dict[str, dict] = {}
        self._undo_info: dict | None = None
        self._sel()

    def load(self) -> None:
        if not self.ctx.ready:
            return
        api, hosts, online = self.ctx.api, self.ctx.hosts, self.ctx.remote_online
        ids = [(f["id"], f["label"]) for f in self.ctx.folders]

        def work():
            out = {}
            for fid, label in ids:
                loc = manage.folder_settings(api.get(f"/rest/config/folders/{fid}"))
                rem = None
                if online:
                    r = manage.remote_cli(hosts, "settings", {"folder": fid})
                    rem = r["settings"] if r and r.get("ok") else None
                out[fid] = {"label": label, "local": loc, "remote": rem}
            return out

        self.run(work, self._loaded)

    ROWS = (("Estado", lambda s: "Pausada" if s["paused"] else "Activa"),
            ("Tipo", lambda s: manage.TYPE_TEXT.get(s["type"], s["type"])),
            ("Escaneo completo cada", lambda s: "solo vigilancia" if not s["rescanIntervalS"]
             else f"{s['rescanIntervalS'] // 3600} h" if s["rescanIntervalS"] % 3600 == 0 else f"{s['rescanIntervalS']} s"),
            ("Vigilancia en vivo", lambda s: "sí" if s["fsWatcherEnabled"] else "no"),
            ("Versionado", lambda s: {"": "ninguno", "simple": "simple", "trashcan": "papelera",
                                      "staggered": "escalonado"}.get(s["versioning"], s["versioning"])),
            ("Copias de conflicto (máx.)", lambda s: str(s["maxConflicts"])))

    def _loaded(self, res) -> None:
        if isinstance(res, Exception):
            self.info.setText(f"No pude leer los ajustes: {res}")
            return
        self._cur = res
        self.table.clear()
        for fid, d in res.items():
            top = QTreeWidgetItem(self.table)
            top.setText(0, d["label"])
            top.setData(0, Qt.ItemDataRole.UserRole, fid)
            top.setExpanded(True)
            for name, fmt in self.ROWS:
                ch = QTreeWidgetItem(top)
                ch.setText(1, name)
                ch.setText(2, fmt(d["local"]))
                ch.setText(3, fmt(d["remote"]) if d["remote"] else "—")
                if d["remote"] and fmt(d["local"]) != fmt(d["remote"]):
                    ch.setForeground(3, QColor(WARN))
        self._sel()

    def _sel(self) -> None:
        self.b_edit.setEnabled(bool(self._current_fid()))
        self.b_undo.setEnabled(self._undo_info is not None)

    def _current_fid(self) -> str | None:
        it = (self.table.selectedItems() or [None])[0]
        if not it:
            return None
        top = it if it.parent() is None else it.parent()
        return top.data(0, Qt.ItemDataRole.UserRole)

    def _edit(self) -> None:
        fid = self._current_fid()
        if not fid:
            return
        d = self._cur[fid]
        dlg = SettingsDialog(self, d["label"], d["local"], self.ctx.local_name,
                             self.ctx.remote.get("name") if d["remote"] else None)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        changes, both = dlg.changes(), dlg.both_pcs()
        if not changes:
            self.info.setText("No cambiaste nada.")
            return
        try:
            changes = manage.validate_changes(changes)
        except ValueError as e:
            self.info.setText(f"Valor inválido: {e}")
            return
        lines = [f"{x}" for x in manage.describe_changes({"paused": d["local"]["paused"], **{
            k: d["local"][k] for k in ("type", "rescanIntervalS", "fsWatcherEnabled", "ignorePerms", "maxConflicts")},
            "versioning": {"type": d["local"]["versioning"], "params": d["local"]["versioning_params"]}}, changes)]
        warns = []
        if changes.get("type") == "receiveonly":
            warns.append("Solo recibe: esta PC deja de enviar sus cambios; lo que modifiques acá queda marcado como cambio local.")
        if changes.get("type") == "sendonly":
            warns.append("Solo envía: esta PC ignora los cambios que lleguen de la otra.")
        if changes.get("paused"):
            warns.append("Pausada: deja de sincronizar hasta que la reanudes.")
        if changes.get("versioning") == "":
            warns.append("Sin versionado: las versiones ya guardadas no se borran, pero no se guardan nuevas.")
        conf = Confirm(self, f"Cambiar ajustes de «{d['label']}»", lines, warns, self.ctx.local_name,
                       self.ctx.remote.get("name") if (both and d["remote"]) else None, "Aplicar", choose_scope=False)
        if conf.exec() != QDialog.DialogCode.Accepted:
            return
        api, hosts = self.ctx.api, self.ctx.hosts
        do_remote = both and d["remote"] is not None

        def work():
            res = {"local": manage.apply_settings(api, fid, changes)}
            if do_remote:
                r = manage.remote_cli(hosts, "apply-settings", {"folder": fid, "changes": changes})
                res["remote"] = r["result"] if r and r.get("ok") else {"ok": False, "note": (r or {}).get("error", "sin ssh")}
            return {"fid": fid, **res}

        self.run(work, self._applied)

    def _applied(self, r) -> None:
        if isinstance(r, Exception):
            self.info.setText(f"No se pudo aplicar: {r}")
            return
        bad = [k for k in ("local", "remote") if k in r and not r[k].get("ok")]
        self._undo_info = {"fid": r["fid"], "local": r["local"].get("backup", ""),
                           "remote": r.get("remote", {}).get("backup", "")}
        self.info.setText("Ajustes aplicados y verificados." if not bad else
                          "Con problemas: " + "; ".join(f"{k}: {r[k].get('note')}" for k in bad))
        self.load()

    def _undo(self) -> None:
        u = self._undo_info
        if not u:
            return
        api, hosts = self.ctx.api, self.ctx.hosts
        self._undo_info = None

        def work():
            out = []
            if u["local"]:
                out.append(manage.restore_settings(api, u["fid"], u["local"]))
            if u["remote"]:
                r = manage.remote_cli(hosts, "restore-settings", {"folder": u["fid"], "backup": u["remote"]})
                out.append(bool(r and r.get("ok")))
            return out

        self.run(work, lambda r: (self.info.setText("No pude deshacer: " + str(r) if isinstance(r, Exception)
                                                    else "Ajustes anteriores restaurados."), self.load()))


class SettingsDialog(QDialog):
    def __init__(self, parent, label: str, cur: dict, local_name: str, remote_name: str | None):
        super().__init__(parent)
        self._cur = cur
        self.setWindowTitle(f"Ajustes de {label}")
        self.setModal(True)
        self.setMinimumWidth(480)
        self.setStyleSheet(f"QDialog{{background:#0f2447;}} QLabel{{color:{TEXT};font-size:13px;}}" + field_style() +
                           f"QRadioButton{{color:{TEXT};font-size:13px;spacing:8px;}}")
        g = QGridLayout(self)
        g.setContentsMargins(22, 20, 22, 18)
        g.setVerticalSpacing(10)
        t = QLabel(f"Ajustes de {label}")
        t.setStyleSheet(f"color:{TEXT};font-size:18px;font-weight:700;")
        g.addWidget(t, 0, 0, 1, 2)
        self.paused = QCheckBox("Pausada")
        self.paused.setChecked(cur["paused"])
        self.type = QComboBox()
        for k, v in manage.TYPE_TEXT.items():
            self.type.addItem(v, k)
        self.type.setCurrentIndex(max(self.type.findData(cur["type"]), 0))
        self.rescan = QSpinBox()
        self.rescan.setRange(0, 720)
        self.rescan.setSuffix(" h  (0 = solo vigilancia)")
        self.rescan.setValue(cur["rescanIntervalS"] // 3600)
        self.watcher = QCheckBox("Vigilancia en vivo de cambios")
        self.watcher.setChecked(cur["fsWatcherEnabled"])
        self.maxc = QSpinBox()
        self.maxc.setRange(-1, 100)
        self.maxc.setValue(cur["maxConflicts"])
        self.ver = QComboBox()
        for k, v in (("", "Ninguno"), ("simple", "Simple (últimas N)"), ("trashcan", "Papelera"),
                     ("staggered", "Escalonado")):
            self.ver.addItem(v, k)
        self.ver.setCurrentIndex(max(self.ver.findData(cur["versioning"]), 0))
        self.vparam = QSpinBox()
        self.vparam.setRange(1, 3650)
        self._ver_changed()
        self.ver.currentIndexChanged.connect(self._ver_changed)
        rows = [("", self.paused), ("Tipo", self.type), ("Escaneo completo cada", self.rescan), ("", self.watcher),
                ("Copias de conflicto (máx.)", self.maxc), ("Versionado", self.ver), ("", self.vparam)]
        for i, (l, w) in enumerate(rows, start=1):
            g.addWidget(QLabel(l), i, 0)
            g.addWidget(w, i, 1)
        self.both = QRadioButton(f"Las dos PC ({local_name} y {remote_name})" if remote_name else "")
        self.here = QRadioButton(f"Solo esta PC ({local_name})")
        n = len(rows) + 1
        if remote_name:
            self.both.setChecked(True)
            g.addWidget(self.both, n, 0, 1, 2)
            n += 1
        else:
            self.here.setChecked(True)
        g.addWidget(self.here, n, 0, 1, 2)
        row = QHBoxLayout()
        row.addStretch()
        c, ok = button("Cancelar"), button("Continuar", primary=True)
        c.clicked.connect(self.reject)
        ok.clicked.connect(self.accept)
        row.addWidget(c)
        row.addWidget(ok)
        g.addLayout(row, n + 1, 0, 1, 2)

    def _ver_changed(self) -> None:
        v = self.ver.currentData()
        self.vparam.setVisible(v != "")
        p = self._cur["versioning_params"]
        if v == "simple":
            self.vparam.setSuffix(" versiones"), self.vparam.setValue(int(p.get("keep", 5) or 5))
        elif v == "trashcan":
            self.vparam.setSuffix(" días hasta vaciar"), self.vparam.setValue(int(p.get("cleanoutDays", 0) or 1))
        elif v == "staggered":
            self.vparam.setSuffix(" días de antigüedad máx."), self.vparam.setValue(max(1, int(p.get("maxAge", 31536000) or 1) // 86400))

    def both_pcs(self) -> bool:
        return self.both.isChecked()

    def changes(self) -> dict:
        c, cur, out = {}, self._cur, {}
        if self.paused.isChecked() != cur["paused"]:
            out["paused"] = self.paused.isChecked()
        if self.type.currentData() != cur["type"]:
            out["type"] = self.type.currentData()
        if self.rescan.value() * 3600 != cur["rescanIntervalS"]:
            out["rescanIntervalS"] = self.rescan.value() * 3600
        if self.watcher.isChecked() != cur["fsWatcherEnabled"]:
            out["fsWatcherEnabled"] = self.watcher.isChecked()
        if self.maxc.value() != cur["maxConflicts"]:
            out["maxConflicts"] = self.maxc.value()
        v = self.ver.currentData()
        param = {"simple": ("keep", self.vparam.value()), "trashcan": ("cleanoutDays", self.vparam.value()),
                 "staggered": ("maxAge", self.vparam.value() * 86400)}.get(v)
        new_params = {param[0]: str(param[1])} if param else {}
        if v != cur["versioning"] or (v and new_params != {k: str(x) for k, x in cur["versioning_params"].items()
                                                           if k in new_params}):
            out["versioning"] = v
            out["versioning_params"] = new_params
        return out


# ================================================================ Vínculos (guardar X en Y de la otra PC)
STATE_TXT = {"synced": "al día", "syncing": "sincronizando", "scanning": "escaneando", "paused": "pausada",
             "offline": "sin conexión", "error": "con errores"}


def plan_html(p: "links.Plan") -> str:
    """Resumen legible del plan. Todo lo que dice acá es lo que va a pasar, y nada se escribe todavía."""
    esc = html.escape
    out = []
    if p.blockers:
        out.append(f"<p style='color:{BAD}'><b>No se puede crear:</b></p><ul>" +
                   "".join(f"<li style='color:{BAD}'>{esc(b)}</li>" for b in p.blockers) + "</ul>")
    out.append(f"<p><b>Origen (esta PC):</b> <code>{esc(p.x['path'])}</code>"
               + (f" · dentro de «{esc(p.x['folder'])}»" if p.x.get("folder") else "") +
               f" · {p.x.get('files', 0):,} archivos · {manage.human_size(p.x.get('bytes', 0))}".replace(",", ".") + "<br>"
               f"<b>Destino (la otra PC):</b> <code>{esc(p.y['path'])}</code> · "
               + ("ya existe" if p.y.get("exists") else "no existe todavía")
               + (f" · {p.y.get('files', 0):,} archivos · {manage.human_size(p.y.get('bytes', 0))}".replace(",", ".")
                  if p.y.get("exists") else "") + "</p>")
    if not p.blockers:
        steps = [f"En las <b>dos PC</b>: sacar <code>{esc(r['rel'])}</code> de la sincronización de «{esc(r['folder'])}» "
                 f"({'origen' if r['why'] == 'origen' else 'destino'}) con una regla del .stignore (con backup)"
                 for r in p.rules]
        if not p.y.get("exists"):
            steps.append("En la otra PC: crear la carpeta de destino")
        steps.append(f"Crear la carpeta compartida <b>{esc(p.label)}</b> (<code>{esc(p.fid)}</code>), bidireccional: "
                     f"acá <code>{esc(p.x['path'])}</code> ↔ allá <code>{esc(p.y['path'])}</code>")
        out.append("<p><b>Se va a hacer, en este orden:</b></p><ol>" + "".join(f"<li>{x}</li>" for x in steps) + "</ol>")
        out.append("<p style='color:#c3d3ea'>No se borra ningún archivo. Quitar el vínculo después deshace las reglas y "
                   "la carpeta compartida, y deja los archivos donde están.</p>")
    d = p.diff
    if d:
        row = lambda k, t: (f"<tr><td>{t}</td><td align=right>{d[k]['n']:,}</td>"  # noqa: E731
                            f"<td align=right>{manage.human_size(d[k]['bytes'])}</td></tr>").replace(",", ".")
        out.append("<p><b>Qué pasa al unirlas:</b></p><table cellspacing=6>"
                   "<tr><td></td><td align=right><b>archivos</b></td><td align=right><b>tamaño</b></td></tr>"
                   + row("same", "iguales por tamaño y fecha (no se copian)")
                   + row("only_here", "solo acá → se copian a la otra PC")
                   + row("only_there", "solo allá → se copian acá")
                   + row("different", "con el mismo nombre pero distintos") + "</table>")
        if d["different"]["n"]:
            ex = "".join(f"<li><code>{esc(x)}</code></li>" for x in d["different"]["examples"])
            out.append(f"<p style='color:{WARN}'>Los {d['different']['n']} archivos distintos: Syncthing conserva la "
                       f"versión más nueva y deja la otra como copia <code>*.sync-conflict-*</code>; nada se pierde.</p>"
                       f"<ul>{ex}</ul>")
    for w in p.warnings:
        out.append(f"<p style='color:{WARN}'>⚠ {esc(w)}</p>")
    return "".join(out)


class LinkDialog(QDialog):
    """Asistente de vínculo nuevo: comprobar (solo lectura) y recién después crear."""

    def __init__(self, parent, ctx: Context):
        super().__init__(parent)
        self.ctx, self.plan, self._for, self._tasks, self.created = ctx, None, None, [], None
        self.setWindowTitle("Nuevo vínculo")
        self.setModal(True)
        self.resize(760, 640)
        self.setStyleSheet(f"QDialog{{background:#0f2447;}} QLabel{{color:{TEXT};font-size:13px;}}" + field_style() +
                           f"QTextEdit{{background:rgba(0,0,0,70);color:{TEXT};border:1px solid rgba(255,255,255,46);"
                           "border-radius:6px;}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(10)
        t = QLabel("Guardar una carpeta de esta PC en otra ruta de la otra PC")
        t.setStyleSheet(f"color:{TEXT};font-size:18px;font-weight:700;")
        lay.addWidget(t)
        lay.addWidget(note("Se crea una carpeta nueva de Syncthing compartida entre las dos PC, con una ruta distinta en "
                           "cada una. «Comprobar» no escribe nada."))
        g = QGridLayout()
        g.addWidget(QLabel("Carpeta de esta PC"), 0, 0)
        self.x = QLineEdit()
        self.x.setPlaceholderText("~/Proyectos/mi-carpeta")
        pick = button("Elegir…")
        g.addWidget(self.x, 0, 1)
        g.addWidget(pick, 0, 2)
        g.addWidget(QLabel("Ruta en la otra PC"), 1, 0)
        self.y = QLineEdit()
        self.y.setPlaceholderText("~/Respaldos/mi-carpeta  (se crea si no existe)")
        self.check = button("Comprobar", primary=True)
        g.addWidget(self.y, 1, 1)
        g.addWidget(self.check, 1, 2)
        g.setColumnStretch(1, 1)
        lay.addLayout(g)
        self.view = QTextEdit()
        self.view.setReadOnly(True)
        lay.addWidget(self.view, 1)
        self.ack = QCheckBox("Entiendo que las dos carpetas se van a fusionar: lo que falte de un lado se copia al otro")
        self.ack.setVisible(False)
        lay.addWidget(self.ack)
        self.status = note()
        lay.addWidget(self.status)
        row = QHBoxLayout()
        row.addStretch()
        self.cancel = button("Cerrar")
        self.create = button("Crear vínculo", primary=True)
        self.create.setEnabled(False)
        row.addWidget(self.cancel)
        row.addWidget(self.create)
        lay.addLayout(row)
        pick.clicked.connect(self._pick)
        self.check.clicked.connect(self._check)
        self.create.clicked.connect(self._create)
        self.cancel.clicked.connect(self.reject)
        self.ack.toggled.connect(self._gate)
        self.x.textChanged.connect(self._stale)
        self.y.textChanged.connect(self._stale)

    def _run(self, fn, done) -> None:
        t = Task(fn)
        t.done.connect(done)
        self._tasks = [x for x in self._tasks if x.isRunning()] + [t]
        t.start()

    def _pick(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Carpeta de esta PC", os.path.expanduser("~"))
        if d:
            self.x.setText(d)

    def _stale(self) -> None:
        """Si se cambia una ruta después de comprobar, el plan deja de valer."""
        if self.plan is not None and self._for != (self.x.text().strip(), self.y.text().strip()):
            self.plan = None
            self.create.setEnabled(False)
            self.status.setText("Cambiaste una ruta: volvé a comprobar.")

    def _gate(self) -> None:
        p = self.plan
        self.create.setEnabled(bool(p and not p.blockers and (not p.y_has_data or self.ack.isChecked())))

    def _check(self) -> None:
        x, y = self.x.text().strip(), self.y.text().strip()
        if not x or not y:
            self.status.setText("Completá las dos rutas.")
            return
        api, hosts = self.ctx.api, self.ctx.hosts
        self.check.setEnabled(False)
        self.status.setText("Comprobando… (compara las dos carpetas; puede tardar si son grandes)")
        self.view.clear()

        def work():
            home = os.path.expanduser("~")
            cfg = [{"id": f["id"], "path": f["path"], "label": f.get("label") or f["id"]}
                   for f in api.get("/rest/config/folders")]
            r = manage.remote_cli(hosts, "link-inspect", {"path": y}, timeout=180)
            info = r["info"] if r and r.get("ok") else None
            return links.build_plan(links.expand(x, home), y, cfg, home, info), cfg

        self._run(work, lambda res: self._checked(res, (x, y)))

    def _checked(self, res, key) -> None:
        self.check.setEnabled(True)
        if isinstance(res, Exception):
            self.status.setText(f"No pude comprobar: {res}")
            return
        self.plan, self._local_folders = res
        self._for = key
        self.view.setHtml(plan_html(self.plan))
        self.ack.setChecked(False)
        self.ack.setVisible(bool(self.plan.y_has_data and not self.plan.blockers))
        self.status.setText("Hay bloqueos: corregí las rutas." if self.plan.blockers else
                            "Revisá el resumen. Nada se escribió todavía.")
        self._gate()

    def _create(self) -> None:
        p, api, hosts = self.plan, self.ctx.api, self.ctx.hosts
        if not p or p.blockers:
            return
        peer, folders = self.ctx.remote.get("id", ""), self._local_folders
        self.create.setEnabled(False)
        self.check.setEnabled(False)
        self.status.setText("Creando el vínculo en las dos PC…")
        call = lambda sub, payload: manage.remote_cli(hosts, sub, payload, timeout=90)  # noqa: E731
        self._run(lambda: links.create_link(api, p, folders, call, peer), self._created)

    def _created(self, res) -> None:
        if isinstance(res, Exception) or not res.get("ok"):
            self.check.setEnabled(True)
            err = res if isinstance(res, Exception) else f"{res.get('stage')}: {res.get('error')}"
            self.status.setText(f"No se creó (lo hecho se deshizo): {err}")
            self._gate()
            return
        self.created = res
        self.status.setText("Vínculo creado. Syncthing empieza a compararlas; mirá el estado en la pestaña Carpetas.")
        self.cancel.setText("Listo")


class LinksPage(Page):
    refresh_ms = 10000

    def __init__(self, ctx: Context):
        super().__init__(ctx)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 0)
        lay.addWidget(note("Un vínculo guarda una carpeta de esta PC en una ruta distinta de la otra PC. Aparece también "
                           "en la pestaña Carpetas, con su estado y sus GB."))
        self.table = tree(["Vínculo", "Esta PC", "La otra PC", "Estado", "Sincroniza"], {0: 160, 1: 300, 2: 300, 3: 120})
        lay.addWidget(self.table, 1)
        row = QHBoxLayout()
        self.b_new = button("Nuevo vínculo…", primary=True)
        self.b_drop = button("Quitar vínculo…")
        self.b_scan = button("Reescanear ahora")
        for b in (self.b_new, self.b_scan, self.b_drop):
            row.addWidget(b)
        row.addStretch()
        lay.addLayout(row)
        self.info = note()
        lay.addWidget(self.info)
        self.b_new.clicked.connect(self._new)
        self.b_drop.clicked.connect(self._drop)
        self.b_scan.clicked.connect(self._rescan)
        self.table.itemSelectionChanged.connect(self._sel)
        self._sel()

    def _sel(self) -> None:
        on = bool(self.table.selectedItems())
        self.b_drop.setEnabled(on)
        self.b_scan.setEnabled(on)

    def _rescan(self) -> None:
        it = (self.table.selectedItems() or [None])[0]
        if not it:
            return
        fid, api, hosts = it.data(0, Qt.ItemDataRole.UserRole), self.ctx.api, self.ctx.hosts
        call = lambda sub, payload: manage.remote_cli(hosts, sub, payload, timeout=60)  # noqa: E731
        self.run(lambda: links.rescan_link(api, fid, call),
                 lambda r: self.info.setText("Reescaneo pedido en las dos PC." if not isinstance(r, Exception) and r["local"]
                                             and r["remote"] else f"Reescaneo parcial o fallido: {r}"))

    def load(self) -> None:
        recs = links.load_links()
        live = {f["id"]: f for f in self.ctx.folders}
        sel = (self.table.selectedItems() or [None])[0]
        keep = sel.data(0, Qt.ItemDataRole.UserRole) if sel else None
        self.table.clear()
        for fid, r in recs.items():
            it = QTreeWidgetItem(self.table)
            f = live.get(fid)
            it.setData(0, Qt.ItemDataRole.UserRole, fid)
            it.setText(0, r.get("label", fid))
            it.setText(1, r["x"])
            it.setText(2, r["y"])
            it.setText(3, (STATE_TXT.get(f["state"], f["state"]) + (f" {int(f['percent'])} %" if f.get("percent") is not None else ""))
                       if f else "sin datos")
            it.setText(4, manage.human_size(f["sync_bytes"]) if f else "—")
            if fid == keep:
                it.setSelected(True)
        self.info.setText(f"{len(recs)} vínculos." if recs else "Todavía no hay vínculos.")

    def _new(self) -> None:
        if not self.ctx.ready or not self.ctx.remote_online:
            self.info.setText("La otra PC tiene que estar conectada para crear un vínculo.")
            return
        dlg = LinkDialog(self, self.ctx)
        dlg.exec()
        self.load()

    def _drop(self) -> None:
        it = (self.table.selectedItems() or [None])[0]
        if not it:
            return
        fid = it.data(0, Qt.ItemDataRole.UserRole)
        rec = links.load_links().get(fid, {})
        dlg = Confirm(self, "Quitar vínculo", [f"{rec.get('x')}  ↔  {rec.get('y')}"],
                      ["Se quita la carpeta compartida y las reglas que agregó el vínculo, en las dos PC.",
                       "Los archivos NO se borran: quedan donde están."],
                      self.ctx.local_name, None, "Quitar vínculo", choose_scope=False)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        api, hosts = self.ctx.api, self.ctx.hosts
        call = lambda sub, payload: manage.remote_cli(hosts, sub, payload, timeout=90)  # noqa: E731

        def done(r):
            if isinstance(r, Exception):
                self.info.setText(f"No se pudo quitar: {r}")
            else:
                self.info.setText("Vínculo quitado en las dos PC." if r["ok"] else
                                  f"Quitado con problemas: {r['local']['errors']} {r.get('remote')}")
            self.load()

        self.run(lambda: links.drop_link(api, fid, call), done)
