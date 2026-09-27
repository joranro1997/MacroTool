"""Ventana principal de MacroTool.

Contiene la biblioteca de macros (barra lateral), el editor de pasos, el panel de configuración
de la macro, la barra de estado, la grabación, el diálogo de ajustes, el icono de la bandeja y
el puente Qt que trae al hilo principal los eventos del hook y del reproductor.
"""
from __future__ import annotations

import json
import logging
import math
import sys
import threading
import time
from dataclasses import fields
from typing import Any, Callable, Optional

from PySide6.QtCore import QByteArray, QEvent, QObject, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemDelegate,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStatusBar,
    QSystemTrayIcon,
    QToolButton,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import APP_NAME, __version__, storage
from ..model import (
    MAX_DELAY_MS,
    MAX_JITTER_MS,
    MAX_REPEAT_DELAY_MS,
    MAX_REPEATS,
    MAX_STAGGER_MS,
    Macro,
    Settings,
    Step,
    WaitStep,
)
from . import theme
from .step_dialog import TYPE_LABELS, TYPE_ORDER, edit_step
from .widgets import (
    ROLE_COMMENT,
    ROLE_DELAY,
    ROLE_DELAY_DEFAULT,
    ROLE_ENABLED,
    ROLE_ERROR,
    ROLE_ID,
    ROLE_KIND,
    ROLE_NUMBER,
    ROLE_RUNNING,
    ROLE_SUBTITLE,
    ROLE_TRIGGER,
    ClickableFrame,
    ComboBox,
    ComboCaptureEdit,
    FlowLayout,
    MacroList,
    SpinBox,
    StepTree,
    SwitchRow,
    ToggleSwitch,
    combo_tokens,
    format_ms,
    hint_label,
    make_button,
    make_tool_button,
    move_rows,
    repolish,
    safe_format_combo,
    section_label,
    separator,
    shift_rows,
)

log = logging.getLogger(__name__)

CLIPBOARD_MIME = "application/x-macrotool-steps"  # solo lectura (versiones anteriores); se copia como texto
UNDO_LIMIT = 100
COALESCE_SECONDS = 1.5

TRIGGER_MODES = (
    ("once", "Una vez"),
    ("toggle", "Alternar (pulsar para iniciar/detener)"),
    ("hold", "Mientras se mantiene pulsado"),
)
MODE_SHORT = {"once": "Una vez", "toggle": "Alternar", "hold": "Mantener"}
MODE_HINTS = {
    "once": "Cada pulsación ejecuta la macro completa (con sus repeticiones).",
    "toggle": "Una pulsación la inicia y otra la detiene; se repite según «Repeticiones».",
    "hold": "Se repite mientras mantienes pulsado el disparador y se detiene al soltarlo.",
}
# (acción de control, campo de Settings, descripción)
CONTROL_HOTKEYS = (
    ("run_selected", "hotkey_run_selected", "Ejecutar / detener la macro seleccionada"),
    ("stop", "hotkey_stop", "Detener cualquier macro en curso"),
    ("record", "hotkey_record", "Iniciar / detener la grabación"),
    ("enable", "hotkey_enable", "Activar / desactivar todas las macros"),
    ("pause", "hotkey_pause", "Pausar / reanudar la macro"),
)
STEPS_HINT = "Arrastra para reordenar · doble clic para editar · clic derecho para más"
HOTKEY_SHORT = {"run_selected": "Ejecutar", "stop": "Detener", "record": "Grabar", "enable": "Activar", "pause": "Pausa"}


# --- Puente entre hilos -------------------------------------------------------------------
PROGRESS_KINDS = frozenset({"step", "repeat"})
PROGRESS_INTERVAL_MS = 30  # la interfaz refleja el progreso como mucho ~30 veces por segundo


class QtBridge(QObject):
    """Reenvía al hilo principal lo que llega desde otros hilos.

    ``trigger`` viaja en una conexión en cola. Los eventos del reproductor se guardan en una
    cola propia y ``player_event`` se emite SIEMPRE en el hilo principal, en orden. Los eventos
    de progreso ("step"/"repeat") consecutivos se agrupan en el último y se entregan como mucho
    cada ``PROGRESS_INTERVAL_MS``: con retardos de 0 ms el motor emite miles por segundo y
    saturarían la cola de Qt, retrasando la suelta de un disparador o el atajo de parada.
    """

    trigger = Signal(str, bool)
    player_event = Signal(object)
    _wake = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._lock = threading.Lock()
        self._queue: list[Any] = []
        self._wake_pending = False
        self._last_progress = float("-inf")
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._drain)
        self._wake.connect(self._drain, Qt.QueuedConnection)

    def on_trigger(self, binding_id: str, pressed: bool) -> None:
        self.trigger.emit(binding_id, pressed)

    def on_player_event(self, event: Any) -> None:
        """Cualquier hilo (normalmente el del reproductor)."""
        progress = getattr(event, "kind", "") in PROGRESS_KINDS
        with self._lock:
            if progress and self._queue and getattr(self._queue[-1], "kind", "") in PROGRESS_KINDS:
                self._queue[-1] = event  # sólo interesa el progreso más reciente
            else:
                self._queue.append(event)
            post = not self._wake_pending or not progress
            self._wake_pending = True
        if post:
            self._wake.emit()

    def _drain(self) -> None:
        """Hilo principal: entrega en orden lo pendiente (el progreso, con límite de frecuencia)."""
        now = time.monotonic()
        with self._lock:
            if not self._queue:
                self._wake_pending = False
                return
            only_progress = all(getattr(e, "kind", "") in PROGRESS_KINDS for e in self._queue)
            wait_ms = (self._last_progress + PROGRESS_INTERVAL_MS / 1000 - now) * 1000
            if only_progress and wait_ms > 0:
                if not self._timer.isActive():
                    self._timer.start(max(1, math.ceil(wait_ms)))
                return  # _wake_pending sigue activo: el temporizador lo entregará
            events, self._queue = self._queue, []
            self._wake_pending = False
        for event in events:
            if getattr(event, "kind", "") in PROGRESS_KINDS:
                self._last_progress = now
            try:
                self.player_event.emit(event)
            except Exception:  # noqa: BLE001 - un receptor defectuoso no debe perder el resto
                log.exception("Error al procesar un evento del reproductor")


class _InputGuard(QObject):
    """Mientras corre una macro, impide que sus propias entradas modifiquen la interfaz.

    Si MacroTool está en primer plano, una macro podría pulsar Supr, Ctrl+V o hacer clic en
    sus propios botones. Se descartan las teclas y clics que llevan nuestra marca en
    dwExtraInfo (``GetMessageExtraInfo``); las entradas reales del usuario siguen funcionando.
    """

    def __init__(self, is_active: Callable[[], bool], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._is_active = is_active
        self._types = {QEvent.KeyPress, QEvent.KeyRelease, QEvent.ShortcutOverride, QEvent.MouseButtonPress,
                       QEvent.MouseButtonRelease, QEvent.MouseButtonDblClick}
        self._extra_info: Optional[int] = None
        self._get_extra: Optional[Callable[[], int]] = None
        if sys.platform == "win32":
            try:
                import ctypes

                user32 = ctypes.WinDLL("user32")
                fn = user32.GetMessageExtraInfo
                fn.argtypes = []
                fn.restype = ctypes.c_ssize_t
                self._get_extra = fn
            except (OSError, AttributeError):
                self._get_extra = None

    def _injected_by_us(self) -> bool:
        if self._get_extra is None:
            return False
        if self._extra_info is None:
            try:
                from ..winput import MACROTOOL_EXTRA_INFO

                self._extra_info = int(MACROTOOL_EXTRA_INFO)
            except Exception:  # noqa: BLE001
                self._extra_info = -1
        try:
            return int(self._get_extra()) == self._extra_info
        except Exception:  # noqa: BLE001
            return False

    def eventFilter(self, obj: QObject, event) -> bool:  # noqa: N802
        if event.type() in self._types and self._is_active():
            return self._injected_by_us()
        return False


# --- Diálogos auxiliares ------------------------------------------------------------------
class DelayDialog(QDialog):
    """Cambia el retardo tras uno o varios pasos."""

    def __init__(self, parent: QWidget | None, default_ms: int, current: Optional[int], *,
                 waits: str = "none") -> None:
        """``waits``: "none", "some" o "all" según cuántos pasos seleccionados son Espera
        (tras una Espera, «por defecto» significa sin retardo adicional)."""
        super().__init__(parent)
        self.setWindowTitle("Retardo tras el paso")
        self.setWindowIcon(theme.app_icon())
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(12)
        lay.addWidget(QLabel("Tiempo de espera después de los pasos seleccionados:"))
        if waits == "all":
            default_text = "Sin retardo adicional (la espera ya es la pausa)"
        elif waits == "some":
            default_text = f"Usar el de la macro ({format_ms(default_ms)}; ninguno tras las esperas)"
        else:
            default_text = f"Usar el de la macro ({format_ms(default_ms)})"
        self.use_default = QCheckBox(default_text)
        self.spin = SpinBox(0, MAX_DELAY_MS, step=10, value=current if current is not None else default_ms)
        self.use_default.toggled.connect(lambda on: self.spin.setEnabled(not on))
        self.use_default.setChecked(current is None)
        self.spin.setEnabled(current is not None)
        lay.addWidget(self.use_default)
        lay.addWidget(self.spin)
        buttons = QDialogButtonBox()
        ok = buttons.addButton("Aceptar", QDialogButtonBox.AcceptRole)
        ok.setProperty("kind", "primary")
        buttons.addButton("Cancelar", QDialogButtonBox.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def value(self) -> Optional[int]:
        return None if self.use_default.isChecked() else self.spin.value()

    def showEvent(self, event) -> None:  # noqa: N802
        theme.enable_dark_titlebar(self)
        super().showEvent(event)


class SettingsDialog(QDialog):
    """Ajustes globales: atajos, comportamiento y grabación."""

    def __init__(self, parent: QWidget | None, settings: Settings, *, triggers: Any = None) -> None:
        super().__init__(parent)
        self._settings = Settings.from_dict(settings.to_dict())
        self.setWindowTitle("Ajustes")
        self.setWindowIcon(theme.app_icon())
        self.setMinimumWidth(620)
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 16)
        root.setSpacing(14)

        title = QLabel("Ajustes")
        title.setProperty("role", "title")
        root.addWidget(title)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName("DialogBody")
        scroll.setWidget(body)
        lay = QVBoxLayout(body)
        lay.setContentsMargins(0, 0, 8, 0)
        lay.setSpacing(12)

        # Atajos globales.
        lay.addWidget(section_label("Atajos globales"))
        lay.addWidget(hint_label("Funcionan en cualquier aplicación. Haz clic en un campo y pulsa la tecla o "
                                 "combinación; Esc cancela y × lo quita."))
        card = QFrame()
        card.setObjectName("SoftCard")
        grid = QGridLayout(card)
        grid.setContentsMargins(14, 12, 14, 12)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(8)
        self.hotkey_edits: dict[str, ComboCaptureEdit] = {}
        for row, (action, field_name, label) in enumerate(CONTROL_HOTKEYS):
            lab = QLabel(label)
            edit = ComboCaptureEdit(triggers=triggers, placeholder="Sin atajo")
            edit.set_combo(getattr(self._settings, field_name))
            edit.setMinimumWidth(210)
            grid.addWidget(lab, row, 0)
            grid.addWidget(edit, row, 1)
            self.hotkey_edits[field_name] = edit
        grid.setColumnStretch(0, 1)
        lay.addWidget(card)

        # Comportamiento.
        lay.addWidget(section_label("Comportamiento"))
        behaviour = QFrame()
        behaviour.setObjectName("SoftCard")
        bl = QVBoxLayout(behaviour)
        bl.setContentsMargins(14, 10, 14, 10)
        bl.setSpacing(6)
        s = self._settings
        self.switches: dict[str, SwitchRow] = {}
        rows = (
            ("use_scancodes", "Usar scancodes al pulsar teclas",
             "Mejor compatibilidad con juegos (DirectInput). Desactívalo si alguna tecla no funciona."),
            ("ignore_triggers_when_focused", "No disparar macros con MacroTool en primer plano",
             "Así puedes escribir en esta ventana sin lanzar macros (los atajos globales siguen activos)."),
            ("minimize_on_run", "Minimizar la ventana al ejecutar o grabar", ""),
            ("minimize_to_tray", "Cerrar la ventana la deja en la bandeja del sistema",
             "Las macros siguen funcionando; para salir usa «Salir» en el menú de la bandeja."),
            ("always_on_top", "Mantener la ventana siempre visible", ""),
            ("failsafe_corner", "Parada de emergencia en las esquinas",
             "Llevar el ratón a una esquina de la pantalla principal detiene la macro."),
            ("always_admin", "Abrir siempre como administrador",
             "Al abrir MacroTool se pedirá elevación (UAC). Necesario para que los disparadores "
             "funcionen dentro de apps que se ejecutan como administrador."),
        )
        for i, (name, text, sub) in enumerate(rows):
            if i:
                bl.addWidget(separator())
            row_widget = SwitchRow(text, sub, bool(getattr(s, name)))
            self.switches[name] = row_widget
            bl.addWidget(row_widget)
        lay.addWidget(behaviour)

        # Entrada (backend).
        lay.addWidget(section_label("Entrada"))
        lay.addWidget(hint_label("Cómo se envían las pulsaciones. Por defecto SendInput (software). La "
                                 "placa HID (Raspberry Pi Pico) las envía como un teclado USB real."))
        entrada = QFrame()
        entrada.setObjectName("SoftCard")
        el = QVBoxLayout(entrada)
        el.setContentsMargins(14, 10, 14, 10)
        el.setSpacing(6)
        hid_row = SwitchRow("Enviar la entrada por placa HID (teclado real)",
                            "Requiere la Raspberry Pi Pico conectada con su firmware. Solo teclado. "
                            "Si no se detecta la placa, se sigue usando SendInput.",
                            bool(getattr(s, "use_hid_backend")))
        self.switches["use_hid_backend"] = hid_row
        el.addWidget(hid_row)
        lay.addWidget(entrada)

        # Grabación.
        lay.addWidget(section_label("Grabación"))
        rec = QFrame()
        rec.setObjectName("SoftCard")
        rl = QVBoxLayout(rec)
        rl.setContentsMargins(14, 10, 14, 10)
        rl.setSpacing(6)
        rec_rows = (
            ("record_timing", "Grabar los tiempos reales",
             "Si no, entre pasos se usa el retardo entre acciones de la macro."),
            ("record_click_positions", "Grabar la posición de los clics", ""),
            ("record_mouse_moves", "Grabar los movimientos del ratón", "Genera muchos pasos «Mover ratón»."),
        )
        for i, (name, text, sub) in enumerate(rec_rows):
            if i:
                rl.addWidget(separator())
            row_widget = SwitchRow(text, sub, bool(getattr(s, name)))
            self.switches[name] = row_widget
            rl.addWidget(row_widget)
        lay.addWidget(rec)

        data_row = QHBoxLayout()
        open_data = make_button("Abrir carpeta de datos", "export", kind="ghost",
                                tooltip="Donde se guardan macros.json, settings.json y el registro")
        open_data.clicked.connect(self._open_data_dir)
        data_row.addWidget(open_data)
        data_row.addStretch(1)
        version = QLabel(f"{APP_NAME} {__version__}")
        version.setProperty("role", "hint")
        data_row.addWidget(version)
        lay.addLayout(data_row)
        lay.addStretch(1)
        root.addWidget(scroll, 1)

        self.error = QLabel("")
        self.error.setProperty("role", "error")
        self.error.setVisible(False)
        root.addWidget(self.error)
        buttons = QDialogButtonBox()
        ok = buttons.addButton("Guardar", QDialogButtonBox.AcceptRole)
        ok.setProperty("kind", "primary")
        buttons.addButton("Cancelar", QDialogButtonBox.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    @staticmethod
    def _open_data_dir() -> None:
        try:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(storage.app_data_dir())))
        except Exception:  # noqa: BLE001
            log.exception("No se pudo abrir la carpeta de datos")

    def build_settings(self) -> tuple[Optional[Settings], Optional[str]]:
        s = Settings.from_dict(self._settings.to_dict())
        used: dict[str, str] = {}
        for _action, field_name, label in CONTROL_HOTKEYS:
            combo = self.hotkey_edits[field_name].combo()
            if combo:
                if combo in used:
                    return None, f"El atajo «{safe_format_combo(combo_tokens(combo))}» está repetido."
                used[combo] = label
            setattr(s, field_name, combo)
        for name, row in self.switches.items():
            setattr(s, name, row.isChecked())
        return s, None

    def result_settings(self) -> Optional[Settings]:
        return getattr(self, "_result", None)

    def accept(self) -> None:
        settings, problem = self.build_settings()
        if problem:
            self.error.setText(problem)
            self.error.setVisible(True)
            return
        self._result = settings
        super().accept()

    def showEvent(self, event) -> None:  # noqa: N802
        theme.enable_dark_titlebar(self)
        super().showEvent(event)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(660, 700)


