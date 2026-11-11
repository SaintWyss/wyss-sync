"""Ventana de Wyss Sync: dos equipos, sus carpetas vinculadas y el estado de cada vínculo."""
from __future__ import annotations

import sys
import time

from PyQt6.QtCore import QPointF, QRectF, QSize, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import (QColor, QFont, QFontMetrics, QGuiApplication, QLinearGradient,
                         QPainter, QPainterPath, QPen)
from PyQt6.QtWidgets import (QApplication, QFrame, QGridLayout, QHBoxLayout, QLabel, QMenu,
                             QPushButton, QScrollArea, QSizePolicy, QStackedWidget, QTabBar,
                             QToolButton, QVBoxLayout, QWidget)

from .core import ApiError, Collector, SyncthingApi, read_config
from . import manage
from .detail import FolderDetail
from .pages import ConflictsPage, Context, DiffsPage, HistoryPage, LinksPage, SettingsPage

# Tokens muestreados del mockup. Un solo tema; ningún color vive fuera de este bloque.
TEXT = "#eaf2ff"
MUTED = "#c3d3ea"
LINE = "#38bdf8"
FOLDER = "#fbbf24"
PANEL_TOP = (16, 36, 74, 228)      # rgba sobre el escritorio: el vidrio deja ver lo que hay detrás
PANEL_BOTTOM = (8, 20, 46, 236)
PANEL_BORDER = (150, 200, 255, 90)
CARD_FILL = (255, 255, 255, 20)
CARD_BORDER = (255, 255, 255, 46)

# estado -> (texto, color del vínculo, color del texto)
STATES = {
    "synced": ("Sincronizado", "#34d399", "#d1fae5"),
    "syncing": ("Sincronizando", LINE, "#e0f2fe"),
    "scanning": ("Escaneando", LINE, "#e0f2fe"),
    "paused": ("Pausada", "#94a3b8", "#e2e8f0"),
    "offline": ("Sin conexión", "#fbbf24", "#fef3c7"),
    "error": ("Error", "#f87171", "#fee2e2"),
}
KINDS = {"desktop": "Escritorio", "laptop": "Portátil"}
REFRESH_MS = 5000
MONO = ["Hack", "JetBrains Mono", "monospace"]


def rgba(t: tuple[int, int, int, int]) -> QColor:
    return QColor(*t)


def short_path(p: str | None) -> str:
    return p or "ruta no disponible"


class ElideLabel(QLabel):
    """Etiqueta de una línea que recorta con … en vez de ensanchar la ventana."""

    def __init__(self, text: str = "", color: str = TEXT, mode=Qt.TextElideMode.ElideMiddle):
        super().__init__()
        self._full, self._color, self._mode = text, QColor(color), mode
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def set_full(self, text: str) -> None:
        self._full = text
        self.setToolTip(text)
        self.update()

    def minimumSizeHint(self) -> QSize:
        return QSize(40, QFontMetrics(self.font()).height() + 2)

    def sizeHint(self) -> QSize:
        return QSize(200, QFontMetrics(self.font()).height() + 2)

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setPen(self._color)
        p.setFont(self.font())
        txt = QFontMetrics(self.font()).elidedText(self._full, self._mode, self.width())
        p.drawText(self.rect(), int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), txt)


