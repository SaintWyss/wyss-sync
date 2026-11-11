#!/usr/bin/env python3
# ============================================================
#  sync-gui.py — Dashboard GUI (PyQt6) del sistema de sync.
#  Muestra 3 capas: APPS / ARCHIVOS / AJUSTES entre las 2 PCs.
#  Botones: Refrescar · Instalar faltantes · Abrir wishlist.
# ============================================================
import json
import os
import subprocess
import sys
from pathlib import Path

from PyQt6.QtCore import Qt, QProcess, QTimer
from PyQt6.QtGui import QFont, QIcon
from PyQt6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QPushButton,
    QSizePolicy, QVBoxLayout, QWidget, QMessageBox,
)

DIR = Path(__file__).resolve().parent
DATA_SH = DIR / "sync-data.sh"
APPS_SH = DIR / "sync-apps.sh"
WISHLIST = DIR / "apps-wishlist.txt"

OK = "#27ae60"      # verde
WARN = "#e67e22"    # naranja
BAD = "#c0392b"     # rojo
MUTED = "#7f8c8d"   # gris


def fetch():
    try:
        out = subprocess.run(
            ["bash", str(DATA_SH)], capture_output=True, text=True, timeout=20
        ).stdout
        return json.loads(out)
    except Exception as e:
        return {"error": str(e)}


class Card(QFrame):
    """Una de las 3 capas (apps/archivos/ajustes)."""
    def __init__(self, title):
        super().__init__()
        self.setObjectName("card")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(4)

        head = QHBoxLayout()
        self.dot = QLabel("●")
        self.dot.setFont(QFont("", 14))
        self.titleL = QLabel(title)
        self.titleL.setStyleSheet("font-weight:600; font-size:13px; letter-spacing:1px;")
        head.addWidget(self.dot)
        head.addWidget(self.titleL)
        head.addStretch()
        lay.addLayout(head)

        self.body = QLabel("")
        self.body.setStyleSheet("color:#bdc3c7; font-size:13px;")
        self.body.setWordWrap(True)
        lay.addWidget(self.body)

    def set_state(self, color, lines):
        self.dot.setStyleSheet(f"color:{color};")
        self.body.setText("\n".join(lines))


