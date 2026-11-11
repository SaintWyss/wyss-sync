"""Vista de detalle de una carpeta: subcarpetas con su estado en las dos PC y cambios de reglas."""
from __future__ import annotations

import time

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import (QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QRadioButton,
                             QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from . import manage
from .core import SyncthingApi
from .manage import Row

TEXT, MUTED = "#eaf2ff", "#c3d3ea"
STATE_TEXT = {"sync": ("Sincroniza", "#34d399"), "ignored": ("Ignorada", "#fbbf24"),
              "unindexed": ("Sin indexar", "#38bdf8"), "stale": ("Reescaneo pendiente", "#38bdf8"), "missing": ("No existe", "#94a3b8"),
              "unknown": ("Sin datos", "#94a3b8")}
BTN = ("QPushButton{color:%s;background:rgba(255,255,255,22);border:1px solid rgba(255,255,255,46);"
       "border-radius:8px;padding:7px 14px;font-size:12px;}"
       "QPushButton:hover{background:rgba(255,255,255,40);}"
       "QPushButton:focus{border:1px solid #ffffff;}"
       "QPushButton:disabled{color:#7f93b3;background:rgba(255,255,255,10);border-color:rgba(255,255,255,24);}") % TEXT
BTN_PRIMARY = BTN.replace("rgba(255,255,255,22)", "rgba(56,189,248,70)").replace(
    "rgba(255,255,255,46)", "rgba(56,189,248,160)")


def style_tree(tree: QTreeWidget) -> None:
    """Tabla de vidrio: transparente, encabezado discreto y barra de desplazamiento fina."""
    pal = tree.palette()
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.WindowText, QPalette.ColorRole.ButtonText):
        pal.setColor(role, QColor(TEXT))
    tree.setPalette(pal)
    tree.setStyleSheet(
        f"QTreeWidget{{background:rgba(255,255,255,12);border:1px solid rgba(255,255,255,46);"
        f"border-radius:6px;color:{TEXT};font-size:13px;outline:0;}}"
        "QTreeWidget::item{height:30px;}"
        "QTreeWidget::item:selected{background:rgba(56,189,248,70);}"
        "QTreeWidget::item:hover{background:rgba(255,255,255,20);}"
        "QHeaderView{background:transparent;border:none;}"
        f"QHeaderView::section{{background:transparent;color:{MUTED};border:none;"
        "border-bottom:1px solid rgba(255,255,255,40);padding:7px 6px;font-size:12px;}"
        "QTreeWidget QAbstractScrollArea,QTreeWidget::corner{background:transparent;}"
        "QScrollBar:vertical{background:transparent;width:10px;margin:2px;}"
        "QScrollBar::handle:vertical{background:rgba(255,255,255,70);border-radius:4px;min-height:24px;}"
        "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}"
        "QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{background:transparent;}")
    tree.header().setAutoFillBackground(False)
    tree.viewport().setAutoFillBackground(False)


class Task(QThread):
    done = pyqtSignal(object)

    def __init__(self, fn):
        super().__init__()
        self._fn = fn

    def run(self) -> None:
        try:
            self.done.emit(self._fn())
        except Exception as e:  # el error se muestra en la vista; nunca tumba la ventana
            self.done.emit(e)


class SizeTask(QThread):
    # object: un int de Qt es de 32 bits y desborda con >2 GB
    one = pyqtSignal(str, object, object, object, object, bool)   # rel, en disco, sincroniza, archivos, sinc. archivos, completo

    def __init__(self, items: list[tuple[str, str]], rules: manage.Rules):
        super().__init__()
        self._items, self._rules, self._stop = items, rules, False

    def cancel(self) -> None:
        self._stop = True

    def run(self) -> None:
        for rel, path in self._items:
            if self._stop:
                return
            d = manage.dir_sizes(path, rel, self._rules, budget=4.0)
            self.one.emit(rel, d["disk"], d["synced"], d["files"], d["synced_files"], d["complete"])


