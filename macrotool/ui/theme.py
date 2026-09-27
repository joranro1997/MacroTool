"""Tema oscuro de la interfaz: paleta, hoja de estilos (QSS), iconos con glifos e icono de la app.

Los iconos se pintan con los glifos de la fuente "Segoe Fluent Icons" (Windows 11) o, si no
está, "Segoe MDL2 Assets" (Windows 10). Todas las funciones que crean imágenes necesitan que
exista ya una ``QGuiApplication``.
"""
from __future__ import annotations

import sys
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontDatabase,
    QGuiApplication,
    QIcon,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PySide6.QtWidgets import QApplication, QStyleFactory, QWidget

# --- Paleta -------------------------------------------------------------------------------
BG = "#16171d"  # fondo de la ventana
SURFACE = "#1e2029"  # paneles laterales, barra de estado
SURFACE_2 = "#262936"  # campos, tarjetas
SURFACE_3 = "#2f3344"  # hover
BORDER = "#2c2f3c"
BORDER_STRONG = "#3a3e51"
ACCENT = "#7c6cf2"
ACCENT_HOVER = "#8e80f6"
ACCENT_PRESSED = "#6a5ae0"
TEXT = "#e8e8ef"
TEXT_DIM = "#9a9cb0"
TEXT_FAINT = "#686b80"
GREEN = "#3ecf8e"
RED = "#ef5b6b"
AMBER = "#f2b84b"

# Colores suaves por tipo de paso e icono asociado.
STEP_COLORS: dict[str, str] = {
    "press": "#9d8fff",
    "text": "#5cb2f7",
    "move": "#3ecf8e",
    "scroll": "#f2b84b",
    "wait": "#ef85b2",
}
STEP_ICONS: dict[str, str] = {
    "press": "keyboard",
    "text": "text",
    "move": "move",
    "scroll": "scroll",
    "wait": "wait",
}

# Glifos de Segoe Fluent Icons / Segoe MDL2 Assets.
GLYPHS: dict[str, int] = {
    "play": 0xF5B0,
    "stop": 0xEE95,
    "pause": 0xF8AE,
    "record": 0xF137,
    "add": 0xE710,
    "close": 0xE711,
    "more": 0xE712,
    "settings": 0xE713,
    "delete": 0xE74D,
    "edit": 0xE70F,
    "copy": 0xE8C8,
    "paste": 0xE77F,
    "cut": 0xE8C6,
    "duplicate": 0xF413,
    "undo": 0xE7A7,
    "redo": 0xE7A6,
    "up": 0xE74A,
    "down": 0xE74B,
    "import": 0xE8B5,
    "export": 0xEDE1,
    "keyboard": 0xE765,
    "mouse": 0xE962,
    "text": 0xE8D2,
    "move": 0xE7C2,
    "scroll": 0xEC8F,
    "wait": 0xE916,
    "clock": 0xE823,
    "bolt": 0xE945,
    "power": 0xE7E8,
    "crosshair": 0xF272,
    "search": 0xE721,
    "check": 0xE73E,
    "warning": 0xE7BA,
    "info": 0xE946,
    "repeat": 0xE8EE,
    "chevron_down": 0xE70D,
    "chevron_up": 0xE70E,
    "chevron_right": 0xE76C,
    "sliders": 0xE9E9,
    "person": 0xE77B,
    "rename": 0xE8AC,
    "list": 0xEA37,
    "blocked": 0xE733,
    "gripper": 0xE76F,
    "circle": 0xEA3A,
}

_ICON_FONT_FILES = (
    ("Segoe Fluent Icons", "SegoeIcons.ttf"),
    ("Segoe MDL2 Assets", "segmdl2.ttf"),
)


@lru_cache(maxsize=1)
def icon_family() -> Optional[str]:
    """Familia de la fuente de iconos disponible (o None si no hay ninguna)."""
    families = set(QFontDatabase.families())
    for family, _ in _ICON_FONT_FILES:
        if family in families:
            return family
    # Algunas plataformas (p. ej. "offscreen") no ven las fuentes del sistema: se cargan a mano.
    fonts_dir = Path(r"C:\Windows\Fonts")
    for family, filename in _ICON_FONT_FILES:
        path = fonts_dir / filename
        if path.exists():
            font_id = QFontDatabase.addApplicationFont(str(path))
            if font_id >= 0:
                loaded = QFontDatabase.applicationFontFamilies(font_id)
                if loaded:
                    return loaded[0]
    return None