def edit_settings(parent: QWidget | None, settings: Settings, *, triggers: Any = None) -> Optional[Settings]:
    dialog = SettingsDialog(parent, settings, triggers=triggers)
    try:
        return dialog.result_settings() if dialog.exec() == QDialog.Accepted else None
    finally:
        dialog.deleteLater()


def _combo_to_keysequence(combo: str) -> Optional[QKeySequence]:
    """Convierte una combinación sencilla ("f6", "ctrl+shift+r") a QKeySequence (atajo local)."""
    names = {"ctrl": "Ctrl", "shift": "Shift", "alt": "Alt", "win": "Meta"}
    parts: list[str] = []
    for tok in combo_tokens(combo):
        if tok in names:
            parts.append(names[tok])
        elif len(tok) == 1 and tok.isalnum():
            parts.append(tok.upper())
        elif tok.startswith("f") and tok[1:].isdigit():
            parts.append(tok.upper())
        else:
            return None
    return QKeySequence("+".join(parts)) if parts else None


# --- Ventana principal --------------------------------------------------------------------
class MainWindow(QMainWindow):
    """Ventana principal. ``start_hooks=False`` no instala hooks ni icono de bandeja (tests, capturas).

    ``suspend_triggers=True`` instala los hooks con los disparadores ya suspendidos, antes de
    registrar ningún binding (prueba de humo: nunca se bloquea ni se dispara nada).
    """

    def __init__(self, *, start_hooks: bool = True, backend: Any = None, suspend_triggers: bool = False) -> None:
        super().__init__()
        self._start_hooks = start_hooks
        self._suspend_triggers = suspend_triggers
        self._shut_down = False
        self._quitting = False
        self._tray: Optional[QSystemTrayIcon] = None
        self._tray_notified = False
        self._loading = False
        self._filling_list = False
        self._current_id: Optional[str] = None
        self._undo: dict[str, list[dict]] = {}
        self._redo: dict[str, list[dict]] = {}
        self._last_checkpoint: Optional[tuple[str, str]] = None
        self._last_checkpoint_t = 0.0
        self._binding_errors: dict[str, str] = {}
        self._hook_error = ""
        self._is_running = False
        self._is_paused = False
        self._running_macro_id: Optional[str] = None
        self._running_step: Optional[tuple[str, int]] = None
        self._repeat_info = (0, 1)
        self._recording = False
        self._rec_count = 0
        self._rec_minimized = False
        # Instante (perf_counter) en que, grabando, el usuario volvió a MacroTool (ventana
        # activada o menú de la bandeja): al detener desde aquí se descarta lo posterior.
        self._rec_return_t: Optional[float] = None
        self._pending_backend = False
        self._fallback_actions: list[QAction] = []
        self._compact = False
        self.save_on_exit = True  # False en la prueba de humo: no tocar los datos reales
        self._button_texts: dict[QPushButton, str] = {}

        self.settings: Settings = storage.load_settings()
        self._storage_error = ""
        try:
            self.macros: list[Macro] = storage.load_macros()
        except OSError as exc:
            # No se pudo leer (permisos, disco…): no guardar nada para no sobrescribir el archivo.
            log.exception("No se pudieron leer las macros")
            self.macros = []
            self._storage_error = f"No se pudieron leer las macros ({exc}); no se guardarán cambios."

        # Puente Qt: los avisos de otros hilos llegan aquí en cola.
        self.bridge = QtBridge(self)
        self.bridge.trigger.connect(self._on_trigger, Qt.QueuedConnection)
        self.bridge.player_event.connect(self._on_player_event)  # ya llega al hilo principal, en orden

        # Motor.
        from ..controller import MacroController
        from ..engine import MacroPlayer

        self._backend_given = backend is not None
        self.player = MacroPlayer(backend if backend is not None else self._make_backend())
        self.controller = MacroController(self.player, self.get_macro, self.bridge.on_player_event,
                                          failsafe=lambda: bool(self.settings.failsafe_corner))

        # Hooks globales (disparadores, atajos, grabación).
        self.hook: Any = None
        self.triggers: Any = None
        self.recorder: Any = None
        if start_hooks:
            self._start_input_hooks()

        self._autosave = QTimer(self)
        self._autosave.setSingleShot(True)
        self._autosave.setInterval(500)
        self._autosave.timeout.connect(self.save_now)
        self._flash_timer = QTimer(self)
        self._flash_timer.setSingleShot(True)
        self._flash_timer.timeout.connect(self._clear_message)
        self._rec_timer = QTimer(self)
        self._rec_timer.setInterval(150)
        self._rec_timer.timeout.connect(self._update_record_status)
        self._release_retry = QTimer(self)
        self._release_retry.setInterval(500)
        self._release_retry.timeout.connect(self._retry_pending_release)

        # Aviso de elevación: si MacroTool no va como administrador, vigila si aparece una app
        # de mayor integridad en primer plano (los disparadores no funcionarían ahí).
        self._elev_warned = False
        self._elev_timer: Optional[QTimer] = None
        if start_hooks and not self._suspend_triggers and not self._is_elevated():
            self._elev_timer = QTimer(self)
            self._elev_timer.setInterval(4000)
            self._elev_timer.timeout.connect(self._check_elevation)
            self._elev_timer.start()

        self._input_guard = _InputGuard(lambda: self._is_running, self)
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self._input_guard)

        self._build_ui()
        self._build_actions()
        if start_hooks:
            self._create_tray()
        self._restore_geometry()
        self._apply_window_flags()
        self._populate_macro_list(select=self.settings.last_macro_id or None)
        self._update_hotkey_hints()
        self._update_bindings()
        self._update_global_switch()
        self._set_status("Lista", theme.GREEN)
        self._update_run_controls()
        startup_error = self._storage_error or self._hook_error
        if startup_error:
            QTimer.singleShot(300, lambda: self.flash(startup_error, "error", 15000))
        # Al abrir sin administrador (y sin la opción de auto-elevar), avisar con opción de
        # reiniciar elevado o dejarlo siempre así.
        if (start_hooks and not self._suspend_triggers and not self._is_elevated()
                and not bool(self.settings.always_admin)):
            QTimer.singleShot(400, self._prompt_elevation_on_startup)

    # ------------------------------------------------------------------ infraestructura
    def _make_backend(self) -> Any:
        self._backend_note: Optional[str] = None
        if bool(self.settings.use_hid_backend):
            try:
                from ..hidserial import HidSerialBackend

                return HidSerialBackend()  # autodetecta la placa (handshake P→PONG)
            except Exception as exc:  # noqa: BLE001
                log.warning("Backend HID no disponible (%s); se usa SendInput", exc)
                self._backend_note = ("No se detectó la placa HID; se usa SendInput por ahora. "
                                      "Conéctala y vuelve a guardar los ajustes.")
        try:
            from ..winput import WinInputBackend

            return WinInputBackend(use_scancodes=bool(self.settings.use_scancodes))
        except Exception:  # noqa: BLE001
            log.exception("No se pudo crear el backend de entrada")
            return None

    def _start_input_hooks(self) -> None:
        try:
            from ..hooks import InputHook, TriggerManager
            from ..recorder import Recorder

            hook = InputHook()
            hook.start()
        except Exception as exc:  # noqa: BLE001
            log.exception("No se pudieron instalar los hooks de entrada")
            self._hook_error = str(exc) or "No se pudieron instalar los hooks de entrada: los disparadores no funcionarán."
            return
        self.hook = hook
        self.triggers = TriggerManager(hook, self.bridge.on_trigger)
        if self._suspend_triggers:
            self.triggers.set_suspended(True)
        self.recorder = Recorder(hook)

    def get_macro(self, macro_id: str) -> Optional[Macro]:
        for macro in self.macros:
            if macro.id == macro_id:
                return macro
        return None

    def current_macro(self) -> Optional[Macro]:
        return self.get_macro(self._current_id) if self._current_id else None

    # ------------------------------------------------------------------ construcción UI
    def _build_ui(self) -> None:
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(theme.app_icon())
        self.resize(1180, 740)
        self.setMinimumSize(900, 600)
        central = QWidget()
        central.setObjectName("Central")
        outer = QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(1)
        self.splitter.addWidget(self._build_sidebar())
        self.splitter.addWidget(self._build_center())
        self.splitter.addWidget(self._build_right_panel())
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setStretchFactor(2, 0)
        self.splitter.setSizes([262, 598, 320])
        outer.addWidget(self.splitter)
        self.setCentralWidget(central)
        self._build_status_bar()

    def _build_sidebar(self) -> QWidget:
        side = QWidget()
        side.setObjectName("Sidebar")
        side.setMinimumWidth(244)
        side.setMaximumWidth(380)
        lay = QVBoxLayout(side)
        lay.setContentsMargins(14, 16, 14, 14)
        lay.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(10)
        logo = QLabel()
        logo.setPixmap(theme.app_icon().pixmap(28, 28))
        header.addWidget(logo)
        name = QLabel(APP_NAME)
        name.setProperty("role", "appname")
        header.addWidget(name)
        header.addStretch(1)
        self.settings_button = make_tool_button("settings", "Ajustes", icon_size=18)
        self.settings_button.clicked.connect(self.open_settings)
        header.addWidget(self.settings_button)
        lay.addLayout(header)

        # Interruptor global.
        self.global_card = ClickableFrame()
        self.global_card.setObjectName("GlobalSwitchCard")
        self.global_card.setCursor(Qt.PointingHandCursor)
        self.global_card.setToolTip("Activa o desactiva todos los disparadores de macros")
        gl = QHBoxLayout(self.global_card)
        gl.setContentsMargins(12, 10, 12, 10)
        gl.setSpacing(10)
        self.global_icon = QLabel()
        gl.addWidget(self.global_icon, 0, Qt.AlignVCenter)
        texts = QVBoxLayout()
        texts.setSpacing(1)
        self.global_title = QLabel("Macros activas")
        self.global_title.setProperty("role", "heading")
        self.global_subtitle = QLabel("")
        self.global_subtitle.setProperty("role", "hint")
        texts.addWidget(self.global_title)
        texts.addWidget(self.global_subtitle)
        gl.addLayout(texts, 1)
        self.global_switch = ToggleSwitch(on_color=theme.GREEN, width=42, height=24)
        self.global_switch.setChecked(self.settings.triggers_enabled)
        self.global_switch.toggled.connect(self.set_triggers_enabled)
        gl.addWidget(self.global_switch, 0, Qt.AlignVCenter)
        self.global_card.clicked.connect(self.global_switch.toggle)
        lay.addWidget(self.global_card)

        lib_header = QHBoxLayout()
        lib_header.addWidget(section_label("Mis macros"))
        self.macro_count = QLabel("0")
        self.macro_count.setProperty("role", "badge")
        lib_header.addWidget(self.macro_count)
        lib_header.addStretch(1)
        lay.addLayout(lib_header)

        self.macro_list = MacroList()
        self.macro_list.currentItemChanged.connect(self._on_macro_selected)
        self.macro_list.itemChanged.connect(self._on_macro_item_changed)
        self.macro_list.delegate.toggleRequested.connect(self._on_macro_toggle_requested)
        self.macro_list.customContextMenuRequested.connect(self._macro_context_menu)
        lay.addWidget(self.macro_list, 1)

        self.new_macro_button = make_button("Nueva macro", "add", kind="primary", icon_color="#ffffff",
                                            tooltip="Crear una macro vacía (Ctrl+N)")
        self.new_macro_button.clicked.connect(lambda: self.add_macro())
        lay.addWidget(self.new_macro_button)
        tools = QHBoxLayout()
        tools.setSpacing(4)
        self.dup_macro_button = make_tool_button("duplicate", "Duplicar la macro")
        self.dup_macro_button.clicked.connect(self.duplicate_macro)
        self.del_macro_button = make_tool_button("delete", "Eliminar la macro")
        self.del_macro_button.clicked.connect(lambda: self.delete_macro())
        self.import_button = make_tool_button("import", "Importar macros desde un archivo…")
        self.import_button.clicked.connect(lambda: self.import_macros())  # clicked(bool) no es una ruta
        self.export_button = make_tool_button("export", "Exportar…")
        export_menu = QMenu(self.export_button)
        export_menu.addAction("Exportar la macro seleccionada…", lambda: self.export_macros(False))
        export_menu.addAction("Exportar todas las macros…", lambda: self.export_macros(True))
        self.export_button.setMenu(export_menu)
        self.export_button.setPopupMode(QToolButton.InstantPopup)
        for b in (self.dup_macro_button, self.del_macro_button, self.import_button, self.export_button):
            b.setProperty("kind", "framed")
            b.setMinimumSize(36, 32)
            tools.addWidget(b, 1)
        lay.addLayout(tools)
        return side

    def _build_center(self) -> QWidget:
        center = QWidget()
        center.setObjectName("CenterPanel")
        center.setMinimumWidth(360)
        center.installEventFilter(self)
        lay = QVBoxLayout(center)
        lay.setContentsMargins(20, 16, 20, 14)
        lay.setSpacing(10)

        self.name_edit = QLineEdit()
        self.name_edit.setObjectName("NameEdit")
        self.name_edit.setPlaceholderText("Nombre de la macro")
        self.name_edit.textEdited.connect(self._on_name_edited)
        self.name_edit.editingFinished.connect(self._on_name_edit_finished)
        self.name_edit.returnPressed.connect(lambda: self.step_tree.setFocus())
        lay.addWidget(self.name_edit)
        self.summary_label = QLabel("")
        self.summary_label.setProperty("role", "dim")
        self.summary_label.setContentsMargins(8, 0, 0, 0)
        self.summary_label.setWordWrap(True)
        lay.addWidget(self.summary_label)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.run_button = make_button("Ejecutar", "play", kind="primary", icon_color="#ffffff")
        self.run_button.clicked.connect(self.run_selected)
        self.stop_button = make_button("Detener", "stop", icon_color=theme.RED)
        self.stop_button.clicked.connect(self.stop_all)
        self.pause_button = make_button("Pausa", "pause", icon_color=theme.AMBER)
        self.pause_button.clicked.connect(self.toggle_pause)
        self.record_button = make_button("Grabar", "record", icon_color=theme.RED)
        self.record_button.clicked.connect(lambda: self.toggle_recording(from_ui=True))
        self.record_options = make_tool_button("chevron_down", "Opciones de grabación", icon_size=12)
        self.record_options.setProperty("kind", "framed")
        self.record_options.setPopupMode(QToolButton.InstantPopup)
        self.record_options.setMinimumHeight(34)
        rec_menu = QMenu(self.record_options)
        self.act_rec_moves = rec_menu.addAction("Grabar los movimientos del ratón")
        self.act_rec_positions = rec_menu.addAction("Grabar la posición de los clics")
        self.act_rec_timing = rec_menu.addAction("Grabar los tiempos reales")
        rec_menu.addSeparator()
        self.act_rec_minimize = rec_menu.addAction("Minimizar la ventana al grabar/ejecutar")
        for act, field_name in ((self.act_rec_moves, "record_mouse_moves"),
                                (self.act_rec_positions, "record_click_positions"),
                                (self.act_rec_timing, "record_timing"),
                                (self.act_rec_minimize, "minimize_on_run")):
            act.setCheckable(True)
            act.setChecked(bool(getattr(self.settings, field_name)))
            act.toggled.connect(lambda on, f=field_name: self._set_setting(f, on))
        self.record_options.setMenu(rec_menu)
        for b in (self.run_button, self.stop_button, self.pause_button, self.record_button):
            b.setMinimumHeight(34)
            self._button_texts[b] = b.text()
            actions.addWidget(b)
        actions.addWidget(self.record_options)
        actions.addStretch(1)
        lay.addLayout(actions)
        lay.addSpacing(4)

        steps_header = QHBoxLayout()
        steps_header.setSpacing(6)
        steps_header.addWidget(section_label("Pasos"))
        self.step_count = QLabel("0")
        self.step_count.setProperty("role", "badge")
        steps_header.addWidget(self.step_count)
        steps_header.addSpacing(6)
        self.steps_hint = hint_label(STEPS_HINT, wrap=False)
        self.steps_hint.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        steps_header.addWidget(self.steps_hint, 1)
        self.undo_button = make_tool_button("undo", "Deshacer (Ctrl+Z)")
        self.undo_button.clicked.connect(self.undo)
        self.redo_button = make_tool_button("redo", "Rehacer (Ctrl+Y)")
        self.redo_button.clicked.connect(self.redo)
        steps_header.addWidget(self.undo_button)
        steps_header.addWidget(self.redo_button)
        lay.addLayout(steps_header)

        self.step_tree = StepTree()
        self.step_tree.moveRequested.connect(self.move_steps)
        self.step_tree.editRequested.connect(self.edit_step_at)
        self.step_tree.delegate.enabledToggled.connect(lambda row: self.set_steps_enabled([row], None))
        self.step_tree.customContextMenuRequested.connect(self._step_context_menu)
        lay.addWidget(self.step_tree, 1)

        # Barra "Añadir paso".
        add_card = QFrame()
        add_card.setObjectName("SoftCard")
        add_lay = QVBoxLayout(add_card)
        add_lay.setContentsMargins(12, 10, 12, 12)
        add_lay.setSpacing(8)
        add_lay.addWidget(section_label("Añadir paso"))
        flow_host = QWidget()
        flow = FlowLayout(flow_host, spacing=6)
        self.add_buttons: dict[str, QPushButton] = {}
        for t in TYPE_ORDER:
            if t == "press":
                btn = make_button("Pulsación", "keyboard", kind="addstep-primary", icon_color="#ffffff",
                                  tooltip="Teclas y/o botones del ratón a la vez (p. ej. W + Clic derecho)")
            else:
                btn = make_button(TYPE_LABELS[t], theme.STEP_ICONS[t], kind="addstep",
                                  icon_color=theme.step_color(t))
            btn.clicked.connect(lambda _=False, tt=t: self.add_step_dialog(tt))
            flow.addWidget(btn)
            self.add_buttons[t] = btn
            if t != "press":
                self._button_texts[btn] = btn.text()
        self.add_buttons["text"].setToolTip("Texto: escribir un texto")
        self.add_buttons["move"].setToolTip("Mover ratón a una posición")
        self.add_buttons["scroll"].setToolTip("Rueda: girar la rueda del ratón")
        self.add_buttons["wait"].setToolTip("Espera: pausa explícita dentro de la secuencia")
        add_lay.addWidget(flow_host)
        lay.addWidget(add_card)
        self.center_panel = center
        return center

    def _build_right_panel(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setObjectName("RightPanel")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setMinimumWidth(272)
        scroll.setMaximumWidth(440)
        body = QWidget()
        body.setObjectName("PanelBody")
        scroll.setWidget(body)
        lay = QVBoxLayout(body)
        lay.setContentsMargins(18, 18, 18, 18)
        lay.setSpacing(10)

        title = QLabel("Configuración de la macro")
        title.setProperty("role", "heading")
        lay.addWidget(title)
        lay.addSpacing(2)

        # Disparador.
        lay.addWidget(section_label("Disparador"))
        self.trigger_edit = ComboCaptureEdit(triggers=self.triggers, placeholder="Sin disparador")
        self.trigger_edit.setToolTip("Haz clic y pulsa una tecla, una combinación o un botón lateral/central del ratón")
        self.trigger_edit.comboChanged.connect(self._on_trigger_changed)
        lay.addWidget(self.trigger_edit)
        lay.addWidget(hint_label("Tecla (p. ej. 1), combinación (Ctrl + 1) o botón lateral/central del ratón. "
                                 "Esc cancela la captura."))
        mode_label = QLabel("Modo")
        mode_label.setProperty("role", "dim")
        lay.addWidget(mode_label)
        self.mode_combo = ComboBox()
        self.mode_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.mode_combo.setMinimumContentsLength(12)
        for value, text in TRIGGER_MODES:
            self.mode_combo.addItem(text, value)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        lay.addWidget(self.mode_combo)
        self.mode_hint = hint_label("")
        lay.addWidget(self.mode_hint)
        self.enabled_row = SwitchRow("Macro activa", "Si está desactivada, el disparador no hace nada.",
                                     on_color=theme.GREEN)
        self.enabled_row.toggled.connect(self._on_enabled_toggled)
        lay.addWidget(self.enabled_row)
        self.block_row = SwitchRow("Bloquear la tecla original",
                                   "La aplicación no recibe la pulsación del disparador.")
        self.block_row.toggled.connect(lambda on: self._edit_macro("block_trigger", on, bindings=True))
        lay.addWidget(self.block_row)

        lay.addSpacing(6)
        # Retardo entre acciones (destacado).
        delay_card = QFrame()
        delay_card.setObjectName("AccentCard")
        dl = QVBoxLayout(delay_card)
        dl.setContentsMargins(14, 12, 14, 12)
        dl.setSpacing(8)
        dh = QHBoxLayout()
        dh.setSpacing(8)
        clock = QLabel()
        clock.setPixmap(theme.icon("clock", theme.ACCENT_HOVER, 16).pixmap(16, 16))
        dh.addWidget(clock)
        dtitle = QLabel("Retardo entre acciones")
        dtitle.setProperty("role", "heading")
        dh.addWidget(dtitle, 1)
        dl.addLayout(dh)
        self.delay_spin = SpinBox(0, MAX_DELAY_MS, step=10)
        self.delay_spin.setObjectName("BigSpin")
        self.delay_spin.setMinimumHeight(42)
        self.delay_spin.valueChanged.connect(
            lambda v: self._edit_macro("default_delay_ms", v, key="default_delay_ms", refresh_steps=True))
        dl.addWidget(self.delay_spin)
        presets_host = QWidget()
        presets = FlowLayout(presets_host, spacing=5)
        self.delay_presets: list[QPushButton] = []
        for ms in (0, 50, 100, 250, 500, 1000):
            b = make_button(format_ms(ms), kind="chip")
            b.setFocusPolicy(Qt.NoFocus)
            b.setCheckable(True)
            b.clicked.connect(lambda _=False, v=ms: self.delay_spin.setValue(v))
            presets.addWidget(b)
            self.delay_presets.append(b)
        dl.addWidget(presets_host)
        dl.addWidget(hint_label("Espera tras cada paso. Cada paso puede tener su propio retardo."))
        lay.addWidget(delay_card)

        lay.addSpacing(6)
        # Humanización.
        lay.addWidget(section_label("Humanización"))
        human_card = QFrame()
        human_card.setObjectName("SoftCard")
        hl = QVBoxLayout(human_card)
        hl.setContentsMargins(14, 10, 14, 12)
        hl.setSpacing(8)
        self.humanize_row = SwitchRow("Humanizar", "Pequeñas variaciones al azar para que parezca hecho a mano. "
                                                   "Desactívalo para tiempos exactos.")
        self.humanize_row.setToolTip("Varía un poco los retardos y las pulsaciones, y pulsa las teclas "
                                     "simultáneas con un desfase mínimo, como lo haría una persona")
        self.humanize_row.toggled.connect(self._on_humanize_toggled)
        hl.addWidget(self.humanize_row)
        hgrid = QGridLayout()
        hgrid.setHorizontalSpacing(10)
        hgrid.setVerticalSpacing(8)
        self.jitter_spin = SpinBox(0, MAX_JITTER_MS, step=1)
        self.jitter_spin.setPrefix("± ")
        self.hold_jitter_spin = SpinBox(0, MAX_JITTER_MS, step=1)
        self.hold_jitter_spin.setPrefix("± ")
        self.stagger_spin = SpinBox(0, MAX_STAGGER_MS, step=1)
        self.stagger_spin.setPrefix("0–")
        human_rows = (
            ("Variación del retardo", self.jitter_spin, "jitter_ms",
             "El retardo tras cada paso (y la pausa entre repeticiones) varía al azar hasta ± este valor.\n"
             "Casi siempre la variación es pequeña: sigue una distribución normal."),
            ("Variación de la pulsación", self.hold_jitter_spin, "hold_jitter_ms",
             "La duración de cada pulsación (y el intervalo entre caracteres al escribir texto) varía al azar "
             "hasta ± este valor."),
            ("Desfase entre teclas simultáneas", self.stagger_spin, "chord_stagger_ms",
             "Las entradas «a la vez» (p. ej. W + Clic derecho) se pulsan en orden con un desfase al azar de "
             "0 hasta este valor entre cada una, y se sueltan igual.\n0 = exactamente a la vez."),
        )
        self._humanize_labels: list[QLabel] = []
        for row, (text, spin, field_name, tip) in enumerate(human_rows):
            label = QLabel(text)
            label.setProperty("role", "dim")
            label.setWordWrap(True)
            label.setToolTip(tip)
            spin.setToolTip(tip)
            spin.setMaximumWidth(112)  # más sitio para las etiquetas en el panel estrecho
            spin.valueChanged.connect(lambda v, f=field_name: self._edit_macro(f, v, key=f))
            hgrid.addWidget(label, row, 0)
            hgrid.addWidget(spin, row, 1)
            self._humanize_labels.append(label)
        hgrid.setColumnStretch(0, 1)
        hl.addLayout(hgrid)
        lay.addWidget(human_card)

        lay.addSpacing(6)
        # Repetición.
        lay.addWidget(section_label("Repetición"))
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        rl = QLabel("Repeticiones")
        rl.setProperty("role", "dim")
        self.repeat_spin = SpinBox(1, MAX_REPEATS, suffix="", step=1, value=1)
        self.repeat_spin.valueChanged.connect(self._on_repeat_changed)
        self.infinite_check = QCheckBox("Infinitas")
        self.infinite_check.toggled.connect(self._on_infinite_toggled)
        grid.addWidget(rl, 0, 0)
        grid.addWidget(self.repeat_spin, 0, 1)
        grid.addWidget(self.infinite_check, 1, 1)
        pl = QLabel("Pausa entre repeticiones")
        pl.setProperty("role", "dim")
        pl.setWordWrap(True)
        self.repeat_delay_spin = SpinBox(0, MAX_REPEAT_DELAY_MS, step=50)
        self.repeat_delay_spin.valueChanged.connect(
            lambda v: self._edit_macro("repeat_delay_ms", v, key="repeat_delay_ms"))
        grid.addWidget(pl, 2, 0)
        grid.addWidget(self.repeat_delay_spin, 2, 1)
        sl = QLabel("Retardo inicial")
        sl.setProperty("role", "dim")
        sl.setWordWrap(True)
        self.start_delay_spin = SpinBox(0, MAX_DELAY_MS, step=500)
        self.start_delay_spin.valueChanged.connect(
            lambda v: self._edit_macro("start_delay_ms", v, key="start_delay_ms"))
        grid.addWidget(sl, 3, 0)
        grid.addWidget(self.start_delay_spin, 3, 1)
        grid.setColumnStretch(1, 1)
        lay.addLayout(grid)
        lay.addWidget(hint_label("El retardo inicial es una cuenta atrás antes de empezar: útil para cambiar "
                                 "de ventana al ejecutar desde aquí."))
        lay.addStretch(1)
        self.right_panel = scroll
        self.right_body = body
        return scroll

    def _build_status_bar(self) -> None:
        bar = QStatusBar()
        bar.setSizeGripEnabled(False)
        host = QWidget()
        hl = QHBoxLayout(host)
        hl.setContentsMargins(10, 3, 10, 3)
        hl.setSpacing(10)
        self.status_dot = QLabel()
        self.status_dot.setObjectName("StatusDot")
        self._status_color = ""
        self.status_dot.setFixedSize(8, 8)
        hl.addWidget(self.status_dot, 0, Qt.AlignVCenter)
        self.status_label = QLabel("Lista")
        self.status_label.setMinimumWidth(120)
        hl.addWidget(self.status_label)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        self.progress.setFixedSize(170, 4)
        self.progress.setVisible(False)
        hl.addWidget(self.progress, 0, Qt.AlignVCenter)
        hl.addStretch(1)
        self.message_icon = QLabel()
        self.message_icon.setFixedSize(16, 16)
        self.message_icon.setVisible(False)
        hl.addWidget(self.message_icon)
        self.message_label = QLabel("")
        self.message_label.setTextFormat(Qt.PlainText)
        hl.addWidget(self.message_label)
        self.warning_button = make_tool_button("warning", "", icon_color=theme.AMBER, icon_size=14, kind="small")
        self.warning_button.setVisible(False)
        self.warning_button.clicked.connect(self._show_binding_errors)
        hl.addWidget(self.warning_button)
        sep = QFrame()
        sep.setFixedSize(1, 14)
        sep.setStyleSheet(f"background: {theme.BORDER_STRONG};")
        hl.addWidget(sep)
        self.hotkey_hint = QLabel("")
        self.hotkey_hint.setProperty("role", "hint")
        hl.addWidget(self.hotkey_hint)
        bar.addWidget(host, 1)
        self.setStatusBar(bar)

    def _build_actions(self) -> None:
        def action(text: str, shortcut: Any, slot: Callable[[], None], widget: QWidget,
                   context: Qt.ShortcutContext = Qt.WidgetShortcut, icon_name: str | None = None) -> QAction:
            act = QAction(text, widget)
            if shortcut:
                act.setShortcuts(shortcut if isinstance(shortcut, list) else [QKeySequence(shortcut)])
            act.setShortcutContext(context)
            if icon_name:
                act.setIcon(theme.icon(icon_name, None, 16))
            act.triggered.connect(slot)
            widget.addAction(act)
            return act

        tree = self.step_tree
        self.act_edit = action("Editar…", None, lambda: self._edit_current_step(), tree, icon_name="edit")
        self.act_duplicate = action("Duplicar\tCtrl+D", "Ctrl+D", lambda: self.duplicate_steps(tree.selected_rows()), tree,
                                    icon_name="duplicate")
        self.act_copy = action("Copiar\tCtrl+C", QKeySequence.Copy, lambda: self.copy_steps(tree.selected_rows()), tree,
                               icon_name="copy")
        self.act_cut = action("Cortar\tCtrl+X", QKeySequence.Cut, lambda: self.cut_steps(tree.selected_rows()), tree,
                              icon_name="cut")
        self.act_paste = action("Pegar\tCtrl+V", QKeySequence.Paste, self.paste_steps, tree, icon_name="paste")
        self.act_delete = action("Eliminar\tSupr", QKeySequence.Delete, lambda: self.delete_steps(tree.selected_rows()),
                                 tree, icon_name="delete")
        self.act_up = action("Mover arriba\tCtrl+Arriba", "Ctrl+Up", lambda: self.shift_steps(tree.selected_rows(), -1), tree,
                             icon_name="up")
        self.act_down = action("Mover abajo\tCtrl+Abajo", "Ctrl+Down", lambda: self.shift_steps(tree.selected_rows(), 1), tree,
                               icon_name="down")
        self.act_toggle = action("Activar / desactivar\tEspacio", "Space",
                                 lambda: self.set_steps_enabled(tree.selected_rows(), None), tree, icon_name="check")
        self.act_delay = action("Cambiar retardo…", None, lambda: self.ask_steps_delay(tree.selected_rows()), tree,
                                icon_name="clock")

        win = Qt.WindowShortcut
        action("Deshacer", QKeySequence.Undo, self.undo, self, win)
        action("Rehacer", [QKeySequence("Ctrl+Y"), QKeySequence("Ctrl+Shift+Z")], self.redo, self, win)
        action("Nueva macro", "Ctrl+N", lambda: self.add_macro(), self, win)
        action("Guardar", QKeySequence.Save, self.save_all, self, win)
        action("Ajustes", "Ctrl+,", self.open_settings, self, win)
        ml = self.macro_list
        action("Eliminar macro", QKeySequence.Delete, lambda: self.delete_macro(), ml)
        action("Duplicar macro", "Ctrl+D", self.duplicate_macro, ml)

    # ------------------------------------------------------------------ biblioteca de macros
    def _macro_subtitle(self, macro: Macro) -> str:
        n = len(macro.steps)
        steps = f"{n} paso" if n == 1 else f"{n} pasos"
        error = self._binding_errors.get(f"macro:{macro.id}")
        if error:
            return error
        if not macro.trigger:
            return f"Sin disparador · {steps}"
        return f"{MODE_SHORT.get(macro.trigger_mode, '')} · {steps}"

    def _fill_macro_item(self, item: QTreeWidgetItem, macro: Macro) -> None:
        tokens = combo_tokens(macro.trigger)
        trigger_text = safe_format_combo(tokens) if tokens else (macro.trigger or "")
        error = self._binding_errors.get(f"macro:{macro.id}", "")
        self._filling_list = True
        try:
            item.setData(0, Qt.DisplayRole, macro.name)
            item.setData(0, ROLE_ID, macro.id)
            item.setData(0, ROLE_ENABLED, macro.enabled)
            item.setData(0, ROLE_TRIGGER, trigger_text)
            item.setData(0, ROLE_ERROR, error)
            item.setData(0, ROLE_SUBTITLE, self._macro_subtitle(macro))
            tip = f"{macro.name}\nDisparador: {trigger_text or 'ninguno'}"
            if error:
                tip += f"\nProblema: {error}"
            tip += "\nDoble clic o F2 para renombrar"
            item.setToolTip(0, tip)
        finally:
            self._filling_list = False

    def _populate_macro_list(self, select: Optional[str] = None) -> None:
        self._filling_list = True
        try:
            self.macro_list.blockSignals(True)
            self.macro_list.clear()
            for macro in self.macros:
                item = QTreeWidgetItem()
                item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled | Qt.ItemIsEditable)
                self._fill_macro_item(item, macro)
                self.macro_list.addTopLevelItem(item)
                self._filling_list = True
        finally:
            self.macro_list.blockSignals(False)
            self._filling_list = False
        self.macro_count.setText(str(len(self.macros)))
        target = select if select and self.get_macro(select) else (self.macros[0].id if self.macros else None)
        self.select_macro(target)

    def _macro_item(self, macro_id: str) -> Optional[QTreeWidgetItem]:
        for i in range(self.macro_list.topLevelItemCount()):
            item = self.macro_list.topLevelItem(i)
            if item.data(0, ROLE_ID) == macro_id:
                return item
        return None

    def _refresh_macro_item(self, macro: Optional[Macro] = None) -> None:
        macro = macro or self.current_macro()
        if macro is None:
            return
        item = self._macro_item(macro.id)
        if item is not None:
            self._fill_macro_item(item, macro)

    def select_macro(self, macro_id: Optional[str]) -> None:
        item = self._macro_item(macro_id) if macro_id else None
        if item is not None:
            self.macro_list.setCurrentItem(item)
            if self._current_id != macro_id:
                self._current_id = macro_id
                self._load_macro_into_ui()
        else:
            self._current_id = None
            self._load_macro_into_ui()

    def _on_macro_selected(self, current: Optional[QTreeWidgetItem], _previous=None) -> None:
        macro_id = current.data(0, ROLE_ID) if current is not None else None
        if macro_id == self._current_id:
            return
        self._current_id = macro_id
        self._last_checkpoint = None
        if macro_id:
            self.settings.last_macro_id = macro_id
        self._load_macro_into_ui()

    def _on_macro_item_changed(self, item: QTreeWidgetItem, _column: int) -> None:
        if self._filling_list:
            return
        macro = self.get_macro(item.data(0, ROLE_ID))
        if macro is None:
            return
        name = (item.text(0) or "").strip()
        if not name or name == macro.name:
            self._fill_macro_item(item, macro)
            return
        if macro.id == self._current_id:
            self._checkpoint("rename")
        macro.name = name
        self._fill_macro_item(item, macro)
        if macro.id == self._current_id:
            self.name_edit.setText(name)
        self._schedule_save()

    def _on_macro_toggle_requested(self, row: int) -> None:
        item = self.macro_list.topLevelItem(row)
        macro = self.get_macro(item.data(0, ROLE_ID)) if item is not None else None
        if macro is None:
            return
        # Siempre en la pila de ESA macro (aunque no sea la seleccionada): así el cambio se puede
        # deshacer y ningún deshacer posterior la vuelve a activar a escondidas.
        self._checkpoint("enabled", macro=macro)
        macro.enabled = not macro.enabled
        self._refresh_macro_item(macro)
        if macro.id == self._current_id:
            self._loading = True
            self.enabled_row.setChecked(macro.enabled)
            self._loading = False
            self._update_summary()
        if not macro.enabled:
            self._stop_running_macro("Macro desactivada", macro.id)
        self._update_bindings()
        self._schedule_save()

    def add_macro(self, name: Optional[str] = None) -> Macro:
        base = name or "Nueva macro"
        existing = {m.name for m in self.macros}
        candidate, n = base, 2
        while candidate in existing:
            candidate = f"{base} {n}"
            n += 1
        macro = Macro(name=candidate)
        self.macros.append(macro)
        self._populate_macro_list(select=macro.id)
        self._schedule_save()
        self._update_bindings()
        if name is None:
            self.name_edit.setFocus()
            self.name_edit.selectAll()
        return macro

    def duplicate_macro(self) -> Optional[Macro]:
        macro = self.current_macro()
        if macro is None:
            return None
        copy = macro.clone(new_identity=True)
        copy.name = f"{macro.name} (copia)"
        copy.trigger = ""  # dos macros con el mismo disparador entrarían en conflicto
        self.macros.insert(self.macros.index(macro) + 1, copy)
        self._populate_macro_list(select=copy.id)
        self._schedule_save()
        self._update_bindings()
        self.flash("Macro duplicada (sin disparador, para evitar conflictos)")
        return copy

    def delete_macro(self, macro_id: Optional[str] = None, *, confirm: bool = True) -> bool:
        macro = self.get_macro(macro_id) if macro_id else self.current_macro()
        if macro is None:
            return False
        if confirm:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Warning)
            box.setWindowTitle("Eliminar macro")
            box.setText(f"¿Eliminar la macro «{macro.name}»?")
            box.setInformativeText("Esta acción no se puede deshacer.")
            delete = box.addButton("Eliminar", QMessageBox.DestructiveRole)
            delete.setProperty("kind", "danger")
            cancel = box.addButton("Cancelar", QMessageBox.RejectRole)
            box.setDefaultButton(cancel)
            box.exec()
            if box.clickedButton() is not delete:
                return False
        if macro.id in (self._running_macro_id, self.controller.running_macro_id):
            self.controller.stop()
        index = self.macros.index(macro)
        self.macros.remove(macro)
        self._undo.pop(macro.id, None)
        self._redo.pop(macro.id, None)
        neighbour = self.macros[min(index, len(self.macros) - 1)].id if self.macros else None
        self._current_id = None
        self._populate_macro_list(select=neighbour)
        self._schedule_save()
        self._update_bindings()
        return True

    def import_macros(self, path: Optional[str] = None) -> list[Macro]:
        if path is None:
            path, _ = QFileDialog.getOpenFileName(self, "Importar macros", "",
                                                  "Macros de MacroTool (*.json);;Todos los archivos (*)")
        if not path:
            return []
        try:
            imported = storage.import_macros(path)
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "No se pudo importar", str(exc))
            return []
        if not imported:
            self.flash("El archivo no contiene macros", "warning")
            return []
        self.macros.extend(imported)
        self._populate_macro_list(select=imported[0].id)
        self._schedule_save()
        self._update_bindings()
        n = len(imported)
        self.flash(f"Se importó 1 macro" if n == 1 else f"Se importaron {n} macros")
        return imported

    def export_macros(self, all_macros: bool = False, path: Optional[str] = None) -> bool:
        macros = list(self.macros) if all_macros else ([self.current_macro()] if self.current_macro() else [])
        if not macros:
            return False
        if path is None:
            suggested = "macros.json" if all_macros else f"{_safe_filename(macros[0].name)}.json"
            path, _ = QFileDialog.getSaveFileName(self, "Exportar macros", suggested, "Macros de MacroTool (*.json)")
        if not path:
            return False
        try:
            storage.export_macros(macros, path)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "No se pudo exportar", str(exc))
            return False
        self.flash("Exportado correctamente")
        return True

    def _macro_context_menu(self, pos) -> None:
        item = self.macro_list.itemAt(pos)
        menu = QMenu(self)
        if item is not None:
            menu.addAction(theme.icon("rename", None, 16), "Renombrar\tF2", lambda: self.macro_list.editItem(item, 0))
            menu.addAction(theme.icon("duplicate", None, 16), "Duplicar", self.duplicate_macro)
            menu.addAction(theme.icon("export", None, 16), "Exportar…", lambda: self.export_macros(False))
            menu.addSeparator()
            menu.addAction(theme.icon("delete", theme.RED, 16), "Eliminar", lambda: self.delete_macro())
        else:
            menu.addAction(theme.icon("add", None, 16), "Nueva macro", lambda: self.add_macro())
            menu.addAction(theme.icon("import", None, 16), "Importar…", self.import_macros)
        menu.exec(self.macro_list.viewport().mapToGlobal(pos))

    # ------------------------------------------------------------------ panel de la macro
    def _load_macro_into_ui(self) -> None:
        macro = self.current_macro()
        has = macro is not None
        self._loading = True
        try:
            for w in (self.name_edit, self.right_body, self.run_button, self.record_button):
                w.setEnabled(has)
            if has:
                self.name_edit.setText(macro.name)
                self.trigger_edit.set_combo(macro.trigger)
                self.trigger_edit.set_error(self._binding_errors.get(f"macro:{macro.id}"))
                self.mode_combo.setCurrentIndex(max(0, self.mode_combo.findData(macro.trigger_mode)))
                self.mode_hint.setText(MODE_HINTS.get(macro.trigger_mode, ""))
                self.enabled_row.setChecked(macro.enabled)
                self.block_row.setChecked(macro.block_trigger)
                self.delay_spin.setValue(macro.default_delay_ms)
                self.humanize_row.setChecked(macro.humanize)
                self.jitter_spin.setValue(macro.jitter_ms)
                self.hold_jitter_spin.setValue(macro.hold_jitter_ms)
                self.stagger_spin.setValue(macro.chord_stagger_ms)
                self._sync_humanize_controls(macro.humanize)
                infinite = macro.repeat_count == 0
                self.infinite_check.setChecked(infinite)
                self.repeat_spin.setEnabled(not infinite)
                if not infinite:
                    self.repeat_spin.setValue(max(1, macro.repeat_count))
                self.repeat_delay_spin.setValue(macro.repeat_delay_ms)
                self.start_delay_spin.setValue(macro.start_delay_ms)
            else:
                self.name_edit.setText("")
                self.trigger_edit.set_combo("")
        finally:
            self._loading = False
            # Siempre, aunque algo falle arriba: la lista de pasos debe ser la de _current_id
            # (si no, «Eliminar» borraría pasos de otra macro distinta de la que se ve).
            self._sync_delay_presets()
            self._refresh_steps(select=[])
            self._update_summary()
            self._update_undo_buttons()
            self._update_run_controls()

    def _sync_delay_presets(self) -> None:
        value = self.delay_spin.value()
        for b in self.delay_presets:
            b.setChecked(b.text() == format_ms(value))

    def _update_summary(self) -> None:
        macro = self.current_macro()
        if macro is None:
            self.summary_label.setText("Crea una macro con «Nueva macro» para empezar.")
            return
        parts = []
        tokens = combo_tokens(macro.trigger)
        parts.append(f"Disparador: {safe_format_combo(tokens)}" if tokens else "Sin disparador")
        parts.append(MODE_SHORT.get(macro.trigger_mode, ""))
        delay = f"retardo {format_ms(macro.default_delay_ms)}"
        if macro.humanize and macro.jitter_ms and macro.default_delay_ms:
            delay += f" ± {format_ms(macro.jitter_ms)}"
        parts.append(delay)
        reps ="∞ repeticiones" if macro.repeat_count == 0 else (
            "1 repetición" if macro.repeat_count == 1 else f"{macro.repeat_count} repeticiones")
        parts.append(reps)
        if not macro.enabled:
            parts.append("desactivada")
        self.summary_label.setText("  ·  ".join(parts))

    def _edit_macro(self, field_name: str, value: Any, *, key: Optional[str] = None, bindings: bool = False,
                    refresh_steps: bool = False) -> None:
        if self._loading:
            return
        macro = self.current_macro()
        if macro is None or getattr(macro, field_name) == value:
            return
        self._checkpoint(key)
        setattr(macro, field_name, value)
        self._after_macro_change(bindings=bindings, refresh_steps=refresh_steps)

    def _after_macro_change(self, *, bindings: bool = False, refresh_steps: bool = False) -> None:
        self._refresh_macro_item()
        self._update_summary()
        self._sync_delay_presets()
        if refresh_steps:
            self._refresh_steps()
        if bindings:
            self._update_bindings()
        self._schedule_save()
        self._update_undo_buttons()

    def _on_enabled_toggled(self, on: bool) -> None:
        self._edit_macro("enabled", on, bindings=True)
        macro = self.current_macro()
        if not on and macro is not None and not self._loading:
            # Desactivada su tecla ya no la detendría (p. ej. en modo Alternar): se para aquí.
            self._stop_running_macro("Macro desactivada", macro.id)

    def _on_name_edited(self, text: str) -> None:
        name = text.strip()
        if name:
            self._edit_macro("name", name, key="name")

    def _on_name_edit_finished(self) -> None:
        """Un nombre vacío no se aplica: el campo vuelve a mostrar el nombre real (sin espacios)."""
        macro = self.current_macro()
        if macro is not None and self.name_edit.text() != macro.name:
            self.name_edit.setText(macro.name)

    def _on_trigger_changed(self, combo: str) -> None:
        self._edit_macro("trigger", combo, bindings=True)
        macro = self.current_macro()
        if macro is not None and combo:
            error = self._binding_errors.get(f"macro:{macro.id}")
            if error:
                self.flash(error, "warning", 8000)
            else:
                self.flash(f"Disparador: {safe_format_combo(combo_tokens(combo))}")

    def _on_mode_changed(self, _index: int) -> None:
        mode = self.mode_combo.currentData()
        self.mode_hint.setText(MODE_HINTS.get(mode, ""))
        self._edit_macro("trigger_mode", mode)

    def _on_humanize_toggled(self, on: bool) -> None:
        self._sync_humanize_controls(on)
        self._edit_macro("humanize", on)

    def _sync_humanize_controls(self, on: bool) -> None:
        for widget in (self.jitter_spin, self.hold_jitter_spin, self.stagger_spin, *self._humanize_labels):
            widget.setEnabled(on)

    def _on_repeat_changed(self, value: int) -> None:
        if not self.infinite_check.isChecked():
            self._edit_macro("repeat_count", value, key="repeat_count")

    def _on_infinite_toggled(self, infinite: bool) -> None:
        self.repeat_spin.setEnabled(not infinite)
        self._edit_macro("repeat_count", 0 if infinite else max(1, self.repeat_spin.value()))

    # ------------------------------------------------------------------ lista de pasos
    def _delay_text(self, macro: Macro, step: Step) -> tuple[str, bool]:
        if step.delay_after_ms is not None:
            return format_ms(step.delay_after_ms), False
        if isinstance(step, WaitStep):
            return "", True
        return f"por defecto ({format_ms(macro.default_delay_ms)})", True

    def _fill_step_item(self, item: QTreeWidgetItem, index: int, step: Step, macro: Macro) -> None:
        try:
            desc = step.describe()
        except Exception:  # noqa: BLE001
            desc = step.LABEL
        delay, is_default = self._delay_text(macro, step)
        item.setData(0, Qt.DisplayRole, desc)
        item.setData(0, ROLE_KIND, step.TYPE)
        item.setData(0, ROLE_ENABLED, step.enabled)
        item.setData(0, ROLE_NUMBER, index + 1)
        item.setData(0, ROLE_DELAY, delay)
        item.setData(0, ROLE_DELAY_DEFAULT, is_default)
        item.setData(0, ROLE_COMMENT, step.comment)
        item.setData(0, ROLE_RUNNING, self._running_step == (macro.id, index))
        tip = f"{step.LABEL}: {desc}"
        if step.comment:
            tip += f"\n{step.comment}"
        if not step.enabled:
            tip += "\n(desactivado: se salta al ejecutar)"
        item.setToolTip(0, tip)

    def _refresh_steps(self, select: Optional[list[int]] = None, current: Optional[int] = None) -> None:
        macro = self.current_macro()
        tree = self.step_tree
        if select is None:
            select = tree.selected_rows()
        tree.setUpdatesEnabled(False)
        try:
            tree.clear()
            if macro is not None:
                items = []
                for i, step in enumerate(macro.steps):
                    item = QTreeWidgetItem()
                    item.setFlags(StepTree.item_flags())
                    self._fill_step_item(item, i, step, macro)
                    items.append(item)
                tree.addTopLevelItems(items)
                tree.empty_title = "Añade tu primer paso…"
                tree.empty_text = ("Usa los botones de «Añadir paso», graba tus acciones con «Grabar» "
                                   "o pega pasos con Ctrl+V.")
            else:
                tree.empty_title = "No hay ninguna macro seleccionada"
                tree.empty_text = "Crea una con «Nueva macro» o importa un archivo."
            tree.select_rows(select, current)
        finally:
            tree.setUpdatesEnabled(True)
        n = len(macro.steps) if macro else 0
        self.step_count.setText(str(n))

    def _apply_steps(self, new_steps: list[Step], select: list[int], *, key: Optional[str] = None,
                     checkpoint: bool = True) -> None:
        macro = self.current_macro()
        if macro is None:
            return
        if checkpoint:
            self._checkpoint(key)
        macro.steps = list(new_steps)
        self._refresh_steps(select=select)
        self._after_macro_change()

    def _insert_position(self) -> int:
        macro = self.current_macro()
        rows = self.step_tree.selected_rows()
        if macro is None:
            return 0
        return (rows[-1] + 1) if rows else len(macro.steps)

    def insert_steps(self, steps: list[Step], at: Optional[int] = None) -> list[int]:
        macro = self.current_macro()
        if macro is None:
            macro = self.add_macro()
        if not steps:
            return []
        pos = self._insert_position() if at is None else max(0, min(at, len(macro.steps)))
        new = macro.steps[:pos] + list(steps) + macro.steps[pos:]
        rows = list(range(pos, pos + len(steps)))
        self._apply_steps(new, rows)
        return rows

    def replace_step(self, index: int, step: Step) -> None:
        macro = self.current_macro()
        if macro is None or not 0 <= index < len(macro.steps):
            return
        new = list(macro.steps)
        new[index] = step
        self._apply_steps(new, [index])

    def delete_steps(self, rows: list[int]) -> None:
        macro = self.current_macro()
        rows = sorted({r for r in rows if macro is not None and 0 <= r < len(macro.steps)})
        if macro is None or not rows:
            return
        new = [s for i, s in enumerate(macro.steps) if i not in set(rows)]
        after = min(rows[0], len(new) - 1)
        self._apply_steps(new, [after] if after >= 0 else [])
        self.flash("Paso eliminado" if len(rows) == 1 else f"{len(rows)} pasos eliminados")

    def move_steps(self, rows: list[int], target: int) -> None:
        macro = self.current_macro()
        if macro is None:
            return
        new, selected = move_rows(macro.steps, rows, target)
        if [id(s) for s in new] == [id(s) for s in macro.steps]:
            return
        self._apply_steps(new, selected)

    def shift_steps(self, rows: list[int], delta: int) -> None:
        macro = self.current_macro()
        if macro is None or not rows:
            return
        new, selected = shift_rows(macro.steps, rows, delta)
        if [id(s) for s in new] == [id(s) for s in macro.steps]:
            return
        self._apply_steps(new, selected, key=f"shift:{sorted(rows)}")

    def duplicate_steps(self, rows: list[int]) -> None:
        macro = self.current_macro()
        rows = sorted({r for r in rows if macro is not None and 0 <= r < len(macro.steps)})
        if macro is None or not rows:
            return
        copies = [macro.steps[r].clone() for r in rows]
        self.insert_steps(copies, at=rows[-1] + 1)

    def set_steps_enabled(self, rows: list[int], enabled: Optional[bool]) -> None:
        """Activa/desactiva los pasos; ``enabled=None`` alterna según el primero."""
        macro = self.current_macro()
        rows = sorted({r for r in rows if macro is not None and 0 <= r < len(macro.steps)})
        if macro is None or not rows:
            return
        value = (not macro.steps[rows[0]].enabled) if enabled is None else enabled
        self._checkpoint()
        for r in rows:
            macro.steps[r].enabled = value
        self._refresh_steps()
        self._after_macro_change()

    def set_steps_delay(self, rows: list[int], delay_ms: Optional[int]) -> None:
        macro = self.current_macro()
        rows = sorted({r for r in rows if macro is not None and 0 <= r < len(macro.steps)})
        if macro is None or not rows:
            return
        self._checkpoint()
        for r in rows:
            macro.steps[r].delay_after_ms = None if delay_ms is None else max(0, int(delay_ms))
        self._refresh_steps(select=rows)
        self._after_macro_change()

    def ask_steps_delay(self, rows: list[int]) -> None:
        macro = self.current_macro()
        if macro is None or not rows:
            return
        rows = [r for r in rows if 0 <= r < len(macro.steps)]
        if not rows:
            return
        n_waits = sum(isinstance(macro.steps[r], WaitStep) for r in rows)
        waits = "all" if n_waits == len(rows) else ("some" if n_waits else "none")
        dialog = DelayDialog(self, macro.default_delay_ms, macro.steps[rows[0]].delay_after_ms, waits=waits)
        if dialog.exec() == QDialog.Accepted:
            self.set_steps_delay(rows, dialog.value())
        dialog.deleteLater()

    # Portapapeles ---------------------------------------------------------------------
    def copy_steps(self, rows: list[int]) -> int:
        macro = self.current_macro()
        rows = sorted({r for r in rows if macro is not None and 0 <= r < len(macro.steps)})
        if macro is None or not rows:
            return 0
        payload = json.dumps({"macrotool": "steps", "version": 1,
                              "steps": [macro.steps[r].to_dict() for r in rows]}, ensure_ascii=False, indent=2)
        # Solo texto (JSON): con setText el QMimeData lo crea Qt en C++. Uno creado desde Python
        # queda en manos del portapapeles y Qt lo destruye al cerrar, cuando Python ya no existe:
        # el proceso terminaba con una violación de acceso. Pegar lee el JSON del texto.
        QApplication.clipboard().setText(payload)
        self.flash("Paso copiado" if len(rows) == 1 else f"{len(rows)} pasos copiados")
        return len(rows)

    def cut_steps(self, rows: list[int]) -> None:
        if self.copy_steps(rows):
            self.delete_steps(rows)

    @staticmethod
    def _steps_from_clipboard() -> list[Step]:
        mime = QApplication.clipboard().mimeData()
        if mime is None:
            return []
        raw = ""
        if mime.hasFormat(CLIPBOARD_MIME):
            raw = bytes(mime.data(CLIPBOARD_MIME)).decode("utf-8", "replace")
        elif mime.hasText():
            raw = mime.text()
        if not raw.strip():
            return []
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return []
        items = data.get("steps") if isinstance(data, dict) else data
        if isinstance(data, dict) and "type" in data:
            items = [data]
        if not isinstance(items, list):
            return []
        steps = []
        for raw_step in items:
            try:
                if isinstance(raw_step, dict):
                    steps.append(Step.from_dict(raw_step))
            except (ValueError, TypeError, KeyError):
                continue
        return steps

    def paste_steps(self) -> list[int]:
        steps = self._steps_from_clipboard()
        if not steps:
            self.flash("El portapapeles no contiene pasos", "warning")
            return []
        rows = self.insert_steps(steps)
        self.flash("Paso pegado" if len(steps) == 1 else f"{len(steps)} pasos pegados")
        return rows

    # Diálogos de pasos -----------------------------------------------------------------
    def add_step_dialog(self, step_type: str) -> None:
        macro = self.current_macro() or self.add_macro()
        step = edit_step(self, None, step_type, macro=macro, triggers=self.triggers)
        if step is not None:
            self.insert_steps([step])
            self.step_tree.setFocus()

    def edit_step_at(self, row: int) -> None:
        macro = self.current_macro()
        if macro is None or not 0 <= row < len(macro.steps):
            return
        step = edit_step(self, macro.steps[row], macro=macro, triggers=self.triggers)
        if step is not None:
            self.replace_step(row, step)
        self.step_tree.setFocus()

    def _edit_current_step(self) -> None:
        rows = self.step_tree.selected_rows()
        if rows:
            self.edit_step_at(rows[0])

    def _step_context_menu(self, pos) -> None:
        rows = self.step_tree.selected_rows()
        item = self.step_tree.itemAt(pos)
        if item is not None and self.step_tree.indexOfTopLevelItem(item) not in rows:
            self.step_tree.select_rows([self.step_tree.indexOfTopLevelItem(item)])
            rows = self.step_tree.selected_rows()
        menu = QMenu(self)
        insert = menu.addMenu(theme.icon("add", None, 16), "Insertar paso")
        for t in TYPE_ORDER:
            insert.addAction(theme.icon(theme.STEP_ICONS[t], theme.step_color(t), 16), TYPE_LABELS[t],
                             lambda tt=t: self.add_step_dialog(tt))
        if rows:
            menu.addSeparator()
            if len(rows) == 1:
                self.act_edit.setText("Editar…\tIntro")
                menu.addAction(self.act_edit)
            for act in (self.act_duplicate, self.act_copy, self.act_cut):
                menu.addAction(act)
        menu.addAction(self.act_paste)
        if rows:
            menu.addSeparator()
            menu.addAction(self.act_up)
            menu.addAction(self.act_down)
            menu.addSeparator()
            macro = self.current_macro()
            enabled = macro is not None and macro.steps[rows[0]].enabled
            self.act_toggle.setText("Desactivar\tEspacio" if enabled else "Activar\tEspacio")
            menu.addAction(self.act_toggle)
            menu.addAction(self.act_delay)
            menu.addSeparator()
            menu.addAction(self.act_delete)
        menu.exec(self.step_tree.viewport().mapToGlobal(pos))

    # ------------------------------------------------------------------ deshacer / rehacer
    def _checkpoint(self, key: Optional[str] = None, *, macro: Optional[Macro] = None) -> None:
        """Guarda un snapshot de la macro (la actual si no se indica) antes de modificarla.

        Cambios seguidos con la misma ``key`` (p. ej. girar un spinbox) se agrupan en uno.
        """
        macro = macro or self.current_macro()
        if macro is None:
            return
        now = time.monotonic()
        if key is not None and self._last_checkpoint == (macro.id, key) and now - self._last_checkpoint_t < COALESCE_SECONDS:
            self._last_checkpoint_t = now
            return
        stack = self._undo.setdefault(macro.id, [])
        stack.append(macro.to_dict())
        del stack[:-UNDO_LIMIT]
        self._redo[macro.id] = []
        self._last_checkpoint = (macro.id, key) if key is not None else None
        self._last_checkpoint_t = now
        self._update_undo_buttons()

    def can_undo(self) -> bool:
        macro = self.current_macro()
        return bool(macro and self._undo.get(macro.id))

    def can_redo(self) -> bool:
        macro = self.current_macro()
        return bool(macro and self._redo.get(macro.id))

    def undo(self) -> bool:
        macro = self.current_macro()
        if macro is None or not self._undo.get(macro.id):
            return False
        self._redo.setdefault(macro.id, []).append(macro.to_dict())
        self._restore_snapshot(macro, self._undo[macro.id].pop())
        self.flash("Deshecho")
        return True

    def redo(self) -> bool:
        macro = self.current_macro()
        if macro is None or not self._redo.get(macro.id):
            return False
        self._undo.setdefault(macro.id, []).append(macro.to_dict())
        self._restore_snapshot(macro, self._redo[macro.id].pop())
        self.flash("Rehecho")
        return True

    def _restore_snapshot(self, macro: Macro, snapshot: dict) -> None:
        restored = Macro.from_dict(snapshot)
        for f in fields(Macro):
            setattr(macro, f.name, getattr(restored, f.name))
        self._last_checkpoint = None
        rows = self.step_tree.selected_rows()
        self._load_macro_into_ui()
        self._refresh_steps(select=[r for r in rows if r < len(macro.steps)])
        self._refresh_macro_item(macro)
        self._update_bindings()
        self._schedule_save()

    def _update_undo_buttons(self) -> None:
        self.undo_button.setEnabled(self.can_undo())
        self.redo_button.setEnabled(self.can_redo())

    # ------------------------------------------------------------------ ejecución
    def run_selected(self) -> bool:
        macro = self.current_macro()
        if macro is None:
            return False
        if self._recording:
            self.flash("Detén la grabación antes de ejecutar", "warning")
            return False
        if not any(s.enabled for s in macro.steps):
            self.flash("La macro no tiene pasos activos", "warning")
            return False
        started = self.controller.run(macro.id)
        if started is False:
            self.flash("No se pudo iniciar: ya hay una macro en ejecución", "warning")
            return False
        self._is_running = True
        self._running_macro_id = macro.id
        self._update_run_controls()
        if self.settings.minimize_on_run and self._start_hooks:
            self.showMinimized()
        elif macro.start_delay_ms == 0 and self.isActiveWindow():
            self.flash("Consejo: usa el disparador en la aplicación de destino o un retardo inicial para "
                       "cambiar de ventana", "info", 7000)
        return True

    def stop_all(self) -> None:
        if self._recording:
            self.stop_recording(from_ui=True)
            return
        self.controller.stop()
        self.player.retry_pending_release()

    def toggle_pause(self) -> None:
        # El controlador es la referencia: el estado de la interfaz se deduce de eventos en cola.
        if self._is_running or self.controller.is_running:
            self.controller.toggle_pause()

    def _on_player_event(self, event: Any) -> None:
        kind = getattr(event, "kind", "")
        macro_id = getattr(event, "macro_id", "") or self._running_macro_id
        macro = self.get_macro(macro_id) if macro_id else None
        # "paused" también: puede ser el primer evento de una ejecución (pausa durante la cuenta atrás).
        if kind in ("countdown", "started", "step", "repeat", "paused", "resumed"):
            self._is_running = True
            self._running_macro_id = macro_id
        if kind == "countdown":
            seconds = max(1, math.ceil(getattr(event, "remaining_ms", 0) / 1000))
            self._set_status(f"Empieza en {seconds}…", theme.AMBER)
        elif kind == "started":
            self._is_paused = False
            self._set_status(f"Ejecutando «{macro.name}»…" if macro else "Ejecutando…", theme.GREEN)
            self.progress.setValue(0)
            self.progress.setVisible(True)
        elif kind == "repeat":
            self._repeat_info = (getattr(event, "repeat_index", 0), getattr(event, "total_repeats", 1))
        elif kind == "step":
            self._repeat_info = (getattr(event, "repeat_index", 0), getattr(event, "total_repeats", 1))
            self._highlight_step(macro_id, getattr(event, "step_index", -1))
            self._show_step_progress(macro, getattr(event, "step_index", -1))
        elif kind == "paused":
            self._is_paused = True
            self._set_status("Pausada", theme.AMBER)
        elif kind == "resumed":
            self._is_paused = False
            self._set_status("Ejecutando…", theme.GREEN)
        elif kind in ("finished", "stopped", "error"):
            self._is_running = False
            self._is_paused = False
            self._running_macro_id = None
            self._highlight_step(None, -1)
            self.progress.setVisible(False)
            self._set_status("Lista", theme.GREEN)
            message = getattr(event, "message", "")
            if kind == "finished" and message:  # p. ej. "La macro no tiene pasos activos"
                self.flash(f"«{macro.name}»: {message}" if macro else message, "warning", 6000)
            elif kind == "finished":
                self.flash(f"«{macro.name}» terminada" if macro else "Macro terminada")
            elif kind == "stopped":
                self.flash(message or "Macro detenida", "warning" if "emergencia" in message.lower() else "info")
            else:
                log.error("Error al ejecutar la macro: %s", message)
                self.flash(message or "Error al ejecutar la macro", "error", 10000)
            if self._pending_backend:
                self._apply_backend()
            if self.player.pending_release:
                self._release_retry.start()  # Windows rechazó soltar algo: se reintenta
        self._update_run_controls()

    def _retry_pending_release(self) -> None:
        """Reintenta soltar las teclas/botones que Windows no dejó soltar (UIPI, UAC...)."""
        if self.player.retry_pending_release():
            self._release_retry.stop()
            self.flash("Se soltaron las teclas que habían quedado pulsadas")

    def _highlight_step(self, macro_id: Optional[str], index: int) -> None:
        previous = self._running_step
        self._running_step = (macro_id, index) if macro_id and index >= 0 else None
        if previous == self._running_step:
            return
        tree = self.step_tree
        if previous and previous[0] == self._current_id and 0 <= previous[1] < tree.topLevelItemCount():
            tree.topLevelItem(previous[1]).setData(0, ROLE_RUNNING, False)
        if self._running_step and macro_id == self._current_id and 0 <= index < tree.topLevelItemCount():
            item = tree.topLevelItem(index)
            item.setData(0, ROLE_RUNNING, True)
            tree.scrollToItem(item)

    def _show_step_progress(self, macro: Optional[Macro], index: int) -> None:
        if macro is None:
            return
        active = [i for i, s in enumerate(macro.steps) if s.enabled]
        n = max(1, len(active))
        k = active.index(index) + 1 if index in active else min(n, index + 1)
        rep, total = self._repeat_info
        total_text = "∞" if not total else str(total)
        self._set_status(f"Ejecutando paso {k}/{n} · repetición {rep + 1}/{total_text}", theme.GREEN)
        if total:
            value = ((rep * n) + k) / (total * n)
        else:
            value = k / n
        self.progress.setVisible(True)
        self.progress.setValue(int(max(0.0, min(1.0, value)) * 1000))

    def _update_run_controls(self) -> None:
        has = self.current_macro() is not None
        running, recording = self._is_running, self._recording
        self.run_button.setEnabled(has and not running and not recording)
        self.stop_button.setEnabled(running or recording)
        self.pause_button.setEnabled(running)
        self._set_button_text(self.pause_button, "Reanudar" if self._is_paused else "Pausa")
        self.pause_button.setIcon(theme.icon("play" if self._is_paused else "pause", theme.AMBER, 16))
        self.record_button.setEnabled((has or recording) and not running)
        self._set_button_text(self.record_button, "Detener grabación" if recording else "Grabar")
        self.record_button.setIcon(theme.icon("stop" if recording else "record",
                                              "#ffffff" if recording else theme.RED, 16))
        kind = "recording" if recording else ""
        if self.record_button.property("kind") != kind:
            self.record_button.setProperty("kind", kind)
            repolish(self.record_button)
        if self._tray is not None:
            self._tray_stop.setEnabled(running or recording)
        # Mientras corre una macro, su atajo de parada funciona incluso con un campo de captura
        # activo (que suspende los disparadores): p. ej. el pad del diálogo «Pulsación».
        set_exceptions = getattr(self.triggers, "set_suspend_exceptions", None)
        if set_exceptions is not None:
            try:
                set_exceptions(("control:stop",) if running else ())
            except Exception:  # noqa: BLE001
                log.exception("No se pudo actualizar el atajo de parada")

    def _set_button_text(self, button: QPushButton, text: str) -> None:
        self._button_texts[button] = text
        button.setText("" if self._compact else text)

    def _update_compact(self) -> None:
        """Con la zona central estrecha, los botones grandes pasan a ser sólo iconos."""
        compact = self.center_panel.width() < 540
        if compact == self._compact:
            return
        self._compact = compact
        for button, text in self._button_texts.items():
            button.setText("" if compact else text)
        for t, button in self.add_buttons.items():
            if t == "press":
                continue
            if compact:
                button.setFixedWidth(40)
            else:
                button.setMinimumWidth(0)
                button.setMaximumWidth(16_777_215)
        self.steps_hint.setText("" if compact else STEPS_HINT)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802
        if obj is getattr(self, "center_panel", None) and event.type() == QEvent.Resize:
            self._update_compact()
        return super().eventFilter(obj, event)

    # ------------------------------------------------------------------ grabación
    def toggle_recording(self, from_ui: bool = False) -> None:
        if self._recording:
            self.stop_recording(from_ui=from_ui)
        else:
            self.start_recording()

    def start_recording(self) -> bool:
        if self.recorder is None:
            self.flash("La grabación no está disponible: no se pudieron instalar los hooks de entrada.", "error")
            return False
        if self._is_running or self.controller.is_running:
            self.flash("Espera a que termine la macro o detenla antes de grabar", "warning")
            return False
        if self.current_macro() is None:
            self.add_macro("Grabación")
        try:
            self.recorder.start(record_moves=bool(self.settings.record_mouse_moves), on_event=self._on_record_event)
        except Exception as exc:  # noqa: BLE001
            log.exception("No se pudo iniciar la grabación")
            self.flash(f"No se pudo iniciar la grabación: {exc}", "error")
            return False
        self._recording = True
        self._rec_count = 0
        self._rec_return_t = None
        if self.triggers is not None:
            self.triggers.set_macro_triggers_enabled(False)
        self._rec_timer.start()
        self._update_record_status()
        self._update_run_controls()
        if self.settings.minimize_on_run:
            self._rec_minimized = True
            self.showMinimized()
        return True

    def _on_record_event(self, _event: Any) -> None:
        # Hilo del hook: sólo contamos; la barra de estado se refresca con un temporizador.
        self._rec_count += 1

    def _update_record_status(self) -> None:
        if self._recording:
            hk = safe_format_combo(combo_tokens(self.settings.hotkey_record)) if self.settings.hotkey_record else ""
            extra = f" · {hk} para terminar" if hk else ""
            self._set_status(f"Grabando… ({self._rec_count} eventos){extra}", theme.RED)

    def stop_recording(self, from_ui: bool = False) -> list[Step]:
        if not self._recording or self.recorder is None:
            return []
        self._rec_timer.stop()
        # Detenida desde la ventana o la bandeja: lo hecho para volver a MacroTool (clic en la
        # barra de tareas o en el icono, Alt+Tab...) y para pulsar «Detener» no es de la macro.
        ui_since = self._rec_return_t if from_ui else None
        self._rec_return_t = None
        held: frozenset[str] = frozenset()
        try:
            # También recorta los clics finales sobre ventanas de MacroTool.
            events = self.recorder.stop(ui_since=ui_since)
            held = frozenset(getattr(self.recorder, "held_at_stop", ()) or ())
        except Exception:  # noqa: BLE001
            log.exception("Error al detener la grabación")
            events = []
        self._recording = False
        if self.triggers is not None:
            self.triggers.set_macro_triggers_enabled(bool(self.settings.triggers_enabled))
        steps: list[Step] = []
        try:
            from ..recorder import events_to_steps

            steps = events_to_steps(events, record_timing=bool(self.settings.record_timing),
                                    record_click_positions=bool(self.settings.record_click_positions),
                                    min_delay_ms=0, held_at_end=held)
        except Exception:  # noqa: BLE001
            log.exception("No se pudieron convertir los eventos grabados")
            self.flash("No se pudieron convertir los eventos grabados", "error")
        self._set_status("Lista", theme.GREEN)
        if steps:
            self.insert_steps(steps)
            self.flash("Se añadió 1 paso grabado" if len(steps) == 1 else f"Se añadieron {len(steps)} pasos grabados")
        else:
            self.flash("No se grabó ninguna acción", "warning")
        if self._rec_minimized:
            self._rec_minimized = False
            self.show_window()
        self._update_run_controls()
        return steps

    # ------------------------------------------------------------------ disparadores
    def _on_trigger(self, binding_id: str, pressed: bool) -> None:
        if binding_id.startswith("macro:"):
            if not self._recording:
                self.controller.handle_trigger(binding_id[len("macro:"):], pressed)
            return
        if not pressed:
            return
        action = binding_id.split(":", 1)[-1]
        if action == "run_selected":
            # Como toggle_run: si corre ESTA macro se detiene; si no, se ejecuta (y si corría
            # otra, el controlador la detiene antes de empezar ésta).
            macro = self.current_macro()
            if macro is not None and not self._recording:
                if self.controller.running_macro_id == macro.id:
                    self.controller.stop()
                else:
                    self.run_selected()
        elif action == "stop":
            if self._recording:
                self.stop_recording()
            else:
                self.controller.stop()
                self.player.retry_pending_release()
        elif action == "record":
            self.toggle_recording()
        elif action == "enable":
            self.set_triggers_enabled(not self.settings.triggers_enabled)
        elif action == "pause":
            self.toggle_pause()

    def _binding_specs(self) -> list[tuple[str, str, bool, bool]]:
        """(id, combinación, bloquear, es_control) de todos los disparadores y atajos."""
        specs: list[tuple[str, str, bool, bool]] = []
        for action, field_name, _label in CONTROL_HOTKEYS:
            combo = getattr(self.settings, field_name) or ""
            if combo:
                specs.append((f"control:{action}", combo, True, True))
        for macro in self.macros:
            if macro.trigger and macro.enabled:
                specs.append((f"macro:{macro.id}", macro.trigger, bool(macro.block_trigger), False))
        return specs

    def _update_bindings(self) -> None:
        errors: dict[str, str] = {}
        if self.triggers is not None:
            try:
                from ..hooks import Binding

                bindings = [Binding(id=i, combo=c, block=b, control=ctl) for i, c, b, ctl in self._binding_specs()]
                errors = dict(self.triggers.set_bindings(bindings) or {})
            except Exception:  # noqa: BLE001
                log.exception("No se pudieron registrar los disparadores")
            try:
                self.triggers.set_macro_triggers_enabled(bool(self.settings.triggers_enabled) and not self._recording)
                self.triggers.set_ignore_when_own_foreground(bool(self.settings.ignore_triggers_when_focused))
            except Exception:  # noqa: BLE001
                log.exception("No se pudo actualizar el estado de los disparadores")
        else:
            self._install_fallback_shortcuts()
        changed = errors != self._binding_errors
        self._binding_errors = errors
        if changed:
            for macro in self.macros:
                self._refresh_macro_item(macro)
        macro = self.current_macro()
        if macro is not None:
            self.trigger_edit.set_error(errors.get(f"macro:{macro.id}"))
        self.warning_button.setVisible(bool(errors))
        if errors:
            n = len(errors)
            self.warning_button.setToolTip(
                ("1 disparador o atajo no se pudo registrar" if n == 1 else f"{n} disparadores o atajos no se pudieron registrar")
                + " (clic para ver detalles)")

    def binding_errors(self) -> dict[str, str]:
        return dict(self._binding_errors)

    def _show_binding_errors(self) -> None:
        if not self._binding_errors:
            return
        lines = []
        for binding_id, message in self._binding_errors.items():
            if binding_id.startswith("macro:"):
                macro = self.get_macro(binding_id[6:])
                who = f"Macro «{macro.name}»" if macro else "Macro"
            else:
                action = binding_id.split(":", 1)[-1]
                who = f"Atajo «{HOTKEY_SHORT.get(action, action)}»"
            lines.append(f"• {who}: {message}")
        QMessageBox.warning(self, "Disparadores con problemas", "\n".join(lines))

    def _install_fallback_shortcuts(self) -> None:
        """Sin hooks globales, los atajos de control funcionan al menos dentro de la ventana."""
        for act in self._fallback_actions:
            self.removeAction(act)
            act.deleteLater()
        self._fallback_actions = []
        for action, field_name, _label in CONTROL_HOTKEYS:
            seq = _combo_to_keysequence(getattr(self.settings, field_name) or "")
            if seq is None:
                continue
            act = QAction(self)
            act.setShortcut(seq)
            act.setShortcutContext(Qt.WindowShortcut)
            act.triggered.connect(lambda _=False, a=action: self._on_trigger(f"control:{a}", True))
            self.addAction(act)
            self._fallback_actions.append(act)

    def set_triggers_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        changed = enabled != self.settings.triggers_enabled
        self.settings.triggers_enabled = enabled
        self._update_global_switch()
        if self.triggers is not None:
            try:
                self.triggers.set_macro_triggers_enabled(enabled and not self._recording)
            except Exception:  # noqa: BLE001
                log.exception("No se pudo cambiar el estado de los disparadores")
        stopped = ""
        if not enabled:
            # «Macros en pausa» significa que no hay nada en marcha: además, con los disparadores
            # apagados el de una macro en modo Alternar ya no podría detenerla.
            stopped = self._stop_running_macro("Macros desactivadas")
        if changed:
            self._save_settings()
            if enabled:
                self.flash("Macros activadas")
            else:
                self.flash("Macros desactivadas: los disparadores no harán nada"
                           + (f" (se detuvo «{stopped}»)" if stopped else ""))

    def _stop_running_macro(self, reason: str, macro_id: Optional[str] = None) -> str:
        """Detiene la macro en curso (o sólo ``macro_id`` si se indica). Devuelve su nombre o ""."""
        running = self.controller.running_macro_id
        if running is None or (macro_id is not None and running != macro_id):
            return ""
        self.controller.stop(reason)
        macro = self.get_macro(running)
        return macro.name if macro is not None else "la macro"

    def _update_global_switch(self) -> None:
        on = bool(self.settings.triggers_enabled)
        self.global_switch.blockSignals(True)
        self.global_switch.setChecked(on)
        self.global_switch.blockSignals(False)
        self.global_card.setProperty("active", "true" if on else "false")
        repolish(self.global_card)
        self.global_icon.setPixmap(theme.icon("bolt", theme.GREEN if on else theme.TEXT_FAINT, 20).pixmap(20, 20))
        hk = safe_format_combo(combo_tokens(self.settings.hotkey_enable)) if self.settings.hotkey_enable else ""
        if hk:
            self.global_subtitle.setText(f"{hk} para {'desactivar' if on else 'activar'}")
        else:
            self.global_subtitle.setText("Escuchando disparadores" if on else "Nada se dispara")
        self.global_title.setText("Macros activas" if on else "Macros en pausa")
        if self._tray is not None:
            self._tray_enable.blockSignals(True)
            self._tray_enable.setChecked(on)
            self._tray_enable.blockSignals(False)
            self._tray.setToolTip(f"{APP_NAME} — {'macros activas' if on else 'macros desactivadas'}")

    # ------------------------------------------------------------------ ajustes
    def _set_setting(self, field_name: str, value: Any) -> None:
        if getattr(self.settings, field_name) == value:
            return
        setattr(self.settings, field_name, value)
        self._save_settings()

    def open_settings(self) -> None:
        new = edit_settings(self, self.settings, triggers=self.triggers)
        if new is not None:
            self.apply_settings(new)

    def apply_settings(self, new: Settings) -> None:
        old = self.settings
        new.last_macro_id = old.last_macro_id
        new.window_geometry = old.window_geometry
        new.triggers_enabled = old.triggers_enabled
        self.settings = new
        if ((new.use_scancodes != old.use_scancodes or new.use_hid_backend != old.use_hid_backend)
                and not self._backend_given):
            self._pending_backend = True
            if not self._is_running:
                self._apply_backend()
        if new.always_on_top != old.always_on_top:
            self._apply_window_flags()
        for act, field_name in ((self.act_rec_moves, "record_mouse_moves"),
                                (self.act_rec_positions, "record_click_positions"),
                                (self.act_rec_timing, "record_timing"),
                                (self.act_rec_minimize, "minimize_on_run")):
            act.blockSignals(True)
            act.setChecked(bool(getattr(new, field_name)))
            act.blockSignals(False)
        self._update_hotkey_hints()
        self._update_global_switch()
        self._update_bindings()
        self._save_settings()
        self.flash("Ajustes guardados")

    def _apply_backend(self) -> None:
        self._pending_backend = False
        backend = self._make_backend()
        if backend is not None:
            try:
                self.player.set_backend(backend)
            except Exception:  # noqa: BLE001
                log.exception("No se pudo cambiar el backend")
        if getattr(self, "_backend_note", None):
            self.flash(self._backend_note)

    def _apply_window_flags(self) -> None:
        on_top = bool(self.settings.always_on_top)
        if bool(self.windowFlags() & Qt.WindowStaysOnTopHint) == on_top:
            return
        visible = self.isVisible()
        self.setWindowFlag(Qt.WindowStaysOnTopHint, on_top)
        if visible:
            self.show()

    def _update_hotkey_hints(self) -> None:
        parts = []
        tips = {}
        for action, field_name, _label in CONTROL_HOTKEYS:
            combo = getattr(self.settings, field_name) or ""
            if combo:
                text = safe_format_combo(combo_tokens(combo))
                tips[action] = text
                if action != "pause":
                    parts.append(f"{text} {HOTKEY_SHORT[action]}")
        self.hotkey_hint.setText("  ·  ".join(parts))
        self.run_button.setToolTip("Ejecutar la macro seleccionada" + (f" ({tips['run_selected']})" if "run_selected" in tips else ""))
        self.stop_button.setToolTip("Detener la macro o la grabación" + (f" ({tips['stop']})" if "stop" in tips else ""))
        self.pause_button.setToolTip("Pausar / reanudar" + (f" ({tips['pause']})" if "pause" in tips else ""))
        self.record_button.setToolTip("Grabar tus acciones como pasos" + (f" ({tips['record']})" if "record" in tips else ""))

    # ------------------------------------------------------------------ estado y mensajes
    def _set_status(self, text: str, color: str) -> None:
        self.status_label.setText(text)
        if color != self._status_color:  # volver a aplicar QSS es caro: sólo si cambia el color
            self._status_color = color
            self.status_dot.setStyleSheet(f"QLabel#StatusDot {{ background: {color}; border-radius: 4px; padding: 0; }}")

    def status_text(self) -> str:
        return self.status_label.text()

    def flash(self, text: str, level: str = "info", ms: int = 4000) -> None:
        """Mensaje temporal en la barra de estado (info / warning / error)."""
        colors = {"info": theme.TEXT_DIM, "warning": theme.AMBER, "error": theme.RED}
        icons = {"info": "info", "warning": "warning", "error": "warning"}
        color = colors.get(level, theme.TEXT_DIM)
        self.message_label.setText(text)
        self.message_label.setStyleSheet(f"color: {color};")
        self.message_icon.setPixmap(theme.icon(icons.get(level, "info"), color, 14).pixmap(14, 14))
        self.message_icon.setVisible(True)
        self._flash_timer.start(ms)

    def message_text(self) -> str:
        return self.message_label.text()

    def _clear_message(self) -> None:
        self.message_label.setText("")
        self.message_icon.setVisible(False)

    # ------------------------------------------------------------------ persistencia
    def _schedule_save(self) -> None:
        self._autosave.start()

    def save_now(self) -> bool:
        self._autosave.stop()
        if self._storage_error:
            return False
        try:
            storage.save_macros(self.macros)
            return True
        except Exception as exc:  # noqa: BLE001
            log.exception("No se pudieron guardar las macros")
            self.flash(f"No se pudieron guardar las macros: {exc}", "error", 10000)
            return False

    def save_all(self) -> bool:
        """Ctrl+S: guarda macros y ajustes al momento e informa del resultado real."""
        macros_ok = self.save_now()  # si falla al escribir, ya muestra el motivo
        settings_ok = self._save_settings()
        if macros_ok and settings_ok:
            self.flash("Guardado")
        elif self._storage_error:
            self.flash(self._storage_error, "error", 10000)
        elif macros_ok:
            self.flash("No se pudieron guardar los ajustes", "error", 10000)
        return macros_ok and settings_ok

    def _save_settings(self) -> bool:
        try:
            storage.save_settings(self.settings)
            return True
        except Exception:  # noqa: BLE001
            log.exception("No se pudieron guardar los ajustes")
            return False

    def _restore_geometry(self) -> None:
        if self.settings.window_geometry:
            try:
                self.restoreGeometry(QByteArray.fromBase64(self.settings.window_geometry.encode("ascii")))
            except Exception:  # noqa: BLE001
                log.debug("Geometría guardada no válida")

    # ------------------------------------------------------------------ bandeja y cierre
    def _create_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        tray = QSystemTrayIcon(theme.app_icon(), self)
        menu = QMenu()
        menu.addAction(theme.icon("list", None, 16), "Mostrar MacroTool", self.show_window)
        self._tray_enable = menu.addAction("Macros activas")
        self._tray_enable.setCheckable(True)
        self._tray_enable.setChecked(bool(self.settings.triggers_enabled))
        self._tray_enable.toggled.connect(self.set_triggers_enabled)
        self._tray_stop = menu.addAction(theme.icon("stop", theme.RED, 16), "Detener macro", self.stop_all)
        menu.addSeparator()
        if not self._is_elevated():
            menu.addAction("Reiniciar como administrador", self.restart_as_admin)
        menu.addAction(theme.icon("power", None, 16), "Salir", self.quit_app)
        tray.setContextMenu(menu)
        tray.activated.connect(self._on_tray_activated)
        menu.aboutToShow.connect(self._note_ui_return)
        # QMenu se oculta ANTES de emitir triggered: se olvida después, si no se eligió «Detener».
        menu.aboutToHide.connect(lambda: QTimer.singleShot(0, self._forget_ui_return))
        self._tray_menu = menu
        self._tray = tray
        tray.setToolTip(APP_NAME)
        tray.show()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self._note_ui_return()
            self.show_window()

    def show_window(self) -> None:
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()

    def quit_app(self) -> None:
        self._quitting = True
        self.close()

    # ------------------------------------------------------------------ elevación
    def _is_elevated(self) -> bool:
        """True si MacroTool ya se ejecuta como administrador (cacheado)."""
        val = getattr(self, "_elevated", None)
        if val is None:
            try:
                from .. import elevation
                val = elevation.is_elevated()
            except Exception:  # noqa: BLE001
                val = False
            self._elevated = val
        return val

    def restart_as_admin(self) -> None:
        """Relanza MacroTool con permisos de administrador y cierra esta instancia."""
        try:
            from .. import elevation
            launched = elevation.relaunch_as_admin()
        except Exception:  # noqa: BLE001
            log.exception("No se pudo reiniciar como administrador")
            launched = False
        if launched:
            self._quitting = True
            self.quit_app()  # libera el mutex de instancia única para la instancia elevada
        else:
            self.flash("No se pudo reiniciar como administrador (¿cancelaste el aviso de Windows?)", "error")

    def _check_elevation(self) -> None:
        """Aviso único, no intrusivo, cuando una app de mayor integridad toma el primer plano."""
        if self._elev_warned:
            return
        try:
            from .. import elevation
            needs = elevation.foreground_needs_admin()
        except Exception:  # noqa: BLE001
            return
        if not needs:
            return
        self._elev_warned = True
        if self._elev_timer is not None:
            self._elev_timer.stop()
        msg = ("Una app que se ejecuta como administrador está en primer plano. Para que los "
               "disparadores de MacroTool funcionen ahí, reinícialo como administrador "
               "(menú de la bandeja del sistema).")
        tray = getattr(self, "_tray", None)
        if tray is not None:
            try:
                tray.showMessage(APP_NAME, msg, QSystemTrayIcon.Warning, 9000)
                return
            except Exception:  # noqa: BLE001
                pass
        self.flash(msg, "error", 9000)

    def _prompt_elevation_on_startup(self) -> None:
        """Aviso al abrir sin administrador: reiniciar elevado ahora y/o hacerlo siempre."""
        if self._is_elevated() or bool(self.settings.always_admin):
            return
        # Este aviso sustituye al de la bandeja: que no salgan los dos.
        self._elev_warned = True
        if self._elev_timer is not None:
            self._elev_timer.stop()
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle(APP_NAME)
        box.setText("MacroTool no se está ejecutando como administrador.")
        box.setInformativeText(
            "Los disparadores y las macros no funcionarán dentro de aplicaciones que se ejecuten "
            "como administrador. Reinícialo como administrador para que funcionen en todas partes.")
        always = QCheckBox("Abrir siempre como administrador a partir de ahora")
        box.setCheckBox(always)
        restart = box.addButton("Reiniciar como administrador", QMessageBox.AcceptRole)
        box.addButton("Ahora no", QMessageBox.RejectRole)
        box.exec()
        if always.isChecked():
            self._set_setting("always_admin", True)
        if box.clickedButton() is restart:
            self.restart_as_admin()

    def showEvent(self, event) -> None:  # noqa: N802
        theme.enable_dark_titlebar(self)
        super().showEvent(event)

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802
        if event.type() == QEvent.ActivationChange and self._recording:
            if self.isActiveWindow():
                self._note_ui_return()
            else:
                self._forget_ui_return()  # volvió a otra aplicación: sigue grabando
        super().changeEvent(event)

    def _note_ui_return(self) -> None:
        if self._recording and self._rec_return_t is None:
            self._rec_return_t = time.perf_counter()

    def _forget_ui_return(self) -> None:
        tray_menu = getattr(self, "_tray_menu", None)
        if not self.isActiveWindow() and not (tray_menu is not None and tray_menu.isVisible()):
            self._rec_return_t = None

    def on_session_end(self) -> None:
        """Windows se apaga o se cierra la sesión (``commitDataRequest``): guardar YA.

        Tras WM_ENDSESSION Windows puede terminar el proceso en cualquier momento, así que no
        se espera a ``shutdown()``. Además, el cierre de la ventana ya no se desvía a la bandeja
        (ignorarlo cancelaría la salida de Qt).
        """
        self._quitting = True
        self._commit_open_editors()
        if self.save_on_exit:
            self._store_ui_state()
            self.save_now()
            self._save_settings()

    def _store_ui_state(self) -> None:
        """Geometría y macro seleccionada en los ajustes (en memoria)."""
        self.settings.window_geometry = bytes(self.saveGeometry().toBase64()).decode("ascii")
        if self._current_id:
            self.settings.last_macro_id = self._current_id

    def _commit_open_editors(self) -> None:
        """Aplica un renombrado en curso en la lista de macros (sólo se aplica al perder el foco)."""
        ml = self.macro_list
        for i in range(ml.topLevelItemCount()):
            editor = ml.indexWidget(ml.model().index(i, 0))
            if editor is not None:
                try:
                    ml.commitData(editor)
                    ml.closeEditor(editor, QAbstractItemDelegate.NoHint)
                except RuntimeError:  # el editor ya se destruyó
                    pass

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if (not self._quitting and not self._shut_down and self.settings.minimize_to_tray
                and self._tray is not None and self._tray.isVisible()):
            event.ignore()
            self.hide()
            if self.save_on_exit:
                self._store_ui_state()  # por si Windows se apaga con la app en la bandeja
                self._save_settings()
            if not self._tray_notified:
                self._tray_notified = True
                self._tray.showMessage(APP_NAME, "MacroTool sigue funcionando en la bandeja del sistema. "
                                       "Haz clic en el icono para abrirlo o usa «Salir» en su menú.",
                                       theme.app_icon(), 5000)
            return
        self.shutdown()
        event.accept()
        QApplication.quit()  # la app no se cierra sola (quitOnLastWindowClosed=False por la bandeja)

    def shutdown(self) -> None:
        """Detiene todo y guarda. Idempotente."""
        if self._shut_down:
            return
        self._shut_down = True
        self._commit_open_editors()
        if self._recording and self.recorder is not None:
            try:
                self.recorder.stop()
            except Exception:  # noqa: BLE001
                log.exception("Error al detener la grabación")
            self._recording = False
        try:
            self.controller.stop("Cerrando MacroTool")
            self.player.wait(1.5)
            self._release_retry.stop()
            self.player.retry_pending_release()  # último intento de soltar lo que Windows rechazó
        except Exception:  # noqa: BLE001
            log.exception("Error al detener la macro")
        if self.hook is not None:
            try:
                self.hook.stop()
            except Exception:  # noqa: BLE001
                log.exception("Error al detener los hooks")
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self._input_guard)
        self._autosave.stop()
        if self.save_on_exit:
            self._store_ui_state()
            self.save_now()
            self._save_settings()
        if self._tray is not None:
            self._tray.hide()


def _safe_filename(name: str) -> str:
    cleaned = "".join("_" if c in '<>:"/\\|?*' or ord(c) < 32 else c for c in name).strip(" .")
    return cleaned or "macro"