class DeviceIcon(QWidget):
    """Escritorio (monitor + torre) o portátil, dibujados con las mismas luces que el mockup."""

    def __init__(self, kind: str):
        super().__init__()
        self.kind, self._dim = kind, False
        self.setFixedSize(220, 100)

    def set_state(self, kind: str, online: bool) -> None:
        self.kind, self._dim = kind, not online
        self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._dim:
            p.setOpacity(0.45)
        w, h = self.width(), self.height()
        screen = QLinearGradient(0, 0, w, h)
        screen.setColorAt(0, QColor("#1d4ed8"))
        screen.setColorAt(1, QColor("#0ea5e9"))
        metal, edge = QColor("#cbd5e1"), QColor("#64748b")
        p.setPen(QPen(edge, 1.2))
        if self.kind == "laptop":
            lid = QRectF(w / 2 - 74, 6, 148, 82)
            p.setBrush(QColor("#0f172a"))
            p.drawRoundedRect(lid, 7, 7)
            p.setBrush(screen)
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(lid.adjusted(6, 6, -6, -6), 3, 3)
            p.setPen(QPen(edge, 1.2))
            p.setBrush(metal)
            p.drawRoundedRect(QRectF(w / 2 - 92, 88, 184, 9), 4, 4)
        else:
            mon = QRectF(w / 2 - 82, 6, 124, 78)
            p.setBrush(QColor("#0f172a"))
            p.drawRoundedRect(mon, 6, 6)
            p.setBrush(screen)
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(mon.adjusted(5, 5, -5, -5), 3, 3)
            p.setPen(QPen(edge, 1.2))
            p.setBrush(metal)
            p.drawRect(QRectF(mon.center().x() - 5, 84, 10, 12))
            p.drawRoundedRect(QRectF(mon.center().x() - 28, 96, 56, 7), 3, 3)
            tower = QRectF(w / 2 + 52, 14, 34, 86)
            p.setBrush(QColor("#1e293b"))
            p.drawRoundedRect(tower, 5, 5)
            p.setPen(QPen(QColor(LINE), 2))
            p.drawLine(QPointF(tower.center().x(), tower.top() + 12), QPointF(tower.center().x(), tower.top() + 40))
            p.drawEllipse(QPointF(tower.center().x(), tower.bottom() - 16), 6, 6)


class FolderIcon(QWidget):
    def __init__(self):
        super().__init__()
        self.setFixedSize(40, 32)

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#d97706"))
        p.drawRoundedRect(QRectF(2, 3, 18, 9), 3, 3)
        grad = QLinearGradient(0, 8, 0, 30)
        grad.setColorAt(0, QColor("#fde68a"))
        grad.setColorAt(1, QColor(FOLDER))
        p.setBrush(grad)
        p.drawRoundedRect(QRectF(2, 8, 36, 22), 4, 4)


class Card(QFrame):
    """Superficie de vidrio: relleno blanco al 8 % con borde de 1 px."""

    def __init__(self, radius: int = 6):
        super().__init__()
        self._radius = radius
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setBrush(rgba(CARD_FILL))
        p.setPen(QPen(rgba(CARD_BORDER), 1))
        p.drawRoundedRect(r, self._radius, self._radius)


class DiskBar(QWidget):
    """Una fila por disco: qué es, dónde está montado y cuánto tiene usado."""

    def __init__(self):
        super().__init__()
        self._d: dict | None = None
        self.setFixedHeight(38)
        self.setMinimumWidth(180)

    def set_disk(self, d: dict) -> None:
        self._d = d
        self.setToolTip(f"{d.get('model') or d.get('source')}\nMontado en {d['mount']} ({d['fstype']})\n"
                        f"Libre: {manage.human_size(d['free'])}")
        self.update()

    def paintEvent(self, _e) -> None:
        if not self._d:
            return
        d, p = self._d, QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        f = QFont()
        f.setPixelSize(12)
        p.setFont(f)
        used = (d["used"] / d["total"]) if d["total"] else 0
        p.setPen(QColor(TEXT))
        label = f"{manage.disk_label(d)}"
        p.drawText(QRectF(0, 0, self.width() * 0.62, 18), int(Qt.AlignmentFlag.AlignVCenter), label)
        p.setPen(QColor(MUTED))
        p.drawText(QRectF(self.width() * 0.45, 0, self.width() * 0.55, 18),
                   int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight),
                   f"{manage.human_size(d['used'])} de {manage.human_size(d['total'])} · {used * 100:.0f} %")
        track = QRectF(0, 24, self.width(), 6)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 38))
        p.drawRoundedRect(track, 2, 2)
        color = "#34d399" if used < 0.75 else ("#fbbf24" if used < 0.9 else "#f87171")
        p.setBrush(QColor(color))
        p.drawRoundedRect(QRectF(0, 24, max(3.0, self.width() * used), 6), 2, 2)