def glyph_font(pixel_size: int) -> QFont:
    font = QFont(icon_family() or "Segoe UI")
    font.setPixelSize(max(1, int(pixel_size)))
    font.setHintingPreference(QFont.PreferNoHinting)
    return font


def glyph(name: str) -> str:
    return chr(GLYPHS.get(name, GLYPHS["circle"]))


def draw_glyph(painter: QPainter, rect: QRectF, name: str, color: str | QColor, pixel_size: int) -> None:
    """Pinta el glifo ``name`` centrado en ``rect``."""
    painter.save()
    painter.setPen(QColor(color))
    if icon_family() is None:
        # Sin fuente de iconos: un punto discreto en lugar de un cuadrado vacío.
        r = pixel_size * 0.18
        painter.setBrush(QColor(color))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(rect.center(), r, r)
    else:
        painter.setFont(glyph_font(pixel_size))
        painter.drawText(rect, Qt.AlignCenter, glyph(name))
    painter.restore()


def _glyph_pixmap(name: str, color: str, size: int, dpr: float) -> QPixmap:
    pm = QPixmap(QSize(round(size * dpr), round(size * dpr)))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.TextAntialiasing)
    draw_glyph(p, QRectF(0, 0, size, size), name, color, round(size * 0.8))
    p.end()
    return pm


_icon_cache: dict[tuple[str, str, int], QIcon] = {}


def icon(name: str, color: str | None = None, size: int = 20) -> QIcon:
    """QIcon con el glifo ``name`` en ``color`` (por defecto, el color de texto)."""
    color = color or TEXT
    key = (name, color, size)
    cached = _icon_cache.get(key)
    if cached is not None:
        return cached
    ic = QIcon()
    for dpr in (1.0, 1.5, 2.0):
        ic.addPixmap(_glyph_pixmap(name, color, size, dpr), QIcon.Normal, QIcon.Off)
        ic.addPixmap(_glyph_pixmap(name, TEXT_FAINT, size, dpr), QIcon.Disabled, QIcon.Off)
    _icon_cache[key] = ic
    return ic


def step_color(step_type: str) -> str:
    return STEP_COLORS.get(step_type, TEXT_DIM)


def with_alpha(color: str | QColor, alpha: float) -> QColor:
    c = QColor(color)
    c.setAlphaF(max(0.0, min(1.0, alpha)))
    return c


# --- Icono de la aplicación --------------------------------------------------------------
def paint_app_icon(painter: QPainter, size: int) -> None:
    """Pinta el icono de MacroTool (cuadrado redondeado violeta con una tecla y un rayo)."""
    s = float(size)
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing)
    margin = s * 0.04
    body = QRectF(margin, margin, s - 2 * margin, s - 2 * margin)
    radius = body.width() * 0.24

    grad = QLinearGradient(body.topLeft(), body.bottomRight())
    grad.setColorAt(0.0, QColor("#a193ff"))
    grad.setColorAt(0.55, QColor("#7c6cf2"))
    grad.setColorAt(1.0, QColor("#4b3bc4"))
    painter.setPen(Qt.NoPen)
    painter.setBrush(QBrush(grad))
    painter.drawRoundedRect(body, radius, radius)

    # Brillo suave en la parte superior.
    shine = QRadialGradient(QPointF(body.left() + body.width() * 0.3, body.top()), body.width() * 0.9)
    shine.setColorAt(0.0, QColor(255, 255, 255, 70))
    shine.setColorAt(1.0, QColor(255, 255, 255, 0))
    painter.setBrush(QBrush(shine))
    painter.drawRoundedRect(body, radius, radius)

    # Tecla (keycap) en blanco translúcido; se omite en tamaños muy pequeños.
    if size >= 32:
        cap = QRectF(body.left() + body.width() * 0.19, body.top() + body.height() * 0.2,
                     body.width() * 0.62, body.height() * 0.6)
        cap_r = cap.width() * 0.2
        painter.setBrush(QColor(255, 255, 255, 38))
        pen = QPen(QColor(255, 255, 255, 215))
        pen.setWidthF(max(1.2, s * 0.035))
        painter.setPen(pen)
        painter.drawRoundedRect(cap, cap_r, cap_r)
        painter.setPen(Qt.NoPen)
        bolt_box = cap.adjusted(cap.width() * 0.2, cap.height() * 0.12, -cap.width() * 0.2, -cap.height() * 0.12)
    else:
        bolt_box = body.adjusted(body.width() * 0.2, body.height() * 0.12, -body.width() * 0.2, -body.height() * 0.12)

    # Rayo.
    pts = [(0.62, 0.0), (0.12, 0.56), (0.46, 0.56), (0.36, 1.0), (0.88, 0.40), (0.54, 0.40), (0.62, 0.0)]
    path = QPainterPath()
    for i, (px, py) in enumerate(pts):
        point = QPointF(bolt_box.left() + px * bolt_box.width(), bolt_box.top() + py * bolt_box.height())
        if i == 0:
            path.moveTo(point)
        else:
            path.lineTo(point)
    path.closeSubpath()
    painter.setBrush(QColor("#ffffff"))
    painter.drawPath(path)
    painter.restore()