class ChangeDialog(QDialog):
    """Muestra la regla exacta, a qué PC se aplica y qué va a pasar. Nada cambia sin 'Aplicar'."""

    def __init__(self, parent, action: str, rows: list[Row], local_name: str, remote_name: str | None):
        super().__init__(parent)
        self._critical = [r.rel for r in rows if action == "ignore" and manage.is_critical(r.rel)]
        self.setWindowTitle("Ignorar" if action == "ignore" else "Dejar de ignorar")
        self.setModal(True)
        self.setMinimumWidth(560)
        self.setStyleSheet(f"QDialog{{background:#0f2447;}} QLabel{{color:{TEXT};font-size:13px;}}"
                           f"QRadioButton{{color:{TEXT};font-size:13px;spacing:8px;}}"
                           "QRadioButton::indicator{width:14px;height:14px;border-radius:9px;"
                           "border:2px solid #7dd3fc;background:transparent;}"
                           "QRadioButton::indicator:checked{background:qradialgradient(cx:0.5,cy:0.5,radius:0.5,"
                           "fx:0.5,fy:0.5,stop:0 #38bdf8,stop:0.55 #38bdf8,stop:0.6 transparent);}"
                           "QRadioButton:focus{outline:1px solid #ffffff;}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(10)
        n = len(rows)
        noun = "subcarpeta" if n == 1 else "subcarpetas"
        title = QLabel(f"{'Ignorar' if action == 'ignore' else 'Dejar de ignorar'} {n} {noun}")
        title.setStyleSheet(f"color:{TEXT};font-size:18px;font-weight:700;")
        lay.addWidget(title)

        rules = QLabel("<br>".join(
            f"<span style='font-family:monospace;color:#7dd3fc'>{manage.rule_for(r.rel)}</span>"
            f"<span style='color:{MUTED}'> &nbsp;{self._hint(action, r)}</span>" for r in rows[:12])
            + (f"<br><span style='color:{MUTED}'>y {n - 12} más…</span>" if n > 12 else ""))
        rules.setTextFormat(Qt.TextFormat.RichText)
        rules.setWordWrap(True)
        lay.addWidget(rules)

        self.both = QRadioButton(f"Las dos PC ({local_name} y {remote_name})" if remote_name else "")
        self.only_here = QRadioButton(f"Solo esta PC ({local_name})")
        if remote_name:
            self.both.setChecked(True)
            lay.addWidget(self.both)
        else:
            self.only_here.setChecked(True)
        lay.addWidget(self.only_here)
        if not remote_name:
            note = QLabel("La otra PC no responde por ssh: el cambio se aplica solo acá.")
            note.setStyleSheet(f"color:#fbbf24;font-size:12px;")
            lay.addWidget(note)

        what = ("Las copias que ya existen no se borran en ninguna PC. Dejan de enviarse y de recibirse "
                "los cambios de esas carpetas." if action == "ignore" else
                "Syncthing las vuelve a comparar con la otra PC: lo que falte de un lado se copia al otro. "
                "Si la otra PC la borró mientras estaba ignorada, Syncthing puede borrarla acá.")
        info = QLabel(what + "<br>Antes de cambiar se guarda una copia del .stignore "
                      "(~/.local/state/wyss-sync/backups) y se puede deshacer desde el panel.")
        info.setTextFormat(Qt.TextFormat.RichText)
        info.setWordWrap(True)
        info.setStyleSheet(f"color:{MUTED};font-size:12px;")
        lay.addWidget(info)

        self.ack = None
        if self._critical:
            from PyQt6.QtWidgets import QCheckBox
            warn = QLabel("⚠ " + ", ".join(self._critical) + (" guarda" if len(self._critical) == 1 else " guardan") +
                          " tus configuraciones, credenciales o datos. Si la ignorás, deja de sincronizarse entre las PC "
                          "(no se borra nada, pero los cambios futuros no viajan).")
            warn.setWordWrap(True)
            warn.setStyleSheet("color:#fbbf24;font-size:12px;")
            lay.addWidget(warn)
            self.ack = QCheckBox("Entiendo que deja de sincronizarse")
            self.ack.setStyleSheet(f"QCheckBox{{color:{TEXT};font-size:13px;spacing:8px;}}")
            lay.addWidget(self.ack)
        btns = QHBoxLayout()
        btns.addStretch()
        cancel, ok = QPushButton("Cancelar"), QPushButton("Aplicar")
        self.ok_btn = ok
        cancel.setStyleSheet(BTN)
        ok.setStyleSheet(BTN_PRIMARY)
        cancel.clicked.connect(self.reject)
        ok.clicked.connect(self.accept)
        ok.setDefault(True)
        if self.ack is not None:
            ok.setEnabled(False)
            self.ack.toggled.connect(ok.setEnabled)
        btns.addWidget(cancel)
        btns.addWidget(ok)
        lay.addLayout(btns)

    @staticmethod
    def _hint(action: str, r: Row) -> str:
        if action == "unignore" and r.there == "missing":
            return "no existe en la otra PC: se va a copiar allá"
        if action == "unignore" and r.rule:
            return f"hoy la ignora «{r.rule}»: se agrega una excepción"
        return ""

    def scope_both(self) -> bool:
        return self.both.isChecked()


class FolderDetail(QWidget):
    back = pyqtSignal()

    def __init__(self, api: SyncthingApi | None):
        super().__init__()
        self._api, self._tasks, self._sizes = api, [], None
        self._folder: dict = {}
        self._hosts: list[str] = []
        self._remote_online = False
        self._remote_name = ""
        self._local_name = ""
        self._undo: dict | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        head = QHBoxLayout()
        self.btn_back = QPushButton("←  Carpetas")
        self.btn_back.setStyleSheet(BTN)
        self.btn_back.clicked.connect(self._go_back)
        self.title = QLabel("")
        self.title.setStyleSheet(f"color:{TEXT};font-size:20px;font-weight:700;")
        head.addWidget(self.btn_back)
        head.addSpacing(8)
        head.addWidget(self.title)
        head.addStretch()
        lay.addLayout(head)
        self.sub = QLabel("")
        self.sub.setStyleSheet(f"color:{MUTED};font-size:12px;")
        lay.addWidget(self.sub)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(8)
        self.tree.setHeaderLabels(["Subcarpeta", "Aquí", "Allá", "Disco (aquí / allá)", "En disco", "Sincroniza",
                                   "Modificada", "Regla"])
        self.tree.setSelectionMode(QTreeWidget.SelectionMode.ExtendedSelection)
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(False)
        self.tree.setIndentation(18)
        style_tree(self.tree)
        hdr = self.tree.header()
        hdr.setStretchLastSection(True)
        hdr.setSectionResizeMode(0, hdr.ResizeMode.Stretch)
        for c, w in ((1, 120), (2, 120), (3, 200), (4, 80), (5, 90), (6, 125), (7, 140)):
            hdr.setSectionResizeMode(c, hdr.ResizeMode.Interactive)
            self.tree.setColumnWidth(c, w)
        self.tree.itemExpanded.connect(self._expanded)
        self.tree.itemSelectionChanged.connect(self._sel_changed)
        lay.addWidget(self.tree, 1)

        bar = QHBoxLayout()
        self.b_ignore = QPushButton("Ignorar…")
        self.b_unignore = QPushButton("Dejar de ignorar…")
        self.b_undo = QPushButton("Deshacer último cambio")
        self.b_reload = QPushButton("Actualizar")
        for b in (self.b_ignore, self.b_unignore, self.b_undo, self.b_reload):
            b.setStyleSheet(BTN)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            bar.addWidget(b)
        bar.addStretch()
        self.msg = QLabel("")
        self.msg.setStyleSheet(f"color:{MUTED};font-size:12px;")
        self.msg.setWordWrap(True)
        self.b_ignore.clicked.connect(lambda: self._change("ignore"))
        self.b_unignore.clicked.connect(lambda: self._change("unignore"))
        self.b_undo.clicked.connect(self._undo_last)
        self.b_reload.clicked.connect(self.reload)
        lay.addLayout(bar)
        lay.addWidget(self.msg)
        self._sel_changed()

    # ---------- apertura y carga
    def open(self, folder: dict, local_name: str, remote_name: str, hosts: list[str], remote_online: bool) -> None:
        self._folder, self._hosts = folder, hosts
        self._local_name, self._remote_name, self._remote_online = local_name, remote_name, remote_online
        self._undo = None
        self.title.setText(folder["label"])
        self.sub.setText(f"{folder['local_path']}  →  {folder.get('remote_path') or 'ruta remota no disponible'}"
                         f"   ·   sincroniza todo salvo lo que ignore una regla del .stignore de cada PC")
        self.msg.setText("")
        self.reload()

    def reload(self) -> None:
        self._stop_sizes()
        self.tree.clear()
        self._load(None, "")

    def _load(self, parent: QTreeWidgetItem | None, rel: str) -> None:
        fid, root, api = self._folder["id"], self._folder["local_path"], self._api
        hosts, online = self._hosts, self._remote_online

        def work():
            local = manage.children(api, fid, rel, root=root)
            remote = None
            if online:
                r = manage.remote_cli(hosts, "children", {"folder": fid, "rel": rel})
                if r and r.get("ok"):
                    remote = r["children"]
            lines = api.get("/rest/db/ignores", folder=fid).get("ignore") or []
            return manage.merge_rows(local, remote), remote is not None, lines

        t = Task(work)
        t.done.connect(lambda res, p=parent: self._loaded(p, res))
        self._start(t)
        if parent is None:
            self.msg.setText("Consultando subcarpetas…")

    def _loaded(self, parent: QTreeWidgetItem | None, res) -> None:
        if isinstance(res, Exception):
            self.msg.setText(f"No pude leer las subcarpetas: {res}")
            return
        rows, remote_ok, lines = res
        if parent is not None:
            parent.takeChildren()
        paths = []
        for r in rows:
            item = QTreeWidgetItem(parent or self.tree.invisibleRootItem())
            self._fill(item, r)
            if r.here != "missing" and r.kind == "dir":
                item.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
                paths.append((r.rel, f"{self._folder['local_path']}/{r.rel}"))
        if parent is None:
            ign = sum(r.here == "ignored" for r in rows)
            self.msg.setText(f"{len(rows)} subcarpetas · {ign} ignoradas acá"
                             + ("" if remote_ok or not self._remote_online else " · sin datos de la otra PC (ssh)")
                             + ("" if self._remote_online else " · la otra PC está desconectada"))
        self._start_sizes(paths, manage.Rules(lines))

    def _fill(self, item: QTreeWidgetItem, r: Row) -> None:
        item.setData(0, Qt.ItemDataRole.UserRole, r)
        item.setText(0, r.name + ("  ↪" if r.kind == "link" else ""))
        for col, st in ((1, r.here), (2, r.there)):
            text, color = STATE_TEXT[st]
            item.setText(col, text)
            item.setForeground(col, QColor(color))
        here, there = manage.disk_label(r.disk_here), manage.disk_label(r.disk_there)
        item.setText(3, "—" if not (here or there) else (here if here == there else f"{here or '—'}  /  {there or '—'}"))
        item.setToolTip(3, "\n".join(x for x in (
            f"Aquí: {here} → {r.disk_here['real']}" if r.disk_here else "",
            f"Allá: {there} → {r.disk_there['real']}" if r.disk_there else "") if x))
        if r.disk_here and r.disk_there and here != there:
            item.setForeground(3, QColor("#fbbf24"))     # no están en el mismo tipo de disco en las dos PC
        item.setText(4, "…" if r.here != "missing" else "—")
        item.setText(5, "…" if r.here != "missing" else "—")
        item.setText(6, time.strftime("%Y-%m-%d %H:%M", time.localtime(r.mtime)) if r.mtime else "—")
        item.setText(7, r.rule or r.remote_rule or "")
        item.setToolTip(0, f"{self._folder['local_path']}/{r.rel}")
        item.setToolTip(7, r.rule or r.remote_rule or "")
        item.setData(0, Qt.ItemDataRole.UserRole + 1, r.rel)
        if r.kind == "link":
            item.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.DontShowIndicator)

    def _expanded(self, item: QTreeWidgetItem) -> None:
        if item.childCount() == 0:
            r: Row = item.data(0, Qt.ItemDataRole.UserRole)
            self._load(item, r.rel)

    # ---------- tamaños (en segundo plano, con presupuesto)
    def _start_sizes(self, items: list[tuple[str, str]], rules: manage.Rules) -> None:
        if not items:
            return
        t = SizeTask(items, rules)
        t.one.connect(self._size_ready)
        self._sizes = t
        self._start(t)

    def _stop_sizes(self) -> None:
        if self._sizes is not None:
            self._sizes.cancel()
            self._sizes = None

    def _size_ready(self, rel: str, disk: int, synced: int, files: int, synced_files: int, complete: bool) -> None:
        it = self._find(rel)
        if it is not None:
            it.setText(4, manage.human_size(disk, complete))
            it.setToolTip(4, f"{files:,} archivos en disco".replace(",", ".") + ("" if complete else " (medición parcial)"))
            it.setText(5, manage.human_size(synced, complete))
            it.setToolTip(5, f"{synced_files:,} archivos entran en la sincronización".replace(",", ".")
                          + ("" if complete else " (medición parcial)"))
            if synced == 0:
                it.setForeground(5, QColor("#94a3b8"))

    def _find(self, rel: str, parent: QTreeWidgetItem | None = None) -> QTreeWidgetItem | None:
        parent = parent or self.tree.invisibleRootItem()
        for i in range(parent.childCount()):
            c = parent.child(i)
            key = c.data(0, Qt.ItemDataRole.UserRole + 1)
            if key == rel:
                return c
            if key and rel.startswith(key + "/"):
                return self._find(rel, c)
        return None

    # ---------- cambios
    def _selected(self) -> list[Row]:
        return [i.data(0, Qt.ItemDataRole.UserRole) for i in self.tree.selectedItems()]

    def _sel_changed(self) -> None:
        sel = self._selected()
        self.b_ignore.setEnabled(any(r.here in ("sync", "unindexed", "stale") for r in sel))
        self.b_unignore.setEnabled(any(r.here in ("ignored", "stale") for r in sel))
        self.b_undo.setEnabled(self._undo is not None)

    def _change(self, action: str) -> None:
        want = ("sync", "unindexed", "stale") if action == "ignore" else ("ignored", "stale")
        rows = [r for r in self._selected() if r.here in want]
        if not rows:
            return
        dlg = ChangeDialog(self, action, rows, self._local_name,
                           self._remote_name if self._remote_online else None)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        both = dlg.scope_both() and self._remote_online
        self._apply(action, [r.rel for r in rows], both)

    def _apply(self, action: str, rels: list[str], both: bool) -> None:
        fid, api, hosts = self._folder["id"], self._api, self._hosts
        for b in (self.b_ignore, self.b_unignore, self.b_undo, self.b_reload):
            b.setEnabled(False)
        self.msg.setText("Aplicando…")

        def work():
            local, remote = [], []
            for rel in rels:
                try:
                    local.append(manage.apply_change(api, fid, rel, action))
                except Exception as e:
                    local.append(manage.ApplyResult(False, action, rel, note=f"{type(e).__name__}: {e}"))
            if both:
                for rel in rels:
                    r = manage.remote_cli(hosts, "apply", {"folder": fid, "rel": rel, "action": action})
                    if r and r.get("ok"):
                        remote.append(manage.ApplyResult(**r["result"]))
                    else:
                        remote.append(manage.ApplyResult(False, action, rel,
                                                         note=(r or {}).get("error", "sin respuesta por ssh")))
            return local, remote

        t = Task(work)
        t.done.connect(self._applied)
        self._start(t)

    def _applied(self, res) -> None:
        if isinstance(res, Exception):
            self.msg.setText(f"Error al aplicar: {res}")
            self.b_reload.setEnabled(True)
            return
        local, remote = res
        ok = lambda rs: sum(1 for r in rs if r.ok)  # noqa: E731
        confirmed = all(r.verified is True for r in local + remote)
        parts = [f"acá {ok(local)}/{len(local)}"]
        if remote:
            parts.append(f"allá {ok(remote)}/{len(remote)}")
        bad = [f"{r.rel}: {r.note}" for r in local + remote if not r.ok]
        first = lambda rs: next((r.backup for r in rs if r.backup), "")  # noqa: E731
        self._undo = {"local": first(local), "remote": first(remote)} if (first(local) or first(remote)) else None
        unsure = [f"{r.rel}: {r.note}" for r in local + remote if r.ok is False and r.verified is None]
        self.msg.setText(("Cambio aplicado y verificado (" + ", ".join(parts) + ").") if (not bad and confirmed)
                         else "Hubo problemas: " + "; ".join((bad or unsure)[:3]))
        self.b_reload.setEnabled(True)
        self.reload()

    def _undo_last(self) -> None:
        if not self._undo:
            return
        u, fid, api, hosts = self._undo, self._folder["id"], self._api, self._hosts
        self._undo = None
        self.b_undo.setEnabled(False)

        def work():
            out = []
            if u["local"]:
                out.append(("acá", manage.restore_backup(api, fid, u["local"])))
            if u["remote"]:
                r = manage.remote_cli(hosts, "restore", {"folder": fid, "backup": u["remote"]})
                out.append(("allá", r.get("lines") if r and r.get("ok") else None))
            return out

        t = Task(work)
        t.done.connect(lambda res: (self.msg.setText(
            "No pude deshacer: " + str(res) if isinstance(res, Exception) else
            "Se restauró el .stignore anterior (" + ", ".join(
                f"{w}: {'ok' if n else 'falló'}" for w, n in res) + ")."), self.reload()))
        self._start(t)

    # ---------- varios
    def _start(self, t: QThread) -> None:
        self._tasks = [x for x in self._tasks if x.isRunning()] + [t]
        t.start()

    def _go_back(self) -> None:
        self._stop_sizes()
        self.back.emit()
