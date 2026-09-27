"""Widgets reutilizables de la interfaz.

- ``ToggleSwitch`` / ``SwitchRow``: interruptor animado.
- ``CapturePad``: zona grande que captura combinaciones de teclas y botones del ratón a la vez.
- ``ComboCaptureEdit``: campo compacto para capturar disparadores y atajos.
- ``TokenChips`` / ``KeyPicker``: chips removibles y desplegable con búsqueda de teclas.
- ``pick_screen_position``: selector de posición con overlays por pantalla.
- Delegados y vista de las listas de macros y de pasos (arrastrar y soltar fiable).
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Optional, Sequence

from PySide6.QtCore import (
    Property,
    QItemSelectionModel,
    QEasingCurve,
    QEvent,
    QEventLoop,
    QModelIndex,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QCursor,
    QFont,
    QFontMetricsF,
    QGuiApplication,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPen,
    QScreen,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QComboBox,
    QCompleter,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLayoutItem,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QToolButton,
    QTreeWidget,
    QVBoxLayout,
    QWidget,
)

from .. import keys
from . import theme

log = logging.getLogger(__name__)

# Roles de datos de los ítems de las listas.
ROLE_ID = Qt.UserRole
ROLE_KIND = Qt.UserRole + 1
ROLE_ENABLED = Qt.UserRole + 2
ROLE_DELAY = Qt.UserRole + 3
ROLE_DELAY_DEFAULT = Qt.UserRole + 4
ROLE_COMMENT = Qt.UserRole + 5
ROLE_RUNNING = Qt.UserRole + 6
ROLE_NUMBER = Qt.UserRole + 7
ROLE_TRIGGER = Qt.UserRole + 8
ROLE_ERROR = Qt.UserRole + 9
ROLE_SUBTITLE = Qt.UserRole + 10


# --- Utilidades ---------------------------------------------------------------------------
def section_label(text: str) -> QLabel:
    label = QLabel(text.upper())
    label.setProperty("role", "section")
    return label


def hint_label(text: str, *, wrap: bool = True) -> QLabel:
    label = QLabel(text)
    label.setProperty("role", "hint")
    label.setWordWrap(wrap)
    return label


def separator() -> QFrame:
    line = QFrame()
    line.setObjectName("Separator")
    line.setFrameShape(QFrame.NoFrame)
    return line


def make_button(text: str = "", icon_name: str | None = None, *, kind: str | None = None,
                tooltip: str = "", icon_color: str | None = None, icon_size: int = 16) -> QPushButton:
    btn = QPushButton(text)
    if icon_name:
        btn.setIcon(theme.icon(icon_name, icon_color, icon_size))
        btn.setIconSize(QSize(icon_size, icon_size))
    if kind:
        btn.setProperty("kind", kind)
    if tooltip:
        btn.setToolTip(tooltip)
    btn.setCursor(Qt.PointingHandCursor)
    return btn


def make_tool_button(icon_name: str, tooltip: str = "", *, kind: str | None = None,
                     icon_color: str | None = None, icon_size: int = 16) -> QToolButton:
    btn = QToolButton()
    btn.setIcon(theme.icon(icon_name, icon_color, icon_size))
    btn.setIconSize(QSize(icon_size, icon_size))
    btn.setToolTip(tooltip)
    btn.setAutoRaise(True)
    btn.setCursor(Qt.PointingHandCursor)
    if kind:
        btn.setProperty("kind", kind)
    return btn


def repolish(widget: QWidget) -> None:
    """Vuelve a aplicar el QSS tras cambiar una propiedad dinámica."""
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def format_ms(ms: int) -> str:
    """"150 ms", "1,5 s", "2 min 5 s"… (formato corto en español)."""
    ms = int(ms)
    if ms < 1000:
        return f"{ms} ms"
    if ms < 60_000:
        seconds = ms / 1000
        text = f"{seconds:.2f}".rstrip("0").rstrip(".")
        return text.replace(".", ",") + " s"
    minutes, rest = divmod(ms // 1000, 60)
    return f"{minutes} min {rest} s" if rest else f"{minutes} min"


def safe_format_combo(tokens: Sequence[str]) -> str:
    try:
        return keys.format_combo(list(tokens))
    except Exception:  # noqa: BLE001 - nunca romper el pintado por un token raro
        return " + ".join(tokens) if tokens else "—"


def safe_display_name(token: str) -> str:
    try:
        return keys.display_name(token)
    except Exception:  # noqa: BLE001
        return token


def combo_tokens(combo: str) -> list[str]:
    try:
        return keys.parse_combo(combo)
    except (ValueError, AttributeError):
        return []


# --- Suspensión de disparadores mientras se captura --------------------------------------
_suspend_counts: dict[int, int] = {}


def set_triggers_suspended(triggers: Any, active: bool) -> None:
    """Suspende/reanuda ``triggers`` con contador (varios widgets de captura pueden solaparse)."""
    if triggers is None:
        return
    key = id(triggers)
    count = max(0, _suspend_counts.get(key, 0) + (1 if active else -1))
    _suspend_counts[key] = count
    try:
        if active and count == 1:
            triggers.set_suspended(True)
        elif not active and count == 0:
            triggers.set_suspended(False)
    except Exception:  # noqa: BLE001
        log.exception("No se pudo cambiar la suspensión de los disparadores")


class _CaptureMixin:
    """Gestiona la suspensión de disparadores de un widget de captura de forma idempotente.

    También reconoce AltGr: en las distribuciones con AltGr (como la española) Windows lo
    entrega como un Ctrl izquierdo ficticio seguido de Alt derecho, con la misma marca de
    tiempo. Ese Ctrl no lo ve el hook de disparadores, así que tampoco debe capturarse.
    Las subclases tienen ``_held`` (set) y ``_order`` (list).
    """

    triggers: Any = None
    _capturing: bool = False
    _ctrl_ts: int = 0  # marca de tiempo del último Ctrl izquierdo pulsado

    def _set_capturing(self, active: bool) -> None:
        if active == self._capturing:
            return
        self._capturing = active
        set_triggers_suspended(self.triggers, active)

    def _note_ctrl(self, event: QKeyEvent) -> None:
        self._ctrl_ts = int(event.timestamp() or 0)

    def _drop_altgr_ctrl(self, event: QKeyEvent) -> bool:
        """Con un Alt derecho: True si es AltGr (y quita de la captura su Ctrl ficticio)."""
        ts = int(event.timestamp() or 0)
        order: list[str] = self._order  # type: ignore[attr-defined]
        if not ts or ts != self._ctrl_ts or not order or order[-1] != "ctrl":
            return False
        order.pop()
        self._held.discard("ctrl")  # type: ignore[attr-defined]
        return True


# --- Conversión de eventos Qt a tokens ----------------------------------------------------
_QT_KEY_TOKENS: dict[int, str] = {
    Qt.Key_Return: "enter", Qt.Key_Enter: "num_enter", Qt.Key_Escape: "esc", Qt.Key_Tab: "tab",
    Qt.Key_Backtab: "tab", Qt.Key_Space: "space", Qt.Key_Backspace: "backspace", Qt.Key_Delete: "delete",
    Qt.Key_Insert: "insert", Qt.Key_Home: "home", Qt.Key_End: "end", Qt.Key_PageUp: "pageup",
    Qt.Key_PageDown: "pagedown", Qt.Key_Up: "up", Qt.Key_Down: "down", Qt.Key_Left: "left",
    Qt.Key_Right: "right", Qt.Key_Control: "ctrl", Qt.Key_Shift: "shift", Qt.Key_Alt: "alt",
    Qt.Key_AltGr: "ralt", Qt.Key_Meta: "win", Qt.Key_CapsLock: "capslock", Qt.Key_NumLock: "numlock",
    Qt.Key_ScrollLock: "scrolllock", Qt.Key_Print: "printscreen", Qt.Key_Pause: "pause", Qt.Key_Menu: "apps",
}
_MOUSE_BUTTON_TOKENS: dict[Qt.MouseButton, str] = {
    Qt.LeftButton: "mouse_left",
    Qt.RightButton: "mouse_right",
    Qt.MiddleButton: "mouse_middle",
    Qt.BackButton: "mouse_x1",
    Qt.ForwardButton: "mouse_x2",
}
_VK_RETURN = 0x0D
_VK_SHIFT = 0x10
_VK_CONTROL = 0x11
_VK_MENU = 0x12
_SCAN_RSHIFT = 0x36
_IGNORED_VKS = {0x00, 0xE5, 0xE7, 0xFF}  # sin VK, IME (PROCESSKEY), PACKET


def _scan_is_extended(scan: int) -> bool:
    """¿Scancode nativo de una tecla extendida? Qt 6 lo da como 0xE0xx (antes, con el bit 0x100)."""
    return (scan & 0xFF00) == 0xE000 or bool(scan & 0x100)


def key_event_token(event: QKeyEvent, *, sided: bool = False) -> Optional[str]:
    """Token de ``keys`` para un evento de teclado (usa el VK nativo de Windows si lo hay).

    Alt derecho da siempre "ralt" (en las distribuciones con AltGr es otra tecla). Con
    ``sided`` también Ctrl y Mayús derechos dan su token propio ("rctrl", "rshift"): el pad
    de pasos debe pulsar la misma tecla física; en los disparadores basta la forma genérica.
    """
    vk = int(event.nativeVirtualKey() or 0)
    if vk and vk not in _IGNORED_VKS:
        scan = int(event.nativeScanCode() or 0)
        extended = _scan_is_extended(scan)
        if vk == _VK_RETURN:
            is_num = extended or event.key() == Qt.Key_Enter or bool(event.modifiers() & Qt.KeypadModifier)
            return "num_enter" if is_num else "enter"
        if vk == _VK_MENU and extended:
            return "ralt"  # Alt derecho / AltGr
        if sided and vk == _VK_CONTROL and extended:
            return "rctrl"
        if sided and vk == _VK_SHIFT and (scan & 0xFF) == _SCAN_RSHIFT:
            return "rshift"
        try:
            # El resto (y los modificadores izquierdos) se captura en su forma genérica.
            return keys.vk_to_token(vk, False)
        except Exception:  # noqa: BLE001
            log.debug("VK sin token: %#x", vk)
    key = event.key()
    if key in _QT_KEY_TOKENS:
        return _QT_KEY_TOKENS[key]
    if Qt.Key_A <= key <= Qt.Key_Z:
        return chr(ord("a") + key - Qt.Key_A)
    if Qt.Key_0 <= key <= Qt.Key_9:
        return chr(ord("0") + key - Qt.Key_0)
    if Qt.Key_F1 <= key <= Qt.Key_F24:
        return f"f{key - Qt.Key_F1 + 1}"
    return None


def mouse_button_token(button: Qt.MouseButton) -> Optional[str]:
    return _MOUSE_BUTTON_TOKENS.get(button)


# --- Pintado de "teclas" (keycaps) --------------------------------------------------------
def _is_mouse(token: str) -> bool:
    try:
        return keys.is_mouse(token)
    except Exception:  # noqa: BLE001
        return token.startswith("mouse_")


def _is_modifier(token: str) -> bool:
    try:
        return keys.is_modifier(token)
    except Exception:  # noqa: BLE001
        return False


def keycaps_width(tokens: Sequence[str], font: QFont, cap_height: float) -> float:
    fm = QFontMetricsF(font)
    pad = cap_height * 0.42
    plus = fm.horizontalAdvance("+") + cap_height * 0.5
    total = 0.0
    for i, tok in enumerate(tokens):
        extra = cap_height * 0.62 if _is_mouse(tok) else 0.0
        total += max(cap_height, fm.horizontalAdvance(safe_display_name(tok)) + 2 * pad + extra)
        if i:
            total += plus
    return total


def draw_keycaps(painter: QPainter, rect: QRectF, tokens: Sequence[str], *, font: QFont,
                 cap_height: float, align: Qt.AlignmentFlag = Qt.AlignCenter,
                 highlighted: Iterable[str] = (), dim: bool = False, error: bool = False) -> None:
    """Pinta ``tokens`` como teclas unidas por "+", centradas o alineadas a la izquierda/derecha."""
    if not tokens:
        return
    highlighted = set(highlighted)
    fm = QFontMetricsF(font)
    pad = cap_height * 0.42
    width = keycaps_width(tokens, font, cap_height)
    scale = 1.0
    if width > rect.width() and width > 0:
        scale = max(0.6, rect.width() / width)
    if scale < 1.0:
        font = QFont(font)
        font.setPixelSize(max(8, int(font.pixelSize() * scale)) if font.pixelSize() > 0 else 8)
        cap_height *= scale
        fm = QFontMetricsF(font)
        pad = cap_height * 0.42
        width = keycaps_width(tokens, font, cap_height)
    if align & Qt.AlignLeft:
        x = rect.left()
    elif align & Qt.AlignRight:
        x = rect.right() - width
    else:
        x = rect.center().x() - width / 2
    y = rect.center().y() - cap_height / 2
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setFont(font)
    for i, tok in enumerate(tokens):
        if i:
            painter.setPen(QColor(theme.TEXT_FAINT))
            plus_w = fm.horizontalAdvance("+") + cap_height * 0.5
            painter.drawText(QRectF(x, y, plus_w, cap_height), Qt.AlignCenter, "+")
            x += plus_w
        mouse = _is_mouse(tok)
        name = safe_display_name(tok)
        extra = cap_height * 0.62 if mouse else 0.0
        w = max(cap_height, fm.horizontalAdvance(name) + 2 * pad + extra)
        cap = QRectF(x, y, w, cap_height)
        if error:
            fill, border, text_color = theme.with_alpha(theme.RED, 0.14), QColor(theme.RED), QColor("#ffc2c8")
        elif tok in highlighted:
            fill, border, text_color = theme.with_alpha(theme.ACCENT, 0.35), QColor(theme.ACCENT_HOVER), QColor("#ffffff")
        elif mouse:
            fill, border, text_color = theme.with_alpha("#5cb2f7", 0.13), theme.with_alpha("#5cb2f7", 0.55), QColor(theme.TEXT)
        else:
            fill, border, text_color = QColor("#2c2f3e"), QColor("#474c63"), QColor(theme.TEXT)
        if dim:
            text_color = QColor(theme.TEXT_DIM)
            border.setAlphaF(border.alphaF() * 0.6)
        radius = cap_height * 0.24
        # "Sombra" inferior para dar aspecto de tecla.
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 70))
        painter.drawRoundedRect(cap.translated(0, max(1.0, cap_height * 0.06)), radius, radius)
        painter.setBrush(fill)
        pen = QPen(border)
        pen.setWidthF(1.0)
        painter.setPen(pen)
        painter.drawRoundedRect(cap.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
        text_rect = cap
        if mouse:
            icon_rect = QRectF(cap.left() + pad * 0.7, cap.top(), extra, cap_height)
            theme.draw_glyph(painter, icon_rect, "mouse", "#8cc8fa" if not dim else theme.TEXT_DIM,
                             int(cap_height * 0.5))
            text_rect = cap.adjusted(extra + pad * 0.3, 0, 0, 0)
        painter.setPen(text_color)
        painter.drawText(text_rect, Qt.AlignCenter, name)
        x += w
    painter.restore()


# --- Interruptor animado ------------------------------------------------------------------
class ToggleSwitch(QAbstractButton):
    """Interruptor on/off con animación del mando."""

    def __init__(self, parent: QWidget | None = None, *, on_color: str = theme.ACCENT,
                 width: int = 40, height: int = 22) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        self._on_color = QColor(on_color)
        self._w, self._h = width, height
        self._offset = 0.0
        self._anim = QPropertyAnimation(self, b"offset", self)
        self._anim.setDuration(150)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self.toggled.connect(self._on_toggled)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    def _get_offset(self) -> float:
        return self._offset

    def _set_offset(self, value: float) -> None:
        self._offset = value
        self.update()

    offset = Property(float, _get_offset, _set_offset)

    def set_on_color(self, color: str) -> None:
        self._on_color = QColor(color)
        self.update()

    def _on_toggled(self, checked: bool) -> None:
        end = 1.0 if checked else 0.0
        if not self.isVisible():
            self._anim.stop()
            self._set_offset(end)
            return
        self._anim.stop()
        self._anim.setStartValue(self._offset)
        self._anim.setEndValue(end)
        self._anim.start()

    def setChecked(self, checked: bool) -> None:  # noqa: N802 - API de Qt
        super().setChecked(checked)
        if not self.isVisible():
            self._set_offset(1.0 if checked else 0.0)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(self._w, self._h)

    def hitButton(self, pos: QPoint) -> bool:  # noqa: N802
        return self.rect().contains(pos)

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if not self.isEnabled():
            p.setOpacity(0.4)
        r = QRectF(0.5, 0.5, self._w - 1, self._h - 1)
        off = QColor("#3a3e51")
        t = self._offset
        on = self._on_color
        track = QColor(
            round(off.red() + (on.red() - off.red()) * t),
            round(off.green() + (on.green() - off.green()) * t),
            round(off.blue() + (on.blue() - off.blue()) * t),
        )
        p.setPen(Qt.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        if self.hasFocus():
            pen = QPen(theme.with_alpha(theme.ACCENT_HOVER, 0.8))
            pen.setWidthF(1.5)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
            p.setPen(Qt.NoPen)
        margin = 3.0
        d = self._h - 2 * margin
        x = margin + (self._w - 2 * margin - d) * t
        p.setBrush(QColor(0, 0, 0, 50))
        p.drawEllipse(QRectF(x, margin + 1, d, d))
        p.setBrush(QColor("#ffffff") if t > 0.5 else QColor("#d4d5e0"))
        p.drawEllipse(QRectF(x, margin, d, d))
        p.end()


class SwitchRow(QWidget):
    """Fila con título, subtítulo opcional e interruptor; hacer clic en la fila lo alterna."""

    toggled = Signal(bool)

    def __init__(self, title: str, subtitle: str = "", checked: bool = False,
                 parent: QWidget | None = None, *, on_color: str = theme.ACCENT) -> None:
        super().__init__(parent)
        self.switch = ToggleSwitch(on_color=on_color)
        self.switch.setChecked(checked)
        self.switch.toggled.connect(self.toggled)
        self.title_label = QLabel(title)
        self.title_label.setWordWrap(True)
        text_box = QVBoxLayout()
        text_box.setSpacing(1)
        text_box.setContentsMargins(0, 0, 0, 0)
        text_box.addWidget(self.title_label)
        self.subtitle_label: QLabel | None = None
        if subtitle:
            self.subtitle_label = hint_label(subtitle)
            text_box.addWidget(self.subtitle_label)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 2, 0, 2)
        row.setSpacing(12)
        row.addLayout(text_box, 1)
        row.addWidget(self.switch, 0, Qt.AlignVCenter)
        self.setCursor(Qt.PointingHandCursor)

    def isChecked(self) -> bool:  # noqa: N802
        return self.switch.isChecked()

    def setChecked(self, checked: bool) -> None:  # noqa: N802
        self.switch.setChecked(checked)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.switch.toggle()
        super().mouseReleaseEvent(event)


# --- Campos numéricos que no roban la rueda al hacer scroll por el panel ------------------
class SpinBox(QSpinBox):
    def __init__(self, minimum: int = 0, maximum: int = 600_000, *, suffix: str = " ms",
                 step: int = 10, value: int = 0, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setRange(minimum, maximum)
        self.setSingleStep(step)
        self.setSuffix(suffix)
        self.setValue(value)
        self.setAccelerated(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMinimumWidth(84)

    def setValue(self, value: int) -> None:  # noqa: N802 - API de Qt
        """Como QSpinBox.setValue pero recortando antes al rango: un valor fuera del rango de
        un int de C (p. ej. de un JSON editado a mano) no lanza OverflowError."""
        super().setValue(max(self.minimum(), min(self.maximum(), int(value))))

    def wheelEvent(self, event) -> None:  # noqa: N802
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class ComboBox(QComboBox):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.StrongFocus)

    def wheelEvent(self, event) -> None:  # noqa: N802
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


# --- Pad de captura de combinaciones ------------------------------------------------------
class CapturePad(_CaptureMixin, QWidget):
    """Área grande que captura una combinación de teclas y botones del ratón pulsados a la vez.

    La combinación se fija (señal ``captured``) cuando se ha soltado todo. Captura también Esc y
    Tab (no cierran el diálogo ni mueven el foco mientras el pad tiene el foco).
    """

    captured = Signal(list)
    liveChanged = Signal(list)

    def __init__(self, parent: QWidget | None = None, *, triggers: Any = None) -> None:
        super().__init__(parent)
        self.triggers = triggers
        self.setFocusPolicy(Qt.StrongFocus)
        self.setContextMenuPolicy(Qt.NoContextMenu)
        self.setAttribute(Qt.WA_Hover)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(136)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._held: set[str] = set()
        self._order: list[str] = []
        self._last: list[str] = []
        self._swallow_click = False
        self._flash = QTimer(self)
        self._flash.setSingleShot(True)
        self._flash.setInterval(1600)
        self._flash.timeout.connect(self.update)

    # Estado --------------------------------------------------------------------------
    def current_tokens(self) -> list[str]:
        return list(self._order)

    def last_captured(self) -> list[str]:
        return list(self._last)

    def reset(self) -> None:
        self._held.clear()
        self._order.clear()
        self.update()

    def _press(self, token: str) -> None:
        if not self._held:
            self._order = []
        self._held.add(token)
        if token not in self._order:
            self._order.append(token)
        self.liveChanged.emit(list(self._order))
        self.update()

    def _release(self, token: str) -> None:
        if token in self._held:
            self._held.discard(token)
        elif token == "printscreen" and token not in self._order:
            # ImprPant sólo genera la liberación en Windows.
            if not self._held:
                self._order = []
            self._order.append(token)
        else:
            return
        if not self._held and self._order:
            self._last = list(self._order)
            self._order = []
            self._flash.start()
            self.captured.emit(list(self._last))
        self.update()

    # Eventos -------------------------------------------------------------------------
    def event(self, event: QEvent) -> bool:
        etype = event.type()
        if etype == QEvent.ShortcutOverride:
            event.accept()  # ningún atajo de la ventana debe robar la tecla
            return True
        if etype in (QEvent.KeyPress, QEvent.KeyRelease) and event.key() in (Qt.Key_Tab, Qt.Key_Backtab):
            if etype == QEvent.KeyPress:
                self.keyPressEvent(event)
            else:
                self.keyReleaseEvent(event)
            return True
        return super().event(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        event.accept()
        if event.isAutoRepeat():
            return
        # Con lado: Ctrl/Mayús/Alt derechos se reproducen como la tecla física que se pulsó.
        token = key_event_token(event, sided=True)
        if token == "ctrl":
            self._note_ctrl(event)
        elif token == "ralt":
            self._drop_altgr_ctrl(event)
        if token:
            self._press(token)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        event.accept()
        if event.isAutoRepeat():
            return
        token = key_event_token(event, sided=True)
        if token:
            self._release(token)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        event.accept()
        token = mouse_button_token(event.button())
        if token is None:
            return
        if self._swallow_click and event.button() == Qt.LeftButton and not self._held:
            self._swallow_click = False  # el clic que da el foco no cuenta
            self.update()
            return
        self._swallow_click = False
        self._press(token)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self.mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        event.accept()
        token = mouse_button_token(event.button())
        if token:
            self._release(token)

    def focusInEvent(self, event) -> None:  # noqa: N802
        self._swallow_click = event.reason() == Qt.MouseFocusReason
        self._set_capturing(True)
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event) -> None:  # noqa: N802
        self._set_capturing(False)
        # Si el foco se pierde a mitad (Alt+Tab, tecla Windows…), se descarta lo incompleto.
        self._held.clear()
        self._order.clear()
        super().focusOutEvent(event)
        self.update()

    def hideEvent(self, event) -> None:  # noqa: N802
        self._set_capturing(False)
        super().hideEvent(event)

    # Pintado -------------------------------------------------------------------------
    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        focused = self.hasFocus()
        hovered = self.underMouse()
        if focused:
            p.setBrush(theme.with_alpha(theme.ACCENT, 0.10))
            pen = QPen(QColor(theme.ACCENT))
            pen.setWidthF(1.6)
        else:
            p.setBrush(QColor(theme.SURFACE_2) if hovered else QColor("#20222c"))
            pen = QPen(QColor("#565b75") if hovered else QColor("#454a60"))
            pen.setWidthF(1.3)
            pen.setStyle(Qt.DashLine)
            pen.setDashPattern([4, 3])
        p.setPen(pen)
        p.drawRoundedRect(r, 12, 12)

        title_font = QFont(self.font())
        title_font.setPointSizeF(10.5)
        title_font.setWeight(QFont.DemiBold)
        sub_font = QFont(self.font())
        sub_font.setPointSizeF(9)
        cap_font = QFont(self.font())
        cap_font.setPixelSize(17)
        cap_font.setWeight(QFont.DemiBold)

        showing = self._order or (self._last if self._flash.isActive() else [])
        inner = r.adjusted(16, 0, -16, 0)
        title_h = QFontMetricsF(title_font).height()
        sub_h = QFontMetricsF(sub_font).height()
        if showing:
            cap_h = 40.0
            y = r.center().y() - (cap_h + 12 + title_h) / 2
            draw_keycaps(p, QRectF(inner.left(), y, inner.width(), cap_h), showing, font=cap_font,
                         cap_height=cap_h, highlighted=self._held if self._order else ())
            if self._order:
                title, color = "Suelta todo para fijar la combinación", theme.TEXT_DIM
            else:
                title, color = "Combinación capturada", theme.GREEN
            p.setFont(title_font)
            p.setPen(QColor(color))
            p.drawText(QRectF(inner.left(), y + cap_h + 12, inner.width(), title_h), Qt.AlignCenter, title)
        else:
            icon_h = 36.0
            y = r.center().y() - (icon_h + 10 + title_h + 2 + sub_h) / 2
            cx = inner.center().x()
            glyph_color = theme.ACCENT_HOVER if focused else theme.TEXT_DIM
            theme.draw_glyph(p, QRectF(cx - 46, y, 40, icon_h), "keyboard", glyph_color, 30)
            p.setPen(QColor(theme.TEXT_FAINT))
            p.setFont(title_font)
            p.drawText(QRectF(cx - 6, y, 12, icon_h), Qt.AlignCenter, "+")
            theme.draw_glyph(p, QRectF(cx + 6, y, 40, icon_h), "mouse", glyph_color, 28)
            if focused:
                title = "Pulsa ahora la combinación"
                sub = "Teclas y/o botones del ratón a la vez · se fija al soltarlo todo"
            else:
                title = "Pulsa aquí la combinación: teclas y/o botones del ratón a la vez"
                sub = "Haz clic en esta zona para empezar a capturar"
            ty = y + icon_h + 10
            p.setFont(title_font)
            p.setPen(QColor(theme.TEXT if focused else theme.TEXT_DIM))
            p.drawText(QRectF(inner.left(), ty, inner.width(), title_h), Qt.AlignCenter,
                       QFontMetricsF(title_font).elidedText(title, Qt.ElideRight, inner.width()))
            p.setFont(sub_font)
            p.setPen(QColor(theme.TEXT_FAINT))
            p.drawText(QRectF(inner.left(), ty + title_h + 2, inner.width(), sub_h), Qt.AlignCenter,
                       QFontMetricsF(sub_font).elidedText(sub, Qt.ElideRight, inner.width()))
        p.end()


# --- Captura compacta de disparadores / atajos ---------------------------------------------
class ComboCaptureEdit(_CaptureMixin, QFrame):
    """Campo que captura una tecla, combinación o botón lateral/central del ratón.

    Haz clic y pulsa; se fija al soltar. Esc cancela. Emite ``comboChanged`` con la cadena
    canónica ("ctrl+1", "mouse_x1"…; "" al limpiar).
    """

    comboChanged = Signal(str)

    def __init__(self, parent: QWidget | None = None, *, triggers: Any = None,
                 placeholder: str = "Sin asignar",
                 capture_text: str = "Pulsa una tecla, combinación o botón del ratón…",
                 mouse_alone: Sequence[str] = ("mouse_middle", "mouse_x1", "mouse_x2")) -> None:
        super().__init__(parent)
        self.triggers = triggers
        self._combo = ""
        self._error = ""
        self._base_tooltip = ""
        self._placeholder = placeholder
        self._capture_text = capture_text
        self._mouse_alone = set(mouse_alone)
        self._held: set[str] = set()
        self._order: list[str] = []
        self._swallow_click = False
        self.setFocusPolicy(Qt.StrongFocus)
        self.setContextMenuPolicy(Qt.NoContextMenu)
        self.setAttribute(Qt.WA_Hover)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(40)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._clear = make_tool_button("close", "Quitar", icon_size=12, kind="small")
        self._clear.setFocusPolicy(Qt.NoFocus)
        self._clear.clicked.connect(lambda: self._commit_combo(""))
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 6, 0)
        lay.addStretch(1)
        lay.addWidget(self._clear, 0, Qt.AlignVCenter)
        self._update_clear()

    def combo(self) -> str:
        return self._combo

    def set_combo(self, combo: str) -> None:
        """Establece la combinación sin emitir ``comboChanged``."""
        self._combo = combo or ""
        self._update_clear()
        self.update()

    def setToolTip(self, text: str) -> None:  # noqa: N802 - API de Qt
        """Ayuda del campo; mientras haya un error se muestra el error en su lugar."""
        self._base_tooltip = text or ""
        super().setToolTip(self._error or self._base_tooltip)

    def set_error(self, message: str | None) -> None:
        self._error = message or ""
        super().setToolTip(self._error or self._base_tooltip)
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(220, 40)

    def _update_clear(self) -> None:
        self._clear.setVisible(bool(self._combo))

    def _commit_combo(self, combo: str) -> None:
        changed = combo != self._combo
        self._combo = combo
        self._update_clear()
        self.update()
        if changed:
            self.comboChanged.emit(combo)

    def _finish(self) -> None:
        tokens = list(self._order)
        self._order = []
        if not tokens:
            return
        if len(tokens) == 1 and _is_mouse(tokens[0]) and tokens[0] not in self._mouse_alone:
            return  # clic izquierdo/derecho solos no sirven como disparador
        try:
            combo = keys.combo_to_string(keys.sort_combo(tokens))
        except Exception:  # noqa: BLE001
            combo = "+".join(tokens)
        if combo != self._combo and all(_is_modifier(t) for t in tokens):
            # Casi siempre es un descuido (se soltó Mayús antes de pulsar la otra tecla) y, como
            # disparador bloqueante, dejaría la tecla inutilizada en todas las aplicaciones.
            if not self.confirm_modifier_only(tokens):
                return  # se sigue capturando: el usuario puede pulsar la combinación buena
        self._commit_combo(combo)
        self.clearFocus()

    def confirm_modifier_only(self, tokens: Sequence[str]) -> bool:
        """Pide confirmación para usar sólo modificadores (p. ej. «Mayús») como combinación."""
        shown = safe_format_combo(tokens)
        box = QMessageBox(self.window())
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("¿Sólo una tecla modificadora?")
        box.setText(f"¿Usar «{shown}» sola?")
        box.setInformativeText(
            f"Cada pulsación de {shown} lo activará en cualquier aplicación y, si se bloquea la tecla "
            f"original, {shown} dejará de funcionar en las demás aplicaciones mientras esté asignado.\n\n"
            "Si querías una combinación (p. ej. Mayús + 1), cancela y pulsa las dos teclas a la vez.")
        use = box.addButton(f"Usar «{shown}»", QMessageBox.AcceptRole)
        cancel = box.addButton("Cancelar", QMessageBox.RejectRole)
        box.setDefaultButton(cancel)
        box.exec()
        return box.clickedButton() is use

    def _press(self, token: str) -> None:
        if not self._held:
            self._order = []
        self._held.add(token)
        if token not in self._order:
            self._order.append(token)
        self.update()

    def _release(self, token: str) -> None:
        if token in self._held:
            self._held.discard(token)
        elif token == "printscreen" and not self._held:
            self._order = [token]
        else:
            return
        if not self._held:
            self._finish()
        self.update()

    def event(self, event: QEvent) -> bool:
        etype = event.type()
        if etype == QEvent.ShortcutOverride and self._capturing:
            event.accept()
            return True
        if etype in (QEvent.KeyPress, QEvent.KeyRelease) and event.key() in (Qt.Key_Tab, Qt.Key_Backtab):
            if etype == QEvent.KeyPress:
                self.keyPressEvent(event)
            else:
                self.keyReleaseEvent(event)
            return True
        return super().event(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        event.accept()
        if event.isAutoRepeat():
            return
        if event.key() == Qt.Key_Escape and not self._held:
            self._order = []
            self.clearFocus()
            return
        token = key_event_token(event)
        if token == "ctrl":
            self._note_ctrl(event)
        elif token == "ralt" and not self._drop_altgr_ctrl(event):
            token = "alt"  # Alt derecho sin AltGr (p. ej. teclado inglés): vale cualquier Alt
        if token:
            self._press(token)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        event.accept()
        if event.isAutoRepeat():
            return
        token = key_event_token(event)
        if token == "ralt" and "ralt" not in self._held:
            token = "alt"
        if token:
            self._release(token)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        event.accept()
        token = mouse_button_token(event.button())
        if token is None:
            return
        if event.button() == Qt.LeftButton and (self._swallow_click or not self._held):
            self._swallow_click = False
            if not self._capturing:
                self.setFocus(Qt.MouseFocusReason)
            return
        self._swallow_click = False
        if not self._capturing:
            return
        if event.button() == Qt.RightButton and not self._held:
            return
        self._press(token)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        event.accept()
        token = mouse_button_token(event.button())
        if token:
            self._release(token)

    def focusInEvent(self, event) -> None:  # noqa: N802
        self._swallow_click = event.reason() == Qt.MouseFocusReason
        self._set_capturing(True)
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event) -> None:  # noqa: N802
        self._set_capturing(False)
        self._held.clear()
        self._order.clear()
        super().focusOutEvent(event)
        self.update()

    def hideEvent(self, event) -> None:  # noqa: N802
        self._set_capturing(False)
        super().hideEvent(event)

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        focused = self.hasFocus()
        if focused:
            p.setBrush(theme.with_alpha(theme.ACCENT, 0.12))
            border = QColor(theme.ACCENT)
        else:
            p.setBrush(QColor(theme.SURFACE_2))
            border = QColor(theme.RED) if self._error else QColor("#4a4f66" if self.underMouse() else theme.BORDER_STRONG)
        pen = QPen(border)
        pen.setWidthF(1.2 if focused else 1.0)
        p.setPen(pen)
        p.drawRoundedRect(r, 8, 8)
        content = r.adjusted(8, 4, -(34 if self._combo else 10), -4)
        cap_font = QFont(self.font())
        cap_font.setPixelSize(13)
        cap_font.setWeight(QFont.DemiBold)
        if focused and self._order:
            draw_keycaps(p, content, self._order, font=cap_font, cap_height=26,
                         align=Qt.AlignLeft, highlighted=self._held)
        elif focused:
            p.setPen(QColor(theme.ACCENT_HOVER))
            f = QFont(self.font())
            f.setPointSizeF(9)
            p.setFont(f)
            p.drawText(content, Qt.AlignVCenter | Qt.AlignLeft,
                       QFontMetricsF(f).elidedText(self._capture_text, Qt.ElideRight, content.width()))
        elif self._combo:
            tokens = combo_tokens(self._combo)
            if tokens:
                draw_keycaps(p, content, tokens, font=cap_font, cap_height=26, align=Qt.AlignLeft,
                             error=bool(self._error))
            else:
                p.setPen(QColor(theme.RED))
                p.drawText(content, Qt.AlignVCenter | Qt.AlignLeft, self._combo)
        else:
            p.setPen(QColor(theme.TEXT_FAINT))
            p.drawText(content, Qt.AlignVCenter | Qt.AlignLeft, self._placeholder)
        p.end()


# --- Chips de entradas y selector de teclas -----------------------------------------------
class FlowLayout(QLayout):
    """Layout que coloca los widgets en filas y salta de línea cuando no caben."""

    def __init__(self, parent: QWidget | None = None, spacing: int = 6) -> None:
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self._spacing = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item: QLayoutItem) -> None:  # noqa: N802
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:  # noqa: N802
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int) -> QLayoutItem | None:  # noqa: N802
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self) -> Qt.Orientation:  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._layout(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._layout(rect, apply=True)

    def sizeHint(self) -> QSize:  # noqa: N802
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _layout(self, rect: QRect, apply: bool) -> int:
        m = self.contentsMargins()
        area = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, line_h = area.x(), area.y(), 0
        row: list[tuple[QLayoutItem, QSize, int]] = []

        def place_row() -> None:
            # Centrados verticalmente en su fila (p. ej. el "+" entre chips de distinta altura).
            if apply:
                for item, hint, left in row:
                    item.setGeometry(QRect(QPoint(left, y + (line_h - hint.height()) // 2), hint))

        for item in self._items:
            hint = item.sizeHint()
            if x + hint.width() > area.right() + 1 and line_h > 0:
                place_row()
                row = []
                x = area.x()
                y += line_h + self._spacing
                line_h = 0
            row.append((item, hint, x))
            x += hint.width() + self._spacing
            line_h = max(line_h, hint.height())
        place_row()
        return y + line_h - rect.y() + m.bottom()


class TokenChip(QFrame):
    removeRequested = Signal(str)

    def __init__(self, token: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.token = token
        self.setObjectName("Chip")
        self.setProperty("mouse", "true" if _is_mouse(token) else "false")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 3, 4, 3)
        lay.setSpacing(4)
        if _is_mouse(token):
            icon = QLabel()
            icon.setPixmap(theme.icon("mouse", "#8cc8fa", 14).pixmap(14, 14))
            lay.addWidget(icon)
        label = QLabel(safe_display_name(token))
        label.setObjectName("ChipText")
        lay.addWidget(label)
        close = make_tool_button("close", "Quitar", icon_size=10, kind="small")
        close.setFocusPolicy(Qt.NoFocus)
        close.clicked.connect(lambda: self.removeRequested.emit(self.token))
        lay.addWidget(close)


class TokenChips(QWidget):
    """Lista editable de entradas mostrada como chips removibles."""

    changed = Signal(list)

    def __init__(self, parent: QWidget | None = None, *, empty_text: str = "") -> None:
        super().__init__(parent)
        self._tokens: list[str] = []
        self._flow = FlowLayout(self, spacing=6)
        self._empty = hint_label(empty_text or "Todavía no hay entradas.", wrap=False)
        self._rebuild()

    def tokens(self) -> list[str]:
        return list(self._tokens)

    def set_tokens(self, tokens: Iterable[str], *, emit: bool = True) -> None:
        seen: list[str] = []
        for tok in tokens:
            if tok and tok not in seen:
                seen.append(tok)
        if seen == self._tokens:
            return
        self._tokens = seen
        self._rebuild()
        if emit:
            self.changed.emit(list(self._tokens))

    def add_token(self, token: str) -> None:
        if token and token not in self._tokens:
            self.set_tokens(self._tokens + [token])

    def remove_token(self, token: str) -> None:
        self.set_tokens([t for t in self._tokens if t != token])

    def _rebuild(self) -> None:
        while self._flow.count():
            item = self._flow.takeAt(0)
            w = item.widget() if item else None
            if w is not None and w is not self._empty:
                w.deleteLater()
        self._empty.setParent(None)
        if not self._tokens:
            self._empty.setParent(self)
            self._flow.addWidget(self._empty)
            self._empty.show()
        for i, tok in enumerate(self._tokens):
            if i:
                plus = QLabel("+")
                plus.setProperty("role", "hint")
                self._flow.addWidget(plus)
            chip = TokenChip(tok, self)
            chip.removeRequested.connect(self.remove_token)
            self._flow.addWidget(chip)
        self._flow.invalidate()
        self.updateGeometry()


class KeyPicker(QComboBox):
    """Desplegable con búsqueda para añadir cualquier tecla por su nombre."""

    tokenChosen = Signal(str)

    def __init__(self, parent: QWidget | None = None, placeholder: str = "Buscar y añadir tecla…") -> None:
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.NoInsert)
        self.setMaxVisibleItems(14)
        self.setFocusPolicy(Qt.StrongFocus)
        try:
            choices = keys.key_choices()
        except Exception:  # noqa: BLE001
            choices = []
        for token, display in choices:
            label = display if display.lower() == token else f"{display}  ({token})"
            self.addItem(label, token)
        completer = QCompleter(self.model(), self)
        completer.setCompletionColumn(0)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        completer.setCompletionMode(QCompleter.PopupCompletion)
        self.setCompleter(completer)
        completer.activated[str].connect(self._choose_text)
        self.activated.connect(self._choose_index)
        self.lineEdit().setPlaceholderText(placeholder)
        self._reset()

    def _reset(self) -> None:
        self.setCurrentIndex(-1)
        self.lineEdit().clear()

    def _choose_index(self, index: int) -> None:
        token = self.itemData(index)
        if token:
            self.tokenChosen.emit(str(token))
        QTimer.singleShot(0, self._reset)

    def _choose_text(self, text: str) -> None:
        index = self.findText(text, Qt.MatchExactly)
        if index >= 0:
            self._choose_index(index)

    def resolve(self, text: str) -> Optional[str]:
        """Token que corresponde a un texto escrito (nombre, token o alias)."""
        text = text.strip()
        if not text:
            return None
        try:
            return keys.normalize(text)
        except Exception:  # noqa: BLE001
            pass
        low = text.lower()
        for i in range(self.count()):
            if low == self.itemText(i).lower() or low == str(self.itemData(i)):
                return str(self.itemData(i))
        for i in range(self.count()):
            if low in self.itemText(i).lower():
                return str(self.itemData(i))
        return None

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            token = self.resolve(self.currentText())
            if token:
                self.tokenChosen.emit(token)
            self._reset()
            event.accept()  # que Intro no acepte el diálogo
            return
        super().keyPressEvent(event)


# --- Selector de posición en pantalla ----------------------------------------------------
def physical_cursor_pos() -> tuple[int, int]:
    """Posición del cursor en píxeles físicos (la que usan los pasos)."""
    try:
        from .. import winput

        x, y = winput.get_cursor_pos()
        return int(x), int(y)
    except Exception:  # noqa: BLE001 - fuera de Windows / módulo no disponible
        pos = QCursor.pos()
        screen = QGuiApplication.screenAt(pos)
        dpr = screen.devicePixelRatio() if screen else 1.0
        return round(pos.x() * dpr), round(pos.y() * dpr)


class _ScreenOverlay(QWidget):
    picked = Signal(int, int)
    cancelled = Signal()

    def __init__(self, screen: QScreen, parent: QWidget | None, primary: bool) -> None:
        super().__init__(parent, Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self._primary = primary
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setScreen(screen)
        self.setGeometry(screen.geometry())
        self.setCursor(Qt.CrossCursor)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(12, 12, 20, 96))
        local = self.mapFromGlobal(QCursor.pos())
        font = QFont(self.font())
        font.setPointSizeF(10.5)
        font.setWeight(QFont.DemiBold)
        p.setFont(font)
        fm = QFontMetricsF(font)
        if self._primary:
            text = "Haz clic para elegir la posición  ·  Esc o clic derecho para cancelar"
            w = fm.horizontalAdvance(text) + 36
            banner = QRectF((self.width() - w) / 2, 28, w, 40)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(30, 32, 41, 235))
            p.drawRoundedRect(banner, 10, 10)
            p.setPen(QColor(theme.TEXT))
            p.drawText(banner, Qt.AlignCenter, text)
        if self.rect().contains(local):
            pen = QPen(theme.with_alpha(theme.ACCENT_HOVER, 0.9))
            pen.setWidthF(1.0)
            p.setPen(pen)
            p.drawLine(0, local.y(), self.width(), local.y())
            p.drawLine(local.x(), 0, local.x(), self.height())
            x, y = physical_cursor_pos()
            label = f"X {x}   Y {y}"
            w = fm.horizontalAdvance(label) + 20
            box = QRectF(local.x() + 16, local.y() + 16, w, 28)
            if box.right() > self.width() - 4:
                box.moveRight(local.x() - 16)
            if box.bottom() > self.height() - 4:
                box.moveBottom(local.y() - 16)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(theme.ACCENT))
            p.drawRoundedRect(box, 7, 7)
            p.setPen(QColor("#ffffff"))
            p.drawText(box, Qt.AlignCenter, label)
        p.end()

    def mouseMoveEvent(self, _event) -> None:  # noqa: N802
        self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            x, y = physical_cursor_pos()
            self.picked.emit(x, y)
        else:
            self.cancelled.emit()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() == Qt.Key_Escape:
            self.cancelled.emit()


def pick_screen_position(parent: QWidget | None = None) -> Optional[tuple[int, int]]:
    """Muestra un overlay en cada pantalla y devuelve la posición elegida (píxeles físicos) o None."""
    owner = parent.window() if parent is not None else None
    faded: list[tuple[QWidget, float]] = []
    w = owner
    while w is not None:
        if w.isVisible():
            faded.append((w, w.windowOpacity()))
            w.setWindowOpacity(0.0)
        w = w.parentWidget().window() if w.parentWidget() is not None else None
    result: dict[str, tuple[int, int]] = {}
    loop = QEventLoop()
    primary = QGuiApplication.primaryScreen()
    overlays: list[_ScreenOverlay] = []

    def on_picked(x: int, y: int) -> None:
        result["pos"] = (x, y)
        loop.quit()

    for screen in QGuiApplication.screens():
        overlay = _ScreenOverlay(screen, owner, screen is primary)
        overlay.picked.connect(on_picked)
        overlay.cancelled.connect(loop.quit)
        overlays.append(overlay)
        overlay.show()
    under = QGuiApplication.screenAt(QCursor.pos())
    for overlay in overlays:
        if overlay.screen() is under or under is None:
            overlay.activateWindow()
            overlay.setFocus()
            break
    timer = QTimer()
    timer.setInterval(30)
    timer.timeout.connect(lambda: [o.update() for o in overlays])
    timer.start()
    try:
        loop.exec()
    finally:
        timer.stop()
        for overlay in overlays:
            overlay.close()
        for widget, opacity in faded:
            widget.setWindowOpacity(opacity)
        if owner is not None:
            owner.activateWindow()
    return result.get("pos")


# --- Reordenación de listas (funciones puras) --------------------------------------------
def move_rows(seq: Sequence[Any], rows: Iterable[int], target: int) -> tuple[list[Any], list[int]]:
    """Mueve los elementos ``rows`` para que queden justo antes de la posición ``target``.

    ``target`` se expresa en la lista original (0..len). Devuelve (nueva lista, nuevos índices).
    """
    items = list(seq)
    selected = sorted({r for r in rows if 0 <= r < len(items)})
    if not selected:
        return items, []
    target = max(0, min(int(target), len(items)))
    chosen = set(selected)
    moving = [items[r] for r in selected]
    rest = [x for i, x in enumerate(items) if i not in chosen]
    insert_at = target - sum(1 for r in selected if r < target)
    new = rest[:insert_at] + moving + rest[insert_at:]
    return new, list(range(insert_at, insert_at + len(moving)))


def shift_rows(seq: Sequence[Any], rows: Iterable[int], delta: int) -> tuple[list[Any], list[int]]:
    """Desplaza cada fila seleccionada una posición (delta = -1 arriba, +1 abajo)."""
    items = list(seq)
    selected = {r for r in rows if 0 <= r < len(items)}
    order = sorted(selected, reverse=delta > 0)
    for r in order:
        n = r + delta
        if 0 <= n < len(items) and n not in selected:
            items[r], items[n] = items[n], items[r]
            selected.discard(r)
            selected.add(n)
    return items, sorted(selected)


# --- Lista de macros (barra lateral) -------------------------------------------------------
class MacroListDelegate(QStyledItemDelegate):
    """Pinta cada macro como una tarjeta: nombre, resumen, chip del disparador e interruptor."""

    toggleRequested = Signal(int)
    ROW_HEIGHT = 58
    SWITCH_W, SWITCH_H = 30, 17

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:  # noqa: N802
        return QSize(option.rect.width(), self.ROW_HEIGHT)

    def _card(self, rect: QRect) -> QRectF:
        return QRectF(rect).adjusted(8, 3, -8, -3)

    def _switch_rect(self, rect: QRect) -> QRectF:
        card = self._card(rect)
        return QRectF(card.right() - 12 - self.SWITCH_W, card.center().y() - self.SWITCH_H / 2,
                      self.SWITCH_W, self.SWITCH_H)

    def paint(self, p: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        card = self._card(option.rect)
        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)
        enabled = bool(index.data(ROLE_ENABLED))
        if selected:
            p.setPen(QPen(theme.with_alpha(theme.ACCENT, 0.55), 1))
            p.setBrush(theme.with_alpha(theme.ACCENT, 0.16))
            p.drawRoundedRect(card, 9, 9)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(theme.ACCENT))
            p.drawRoundedRect(QRectF(card.left() + 1, card.top() + 12, 3, card.height() - 24), 1.5, 1.5)
        elif hovered:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 10))
            p.drawRoundedRect(card, 9, 9)

        # Interruptor.
        sw = self._switch_rect(option.rect)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(theme.GREEN) if enabled else QColor("#3a3e51"))
        p.drawRoundedRect(sw, sw.height() / 2, sw.height() / 2)
        d = sw.height() - 5
        knob_x = sw.right() - 2.5 - d if enabled else sw.left() + 2.5
        p.setBrush(QColor("#ffffff") if enabled else QColor("#c9cad6"))
        p.drawEllipse(QRectF(knob_x, sw.top() + 2.5, d, d))

        text_left = card.left() + 14
        text_right = sw.left() - 10
        avail = max(10.0, text_right - text_left)

        # Línea 1: nombre.
        name_font = QFont(option.font)
        name_font.setWeight(QFont.DemiBold)
        fm_name = QFontMetricsF(name_font)
        name = fm_name.elidedText(index.data(Qt.DisplayRole) or "", Qt.ElideRight, avail)
        p.setFont(name_font)
        p.setPen(QColor(theme.TEXT if enabled else theme.TEXT_DIM))
        p.drawText(QRectF(text_left, card.top() + 7, avail, 20), Qt.AlignLeft | Qt.AlignVCenter, name)

        # Línea 2: chip del disparador + resumen.
        trigger = index.data(ROLE_TRIGGER) or ""
        error = index.data(ROLE_ERROR) or ""
        line2 = QRectF(text_left, card.top() + 29, avail, 18)
        x = line2.left()
        if trigger:
            chip_font = QFont(option.font)
            chip_font.setPointSizeF(8)
            chip_font.setWeight(QFont.DemiBold)
            fm_chip = QFontMetricsF(chip_font)
            text = fm_chip.elidedText(trigger, Qt.ElideRight, min(110.0, avail * 0.6))
            chip_w = max(20.0, fm_chip.horizontalAdvance(text) + 12)
            chip = QRectF(x, line2.top(), chip_w, line2.height())
            if error:
                p.setBrush(theme.with_alpha(theme.RED, 0.15))
                p.setPen(QPen(theme.with_alpha(theme.RED, 0.8), 1))
            else:
                p.setBrush(QColor("#2c2f3e"))
                p.setPen(QPen(QColor("#4a4f66"), 1))
            p.drawRoundedRect(chip.adjusted(0.5, 0.5, -0.5, -0.5), 5, 5)
            p.setFont(chip_font)
            p.setPen(QColor("#ffc2c8") if error else QColor(theme.TEXT if enabled else theme.TEXT_DIM))
            p.drawText(chip, Qt.AlignCenter, text)
            x = chip.right() + 7
        sub_font = QFont(option.font)
        sub_font.setPointSizeF(8.5)
        fm_sub = QFontMetricsF(sub_font)
        sub_w = max(10.0, line2.right() - x)
        subtitle = fm_sub.elidedText(index.data(ROLE_SUBTITLE) or "", Qt.ElideRight, sub_w)
        p.setFont(sub_font)
        p.setPen(QColor(theme.RED) if error else QColor(theme.TEXT_DIM))
        p.drawText(QRectF(x, line2.top(), sub_w, line2.height()), Qt.AlignLeft | Qt.AlignVCenter, subtitle)
        p.restore()

    def editorEvent(self, event: QEvent, model, option: QStyleOptionViewItem, index: QModelIndex) -> bool:  # noqa: N802
        if event.type() in (QEvent.MouseButtonPress, QEvent.MouseButtonRelease, QEvent.MouseButtonDblClick):
            if self._switch_rect(option.rect).adjusted(-4, -6, 4, 6).contains(event.position()):
                if event.type() == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
                    self.toggleRequested.emit(index.row())
                return True
        return super().editorEvent(event, model, option, index)

    def createEditor(self, parent: QWidget, option: QStyleOptionViewItem, index: QModelIndex) -> QWidget:  # noqa: N802
        editor = QLineEdit(parent)
        editor.setFrame(False)
        return editor

    def updateEditorGeometry(self, editor: QWidget, option: QStyleOptionViewItem, index: QModelIndex) -> None:  # noqa: N802
        card = self._card(option.rect)
        right = self._switch_rect(option.rect).left() - 8
        editor.setGeometry(QRect(int(card.left() + 8), int(card.top() + 3), int(right - card.left() - 8), 28))


# --- Lista de pasos --------------------------------------------------------------------------
class StepDelegate(QStyledItemDelegate):
    """Pinta cada paso: número, casilla, icono de color por tipo, descripción y retardo."""

    enabledToggled = Signal(int)
    ROW_HEIGHT = 48

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:  # noqa: N802
        return QSize(option.rect.width(), self.ROW_HEIGHT)

    @staticmethod
    def _card(rect: QRect) -> QRectF:
        return QRectF(rect).adjusted(4, 2, -6, -2)

    def _check_rect(self, rect: QRect) -> QRectF:
        card = self._card(rect)
        return QRectF(card.left() + 40, card.center().y() - 9, 18, 18)

    def paint(self, p: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        card = self._card(option.rect)
        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)
        running = bool(index.data(ROLE_RUNNING))
        enabled = bool(index.data(ROLE_ENABLED))
        kind = index.data(ROLE_KIND) or ""
        color = QColor(theme.step_color(kind))

        if running:
            p.setPen(QPen(QColor(theme.GREEN), 1.2))
            p.setBrush(theme.with_alpha(theme.GREEN, 0.13))
        elif selected:
            p.setPen(QPen(theme.with_alpha(theme.ACCENT, 0.6), 1))
            p.setBrush(theme.with_alpha(theme.ACCENT, 0.15))
        elif hovered:
            p.setPen(QPen(QColor(theme.BORDER_STRONG), 1))
            p.setBrush(QColor(theme.SURFACE_2))
        else:
            p.setPen(QPen(QColor(theme.BORDER), 1))
            p.setBrush(QColor(theme.SURFACE))
        p.drawRoundedRect(card.adjusted(0.5, 0.5, -0.5, -0.5), 9, 9)

        # Número (o indicador de ejecución).
        num_rect = QRectF(card.left() + 6, card.top(), 30, card.height())
        if running:
            theme.draw_glyph(p, num_rect, "play", theme.GREEN, 13)
        else:
            f = QFont(option.font)
            f.setPointSizeF(9)
            p.setFont(f)
            p.setPen(QColor(theme.TEXT_FAINT))
            p.drawText(num_rect, Qt.AlignCenter, str(index.data(ROLE_NUMBER) or ""))

        # Casilla "habilitado".
        cb = self._check_rect(option.rect)
        if enabled:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(theme.ACCENT))
            p.drawRoundedRect(cb, 5, 5)
            theme.draw_glyph(p, cb, "check", "#ffffff", 12)
        else:
            p.setPen(QPen(QColor("#565b75"), 1.2))
            p.setBrush(QColor(theme.SURFACE_2))
            p.drawRoundedRect(cb.adjusted(0.5, 0.5, -0.5, -0.5), 5, 5)

        opacity = 1.0 if enabled else 0.45
        p.setOpacity(opacity)
        # Icono del tipo en una "pastilla" de color suave.
        badge = QRectF(cb.right() + 12, card.center().y() - 15, 30, 30)
        p.setPen(Qt.NoPen)
        p.setBrush(theme.with_alpha(color, 0.16))
        p.drawRoundedRect(badge, 8, 8)
        theme.draw_glyph(p, badge, theme.STEP_ICONS.get(kind, "circle"), color, 15)

        # Retardo (derecha).
        delay_text = index.data(ROLE_DELAY) or ""
        is_default = bool(index.data(ROLE_DELAY_DEFAULT))
        if is_default and card.width() < 480 and "(" in delay_text:
            # Con poco espacio, "por defecto (150 ms)" → "150 ms" (el color atenuado ya lo indica).
            delay_text = delay_text[delay_text.find("(") + 1:delay_text.rfind(")")]
        delay_font = QFont(option.font)
        delay_font.setPointSizeF(9)
        fm_d = QFontMetricsF(delay_font)
        delay_w = fm_d.horizontalAdvance(delay_text) + (20 if not is_default else 0)
        right = card.right() - 12
        if delay_text:
            icon_w = 18
            if is_default:
                r = QRectF(right - delay_w, card.top(), delay_w, card.height())
                p.setFont(delay_font)
                p.setPen(QColor(theme.TEXT_FAINT))
                p.drawText(r, Qt.AlignRight | Qt.AlignVCenter, delay_text)
                theme.draw_glyph(p, QRectF(r.left() - icon_w - 2, card.top(), icon_w, card.height()), "clock",
                                 theme.TEXT_FAINT, 12)
                delay_left = r.left() - icon_w - 2
            else:
                pill = QRectF(right - delay_w, card.center().y() - 11, delay_w, 22)
                p.setBrush(theme.with_alpha(theme.AMBER, 0.12))
                p.setPen(QPen(theme.with_alpha(theme.AMBER, 0.45), 1))
                p.drawRoundedRect(pill, 11, 11)
                p.setFont(delay_font)
                p.setPen(QColor(theme.AMBER))
                p.drawText(pill, Qt.AlignCenter, delay_text)
                theme.draw_glyph(p, QRectF(pill.left() - icon_w - 2, card.top(), icon_w, card.height()), "clock",
                                 theme.AMBER, 12)
                delay_left = pill.left() - icon_w - 2
        else:
            delay_left = right

        # Descripción + comentario.
        text_left = badge.right() + 12
        avail = max(20.0, delay_left - 12 - text_left)
        desc_font = QFont(option.font)
        desc_font.setPointSizeF(10)
        fm = QFontMetricsF(desc_font)
        desc = index.data(Qt.DisplayRole) or ""
        comment = index.data(ROLE_COMMENT) or ""
        desc_shown = fm.elidedText(desc, Qt.ElideRight, avail)
        p.setFont(desc_font)
        p.setPen(QColor(theme.TEXT))
        text_rect = QRectF(text_left, card.top(), avail, card.height())
        p.drawText(text_rect, Qt.AlignLeft | Qt.AlignVCenter, desc_shown)
        used = fm.horizontalAdvance(desc_shown)
        if comment and used + 40 < avail:
            cf = QFont(option.font)
            cf.setPointSizeF(9)
            cf.setItalic(True)
            fmc = QFontMetricsF(cf)
            ctext = fmc.elidedText("— " + comment, Qt.ElideRight, avail - used - 12)
            p.setFont(cf)
            p.setPen(QColor(theme.TEXT_DIM))
            p.drawText(QRectF(text_left + used + 10, card.top(), avail - used - 10, card.height()),
                       Qt.AlignLeft | Qt.AlignVCenter, ctext)
        p.restore()

    def editorEvent(self, event: QEvent, model, option: QStyleOptionViewItem, index: QModelIndex) -> bool:  # noqa: N802
        if event.type() in (QEvent.MouseButtonPress, QEvent.MouseButtonRelease, QEvent.MouseButtonDblClick):
            if self._check_rect(option.rect).adjusted(-5, -8, 5, 8).contains(event.position()):
                if event.type() == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
                    self.enabledToggled.emit(index.row())
                return True
        return super().editorEvent(event, model, option, index)


class StepTree(QTreeWidget):
    """Lista de pasos con arrastrar y soltar propio: la vista nunca mueve ítems por su cuenta.

    Al soltar se emite ``moveRequested(filas, destino)`` y la ventana reordena el modelo y
    reconstruye la lista, así que no hay forma de "soltar dentro" de un paso ni de perder ítems.
    """

    moveRequested = Signal(list, int)
    editRequested = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("StepList")
        self.setColumnCount(1)
        self.setHeaderHidden(True)
        self.setRootIsDecorated(False)
        self.setIndentation(0)
        self.setUniformRowHeights(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setDropIndicatorShown(False)  # pintamos nuestro propio indicador
        self.setDragDropOverwriteMode(False)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setMouseTracking(True)
        self.setAutoScroll(True)
        self.setAutoScrollMargin(28)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.setFocusPolicy(Qt.StrongFocus)
        self._drop_row: Optional[int] = None
        self.empty_title = ""
        self.empty_text = ""
        self.delegate = StepDelegate(self)
        self.setItemDelegate(self.delegate)
        root = self.invisibleRootItem()
        root.setFlags(root.flags() | Qt.ItemIsDropEnabled)

    @staticmethod
    def item_flags() -> Qt.ItemFlag:
        # Sin ItemIsDropEnabled: nunca se puede soltar "dentro" de un paso.
        return Qt.ItemIsSelectable | Qt.ItemIsEnabled | Qt.ItemIsDragEnabled

    def selected_rows(self) -> list[int]:
        return sorted(self.indexOfTopLevelItem(it) for it in self.selectedItems())

    def select_rows(self, rows: Iterable[int], current: int | None = None) -> None:
        rows = [r for r in rows if 0 <= r < self.topLevelItemCount()]
        self.clearSelection()
        for r in rows:
            self.topLevelItem(r).setSelected(True)
        if current is None and rows:
            current = rows[-1]
        if current is not None and 0 <= current < self.topLevelItemCount():
            self.selectionModel().setCurrentIndex(
                self.indexFromItem(self.topLevelItem(current)), QItemSelectionModel.NoUpdate
            )
            self.scrollToItem(self.topLevelItem(current))

    def drop_row_at(self, pos: QPoint) -> int:
        """Fila antes de la que se insertaría al soltar en ``pos`` (0..n)."""
        item = self.itemAt(pos)
        if item is None:
            return self.topLevelItemCount()
        row = self.indexOfTopLevelItem(item)
        rect = self.visualItemRect(item)
        return row + 1 if pos.y() >= rect.center().y() else row

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if event.source() is not self:
            event.ignore()
            return
        super().dragMoveEvent(event)
        event.setDropAction(Qt.MoveAction)
        event.accept()
        self._drop_row = self.drop_row_at(event.position().toPoint())
        self.viewport().update()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self._drop_row = None
        super().dragLeaveEvent(event)
        self.viewport().update()

    def dropEvent(self, event) -> None:  # noqa: N802
        target = self.drop_row_at(event.position().toPoint())
        self._drop_row = None
        if event.source() is not self:
            event.ignore()
            return
        rows = self.selected_rows()
        # CopyAction: así Qt no borra por su cuenta los ítems "movidos" al terminar el arrastre.
        event.setDropAction(Qt.CopyAction)
        event.accept()
        self.stopAutoScroll()
        self.setState(QAbstractItemView.NoState)
        self.viewport().update()
        if rows:
            QTimer.singleShot(0, lambda: self.moveRequested.emit(rows, target))

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        item = self.itemAt(event.position().toPoint())
        if item is not None and event.button() == Qt.LeftButton:
            index = self.indexFromItem(item)
            option = QStyleOptionViewItem()
            option.rect = self.visualRect(index)
            if not self.delegate._check_rect(option.rect).adjusted(-5, -8, 5, 8).contains(event.position()):
                self.editRequested.emit(self.indexOfTopLevelItem(item))
                return
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and not event.modifiers():
            item = self.currentItem()
            if item is not None:
                self.editRequested.emit(self.indexOfTopLevelItem(item))
                return
        super().keyPressEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        count = self.topLevelItemCount()
        if count == 0 and self.empty_title:
            self._paint_empty()
            return
        if self._drop_row is None or self.state() != QAbstractItemView.DraggingState or count == 0:
            return
        if self._drop_row < count:
            y = self.visualItemRect(self.topLevelItem(self._drop_row)).top()
        else:
            y = self.visualItemRect(self.topLevelItem(count - 1)).bottom() + 1
        p = QPainter(self.viewport())
        p.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor(theme.ACCENT_HOVER))
        pen.setWidthF(2.5)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.drawLine(QPointF(14, y), QPointF(self.viewport().width() - 10, y))
        p.setBrush(QColor(theme.ACCENT_HOVER))
        p.drawEllipse(QPointF(10, y), 3.5, 3.5)
        p.end()


    def _paint_empty(self) -> None:
        """Estado vacío amable dentro de la propia lista."""
        p = QPainter(self.viewport())
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.viewport().rect()).adjusted(4, 4, -6, -4)
        pen = QPen(QColor("#3a3e51"))
        pen.setStyle(Qt.DashLine)
        pen.setDashPattern([4, 4])
        p.setPen(pen)
        p.setBrush(QColor(255, 255, 255, 6))
        p.drawRoundedRect(r, 12, 12)
        cy = r.center().y()
        badge = QRectF(r.center().x() - 28, cy - 70, 56, 56)
        p.setPen(Qt.NoPen)
        p.setBrush(theme.with_alpha(theme.ACCENT, 0.16))
        p.drawRoundedRect(badge, 16, 16)
        theme.draw_glyph(p, badge, "add", theme.ACCENT_HOVER, 24)
        title_font = QFont(self.font())
        title_font.setPointSizeF(12)
        title_font.setWeight(QFont.DemiBold)
        p.setFont(title_font)
        p.setPen(QColor(theme.TEXT))
        p.drawText(QRectF(r.left() + 20, cy - 4, r.width() - 40, 28), Qt.AlignCenter, self.empty_title)
        text_font = QFont(self.font())
        text_font.setPointSizeF(9.5)
        p.setFont(text_font)
        p.setPen(QColor(theme.TEXT_DIM))
        p.drawText(QRectF(r.left() + 30, cy + 26, r.width() - 60, 60), Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap,
                   self.empty_text)
        p.end()


class MacroList(QTreeWidget):
    """Lista de macros de la barra lateral (una columna, sin arrastre)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("MacroList")
        self.setColumnCount(1)
        self.setHeaderHidden(True)
        self.setRootIsDecorated(False)
        self.setIndentation(0)
        self.setUniformRowHeights(True)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        self.setMouseTracking(True)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.delegate = MacroListDelegate(self)
        self.setItemDelegate(self.delegate)