def render_app_icon(size: int) -> QImage:
    img = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QPainter(img)
    paint_app_icon(p, size)
    p.end()
    return img


def assets_dir() -> Path:
    """Carpeta ``assets`` (también dentro de un ejecutable de PyInstaller)."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / "assets"
    return Path(__file__).resolve().parents[2] / "assets"


@lru_cache(maxsize=1)
def app_icon() -> QIcon:
    for name in ("icon.ico", "icon.png"):
        path = assets_dir() / name
        if path.exists():
            ic = QIcon(str(path))
            if not ic.isNull():
                return ic
    ic = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        ic.addPixmap(QPixmap.fromImage(render_app_icon(size)))
    return ic


# --- Hoja de estilos ---------------------------------------------------------------------
def _write_png(img: QImage, path: Path) -> None:
    if not path.exists():
        img.save(str(path), "PNG")


def _theme_assets() -> dict[str, str]:
    """Genera (una vez) los PNG de flechas/checks que usa el QSS y devuelve sus rutas."""
    folder = Path(tempfile.gettempdir()) / "macrotool-theme-v2"
    folder.mkdir(parents=True, exist_ok=True)
    specs = {
        "down": ("chevron_down", TEXT_DIM, 10),
        "down-disabled": ("chevron_down", TEXT_FAINT, 10),
        "up": ("chevron_up", TEXT_DIM, 10),
        "up-disabled": ("chevron_up", TEXT_FAINT, 10),
        "check": ("check", "#ffffff", 12),
        "check-disabled": ("check", TEXT_FAINT, 12),
        "right": ("chevron_right", TEXT_DIM, 10),
    }
    paths: dict[str, str] = {}
    for key, (name, color, size) in specs.items():
        for suffix, dpr in (("", 1.0), ("@2x", 2.0)):
            pm = _glyph_pixmap(name, color, size, dpr)
            _write_png(pm.toImage(), folder / f"{key}{suffix}.png")
        paths[key] = (folder / f"{key}.png").as_posix()
    return paths


def _qss_url(path: str) -> str:
    """``url("…")`` entre comillas: sin ellas Qt no admite rutas con apóstrofos (C:/Users/O'Brien/…)
    y descarta el resto de la hoja de estilos. (Una ruta de Windows no puede contener comillas.)"""
    return f'url("{path}")'


def stylesheet() -> str:
    a = {key: _qss_url(path) for key, path in _theme_assets().items()}
    accent_soft = "rgba(124, 108, 242, 0.16)"
    accent_mid = "rgba(124, 108, 242, 0.32)"
    return f"""
* {{ outline: 0; }}
QWidget {{
    color: {TEXT};
    font-size: 10pt;
    selection-background-color: {ACCENT};
    selection-color: #ffffff;
}}
QMainWindow, QDialog {{ background: {BG}; }}
QWidget#Central {{ background: {BG}; }}
QWidget#Sidebar {{ background: {SURFACE}; border-right: 1px solid {BORDER}; }}
QWidget#RightPanel {{ background: {SURFACE}; border-left: 1px solid {BORDER}; }}
QWidget#PanelBody {{ background: {SURFACE}; }}
QFrame#Card {{ background: {SURFACE_2}; border: 1px solid {BORDER}; border-radius: 10px; }}
QFrame#SoftCard {{ background: rgba(255, 255, 255, 0.025); border: 1px solid {BORDER}; border-radius: 10px; }}
QFrame#AccentCard {{ background: {accent_soft}; border: 1px solid rgba(124, 108, 242, 0.55); border-radius: 10px; }}
QFrame#GlobalSwitchCard {{ background: {SURFACE_2}; border: 1px solid {BORDER_STRONG}; border-radius: 10px; }}
QFrame#GlobalSwitchCard[active="true"] {{ background: rgba(62, 207, 142, 0.10); border: 1px solid rgba(62, 207, 142, 0.45); }}
QFrame#Separator {{ background: {BORDER}; max-height: 1px; min-height: 1px; border: none; }}
QFrame#Chip {{ background: rgba(124, 108, 242, 0.16); border: 1px solid rgba(124, 108, 242, 0.55); border-radius: 13px; }}
QFrame#Chip[mouse="true"] {{ background: rgba(92, 178, 247, 0.13); border: 1px solid rgba(92, 178, 247, 0.55); }}
QLabel#ChipText {{ font-weight: 600; }}
QFrame#ErrorBox {{ background: rgba(239, 91, 107, 0.12); border: 1px solid rgba(239, 91, 107, 0.45); border-radius: 8px; }}

QLabel {{ background: transparent; }}
QLabel[role="title"] {{ font-size: 15pt; font-weight: 600; }}
QLabel[role="appname"] {{ font-size: 12pt; font-weight: 700; }}
QLabel[role="heading"] {{ font-size: 11pt; font-weight: 600; }}
QLabel[role="section"] {{ color: {TEXT_DIM}; font-size: 8pt; font-weight: 700; letter-spacing: 1px; }}
QLabel[role="dim"] {{ color: {TEXT_DIM}; }}
QLabel[role="hint"] {{ color: {TEXT_FAINT}; font-size: 9pt; }}
QLabel[role="error"] {{ color: {RED}; }}
QLabel[role="badge"] {{
    background: {SURFACE_3}; color: {TEXT_DIM}; border-radius: 9px; padding: 1px 8px; font-size: 8.5pt;
    font-weight: 600;
}}

/* Botones */
QPushButton {{
    background: {SURFACE_2}; border: 1px solid {BORDER_STRONG}; border-radius: 8px;
    padding: 6px 14px; color: {TEXT};
}}
QPushButton:hover {{ background: {SURFACE_3}; border-color: #4a4f66; }}
QPushButton:pressed {{ background: #22242e; }}
QPushButton:disabled {{ color: {TEXT_FAINT}; background: #1d1f27; border-color: {BORDER}; }}
QPushButton:checked {{ background: {accent_mid}; border-color: {ACCENT}; }}
QPushButton[kind="primary"] {{ background: {ACCENT}; border: 1px solid {ACCENT}; color: #ffffff; font-weight: 600; }}
QPushButton[kind="primary"]:hover {{ background: {ACCENT_HOVER}; border-color: {ACCENT_HOVER}; }}
QPushButton[kind="primary"]:pressed {{ background: {ACCENT_PRESSED}; }}
QPushButton[kind="primary"]:disabled {{ background: #3b3766; border-color: #3b3766; color: #a4a0c8; }}
QPushButton[kind="danger"] {{ background: rgba(239, 91, 107, 0.14); border: 1px solid rgba(239, 91, 107, 0.55); color: #ffb3bb; }}
QPushButton[kind="danger"]:hover {{ background: rgba(239, 91, 107, 0.24); }}
QPushButton[kind="danger"]:disabled {{ background: #1d1f27; border-color: {BORDER}; color: {TEXT_FAINT}; }}
QPushButton[kind="recording"] {{ background: {RED}; border: 1px solid {RED}; color: #ffffff; font-weight: 600; }}
QPushButton[kind="ghost"] {{ background: transparent; border: 1px solid transparent; color: {TEXT_DIM}; }}
QPushButton[kind="ghost"]:hover {{ background: {SURFACE_3}; color: {TEXT}; }}
QPushButton[kind="chip"] {{
    background: {SURFACE_2}; border: 1px solid {BORDER_STRONG}; border-radius: 11px;
    padding: 3px 10px; min-height: 16px; font-size: 9pt; color: {TEXT_DIM};
}}
QPushButton[kind="chip"]:hover {{ border-color: {ACCENT}; color: {TEXT}; }}
QPushButton[kind="chip"]:checked {{ background: {accent_mid}; border-color: {ACCENT}; color: #ffffff; }}
QPushButton[kind="segment"] {{
    background: transparent; border: 1px solid transparent; border-radius: 7px; padding: 6px 12px; color: {TEXT_DIM};
}}
QPushButton[kind="segment"]:hover {{ background: {SURFACE_3}; color: {TEXT}; }}
QPushButton[kind="segment"]:checked {{ background: {ACCENT}; color: #ffffff; font-weight: 600; }}
QPushButton[kind="addstep"] {{ padding: 7px 12px; }}
QPushButton[kind="addstep-primary"] {{
    background: {ACCENT}; border: 1px solid {ACCENT}; color: #ffffff; font-weight: 600; padding: 7px 14px;
}}
QPushButton[kind="addstep-primary"]:hover {{ background: {ACCENT_HOVER}; border-color: {ACCENT_HOVER}; }}
QPushButton[kind="addstep-primary"]:disabled {{ background: #3b3766; border-color: #3b3766; color: #a4a0c8; }}
QPushButton::menu-indicator {{ image: {a["down"]}; subcontrol-position: right center; subcontrol-origin: padding; right: 6px; }}
QPushButton[kind="primary"]::menu-indicator, QPushButton[kind="recording"]::menu-indicator {{ image: none; }}

QToolButton {{
    background: transparent; border: 1px solid transparent; border-radius: 8px; padding: 5px; color: {TEXT};
}}
QToolButton:hover {{ background: {SURFACE_3}; border-color: {BORDER_STRONG}; }}
QToolButton:pressed {{ background: #22242e; }}
QToolButton:checked {{ background: {accent_mid}; border-color: {ACCENT}; }}
QToolButton:disabled {{ color: {TEXT_FAINT}; }}
QToolButton[kind="framed"] {{ background: {SURFACE_2}; border: 1px solid {BORDER_STRONG}; }}
QToolButton[kind="framed"]:hover {{ background: {SURFACE_3}; border-color: #4a4f66; }}
QToolButton[kind="small"] {{ padding: 2px; border-radius: 6px; }}
QToolButton::menu-indicator {{ image: none; }}
QToolButton[popupMode="1"] {{ padding-right: 18px; }}
QToolButton::menu-button {{ border: none; border-left: 1px solid {BORDER_STRONG}; width: 18px; }}
QToolButton::menu-arrow {{ image: {a["down"]}; width: 10px; height: 10px; }}

/* Campos */
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {SURFACE_2}; border: 1px solid {BORDER_STRONG}; border-radius: 8px; padding: 5px 8px;
    color: {TEXT}; selection-background-color: {ACCENT};
}}
QLineEdit:hover, QPlainTextEdit:hover, QSpinBox:hover, QComboBox:hover {{ border-color: #4a4f66; }}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
QLineEdit:disabled, QPlainTextEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{
    color: {TEXT_FAINT}; background: #1d1f27; border-color: {BORDER};
}}
QLineEdit#NameEdit {{
    background: transparent; border: 1px solid transparent; border-radius: 8px;
    font-size: 15pt; font-weight: 600; padding: 2px 6px;
}}
QLineEdit#NameEdit:hover {{ border-color: {BORDER_STRONG}; }}
QLineEdit#NameEdit:focus {{ border-color: {ACCENT}; background: {SURFACE_2}; }}
QSpinBox {{ padding-right: 24px; }}
QSpinBox#BigSpin {{ font-size: 15pt; font-weight: 600; padding: 4px 26px 4px 10px; background: {BG}; }}
QSpinBox::up-button, QSpinBox::down-button {{
    subcontrol-origin: border; width: 20px; border: none; background: transparent; margin-right: 2px;
}}
QSpinBox::up-button {{ subcontrol-position: top right; margin-top: 2px; border-top-right-radius: 6px; }}
QSpinBox::down-button {{ subcontrol-position: bottom right; margin-bottom: 2px; border-bottom-right-radius: 6px; }}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {{ background: {SURFACE_3}; }}
QSpinBox::up-arrow {{ image: {a["up"]}; width: 9px; height: 9px; }}
QSpinBox::down-arrow {{ image: {a["down"]}; width: 9px; height: 9px; }}
QSpinBox::up-arrow:disabled, QSpinBox::up-arrow:off {{ image: {a["up-disabled"]}; }}
QSpinBox::down-arrow:disabled, QSpinBox::down-arrow:off {{ image: {a["down-disabled"]}; }}
QComboBox {{ padding-right: 28px; }}
QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: center right; width: 26px; border: none; }}
QComboBox::down-arrow {{ image: {a["down"]}; width: 10px; height: 10px; }}
QComboBox::down-arrow:disabled {{ image: {a["down-disabled"]}; }}
QComboBox QAbstractItemView {{
    background: #232633; border: 1px solid {BORDER_STRONG}; border-radius: 8px; padding: 4px;
    selection-background-color: {accent_mid}; selection-color: {TEXT}; outline: 0;
}}
QComboBox QAbstractItemView::item {{ min-height: 26px; padding: 2px 8px; border-radius: 6px; }}
QComboBox QAbstractItemView::item:hover {{ background: {SURFACE_3}; }}
QComboBox QAbstractItemView::item:selected {{ background: {accent_mid}; }}

QCheckBox, QRadioButton {{ spacing: 8px; background: transparent; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 16px; height: 16px; border: 1px solid #4d5167; background: {SURFACE_2};
}}
QCheckBox::indicator {{ border-radius: 5px; }}
QRadioButton::indicator {{ border-radius: 9px; }}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{ border-color: {ACCENT}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; image: {a["check"]}; }}
QCheckBox::indicator:checked:disabled {{ background: #3b3766; border-color: #3b3766; image: {a["check-disabled"]}; }}
QCheckBox::indicator:disabled {{ border-color: {BORDER}; background: #1d1f27; }}
QCheckBox:disabled, QRadioButton:disabled {{ color: {TEXT_FAINT}; }}
QRadioButton::indicator:checked {{ background: qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5,
    stop:0 #ffffff, stop:0.35 #ffffff, stop:0.45 {ACCENT}, stop:1 {ACCENT}); border-color: {ACCENT}; }}

/* Listas */
QListView, QTreeView, QListWidget, QTreeWidget {{ background: transparent; border: none; }}
QHeaderView::section {{ background: transparent; border: none; color: {TEXT_DIM}; }}

/* Barras de desplazamiento finas */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px 1px; }}
QScrollBar::handle:vertical {{ background: #3a3d4f; border-radius: 3px; min-height: 32px; margin: 0 2px; }}
QScrollBar::handle:vertical:hover {{ background: #555a73; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 1px 2px; }}
QScrollBar::handle:horizontal {{ background: #3a3d4f; border-radius: 3px; min-width: 32px; margin: 2px 0; }}
QScrollBar::handle:horizontal:hover {{ background: #555a73; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; border: none; background: none; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}
QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget#PanelBody {{ background: {SURFACE}; }}

QToolTip {{
    background: #2a2d3b; color: {TEXT}; border: 1px solid {BORDER_STRONG}; border-radius: 6px; padding: 5px 8px;
}}
QMenu {{ background: #21232e; border: 1px solid {BORDER_STRONG}; border-radius: 10px; padding: 6px; }}
QMenu::item {{ padding: 6px 30px 6px 10px; border-radius: 6px; background: transparent; }}
QMenu::item:selected {{ background: {accent_mid}; }}
QMenu::item:disabled {{ color: {TEXT_FAINT}; }}
QMenu::icon {{ padding-left: 8px; }}
QMenu::separator {{ height: 1px; background: {BORDER_STRONG}; margin: 5px 8px; }}
QMenu::indicator {{ width: 16px; height: 16px; margin-left: 6px; }}
QMenu::indicator:checked {{ image: {a["check"]}; }}
QMenu::right-arrow {{ image: {a["right"]}; width: 10px; height: 10px; }}

QStatusBar {{ background: {SURFACE}; border-top: 1px solid {BORDER}; color: {TEXT_DIM}; }}
QStatusBar::item {{ border: none; }}
QStatusBar QLabel {{ color: {TEXT_DIM}; }}
QProgressBar {{ background: {SURFACE_3}; border: none; border-radius: 2px; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 2px; }}

QSplitter::handle {{ background: {BORDER}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QMessageBox {{ background: {BG}; }}
QDialogButtonBox QPushButton {{ min-width: 92px; }}
QGroupBox {{ border: 1px solid {BORDER}; border-radius: 10px; margin-top: 14px; padding: 12px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 4px; color: {TEXT_DIM}; }}
"""


def _palette() -> QPalette:
    pal = QPalette()
    roles = {
        QPalette.Window: BG,
        QPalette.WindowText: TEXT,
        QPalette.Base: SURFACE_2,
        QPalette.AlternateBase: SURFACE,
        QPalette.ToolTipBase: "#2a2d3b",
        QPalette.ToolTipText: TEXT,
        QPalette.Text: TEXT,
        QPalette.Button: SURFACE_2,
        QPalette.ButtonText: TEXT,
        QPalette.BrightText: "#ffffff",
        QPalette.Highlight: ACCENT,
        QPalette.HighlightedText: "#ffffff",
        QPalette.Link: ACCENT_HOVER,
        QPalette.PlaceholderText: TEXT_FAINT,
        QPalette.Light: SURFACE_3,
        QPalette.Midlight: BORDER_STRONG,
        QPalette.Mid: BORDER,
        QPalette.Dark: "#121318",
        QPalette.Shadow: "#000000",
    }
    for role, color in roles.items():
        pal.setColor(role, QColor(color))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        pal.setColor(QPalette.Disabled, role, QColor(TEXT_FAINT))
    return pal


def ui_font() -> QFont:
    families = set(QFontDatabase.families())
    font = QFont()
    preferred = [f for f in ("Segoe UI Variable Text", "Segoe UI") if f in families]
    if preferred:
        font.setFamilies(preferred)
    font.setPointSizeF(10)
    return font


def apply_theme(app: QApplication) -> None:
    """Aplica estilo Fusion, paleta oscura, fuente y hoja de estilos a toda la aplicación."""
    fusion = QStyleFactory.create("Fusion")
    if fusion is not None:
        app.setStyle(fusion)
    try:  # Qt >= 6.8: barras de título oscuras aunque Windows esté en modo claro
        app.styleHints().setColorScheme(Qt.ColorScheme.Dark)
    except (AttributeError, RuntimeError):
        pass
    app.setPalette(_palette())
    app.setFont(ui_font())
    app.setStyleSheet(stylesheet())


def enable_dark_titlebar(widget: QWidget) -> None:
    """Pide a Windows una barra de título oscura (DWMWA_USE_IMMERSIVE_DARK_MODE). Silencioso si falla."""
    if sys.platform != "win32" or QGuiApplication.platformName() != "windows":
        return
    try:
        import ctypes
        from ctypes import wintypes

        hwnd = wintypes.HWND(int(widget.winId()))
        value = ctypes.c_int(1)
        dwm = ctypes.WinDLL("dwmapi")
        for attr in (20, 19):  # 20 en Win10 2004+/Win11, 19 en versiones anteriores
            if dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                break
    except Exception:  # noqa: BLE001 - puramente cosmético
        pass
