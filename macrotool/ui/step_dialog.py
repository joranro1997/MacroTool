"""Diálogo para crear o editar un paso de una macro."""
from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..model import (
    COORD_LIMIT,
    MAX_CHAR_INTERVAL_MS,
    MAX_COUNT,
    MAX_DELAY_MS,
    MAX_SCROLL_NOTCHES,
    MAX_WAIT_MS,
    STEP_TYPES,
    Macro,
    MoveStep,
    PressStep,
    ScrollStep,
    Step,
    TextStep,
    WaitStep,
)
from . import theme
from .widgets import (
    CapturePad,
    ComboBox,
    KeyPicker,
    SpinBox,
    TokenChips,
    format_ms,
    hint_label,
    make_button,
    pick_screen_position,
    section_label,
    separator,
)

TYPE_ORDER = ("press", "text", "move", "scroll", "wait")
TYPE_LABELS = {
    "press": "Pulsación",
    "text": "Texto",
    "move": "Mover ratón",
    "scroll": "Rueda",
    "wait": "Espera",
}
PRESS_ACTIONS = (
    ("tap", "Pulsar y soltar"),
    ("down", "Mantener pulsado"),
    ("up", "Soltar"),
)
ACTION_HINTS = {
    "tap": "Todas las entradas se pulsan a la vez, se mantienen la duración indicada y se sueltan "
           "(con «Humanizar», con unos milisegundos de desfase natural).",
    "down": "Las entradas quedan pulsadas hasta un paso «Soltar» (o hasta que termine la macro).",
    "up": "Suelta entradas que un paso «Mantener pulsado» anterior dejó pulsadas.",
}
QUICK_MOUSE = (
    ("mouse_left", "Clic izq."),
    ("mouse_right", "Clic der."),
    ("mouse_middle", "Clic central"),
    ("mouse_x1", "Lateral 1"),
    ("mouse_x2", "Lateral 2"),
)
SCROLL_DIRECTIONS = (
    ("up", "Arriba"),
    ("down", "Abajo"),
    ("left", "Izquierda"),
    ("right", "Derecha"),
)
COORD_MIN, COORD_MAX = -COORD_LIMIT, COORD_LIMIT