class DeviceCard(Card):
    def __init__(self):
        super().__init__(6)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(4)
        self.icon = DeviceIcon("desktop")
        self.name = QLabel("")
        self.name.setStyleSheet(f"color:{TEXT}; font-size:22px; font-weight:700; background:transparent;")
        self.sub = QLabel("")
        self.sub.setStyleSheet(f"color:{MUTED}; font-size:12px; background:transparent;")
        for w in (self.name, self.sub):
            w.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignHCenter)
        lay.addWidget(self.name)
        lay.addWidget(self.sub)
        self.disks_box = QVBoxLayout()
        self.disks_box.setSpacing(4)
        self.disks_box.setContentsMargins(0, 8, 0, 0)
        lay.addLayout(self.disks_box)
        self._bars: list[DiskBar] = []

    def update_device(self, d: dict, here: bool) -> None:
        disks = d.get("disks") or []
        while len(self._bars) < len(disks):
            bar = DiskBar()
            self.disks_box.addWidget(bar)
            self._bars.append(bar)
        for i, bar in enumerate(self._bars):
            bar.setVisible(i < len(disks))
            if i < len(disks):
                bar.set_disk(disks[i])
        self.icon.set_state(d["kind"], d["online"])
        self.name.setText(d["name"])
        parts = [KINDS.get(d["kind"], ""), d.get("os") or None]
        if here:
            parts.append("esta PC")
        elif not d["online"]:
            parts.append("desconectada")
        self.sub.setText(" · ".join(x for x in parts if x))


class FolderCard(Card):
    opened = pyqtSignal(str)

    def __init__(self, local: bool):
        super().__init__(6)
        self._local, self._path, self._fid = local, "", ""
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 10, 8, 10)
        lay.setSpacing(12)
        lay.addWidget(FolderIcon())
        col = QVBoxLayout()
        col.setSpacing(1)
        self.title = ElideLabel("", TEXT, Qt.TextElideMode.ElideRight)
        f = QFont()
        f.setPixelSize(16)
        f.setWeight(QFont.Weight.DemiBold)
        self.title.setFont(f)
        self.path = ElideLabel("", MUTED, Qt.TextElideMode.ElideMiddle)
        m = QFont(MONO)
        m.setStyleHint(QFont.StyleHint.Monospace)
        m.setPixelSize(12)
        self.path.setFont(m)
        self.disk = ElideLabel("", "#7dd3fc", Qt.TextElideMode.ElideRight)
        self.sync = ElideLabel("", "#86efac", Qt.TextElideMode.ElideRight)
        df = QFont()
        df.setPixelSize(12)
        self.disk.setFont(df)
        self.sync.setFont(df)
        col.addWidget(self.title)
        col.addWidget(self.path)
        col.addWidget(self.disk)
        col.addWidget(self.sync)
        lay.addLayout(col, 1)
        self.menu_btn = QToolButton()
        self.menu_btn.setText("⋮")
        self.menu_btn.setToolTip("Acciones de la carpeta")
        self.menu_btn.setAccessibleName("Acciones de la carpeta")
        self.menu_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.menu_btn.setFixedSize(34, 34)
        self.menu_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.menu_btn.setStyleSheet(
            f"QToolButton{{color:{MUTED};font-size:20px;border:none;border-radius:4px;background:transparent;}}"
            f"QToolButton:hover,QToolButton:focus{{background:rgba(255,255,255,30);color:{TEXT};}}"
            "QToolButton::menu-indicator{image:none;}")
        self.menu = QMenu(self)
        self.menu.setStyleSheet(
            f"QMenu{{background:#0f2447;color:{TEXT};border:1px solid rgba(150,200,255,90);padding:4px;}}"
            "QMenu::item{padding:6px 16px;border-radius:3px;}QMenu::item:selected{background:rgba(56,189,248,60);}")
        self.act_detail = self.menu.addAction("Ver subcarpetas")
        self.act_detail.triggered.connect(lambda: self.opened.emit(self._fid))
        self.act_open = self.menu.addAction("Abrir carpeta")
        self.act_open.triggered.connect(self._open)
        self.act_copy = self.menu.addAction("Copiar ruta")
        self.act_copy.triggered.connect(lambda: QGuiApplication.clipboard().setText(self._path))
        self.menu_btn.setMenu(self.menu)
        lay.addWidget(self.menu_btn)

    def set_data(self, label: str, own: str | None, other: str | None, fid: str = "",
                 disk: dict | None = None, sync: dict | None = None) -> None:
        self._path, self._fid = own or "", fid
        if sync is not None:
            text = (f"⇄ {manage.human_size(sync['bytes'])} sincroniza · {sync['files']:,} archivos".replace(",", "."))
            if sync["need"]:
                text += f" · faltan {manage.human_size(sync['need'])}"
            self.sync.set_full(text)
            if disk and disk.get("used"):
                self.sync.setToolTip(f"{sync['bytes'] / disk['used'] * 100:.1f} % de lo que hay usado en ese disco "
                                     "(el resto está ignorado o es de otras carpetas)")
        if disk:
            self.disk.set_full(f"💾 {manage.disk_label(disk)} · {manage.human_size(disk['used'])} de "
                               f"{manage.human_size(disk['total'])}")
            self.disk.setToolTip(f"{disk.get('model') or disk.get('source')} · montado en {disk['mount']}")
        else:
            self.disk.set_full("disco: sin datos")
        self.title.set_full(label)
        self.path.set_full(f"{short_path(own)} → {short_path(other)}")
        self.path.setToolTip(f"{own or '—'} → {other or '—'}")
        self.act_open.setVisible(self._local and bool(own))
        self.act_copy.setEnabled(bool(own))

    def mouseReleaseEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton and self.rect().contains(e.position().toPoint()) and self._fid:
            self.opened.emit(self._fid)
        super().mouseReleaseEvent(e)

    def _open(self) -> None:
        import subprocess
        subprocess.Popen(["xdg-open", self._path])