class ClickableFrame(QFrame):
    """QFrame que emite ``clicked`` al hacer clic en cualquier parte que no sea un hijo activo."""

    clicked = Signal()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()


class EmptyState(QWidget):
    """Mensaje amable para listas vacías, con icono, título, texto y botones opcionales."""

    def __init__(self, icon_name: str, title: str, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)
        lay.setSpacing(8)
        lay.addStretch(1)
        icon = QLabel()
        icon.setAlignment(Qt.AlignCenter)
        icon.setPixmap(theme.icon(icon_name, theme.ACCENT_HOVER, 40).pixmap(40, 40))
        lay.addWidget(icon)
        title_label = QLabel(title)
        title_label.setProperty("role", "heading")
        title_label.setAlignment(Qt.AlignCenter)
        lay.addWidget(title_label)
        text_label = QLabel(text)
        text_label.setProperty("role", "dim")
        text_label.setAlignment(Qt.AlignCenter)
        text_label.setWordWrap(True)
        lay.addWidget(text_label)
        self.buttons = QHBoxLayout()
        self.buttons.setSpacing(8)
        self.buttons.addStretch(1)
        self.buttons.addStretch(1)
        lay.addSpacing(8)
        lay.addLayout(self.buttons)
        lay.addStretch(2)

    def add_button(self, button: QPushButton) -> None:
        self.buttons.insertWidget(self.buttons.count() - 1, button)