class PositionRow(QWidget):
    """X / Y + botón "Elegir en pantalla"; opcionalmente con casilla para activarlo."""

    def __init__(self, optional_text: str | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self.check: QCheckBox | None = None
        if optional_text:
            self.check = QCheckBox(optional_text)
            self.check.toggled.connect(self._sync)
            lay.addWidget(self.check)
        self.x = SpinBox(COORD_MIN, COORD_MAX, suffix="", step=1)
        self.y = SpinBox(COORD_MIN, COORD_MAX, suffix="", step=1)
        self.x.setPrefix("X  ")
        self.y.setPrefix("Y  ")
        for spin in (self.x, self.y):
            spin.setMinimumWidth(92)
            lay.addWidget(spin)
        self.pick = make_button("Elegir en pantalla", "crosshair", tooltip="Haz clic en cualquier punto de la pantalla (Esc cancela)")
        self.pick.clicked.connect(self._pick)
        lay.addWidget(self.pick)
        lay.addStretch(1)
        self._sync()

    def _sync(self) -> None:
        active = self.check is None or self.check.isChecked()
        for w in (self.x, self.y, self.pick):
            w.setEnabled(active)

    def set_pick_enabled(self, enabled: bool) -> None:
        self.pick.setVisible(enabled)

    def _pick(self) -> None:
        pos = pick_screen_position(self)
        if pos is not None:
            self.set_position(*pos)

    def set_position(self, x: Optional[int], y: Optional[int]) -> None:
        has = x is not None and y is not None
        if self.check is not None:
            self.check.setChecked(has)
        if has:
            self.x.setValue(int(x))
            self.y.setValue(int(y))
        self._sync()

    def position(self) -> tuple[Optional[int], Optional[int]]:
        if self.check is not None and not self.check.isChecked():
            return None, None
        return self.x.value(), self.y.value()


def _form_row(grid: QGridLayout, row: int, label: str, widget: QWidget, hint: str = "") -> None:
    lab = QLabel(label)
    lab.setProperty("role", "dim")
    grid.addWidget(lab, row, 0, Qt.AlignLeft | Qt.AlignVCenter)
    grid.addWidget(widget, row, 1)
    if hint:
        grid.addWidget(hint_label(hint), row, 2)


class _Page(QWidget):
    def load(self, step: Step) -> None:  # pragma: no cover - interfaz
        raise NotImplementedError

    def build(self) -> Step:  # pragma: no cover - interfaz
        raise NotImplementedError

    def validate(self) -> str | None:
        return None

    def focus_target(self) -> QWidget | None:
        return None


class PressPage(_Page):
    def __init__(self, triggers: Any = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        self.pad = CapturePad(triggers=triggers)
        lay.addWidget(self.pad)

        chips_row = QHBoxLayout()
        chips_row.setSpacing(10)
        lab = QLabel("Entradas")
        lab.setProperty("role", "dim")
        chips_row.addWidget(lab, 0, Qt.AlignTop)
        self.chips = TokenChips(empty_text="Ninguna todavía: captura una combinación o añádelas abajo.")
        chips_row.addWidget(self.chips, 1)
        lay.addLayout(chips_row)
        self.pad.captured.connect(self.chips.set_tokens)

        quick = QHBoxLayout()
        quick.setSpacing(6)
        for token, text in QUICK_MOUSE:
            btn = make_button("+ " + text, kind="chip", tooltip="Añadir a la combinación")
            btn.setFocusPolicy(Qt.NoFocus)
            btn.clicked.connect(lambda _=False, t=token: self.chips.add_token(t))
            quick.addWidget(btn)
        quick.addStretch(1)
        lay.addLayout(quick)
        picker_row = QHBoxLayout()
        self.picker = KeyPicker()
        self.picker.setMinimumWidth(240)
        self.picker.tokenChosen.connect(self.chips.add_token)
        picker_row.addWidget(self.picker)
        picker_row.addWidget(hint_label("Cualquier tecla por su nombre (F13, Num 5, Supr, Win…)"), 1)
        lay.addLayout(picker_row)

        lay.addWidget(separator())
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        self.action = ComboBox()
        for value, text in PRESS_ACTIONS:
            self.action.addItem(text, value)
        self.hold = SpinBox(0, MAX_DELAY_MS, step=10, value=50)
        self.count = SpinBox(1, MAX_COUNT, suffix=" vez", step=1, value=1)
        self.count.valueChanged.connect(lambda v: self.count.setSuffix(" vez" if v == 1 else " veces"))
        _form_row(grid, 0, "Acción", self.action)
        _form_row(grid, 1, "Duración de la pulsación", self.hold)
        _form_row(grid, 2, "Repetir", self.count, "2 = doble clic / doble pulsación")
        grid.setColumnStretch(2, 1)
        lay.addLayout(grid)
        self.action_hint = hint_label("")
        lay.addWidget(self.action_hint)
        self.position = PositionRow("Mover el ratón antes a")
        lay.addWidget(self.position)
        self.action.currentIndexChanged.connect(self._sync_action)
        self._sync_action()

    def _sync_action(self) -> None:
        action = self.action.currentData()
        tap = action == "tap"
        self.hold.setEnabled(tap)
        self.count.setEnabled(tap)
        self.action_hint.setText(ACTION_HINTS.get(action, ""))

    def load(self, step: Step) -> None:
        if isinstance(step, PressStep):
            self.chips.set_tokens(step.inputs, emit=False)
            idx = self.action.findData(step.action)
            self.action.setCurrentIndex(max(0, idx))
            self.hold.setValue(step.hold_ms)
            self.count.setValue(max(1, step.count))
            self.position.set_position(step.x, step.y)
        else:
            self.chips.set_tokens([], emit=False)
            self.position.set_position(None, None)

    def build(self) -> Step:
        x, y = self.position.position()
        return PressStep(inputs=self.chips.tokens(), action=self.action.currentData(),
                         hold_ms=self.hold.value(), count=self.count.value(), x=x, y=y)

    def validate(self) -> str | None:
        if not self.chips.tokens():
            return "Añade al menos una tecla o botón del ratón."
        return None

    def focus_target(self) -> QWidget | None:
        return self.pad


class TextPage(_Page):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        self.text = QPlainTextEdit()
        self.text.setPlaceholderText("Escribe aquí el texto que se tecleará…")
        self.text.setTabChangesFocus(False)
        self.text.setMinimumHeight(150)
        lay.addWidget(self.text)
        lay.addWidget(hint_label("Se escribe carácter a carácter (admite acentos, ñ y emojis). "
                                 "Los saltos de línea pulsan Intro y los tabuladores, Tab."))
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        self.interval = SpinBox(0, MAX_CHAR_INTERVAL_MS, step=5, value=20)
        _form_row(grid, 0, "Intervalo entre caracteres", self.interval)
        grid.setColumnStretch(2, 1)
        lay.addLayout(grid)
        lay.addStretch(1)

    def load(self, step: Step) -> None:
        if isinstance(step, TextStep):
            self.text.setPlainText(step.text)
            self.interval.setValue(step.char_interval_ms)

    def build(self) -> Step:
        return TextStep(text=self.text.toPlainText(), char_interval_ms=self.interval.value())

    def validate(self) -> str | None:
        return None if self.text.toPlainText() else "Escribe el texto que se debe teclear."

    def focus_target(self) -> QWidget | None:
        return self.text


class MovePage(_Page):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        lay.addWidget(section_label("Destino"))
        self.position = PositionRow(None)
        lay.addWidget(self.position)
        self.relative = QCheckBox("Relativo a la posición actual (desplazamiento; útil en juegos)")
        self.relative.toggled.connect(self._sync)
        lay.addWidget(self.relative)
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        self.duration = SpinBox(0, MAX_DELAY_MS, step=50, value=0)
        _form_row(grid, 0, "Duración del movimiento", self.duration, "0 = instantáneo")
        grid.setColumnStretch(2, 1)
        lay.addLayout(grid)
        lay.addWidget(hint_label("Las coordenadas son píxeles físicos de la pantalla (la pantalla principal "
                                 "empieza en 0, 0)."))
        lay.addStretch(1)
        self._sync()

    def _sync(self) -> None:
        self.position.set_pick_enabled(not self.relative.isChecked())
        self.position.x.setPrefix("ΔX  " if self.relative.isChecked() else "X  ")
        self.position.y.setPrefix("ΔY  " if self.relative.isChecked() else "Y  ")

    def load(self, step: Step) -> None:
        if isinstance(step, MoveStep):
            self.position.set_position(step.x, step.y)
            self.relative.setChecked(step.relative)
            self.duration.setValue(step.duration_ms)

    def build(self) -> Step:
        x, y = self.position.position()
        return MoveStep(x=int(x or 0), y=int(y or 0), relative=self.relative.isChecked(),
                        duration_ms=self.duration.value())

    def focus_target(self) -> QWidget | None:
        return self.position.x


class ScrollPage(_Page):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        self.direction = ComboBox()
        for value, text in SCROLL_DIRECTIONS:
            self.direction.addItem(text, value)
        self.amount = SpinBox(1, MAX_SCROLL_NOTCHES, suffix=" muescas", step=1, value=3)
        self.amount.valueChanged.connect(lambda v: self.amount.setSuffix(" muesca" if v == 1 else " muescas"))
        _form_row(grid, 0, "Dirección", self.direction)
        _form_row(grid, 1, "Cantidad", self.amount)
        grid.setColumnStretch(2, 1)
        lay.addLayout(grid)
        self.position = PositionRow("Mover el ratón antes a")
        lay.addWidget(self.position)
        lay.addStretch(1)

    def load(self, step: Step) -> None:
        if isinstance(step, ScrollStep):
            if step.horizontal:
                direction = "right" if step.amount > 0 else "left"
            else:
                direction = "up" if step.amount > 0 else "down"
            self.direction.setCurrentIndex(max(0, self.direction.findData(direction)))
            self.amount.setValue(max(1, abs(step.amount)))
            self.position.set_position(step.x, step.y)
        else:
            self.direction.setCurrentIndex(self.direction.findData("down"))

    def build(self) -> Step:
        direction = self.direction.currentData()
        sign = 1 if direction in ("up", "right") else -1
        x, y = self.position.position()
        return ScrollStep(amount=sign * self.amount.value(), horizontal=direction in ("left", "right"), x=x, y=y)

    def focus_target(self) -> QWidget | None:
        return self.amount


class WaitPage(_Page):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        self.ms = SpinBox(0, MAX_WAIT_MS, step=100, value=500)
        self.extra = SpinBox(0, MAX_WAIT_MS, step=50, value=0)
        self.summary = hint_label("")
        _form_row(grid, 0, "Esperar", self.ms)
        _form_row(grid, 1, "Más un aleatorio de hasta", self.extra, "0 = sin variación")
        grid.setColumnStretch(2, 1)
        lay.addLayout(grid)
        lay.addWidget(self.summary)
        lay.addStretch(1)
        self.ms.valueChanged.connect(self._sync)
        self.extra.valueChanged.connect(self._sync)
        self._sync()

    def _sync(self) -> None:
        text = f"Pausa de {format_ms(self.ms.value())}"
        if self.extra.value():
            text += f" a {format_ms(self.ms.value() + self.extra.value())} (aleatorio)"
        self.summary.setText(text + ".")

    def load(self, step: Step) -> None:
        if isinstance(step, WaitStep):
            self.ms.setValue(step.ms)
            self.extra.setValue(step.random_extra_ms)

    def build(self) -> Step:
        return WaitStep(ms=self.ms.value(), random_extra_ms=self.extra.value())

    def focus_target(self) -> QWidget | None:
        return self.ms


class _PageStack(QWidget):
    """Pila de páginas en la que sólo la visible ocupa sitio (el diálogo se adapta a cada tipo).

    A diferencia de QStackedWidget, las páginas ocultas no cuentan para el tamaño.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pages: list[QWidget] = []
        self._current = -1
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

    def addWidget(self, page: QWidget) -> None:  # noqa: N802
        self._pages.append(page)
        self.layout().addWidget(page)
        if self._current < 0:
            self._current = 0
        page.setVisible(len(self._pages) - 1 == self._current)

    def setCurrentWidget(self, page: QWidget) -> None:  # noqa: N802
        self._current = self._pages.index(page)
        for p in self._pages:
            if p is not page:
                p.setVisible(False)
        page.setVisible(True)

    def currentWidget(self) -> QWidget | None:  # noqa: N802
        return self._pages[self._current] if self._current >= 0 else None

    def currentIndex(self) -> int:  # noqa: N802
        return self._current


class StepDialog(QDialog):
    """Formulario de un paso. El tipo puede cambiarse con la barra superior."""

    def __init__(self, parent: QWidget | None = None, step: Step | None = None,
                 step_type: str | None = None, *, macro: Macro | None = None, triggers: Any = None) -> None:
        super().__init__(parent)
        self._macro = macro
        self._original = step.clone() if step is not None else None
        self._result: Step | None = None
        initial_type = step.TYPE if step is not None else (step_type if step_type in TYPE_ORDER else "press")
        self.setWindowTitle("Editar paso" if step is not None else "Nuevo paso")
        self.setWindowIcon(theme.app_icon())
        self.setModal(True)
        self.setMinimumWidth(660)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(14)

        # Selector de tipo (segmentado).
        seg_frame = QFrame()
        seg_frame.setObjectName("Card")
        seg = QHBoxLayout(seg_frame)
        seg.setContentsMargins(4, 4, 4, 4)
        seg.setSpacing(4)
        self.type_group = QButtonGroup(self)
        self.type_group.setExclusive(True)
        self.type_buttons: dict[str, QPushButton] = {}
        for t in TYPE_ORDER:
            btn = make_button(TYPE_LABELS[t], theme.STEP_ICONS[t], kind="segment",
                              icon_color=theme.step_color(t))
            btn.setCheckable(True)
            btn.setFocusPolicy(Qt.NoFocus)
            btn.clicked.connect(lambda _=False, tt=t: self.set_type(tt))
            self.type_group.addButton(btn)
            self.type_buttons[t] = btn
            seg.addWidget(btn, 1)
        root.addWidget(seg_frame)

        # Páginas.
        self.pages: dict[str, _Page] = {
            "press": PressPage(triggers),
            "text": TextPage(),
            "move": MovePage(),
            "scroll": ScrollPage(),
            "wait": WaitPage(),
        }
        self.stack = _PageStack()
        for t in TYPE_ORDER:
            self.stack.addWidget(self.pages[t])
        root.addWidget(self.stack)

        root.addWidget(separator())

        # Comunes: retardo tras el paso y comentario.
        common = QGridLayout()
        common.setHorizontalSpacing(12)
        common.setVerticalSpacing(8)
        delay_label = QLabel("Retardo tras este paso")
        delay_label.setProperty("role", "dim")
        self.use_default_delay = QCheckBox()
        self.delay = SpinBox(0, MAX_DELAY_MS, step=10, value=macro.default_delay_ms if macro else 100)
        self.use_default_delay.toggled.connect(lambda on: self.delay.setEnabled(not on))
        delay_box = QHBoxLayout()
        delay_box.setSpacing(12)
        delay_box.addWidget(self.use_default_delay)
        delay_box.addWidget(self.delay)
        delay_box.addStretch(1)
        common.addWidget(delay_label, 0, 0)
        common.addLayout(delay_box, 0, 1)
        comment_label = QLabel("Comentario")
        comment_label.setProperty("role", "dim")
        self.comment = QLineEdit()
        self.comment.setPlaceholderText("Opcional: una nota para recordar qué hace este paso")
        common.addWidget(comment_label, 1, 0)
        common.addWidget(self.comment, 1, 1)
        common.setColumnStretch(1, 1)
        root.addLayout(common)

        self.error = QLabel("")
        self.error.setProperty("role", "error")
        self.error.setVisible(False)
        root.addWidget(self.error)

        buttons = QDialogButtonBox()
        self.ok_button = buttons.addButton("Aceptar", QDialogButtonBox.AcceptRole)
        self.ok_button.setProperty("kind", "primary")
        cancel = buttons.addButton("Cancelar", QDialogButtonBox.RejectRole)
        for b in (self.ok_button, cancel):
            b.setCursor(Qt.PointingHandCursor)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        # Carga de valores.
        base = self._original
        for t, page in self.pages.items():
            if base is not None and base.TYPE == t:
                page.load(base)
            else:
                page.load(PressStep(inputs=[]) if t == "press" else STEP_TYPES[t]())  # nuevo: sin entradas
        delay_value = base.delay_after_ms if base is not None else None
        self.use_default_delay.setChecked(delay_value is None)
        self.delay.setEnabled(delay_value is not None)
        if delay_value is not None:
            self.delay.setValue(int(delay_value))
        self.comment.setText(base.comment if base is not None else "")
        self.set_type(initial_type)

    # API -------------------------------------------------------------------------------
    @property
    def current_type(self) -> str:
        return TYPE_ORDER[self.stack.currentIndex()]

    def set_type(self, step_type: str) -> None:
        if step_type not in self.pages:
            return
        self.type_buttons[step_type].setChecked(True)
        self.stack.setCurrentWidget(self.pages[step_type])
        if self.isVisible():
            QTimer.singleShot(0, self._fit_height)
        default_ms = self._macro.default_delay_ms if self._macro is not None else 100
        if step_type == "wait":
            self.use_default_delay.setText("Sin retardo adicional (la espera ya es la pausa)")
        else:
            self.use_default_delay.setText(f"Usar el de la macro ({format_ms(default_ms)})")
        self.error.setVisible(False)
        target = self.pages[step_type].focus_target()
        if target is not None:
            QTimer.singleShot(0, target.setFocus)

    def _fit_height(self) -> None:
        self.resize(self.width(), self.heightForWidth(self.width()) if self.hasHeightForWidth()
                    else self.sizeHint().height())

    def build_step(self) -> tuple[Step | None, str | None]:
        page = self.pages[self.current_type]
        problem = page.validate()
        if problem:
            return None, problem
        step = page.build()
        step.enabled = self._original.enabled if self._original is not None else True
        step.delay_after_ms = None if self.use_default_delay.isChecked() else self.delay.value()
        step.comment = self.comment.text().strip()
        return step, None

    def result_step(self) -> Step | None:
        return self._result

    def accept(self) -> None:
        step, problem = self.build_step()
        if problem:
            self.error.setText(problem)
            self.error.setVisible(True)
            return
        self._result = step
        super().accept()

    def showEvent(self, event) -> None:  # noqa: N802
        theme.enable_dark_titlebar(self)
        super().showEvent(event)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(700, super().sizeHint().height())


def edit_step(parent: QWidget | None, step: Step | None = None, step_type: str | None = None, *,
              macro: Macro | None = None, triggers: Any = None) -> Step | None:
    """Abre el diálogo y devuelve el paso nuevo/editado, o None si se cancela."""
    dialog = StepDialog(parent, step, step_type, macro=macro, triggers=triggers)
    try:
        if dialog.exec() == QDialog.Accepted:
            return dialog.result_step()
        return None
    finally:
        dialog.deleteLater()