class LinkPill(QWidget):
    """Píldora central con el estado; dos flechas entran desde cada equipo."""

    def __init__(self):
        super().__init__()
        self._state, self._pct, self._angle = "synced", None, 0
        self.setMinimumSize(280, 56)
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._tick)

    def set_state(self, state: str, pct: float | None) -> None:
        self._state, self._pct = state, pct
        if state in ("syncing", "scanning") and not self._timer.isActive():
            self._timer.start()
        elif state not in ("syncing", "scanning"):
            self._timer.stop()
        label = self.label()
        self.setAccessibleName(f"Estado del vínculo: {label}")
        self.setToolTip(label)
        self.update()

    def label(self) -> str:
        text = STATES[self._state][0]
        # hacia abajo: 99,9 % no puede mostrarse como 100 % mientras falta algo
        return f"{text} {int(self._pct)} %" if self._pct is not None else text

    @staticmethod
    def pill_font() -> QFont:
        f = QFont()
        f.setPixelSize(14)
        f.setWeight(QFont.Weight.DemiBold)
        return f

    def pill_width(self) -> float:
        """Ancho único para todas las filas: el de la etiqueta más larga, con 42 px de flecha a cada lado."""
        longest = max(QFontMetrics(self.pill_font()).horizontalAdvance(t)
                      for t in (f"{v[0]} 100 %" for v in STATES.values()))
        return float(min(self.width() - 84, longest + 62))

    def _tick(self) -> None:
        self._angle = (self._angle + 12) % 360
        self.update()

    def paintEvent(self, _e) -> None:
        text, color, text_color = STATES[self._state]
        c = QColor(color)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setFont(self.pill_font())
        label = self.label()
        pill_w = self.pill_width()
        cy = self.height() / 2
        pill = QRectF((self.width() - pill_w) / 2, cy - 19, pill_w, 38)
        # flechas: punto en el equipo -> cabeza apuntando a la píldora
        link = QColor(LINE) if self._state != "offline" else QColor("#64748b")
        p.setPen(QPen(link, 2))
        p.setBrush(link)
        for x0, x1, d in ((4.0, pill.left() - 4, 1), (self.width() - 4.0, pill.right() + 4, -1)):
            p.drawEllipse(QPointF(x0, cy), 4, 4)
            p.drawLine(QPointF(x0, cy), QPointF(x1 - 8 * d, cy))
            head = QPainterPath()
            head.moveTo(x1, cy)
            head.lineTo(x1 - 9 * d, cy - 5)
            head.lineTo(x1 - 9 * d, cy + 5)
            head.closeSubpath()
            p.drawPath(head)
        fill = QColor(c)
        fill.setAlpha(46)
        p.setBrush(fill)
        p.setPen(QPen(c, 1.5))
        p.drawRoundedRect(pill, 6, 6)
        # icono: dos arcos con punta; gira mientras sincroniza
        p.save()
        p.translate(pill.left() + 22, cy)
        p.rotate(self._angle)
        p.setPen(QPen(c, 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.setBrush(Qt.BrushStyle.NoBrush)
        arc = QRectF(-7, -7, 14, 14)
        p.drawArc(arc, 20 * 16, 140 * 16)
        p.drawArc(arc, 200 * 16, 140 * 16)
        p.restore()
        p.setPen(QColor(text_color))
        p.drawText(QRectF(pill.left() + 38, pill.top(), pill.width() - 46, pill.height()),
                   int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), label)


class Fetcher(QThread):
    done = pyqtSignal(object)

    def __init__(self, collector: Collector):
        super().__init__()
        self._collector = collector

    def run(self) -> None:
        try:
            self.done.emit(self._collector.collect())
        except Exception as e:  # cualquier falla se muestra en el pie, nunca tumba la ventana
            self.done.emit(e)


class Window(QWidget):
    model_ready = pyqtSignal(object)   # las pestañas de historial, diferencias, etc. se enganchan acá

    def __init__(self, collector: Collector | None):
        super().__init__()
        self.setWindowTitle("Wyss Sync")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)   # barra de título: la del sistema
        self.setMinimumSize(920, 600)
        self.resize(1140, 760)
        self._collector, self._fetcher = collector, None
        self._rows: dict[str, tuple[FolderCard, LinkPill, FolderCard]] = {}
        self._build()
        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self.refresh)
        if collector:
            self.refresh()
            self._timer.start()
            QTimer.singleShot(10000, self._auto_purge)
        else:
            self.status.setText("No pude leer la configuración de Syncthing (¿está instalado?).")

    # ---------- layout
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 8, 16, 8)
        root.setSpacing(10)

        bar = QHBoxLayout()
        self.tabs = QTabBar()
        self.tabs.setDrawBase(False)
        self.tabs.setExpanding(False)
        self.tabs.setCursor(Qt.CursorShape.PointingHandCursor)
        self.tabs.setStyleSheet(
            f"QTabBar::tab{{color:{MUTED};background:transparent;padding:8px 14px;margin-right:2px;"
            "border:none;border-bottom:2px solid transparent;font-size:13px;}"
            f"QTabBar::tab:selected{{color:{TEXT};border-bottom:2px solid {LINE};}}"
            "QTabBar::tab:hover{background:rgba(255,255,255,18);}"
            "QTabBar::tab:focus{outline:1px solid #ffffff;}")
        bar.addWidget(self.tabs)
        bar.addStretch()
        self.refresh_btn = QPushButton("Actualizar")
        self.refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh_btn.setStyleSheet(
            f"QPushButton{{color:{TEXT};background:rgba(255,255,255,22);border:1px solid rgba(255,255,255,46);"
            "border-radius:4px;padding:5px 14px;font-size:12px;}"
            "QPushButton:hover{background:rgba(255,255,255,40);}"
            "QPushButton:focus{border:1px solid #ffffff;}")
        self.refresh_btn.clicked.connect(self._refresh_clicked)
        bar.addWidget(self.refresh_btn)
        root.addLayout(bar)

        top = QGridLayout()
        top.setHorizontalSpacing(0)
        self.dev_local, self.dev_remote = DeviceCard(), DeviceCard()
        self.conn = QLabel("")
        self.conn.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.conn.setMinimumWidth(280)
        top.addWidget(self.dev_local, 0, 0)
        top.addWidget(self.conn, 0, 1)
        top.addWidget(self.dev_remote, 0, 2)
        for c, s in ((0, 5), (1, 0), (2, 5)):
            top.setColumnStretch(c, s)
        self.home = QWidget()
        hl = QVBoxLayout(self.home)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(12)
        hl.addLayout(top)

        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(0)
        self.grid.setVerticalSpacing(8)
        for c, s in ((0, 5), (1, 0), (2, 5)):
            self.grid.setColumnStretch(c, s)
        self.grid.setColumnMinimumWidth(1, 280)
        holder = QWidget()
        holder.setLayout(self.grid)
        holder.setStyleSheet("background:transparent;")
        self.empty = QLabel("")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setStyleSheet(f"color:{MUTED}; font-size:14px; background:transparent;")
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea{background:transparent;}"
                             "QScrollBar:vertical{width:8px;background:transparent;}"
                             "QScrollBar::handle:vertical{background:rgba(255,255,255,60);border-radius:3px;}"
                             "QScrollBar::add-line,QScrollBar::sub-line{height:0;}")
        scroll.viewport().setStyleSheet("background:transparent;")
        scroll.setWidget(holder)
        hl.addWidget(scroll, 1)
        hl.addWidget(self.empty)
        self.stack = QStackedWidget()
        self.detail = FolderDetail(self._collector.api if self._collector else None)
        self.detail.back.connect(lambda: self.stack.setCurrentWidget(self.home))
        self.stack.addWidget(self.home)
        self.stack.addWidget(self.detail)

        self.ctx = Context(self._collector.api if self._collector else None)
        self.pages = QStackedWidget()
        self.add_page("Carpetas", self.stack)
        self.add_page("Vínculos", LinksPage(self.ctx))
        self.add_page("Historial", HistoryPage(self.ctx))
        self.add_page("Diferencias", DiffsPage(self.ctx))
        self.add_page("Conflictos y versiones", ConflictsPage(self.ctx))
        self.add_page("Ajustes", SettingsPage(self.ctx))
        self.tabs.currentChanged.connect(self.pages.setCurrentIndex)
        root.addWidget(self.pages, 1)

        self.status = QLabel("Consultando…")
        self.status.setStyleSheet(f"color:{MUTED}; font-size:12px; background:transparent;")
        root.addWidget(self.status)

    def add_page(self, title: str, widget: QWidget) -> None:
        self.tabs.addTab(title)
        self.pages.addWidget(widget)

    def _auto_purge(self) -> None:
        """Borra de descartados/ lo que tiene más de 30 días. En segundo plano; no avisa si no hay nada."""
        from .detail import Task
        t = Task(manage.purge_descartados)
        t.done.connect(lambda _r: None)
        self._purge_task = t
        t.start()

    # ---------- datos
    def _refresh_clicked(self) -> None:
        if self.stack.currentWidget() is self.detail:
            self.detail.reload()
        self.refresh()

    def open_detail(self, fid: str) -> None:
        m = getattr(self, "_model", None)
        folder = next((f for f in (m or {}).get("folders", []) if f["id"] == fid), None)
        if not folder or not self._collector:
            return
        rem = m["remote"]
        self.detail.open(folder, m["local"]["name"], rem["name"],
                         [h for h in (rem["name"], rem.get("address")) if h], bool(rem["online"]))
        self.stack.setCurrentWidget(self.detail)

    def refresh(self) -> None:
        if not self._collector or (self._fetcher and self._fetcher.isRunning()):
            return
        self._fetcher = Fetcher(self._collector)
        self._fetcher.done.connect(self.render)
        self._fetcher.start()

    def render(self, model) -> None:
        if isinstance(model, Exception):
            self.status.setText(f"Syncthing no responde ({model}). Reintento en {REFRESH_MS // 1000} s.")
            self.status.setStyleSheet("color:#fca5a5; font-size:12px; background:transparent;")
            return
        self.status.setStyleSheet(f"color:{MUTED}; font-size:12px; background:transparent;")
        self._model = model
        loc, rem = model["local"], model["remote"]
        self.dev_local.update_device(loc, here=True)
        self.dev_remote.update_device(rem, here=False)
        if rem["online"]:
            self.conn.setText(f"<span style='color:#34d399'>●</span>&nbsp;Conectadas<br>"
                              f"<span style='color:{MUTED};font-size:12px'>{rem.get('address') or ''} · LAN</span><br>"
                              f"<span style='color:#7dd3fc;font-size:12px'>↓ {manage.human_rate(rem.get('rate_in', 0))}"
                              f" &nbsp; ↑ {manage.human_rate(rem.get('rate_out', 0))}</span>")
        else:
            self.conn.setText(f"<span style='color:#fbbf24'>○</span>&nbsp;{rem['name']} sin conexión")
        self.conn.setStyleSheet(f"color:{TEXT}; font-size:14px; font-weight:600; background:transparent;")

        ids = [f["id"] for f in model["folders"]]
        if ids != list(self._rows):
            self._rebuild(ids)
        for f in model["folders"]:
            left, pill, right = self._rows[f["id"]]
            sync = {"bytes": f.get("sync_bytes", 0), "files": f.get("sync_files", 0), "need": f.get("need_bytes", 0)}
            left.set_data(f["label"], f["local_path"], f["remote_path"], f["id"], f.get("local_disk"), sync)
            right.set_data(f["label"], f["remote_path"], f["local_path"], f["id"], f.get("remote_disk"), sync)
            pill.set_state(f["state"], f["percent"])
        self.empty.setText("" if ids else "Syncthing no tiene carpetas compartidas con la otra PC.")
        notes = []
        if rem["online"] and not rem["ssh"]:
            notes.append("sin ssh a la otra PC: rutas remotas no disponibles")
        pend = sum(f["need_items"] for f in model["folders"])
        if pend:
            notes.append(f"{pend} elementos pendientes")
        self.status.setText("Actualizado " + time.strftime("%H:%M:%S") + " · cada "
                            f"{REFRESH_MS // 1000} s" + "".join(f" · {n}" for n in notes))
        first = self.ctx.model is None
        self.ctx.update(model)
        self.model_ready.emit(model)
        if first:   # la pestaña que ya estaba a la vista esperaba este primer modelo
            page = self.pages.currentWidget()
            if hasattr(page, "load"):
                page.load()

    def _rebuild(self, ids: list[str]) -> None:
        self._rows_prev_ids = list(self._rows)
        for left, pill, right in self._rows.values():
            for w in (left, pill, right):
                w.setParent(None)
                w.deleteLater()
        self._rows = {}
        self.grid.setRowStretch(len(self._rows_prev_ids), 0)
        for i, fid in enumerate(ids):
            row = (FolderCard(True), LinkPill(), FolderCard(False))
            row[0].opened.connect(self.open_detail)
            row[2].opened.connect(self.open_detail)
            for c, w in enumerate(row):
                self.grid.addWidget(w, i, c)
            self._rows[fid] = row
        self.grid.setRowStretch(len(ids), 1)

    # ---------- vidrio
    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        g = QLinearGradient(0, 0, 0, self.height())
        g.setColorAt(0, rgba(PANEL_TOP))
        g.setColorAt(1, rgba(PANEL_BOTTOM))
        p.fillRect(self.rect(), g)


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("Wyss Sync")
    font = QFont("Noto Sans")
    font.setPixelSize(13)
    app.setFont(font)
    try:
        base, key = read_config()
        collector = Collector(SyncthingApi(base, key))
    except ApiError as e:
        collector = None
        print(f"Wyss Sync: {e}", file=sys.stderr)
    w = Window(collector)
    w.show()
    sys.exit(app.exec())