class Dashboard(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Sync Control")
        self.setMinimumWidth(440)
        self.proc = None
        self._build()
        self.refresh()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        # header
        self.header = QLabel("…")
        self.header.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.header.setStyleSheet("font-size:18px; font-weight:700;")
        root.addWidget(self.header)

        self.sub = QLabel("")
        self.sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.sub.setStyleSheet(f"color:{MUTED}; font-size:12px;")
        root.addWidget(self.sub)

        self.card_apps = Card("1 · APPS")
        self.card_files = Card("2 · ARCHIVOS")
        self.card_set = Card("3 · AJUSTES")
        root.addWidget(self.card_apps)
        root.addWidget(self.card_files)
        root.addWidget(self.card_set)

        # botones
        btns = QHBoxLayout()
        self.b_refresh = QPushButton("⟳  Refrescar")
        self.b_install = QPushButton("⬇  Instalar faltantes")
        self.b_wish = QPushButton("✎  Editar wishlist")
        for b in (self.b_refresh, self.b_install, self.b_wish):
            b.setCursor(Qt.CursorShape.PointingHandCursor)
        self.b_refresh.clicked.connect(self.refresh)
        self.b_install.clicked.connect(self.install_missing)
        self.b_wish.clicked.connect(self.edit_wishlist)
        btns.addWidget(self.b_refresh)
        btns.addWidget(self.b_install)
        btns.addWidget(self.b_wish)
        root.addLayout(btns)

        self.status = QLabel("")
        self.status.setStyleSheet(f"color:{MUTED}; font-size:11px;")
        root.addWidget(self.status)

        self.setStyleSheet("""
            QWidget { background:#1e272e; color:#ecf0f1; }
            QFrame#card { background:#2c3a47; border-radius:10px; }
            QPushButton {
                background:#34495e; border:none; border-radius:8px;
                padding:9px 12px; color:#ecf0f1; font-size:12px;
            }
            QPushButton:hover { background:#3d566e; }
            QPushButton:disabled { color:#7f8c8d; }
        """)

    def refresh(self):
        self.status.setText("Consultando…")
        QApplication.processEvents()
        d = fetch()
        if "error" in d:
            self.header.setText("Error")
            self.sub.setText(d["error"])
            return

        self.header.setText(f"{d['host']}  ⇄  {d['other_name']}")
        online = d.get("other_online")
        self.sub.setText(f"{d['other_name']}: " + ("conectada ●" if online else "offline ○"))

        # APPS
        miss = d["apps"]["missing"]
        tot = d["apps"]["wishlist_total"]
        if not miss:
            extra = []
            a = d["apps"]
            if a.get("only_here") or a.get("only_there"):
                extra = [f"{len(a.get('only_here', []))} paquetes solo acá · {len(a.get('only_there', []))} solo en la otra PC"
                         f" ({a.get('source', '')})"]
            self.card_apps.set_state(WARN if extra else OK, [f"✓ todas instaladas ({tot} en wishlist)"] + extra)
            self.b_install.setEnabled(False)
            self.b_install.setText("⬇  Instalar faltantes")
        else:
            self.card_apps.set_state(WARN, [f"faltan {len(miss)} de {tot}:", "  " + ", ".join(miss)])
            self.b_install.setEnabled(True)
            self.b_install.setText(f"⬇  Instalar {len(miss)} faltantes")
        self._missing = miss

        # ARCHIVOS (estado real de cada carpeta según la API de Syncthing)
        f = d["files"]
        fl = []
        fc = OK
        fl.append("✓ syncthing activo" if f["syncthing"] else "✗ syncthing detenido")
        if not f["syncthing"]:
            fc = BAD
        names = {"synced": "al día", "syncing": "sincronizando", "scanning": "escaneando", "paused": "pausada",
                 "offline": "sin conexión", "error": "con errores"}
        for fo in f.get("folders", []):
            ok = fo["state"] == "synced"
            pct = f" {int(fo['percent'])} %" if fo.get("percent") is not None else ""
            fl.append(f"{'✓' if ok else '⚠'} {fo['label']}: {names.get(fo['state'], fo['state'])}{pct}"
                      f" · {fo['sync_bytes'] / 2**30:.1f} GB")
            if not ok:
                fc = WARN if fc == OK else fc
            if fo["state"] == "error":
                fc = BAD
        if f.get("error"):
            fl.append(f"✗ API de Syncthing: {f['error']}")
            fc = BAD
        if f["conflicts"] == 0:
            fl.append("✓ 0 conflictos sin resolver")
        else:
            fl.append(f"⚠ {f['conflicts']} conflictos sin resolver"
                      + (f" (+{f['conflicts_archived']} archivados)" if f.get("conflicts_archived") else ""))
            for origin, n in f.get("conflict_sources", [])[:3]:
                fl.append(f"    {n} en {origin}")
            fc = WARN if fc == OK else fc
        self.card_files.set_state(fc, fl)

        # AJUSTES: se comparan las reglas efectivas, no el md5 del archivo (los comentarios y el orden no cuentan)
        s = d["settings"]
        st = s["stignore"]
        if st == "match":
            self.card_set.set_state(OK, [f"{s['rules']} reglas activas", "✓ mismas reglas efectivas en ambas PCs"])
        elif st == "diff":
            n = len(s.get("only_here", [])) + len(s.get("only_there", []))
            self.card_set.set_state(WARN, [f"{s['rules']} reglas activas",
                                           f"⚠ {n} reglas difieren (puede ser a propósito, por PC)"]
                                    + [f"    solo acá: {r}" for r in s.get("only_here", [])[:2]]
                                    + [f"    solo allá: {r}" for r in s.get("only_there", [])[:2]])
        else:
            self.card_set.set_state(MUTED, [f"{s['rules']} reglas activas", "○ otra PC offline (no comparado)"])

        from datetime import datetime
        self.status.setText("Actualizado " + datetime.now().strftime("%H:%M:%S"))

    def install_missing(self):
        if not getattr(self, "_missing", None):
            return
        pkgs = ", ".join(self._missing)
        if QMessageBox.question(
            self, "Instalar faltantes",
            f"Instalar en esta PC:\n\n{pkgs}\n\n¿Continuar?",
        ) != QMessageBox.StandardButton.Yes:
            return
        # corre sync-apps en una terminal (necesita sudo/paru interactivo)
        term = self._find_terminal()
        if term:
            subprocess.Popen(term + [f"bash {APPS_SH}; echo; read -p 'Enter para cerrar...'"])
            self.status.setText("Instalando en terminal… refrescá cuando termine.")
        else:
            QMessageBox.information(self, "Sin terminal",
                f"Corré a mano:\n  bash {APPS_SH}")

    def edit_wishlist(self):
        subprocess.Popen(["xdg-open", str(WISHLIST)])

    def _find_terminal(self):
        for t, args in [
            ("konsole", ["-e", "bash", "-c"]),
            ("alacritty", ["-e", "bash", "-c"]),
            ("kitty", ["bash", "-c"]),
            ("xterm", ["-e", "bash", "-c"]),
        ]:
            if subprocess.run(["which", t], capture_output=True).returncode == 0:
                return [t] + args
        return None


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Sync Control")
    w = Dashboard()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
