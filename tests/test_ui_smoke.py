"""Pruebas de humo de la interfaz.

Qt sin ventanas (``QT_QPA_PLATFORM=offscreen``), datos en una carpeta temporal
(``MACROTOOL_DATA_DIR``), ``MainWindow(start_hooks=False)`` (sin hooks globales ni bandeja) y un
backend falso: nunca se inyecta ninguna pulsación ni clic real.
"""
from __future__ import annotations

import os

os.environ["QT_QPA_PLATFORM"] = "offscreen"  # antes de importar Qt: nada de ventanas visibles

import subprocess  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, Callable  # noqa: E402

import pytest  # noqa: E402

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QFocusEvent, QKeyEvent, QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from macrotool.model import Macro, PressStep, Settings, TextStep, WaitStep, example_macro  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


# --- Utilidades ---------------------------------------------------------------------------
class FakeBackend:
    """Backend de entrada falso: sólo registra las llamadas."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self._lock = threading.Lock()

    def _add(self, *call: Any) -> None:
        with self._lock:
            self.calls.append(call)

    def press(self, tokens: list[str]) -> None:
        self._add("press", list(tokens))

    def release(self, tokens: list[str]) -> None:
        self._add("release", list(tokens))

    def type_char(self, ch: str) -> None:
        self._add("type", ch)

    def move_to(self, x: int, y: int) -> None:
        self._add("move_to", x, y)

    def move_rel(self, dx: int, dy: int) -> None:
        self._add("move_rel", dx, dy)

    def scroll(self, notches: int, horizontal: bool = False) -> None:
        self._add("scroll", notches, horizontal)

    def cursor_pos(self) -> tuple[int, int]:
        return (500, 500)

    def screen_size(self) -> tuple[int, int]:
        return (1920, 1080)

    def release_all(self) -> None:
        self._add("release_all")


class FakeTriggers:
    """Sustituto de TriggerManager para comprobar la suspensión durante la captura."""

    def __init__(self) -> None:
        self.suspended_calls: list[bool] = []

    def set_suspended(self, value: bool) -> None:
        self.suspended_calls.append(value)


def wait_until(app: QApplication, predicate: Callable[[], bool], timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    app.processEvents()
    return predicate()


def key_event(kind: QEvent.Type, key: Qt.Key, vk: int, scan: int = 0) -> QKeyEvent:
    return QKeyEvent(kind, key, Qt.NoModifier, scan, vk, 0, "", False, 1)


def mouse_event(kind: QEvent.Type, button: Qt.MouseButton) -> QMouseEvent:
    pos = QPointF(20, 20)
    buttons = button if kind != QEvent.MouseButtonRelease else Qt.NoButton
    return QMouseEvent(kind, pos, pos, button, buttons, Qt.NoModifier)


# --- Fixtures -----------------------------------------------------------------------------
@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance() or QApplication([])
    from macrotool.ui import theme

    theme.apply_theme(app)
    return app


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("MACROTOOL_DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def make_window(qapp: QApplication, data_dir: Path):
    from macrotool.ui.main_window import MainWindow

    windows = []

    def factory(backend: FakeBackend | None = None):
        win = MainWindow(start_hooks=False, backend=backend or FakeBackend())
        windows.append(win)
        return win

    yield factory
    for win in windows:
        win._quitting = True
        win.close()
    qapp.processEvents()


def select_example(win) -> Macro:
    """Añade (o reutiliza) la macro de ejemplo del usuario y la selecciona."""
    macro = example_macro()
    macro.default_delay_ms = 10  # que la prueba sea rápida
    macro.humanize = False  # cada combinación en un único lote: secuencia exacta
    win.macros.append(macro)
    win._populate_macro_list(select=macro.id)
    assert win.current_macro() is macro
    return macro


# --- Ventana principal --------------------------------------------------------------------
def test_window_builds_without_hooks(make_window, qapp):
    win = make_window()
    assert win.hook is None and win.triggers is None and win.recorder is None
    assert win.macros, "el primer arranque crea macros de ejemplo"
    macro = win.current_macro()
    assert macro is not None
    assert win.step_tree.topLevelItemCount() == len(macro.steps)
    assert win.macro_list.topLevelItemCount() == len(win.macros)
    assert win.status_text() == "Lista"
    assert win.name_edit.text() == macro.name
    # Bindings calculados aunque no haya hooks: atajos de control + disparadores de macros.
    ids = {spec[0] for spec in win._binding_specs()}
    assert {"control:run_selected", "control:stop", "control:record", "control:enable"} <= ids


def test_steps_add_edit_delete_reorder_undo_redo(make_window):
    win = make_window()
    macro = win.add_macro("Prueba")
    assert win.current_macro() is macro and macro.steps == []

    rows = win.insert_steps([PressStep(inputs=["a"]), PressStep(inputs=["b"]), WaitStep(ms=10)])
    assert rows == [0, 1, 2]
    assert win.step_tree.topLevelItemCount() == 3

    def names() -> list[str]:
        return [("wait" if isinstance(s, WaitStep) else "+".join(s.inputs)) for s in macro.steps]

    win.replace_step(1, PressStep(inputs=["ctrl", "c"]))
    assert names() == ["a", "ctrl+c", "wait"]

    win.shift_steps([2], -1)  # Ctrl+↑
    assert names() == ["a", "wait", "ctrl+c"]
    assert win.step_tree.selected_rows() == [1]

    win.move_steps([0], 3)  # arrastrar el primero al final
    assert names() == ["wait", "ctrl+c", "a"]

    win.delete_steps([1])
    assert names() == ["wait", "a"]
    assert win.step_tree.topLevelItemCount() == 2

    assert win.undo()
    assert names() == ["wait", "ctrl+c", "a"]
    assert win.undo()
    assert names() == ["a", "wait", "ctrl+c"]
    assert win.redo()
    assert names() == ["wait", "ctrl+c", "a"]
    assert win.step_tree.topLevelItemCount() == 3

    win.set_steps_enabled([0], False)
    assert macro.steps[0].enabled is False
    from macrotool.ui.widgets import ROLE_DELAY, ROLE_ENABLED

    assert win.step_tree.topLevelItem(0).data(0, ROLE_ENABLED) is False
    win.set_steps_delay([1], 250)
    assert macro.steps[1].delay_after_ms == 250
    assert win.step_tree.topLevelItem(1).data(0, ROLE_DELAY) == "250 ms"

    win.duplicate_steps([2])
    assert names() == ["wait", "ctrl+c", "a", "a"]
    assert macro.steps[2] is not macro.steps[3]

    # Copiar / pegar (JSON en el portapapeles).
    win.step_tree.select_rows([1])
    assert win.copy_steps([0, 1]) == 2
    pasted = win.paste_steps()
    assert pasted == [2, 3]
    assert names() == ["wait", "ctrl+c", "wait", "ctrl+c", "a", "a"]

    # Todo vuelve atrás con deshacer.
    while win.undo():
        pass
    assert macro.steps == []
    assert not win.can_undo() and win.can_redo()


def test_step_list_shortcuts_and_drag_drop(make_window, qapp):
    from PySide6.QtCore import QMimeData
    from PySide6.QtGui import QDropEvent
    from PySide6.QtTest import QTest

    win = make_window()
    win.resize(1180, 740)
    win.show()  # plataforma offscreen: no aparece nada en pantalla
    win.activateWindow()
    macro = win.add_macro("Atajos")
    win.insert_steps([PressStep(inputs=[c]) for c in "abcd"])
    tree = win.step_tree
    tree.setFocus()
    qapp.processEvents()

    def names() -> str:
        return "".join(s.inputs[0] for s in macro.steps)

    tree.select_rows([3])
    QTest.keyClick(tree, Qt.Key_Up, Qt.ControlModifier)
    assert names() == "abdc" and tree.selected_rows() == [2]
    QTest.keyClick(tree, Qt.Key_Down, Qt.ControlModifier)
    assert names() == "abcd"
    QTest.keyClick(tree, Qt.Key_Delete)
    assert names() == "abc"
    QTest.keyClick(win, Qt.Key_Z, Qt.ControlModifier)
    assert names() == "abcd"
    tree.select_rows([0, 1])
    QTest.keyClick(tree, Qt.Key_C, Qt.ControlModifier)
    QTest.keyClick(tree, Qt.Key_V, Qt.ControlModifier)
    assert names() == "ababcd"
    while win.undo():
        pass
    win.insert_steps([PressStep(inputs=[c]) for c in "abcd"])
    qapp.processEvents()

    class FakeDrop(QDropEvent):
        def source(self):  # el arrastre viene de la propia lista
            return tree

    def drop_at(point: QPointF) -> None:
        event = FakeDrop(point, Qt.MoveAction | Qt.CopyAction, QMimeData(), Qt.LeftButton, Qt.NoModifier)
        tree.dropEvent(event)
        assert event.dropAction() == Qt.CopyAction  # Qt no debe borrar nada por su cuenta
        qapp.processEvents()

    tree.select_rows([0])
    last = tree.visualItemRect(tree.topLevelItem(3))
    drop_at(QPointF(last.center().x(), last.bottom() - 2))  # debajo del último
    assert names() == "bcda"
    tree.select_rows([3])
    first = tree.visualItemRect(tree.topLevelItem(0))
    drop_at(QPointF(first.center().x(), first.top() + 3))  # encima del primero
    assert names() == "abcd"
    assert tree.topLevelItemCount() == 4


def test_macro_panel_updates_macro_and_bindings(make_window):
    win = make_window()
    macro = win.add_macro("Config")
    win.trigger_edit._commit_combo("ctrl+1")  # como si se capturase Ctrl+1
    assert macro.trigger == "ctrl+1"
    win.delay_spin.setValue(275)
    assert macro.default_delay_ms == 275
    # Humanización: activada por defecto; al desactivarla sus valores quedan deshabilitados.
    assert win.humanize_row.isChecked() and win.stagger_spin.isEnabled()
    win.jitter_spin.setValue(25)
    assert macro.jitter_ms == 25
    win.hold_jitter_spin.setValue(4)
    assert macro.hold_jitter_ms == 4
    win.stagger_spin.setValue(0)
    assert macro.chord_stagger_ms == 0
    assert "± 25 ms" in win.summary_label.text()
    win.humanize_row.setChecked(False)
    assert macro.humanize is False
    assert not win.jitter_spin.isEnabled() and not win.stagger_spin.isEnabled()
    win.humanize_row.setChecked(True)
    assert macro.humanize is True and win.hold_jitter_spin.isEnabled()
    win.mode_combo.setCurrentIndex(win.mode_combo.findData("toggle"))
    assert macro.trigger_mode == "toggle"
    win.infinite_check.setChecked(True)
    assert macro.repeat_count == 0
    win.infinite_check.setChecked(False)
    win.repeat_spin.setValue(3)
    assert macro.repeat_count == 3
    win.block_row.setChecked(False)
    assert macro.block_trigger is False
    assert (f"macro:{macro.id}", "ctrl+1", False, False) in win._binding_specs()
    win.enabled_row.setChecked(False)
    assert macro.enabled is False
    assert all(spec[0] != f"macro:{macro.id}" for spec in win._binding_specs())
    # Los cambios de la configuración también se deshacen.
    assert win.undo()
    assert macro.enabled is True


def test_run_example_macro_with_fake_backend(make_window, qapp):
    backend = FakeBackend()
    win = make_window(backend)
    macro = select_example(win)
    kinds: list[str] = []
    win.bridge.player_event.connect(lambda ev: kinds.append(ev.kind))

    assert win.run_selected()
    assert wait_until(qapp, lambda: "finished" in kinds and not win._is_running)
    assert win.player.wait(2.0)

    presses = [c[1] for c in backend.calls if c[0] == "press"]
    releases = [c[1] for c in backend.calls if c[0] == "release"]
    assert presses == [["w", "mouse_right"], ["s", "q"], ["s", "mouse_left"], ["space"],
                       ["mouse_left", "mouse_right"]]
    assert releases == [list(reversed(p)) for p in presses]
    assert backend.calls[-1] == ("release_all",)
    assert kinds[0] in ("started", "countdown") and kinds.count("step") == len(macro.steps)
    assert win.status_text() == "Lista"
    assert "terminada" in win.message_text()
    assert win._running_step is None


def test_stop_and_trigger_bridge_from_other_thread(make_window, qapp):
    win = make_window()
    macro = select_example(win)
    macro.repeat_count = 0  # infinita: hay que pararla
    before = win.settings.triggers_enabled

    # Los avisos del hook llegan desde otro hilo: el puente los encola al hilo principal.
    t = threading.Thread(target=win.bridge.on_trigger, args=("control:enable", True))
    t.start()
    t.join()
    assert wait_until(qapp, lambda: win.settings.triggers_enabled is (not before))

    assert win.run_selected()
    assert wait_until(qapp, lambda: win._running_step is not None)
    win.stop_all()
    assert wait_until(qapp, lambda: not win._is_running)
    assert win.player.wait(2.0)


def test_save_and_reload(make_window, data_dir):
    win = make_window()
    macro = win.add_macro("Persistente")
    win.insert_steps([PressStep(inputs=["shift", "mouse_left"], hold_ms=80), TextStep(text="hola\n")])
    win.trigger_edit._commit_combo("mouse_x1")
    assert win.save_now()
    assert (data_dir / "macros.json").exists()
    win._quitting = True
    win.close()  # guarda también los ajustes (macro seleccionada)

    other = make_window()
    loaded = other.get_macro(macro.id)
    assert loaded is not None
    assert loaded.to_dict() == macro.to_dict()
    assert other.current_macro() is loaded  # recuerda la macro seleccionada


def test_delete_macro(make_window):
    win = make_window()
    count = len(win.macros)
    macro = win.add_macro("Borrable")
    assert len(win.macros) == count + 1
    assert win.delete_macro(macro.id, confirm=False)
    assert win.get_macro(macro.id) is None
    assert win.macro_list.topLevelItemCount() == count


# --- Diálogo de paso ----------------------------------------------------------------------
def test_step_dialog_pad_captures_keys_and_mouse(qapp):
    from macrotool.ui.step_dialog import StepDialog

    triggers = FakeTriggers()
    dlg = StepDialog(None, None, "press", triggers=triggers)
    dlg.setAttribute(Qt.WA_DontShowOnScreen, True)
    dlg.show()
    page = dlg.pages["press"]
    pad = page.pad
    pad.setFocus()
    qapp.processEvents()
    QApplication.sendEvent(pad, QFocusEvent(QEvent.FocusIn, Qt.TabFocusReason))

    # W (VK 0x57) mantenida + clic derecho sobre el pad → al soltar todo: ["w", "mouse_right"].
    QApplication.sendEvent(pad, key_event(QEvent.KeyPress, Qt.Key_W, 0x57, 0x11))
    QApplication.sendEvent(pad, mouse_event(QEvent.MouseButtonPress, Qt.RightButton))
    assert pad.current_tokens() == ["w", "mouse_right"]
    QApplication.sendEvent(pad, mouse_event(QEvent.MouseButtonRelease, Qt.RightButton))
    assert page.chips.tokens() == []  # aún no: W sigue pulsada
    QApplication.sendEvent(pad, key_event(QEvent.KeyRelease, Qt.Key_W, 0x57, 0x11))
    assert page.chips.tokens() == ["w", "mouse_right"]

    # Esc y Tab se capturan como teclas: ni cierran el diálogo ni mueven el foco.
    QApplication.sendEvent(pad, key_event(QEvent.KeyPress, Qt.Key_Escape, 0x1B, 0x01))
    QApplication.sendEvent(pad, key_event(QEvent.KeyRelease, Qt.Key_Escape, 0x1B, 0x01))
    assert dlg.isVisible()
    assert page.chips.tokens() == ["esc"]
    QApplication.sendEvent(pad, key_event(QEvent.KeyPress, Qt.Key_Tab, 0x09, 0x0F))
    QApplication.sendEvent(pad, key_event(QEvent.KeyRelease, Qt.Key_Tab, 0x09, 0x0F))
    assert page.chips.tokens() == ["tab"]

    # Botones rápidos y selector de teclas añaden entradas.
    page.chips.set_tokens(["s"])
    page.chips.add_token("mouse_left")
    page.picker.tokenChosen.emit("q")
    step, problem = dlg.build_step()
    assert problem is None
    assert isinstance(step, PressStep)
    assert step.inputs == ["s", "mouse_left", "q"]
    assert step.delay_after_ms is None  # "Usar el de la macro" por defecto

    dlg.use_default_delay.setChecked(False)
    dlg.delay.setValue(333)
    dlg.comment.setText("combo")
    step, _ = dlg.build_step()
    assert step.delay_after_ms == 333 and step.comment == "combo"

    # Mientras el pad tiene el foco, los disparadores globales quedan suspendidos.
    QApplication.sendEvent(pad, QFocusEvent(QEvent.FocusOut, Qt.TabFocusReason))
    assert triggers.suspended_calls and triggers.suspended_calls[0] is True
    assert triggers.suspended_calls[-1] is False
    dlg.close()
    dlg.deleteLater()


def test_step_dialog_validates_and_edits_other_types(qapp):
    from macrotool.ui.step_dialog import StepDialog

    dlg = StepDialog(None, None, "press")
    step, problem = dlg.build_step()
    assert step is None and problem  # sin entradas no se puede aceptar

    dlg.set_type("text")
    dlg.pages["text"].text.setPlainText("Hola\nmundo")
    step, problem = dlg.build_step()
    assert problem is None and isinstance(step, TextStep) and step.text == "Hola\nmundo"

    original = WaitStep(ms=1200, random_extra_ms=300, delay_after_ms=50, comment="pausa", enabled=False)
    dlg = StepDialog(None, original, macro=Macro(default_delay_ms=90))
    assert dlg.current_type == "wait"
    dlg.pages["wait"].ms.setValue(1500)
    dlg.accept()
    result = dlg.result_step()
    assert isinstance(result, WaitStep)
    assert (result.ms, result.random_extra_ms, result.delay_after_ms, result.comment, result.enabled) == (
        1500, 300, 50, "pausa", False)
    assert original.ms == 1200  # el original no se toca


def test_combo_capture_edit_captures_trigger_and_suspends(qapp):
    from macrotool.ui.widgets import ComboCaptureEdit

    triggers = FakeTriggers()
    edit = ComboCaptureEdit(triggers=triggers)
    captured: list[str] = []
    edit.comboChanged.connect(captured.append)
    QApplication.sendEvent(edit, QFocusEvent(QEvent.FocusIn, Qt.MouseFocusReason))
    assert triggers.suspended_calls == [True]
    # Ctrl + 1 → "ctrl+1"
    QApplication.sendEvent(edit, QKeyEvent(QEvent.KeyPress, Qt.Key_Control, Qt.ControlModifier, 0x1D, 0x11, 0))
    QApplication.sendEvent(edit, QKeyEvent(QEvent.KeyPress, Qt.Key_1, Qt.ControlModifier, 0x02, 0x31, 0))
    QApplication.sendEvent(edit, QKeyEvent(QEvent.KeyRelease, Qt.Key_1, Qt.ControlModifier, 0x02, 0x31, 0))
    QApplication.sendEvent(edit, QKeyEvent(QEvent.KeyRelease, Qt.Key_Control, Qt.NoModifier, 0x1D, 0x11, 0))
    assert captured == ["ctrl+1"]
    assert edit.combo() == "ctrl+1"
    QApplication.sendEvent(edit, QFocusEvent(QEvent.FocusOut, Qt.OtherFocusReason))
    assert triggers.suspended_calls == [True, False]
    # Un clic izquierdo solo no sirve como disparador; el botón lateral sí.
    QApplication.sendEvent(edit, QFocusEvent(QEvent.FocusIn, Qt.OtherFocusReason))
    edit.setFocus()
    QApplication.sendEvent(edit, mouse_event(QEvent.MouseButtonPress, Qt.BackButton))
    QApplication.sendEvent(edit, mouse_event(QEvent.MouseButtonRelease, Qt.BackButton))
    assert edit.combo() == "mouse_x1"


# --- Ajustes y utilidades -----------------------------------------------------------------
def test_settings_dialog_builds_and_rejects_duplicates(qapp):
    from macrotool.ui.main_window import SettingsDialog

    dlg = SettingsDialog(None, Settings())
    settings, problem = dlg.build_settings()
    assert problem is None and settings.hotkey_run_selected == "f6"
    dlg.switches["always_on_top"].setChecked(True)
    dlg.hotkey_edits["hotkey_pause"].set_combo("f8")  # repetido con "Grabar"
    settings, problem = dlg.build_settings()
    assert settings is None and "repetido" in problem
    dlg.hotkey_edits["hotkey_pause"].set_combo("ctrl+p")
    settings, problem = dlg.build_settings()
    assert problem is None and settings.hotkey_pause == "ctrl+p" and settings.always_on_top


def test_move_and_shift_rows():
    from macrotool.ui.widgets import move_rows, shift_rows

    seq = list("abcde")
    assert move_rows(seq, [0], 3) == (list("bcade"), [2])
    assert move_rows(seq, [1, 3], 0) == (list("bdace"), [0, 1])
    assert move_rows(seq, [4], 5) == (seq, [4])
    assert move_rows(seq, [0, 1], 5) == (list("cdeab"), [3, 4])
    assert shift_rows(seq, [2], -1) == (list("acbde"), [1])
    assert shift_rows(seq, [0, 1], -1) == (seq, [0, 1])  # ya arriba del todo
    assert shift_rows(seq, [1, 3], 1) == (list("acbed"), [2, 4])


def test_theme_icons_and_app_icon(qapp):
    from macrotool.ui import theme

    for name in ("play", "stop", "record", "keyboard", "mouse", "settings"):
        assert not theme.icon(name, theme.ACCENT, 20).isNull()
    assert not theme.app_icon().isNull()
    assert "QPushButton" in theme.stylesheet()


def test_main_smoke_test_without_hooks(data_dir):
    """``main.py --smoke-test`` arranca, construye todo y sale con código 0."""
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", MACROTOOL_DATA_DIR=str(data_dir))
    proc = subprocess.run([sys.executable, str(ROOT / "main.py"), "--smoke-test", "--no-hooks"],
                          cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert (data_dir / "macrotool.log").exists()


def test_copy_steps_then_exit_does_not_crash(data_dir):
    """Copiar pasos y salir: el proceso termina limpio (antes, el QMimeData creado desde Python
    que guardaba el portapapeles se destruía tras finalizar Python: violación de acceso)."""
    code = (
        "import main\n"
        "from PySide6.QtWidgets import QApplication\n"
        "app = QApplication([])\n"
        "from macrotool.model import PressStep\n"
        "from macrotool.ui.main_window import MainWindow\n"
        "win = MainWindow(start_hooks=False, backend=main._NullBackend())\n"
        "win.add_macro('Copiar')\n"
        "win.insert_steps([PressStep(inputs=['a']), PressStep(inputs=['b'])])\n"
        "assert win.copy_steps([0, 1]) == 2\n"
        "assert [s.inputs for s in win._steps_from_clipboard()] == [['a'], ['b']]\n"
        "assert '\"macrotool\": \"steps\"' in QApplication.clipboard().text()\n"
        "win._quitting = True\n"
        "win.close()\n"
        "app.processEvents()\n"
        "print('ok')\n"
    )
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", MACROTOOL_DATA_DIR=str(data_dir))
    proc = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=env,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, (proc.returncode, proc.stderr)
    assert proc.stdout.strip() == "ok"


# --- Revisión final ---------------------------------------------------------------------------
class PendingBackend(FakeBackend):
    """Backend falso con sueltas pendientes (Windows rechazó soltar algo)."""

    def __init__(self) -> None:
        super().__init__()
        self.pending: list[str] = []
        self.allow = False

    @property
    def pending_release(self) -> list[str]:
        return list(self.pending)

    def retry_pending_release(self) -> bool:
        if self.allow:
            self.pending.clear()
        return not self.pending


class SuspendSpy:
    def __init__(self) -> None:
        self.exceptions: list[tuple[str, ...]] = []

    def set_suspend_exceptions(self, ids) -> None:
        self.exceptions.append(tuple(ids))


def test_import_button_opens_file_dialog(make_window, monkeypatch, tmp_path):
    from macrotool import storage
    from macrotool.ui import main_window

    source = tmp_path / "import.json"
    storage.export_macros([Macro(name="Importada", steps=[PressStep(inputs=["a"])])], source)
    calls: list[int] = []

    def fake_dialog(*_args, **_kw):
        calls.append(1)
        return str(source), ""

    monkeypatch.setattr(main_window.QFileDialog, "getOpenFileName", fake_dialog)
    win = make_window()
    win.import_button.click()  # clicked(bool) no debe llegar como ruta
    assert calls == [1]
    assert win.current_macro().name == "Importada"


def test_toggling_other_macro_from_list_is_undoable_on_its_own_stack(make_window):
    win = make_window()
    a = win.add_macro("A")
    b = win.add_macro("B")
    win.select_macro(a.id)
    win.delay_spin.setValue(333)
    win.select_macro(b.id)
    win._on_macro_toggle_requested(win.macros.index(a))  # interruptor de la tarjeta de A
    assert a.enabled is False and win.current_macro() is b
    win.select_macro(a.id)
    assert win.undo()  # deshace el interruptor (el último cambio), no el retardo a escondidas
    assert a.enabled is True and a.default_delay_ms == 333
    assert win.redo()
    assert a.enabled is False
    assert all(spec[0] != f"macro:{a.id}" for spec in win._binding_specs())


def test_huge_values_are_clamped_and_never_break_the_panel(make_window, tmp_path):
    import json

    from macrotool.model import MAX_DELAY_MS, MAX_JITTER_MS, MAX_WAIT_MS
    from macrotool.ui.step_dialog import StepDialog
    from macrotool.ui.widgets import SpinBox

    path = tmp_path / "enorme.json"
    path.write_text(json.dumps({"name": "Mala", "default_delay_ms": 5_000_000_000, "jitter_ms": 900_000,
                                "steps": [{"type": "wait", "ms": 3_000_000_000}]}), encoding="utf-8")
    win = make_window()
    (bad,) = win.import_macros(str(path))
    assert bad.default_delay_ms == MAX_DELAY_MS and bad.jitter_ms == MAX_JITTER_MS
    assert bad.steps[0].ms == MAX_WAIT_MS
    assert win.current_macro() is bad and win.step_tree.topLevelItemCount() == 1
    assert win.delay_spin.value() == MAX_DELAY_MS  # el panel muestra lo que usará el motor
    dlg = StepDialog(None, WaitStep(ms=3_000_000_000))
    assert dlg.pages["wait"].ms.value() == MAX_WAIT_MS
    dlg.deleteLater()
    spin = SpinBox(0, 10)
    spin.setValue(3_000_000_000)  # sin OverflowError
    assert spin.value() == 10


def test_failed_macro_load_still_shows_its_steps(make_window, monkeypatch):
    win = make_window()
    a = win.add_macro("A")
    b = win.add_macro("B")
    win.select_macro(a.id)
    win.insert_steps([PressStep(inputs=["a"]), PressStep(inputs=["b"])])
    win.select_macro(b.id)

    def boom(*_args):
        raise RuntimeError("fallo al cargar")

    monkeypatch.setattr(win.humanize_row, "setChecked", boom)
    win._current_id = a.id
    with pytest.raises(RuntimeError):
        win._load_macro_into_ui()
    assert win.current_macro() is a
    assert win.step_tree.topLevelItemCount() == 2  # la lista es la de la macro seleccionada


def test_save_shortcut_reports_the_real_result(make_window, monkeypatch):
    from macrotool import storage

    win = make_window()
    assert win.save_all() is True
    assert win.message_text() == "Guardado"
    win._storage_error = "No se pudieron leer las macros (acceso denegado); no se guardarán cambios."
    assert win.save_all() is False
    assert win.message_text() == win._storage_error
    win._storage_error = ""

    def fail(_macros):
        raise OSError("disco lleno")

    monkeypatch.setattr(storage, "save_macros", fail)
    assert win.save_all() is False
    assert "disco lleno" in win.message_text()


def test_rename_in_progress_is_saved_on_close(make_window, qapp):
    from macrotool import storage

    win = make_window()
    win.settings.minimize_to_tray = False
    win.show()
    item = win.macro_list.topLevelItem(0)
    macro_id = item.data(0, Qt.UserRole)
    win.macro_list.editItem(item, 0)
    qapp.processEvents()
    editor = win.macro_list.indexWidget(win.macro_list.model().index(0, 0))
    assert editor is not None
    editor.setText("Renombrada sin Intro")  # sin pulsar Intro
    win._quitting = True
    win.close()
    saved = {m.id: m.name for m in storage.load_macros()}
    assert saved[macro_id] == "Renombrada sin Intro"


def test_modifier_only_trigger_needs_confirmation(qapp, monkeypatch):
    from macrotool.ui.widgets import ComboCaptureEdit

    answers: list[bool] = []
    asked: list[list[str]] = []

    def confirm(self, tokens):
        asked.append(list(tokens))
        return answers.pop(0)

    monkeypatch.setattr(ComboCaptureEdit, "confirm_modifier_only", confirm)
    edit = ComboCaptureEdit()
    captured: list[str] = []
    edit.comboChanged.connect(captured.append)
    QApplication.sendEvent(edit, QFocusEvent(QEvent.FocusIn, Qt.MouseFocusReason))

    def shift_tap() -> None:
        QApplication.sendEvent(edit, QKeyEvent(QEvent.KeyPress, Qt.Key_Shift, Qt.ShiftModifier, 0x2A, 0x10, 0))
        QApplication.sendEvent(edit, QKeyEvent(QEvent.KeyRelease, Qt.Key_Shift, Qt.NoModifier, 0x2A, 0x10, 0))

    answers.append(False)
    shift_tap()  # se soltó Mayús antes de pulsar la otra tecla: no se fija sin confirmar
    assert asked == [["shift"]] and captured == [] and edit.combo() == ""
    answers.append(True)
    shift_tap()  # confirmado: Mayús sola como disparador
    assert captured == ["shift"]
    # Una combinación normal no pregunta nada.
    QApplication.sendEvent(edit, QFocusEvent(QEvent.FocusIn, Qt.MouseFocusReason))
    QApplication.sendEvent(edit, QKeyEvent(QEvent.KeyPress, Qt.Key_Shift, Qt.ShiftModifier, 0x2A, 0x10, 0))
    QApplication.sendEvent(edit, QKeyEvent(QEvent.KeyPress, Qt.Key_1, Qt.ShiftModifier, 0x02, 0x31, 0))
    QApplication.sendEvent(edit, QKeyEvent(QEvent.KeyRelease, Qt.Key_1, Qt.ShiftModifier, 0x02, 0x31, 0))
    QApplication.sendEvent(edit, QKeyEvent(QEvent.KeyRelease, Qt.Key_Shift, Qt.NoModifier, 0x2A, 0x10, 0))
    assert captured == ["shift", "shift+1"] and len(asked) == 2


def _qt_key(kind, key, scan: int, vk: int, ts: int, mods=Qt.NoModifier) -> QKeyEvent:
    """QKeyEvent como los que entrega Qt 6 en Windows (scancode extendido = 0xE0xx)."""
    event = QKeyEvent(kind, key, mods, scan, vk, 0, "", False, 1)
    event.setTimestamp(ts)
    return event


def _altgr_1(widget) -> None:
    """AltGr + 1 en teclado español: Ctrl izquierdo ficticio + Alt derecho con la misma marca de tiempo."""
    seq = [(Qt.Key_Control, 0x1D, 0x11, 1000), (Qt.Key_Alt, 0xE038, 0x12, 1000), (Qt.Key_1, 0x02, 0x31, 1100)]
    for key, scan, vk, ts in seq:
        QApplication.sendEvent(widget, _qt_key(QEvent.KeyPress, key, scan, vk, ts))
    for key, scan, vk, ts in reversed(seq):
        QApplication.sendEvent(widget, _qt_key(QEvent.KeyRelease, key, scan, vk, ts + 500))


def test_capture_recognises_altgr_and_right_side_modifiers(qapp):
    from macrotool.ui.widgets import CapturePad, ComboCaptureEdit, key_event_token

    rshift = _qt_key(QEvent.KeyPress, Qt.Key_Shift, 0x36, 0x10, 5)
    rctrl = _qt_key(QEvent.KeyPress, Qt.Key_Control, 0xE01D, 0x11, 6)
    assert key_event_token(rshift) == "shift" and key_event_token(rshift, sided=True) == "rshift"
    assert key_event_token(rctrl) == "ctrl" and key_event_token(rctrl, sided=True) == "rctrl"
    assert key_event_token(_qt_key(QEvent.KeyPress, Qt.Key_Alt, 0xE038, 0x12, 7)) == "ralt"

    edit = ComboCaptureEdit()
    QApplication.sendEvent(edit, QFocusEvent(QEvent.FocusIn, Qt.MouseFocusReason))
    _altgr_1(edit)
    assert edit.combo() == "ralt+1"  # no "ctrl+alt+1", que el hook nunca vería con AltGr
    pad = CapturePad()
    got: list[list[str]] = []
    pad.captured.connect(got.append)
    QApplication.sendEvent(pad, QFocusEvent(QEvent.FocusIn, Qt.TabFocusReason))
    _altgr_1(pad)
    QApplication.sendEvent(pad, _qt_key(QEvent.KeyPress, Qt.Key_Control, 0xE01D, 0x11, 2000))
    QApplication.sendEvent(pad, _qt_key(QEvent.KeyRelease, Qt.Key_Control, 0xE01D, 0x11, 2050))
    QApplication.sendEvent(pad, _qt_key(QEvent.KeyPress, Qt.Key_Shift, 0x36, 0x10, 2100))
    QApplication.sendEvent(pad, _qt_key(QEvent.KeyRelease, Qt.Key_Shift, 0x36, 0x10, 2150))
    assert got == [["ralt", "1"], ["rctrl"], ["rshift"]]  # la tecla física que se pulsó
    # Alt derecho sin AltGr (teclado inglés): en un disparador vale como Alt.
    edit2 = ComboCaptureEdit()
    QApplication.sendEvent(edit2, QFocusEvent(QEvent.FocusIn, Qt.MouseFocusReason))
    QApplication.sendEvent(edit2, _qt_key(QEvent.KeyPress, Qt.Key_Alt, 0xE038, 0x12, 3000))
    QApplication.sendEvent(edit2, _qt_key(QEvent.KeyPress, Qt.Key_1, 0x02, 0x31, 3050))
    QApplication.sendEvent(edit2, _qt_key(QEvent.KeyRelease, Qt.Key_1, 0x02, 0x31, 3100))
    QApplication.sendEvent(edit2, _qt_key(QEvent.KeyRelease, Qt.Key_Alt, 0xE038, 0x12, 3150))
    assert edit2.combo() == "alt+1"


def test_delay_dialog_text_for_wait_steps(make_window, monkeypatch):
    from macrotool.ui import main_window
    from macrotool.ui.main_window import DelayDialog

    assert "Sin retardo adicional" in DelayDialog(None, 150, None, waits="all").use_default.text()
    assert "150 ms" in DelayDialog(None, 150, None).use_default.text()
    texts: list[str] = []

    def fake_exec(self):
        texts.append(self.use_default.text())
        return 0

    monkeypatch.setattr(main_window.DelayDialog, "exec", fake_exec)
    win = make_window()
    win.add_macro("Esperas")
    win.insert_steps([WaitStep(ms=500), PressStep(inputs=["a"])])
    win.ask_steps_delay([0])
    win.ask_steps_delay([0, 1])
    assert "Sin retardo adicional" in texts[0]
    assert "ninguno tras las esperas" in texts[1]


def test_scroll_amount_suffix_singular(qapp):
    from macrotool.model import ScrollStep
    from macrotool.ui.step_dialog import StepDialog

    dlg = StepDialog(None, ScrollStep(amount=1))
    assert dlg.pages["scroll"].amount.suffix() == " muesca"
    dlg.pages["scroll"].amount.setValue(3)
    assert dlg.pages["scroll"].amount.suffix() == " muescas"
    dlg.deleteLater()


def test_trigger_field_keeps_its_help_tooltip(make_window):
    win = make_window()
    base = win.trigger_edit.toolTip()
    assert "Haz clic" in base
    win.trigger_edit.set_error("Conflicto")
    assert win.trigger_edit.toolTip() == "Conflicto"
    win.trigger_edit.set_error(None)
    assert win.trigger_edit.toolTip() == base


def test_empty_name_field_is_restored(make_window):
    win = make_window()
    macro = win.add_macro("Nombre")
    win.name_edit.setText("")
    win.name_edit.textEdited.emit("")
    win.name_edit.editingFinished.emit()
    assert macro.name == "Nombre" and win.name_edit.text() == "Nombre"
    win.name_edit.setText("  Nuevo  ")
    win.name_edit.textEdited.emit("  Nuevo  ")
    win.name_edit.editingFinished.emit()
    assert macro.name == "Nuevo" and win.name_edit.text() == "Nuevo"


def test_bridge_coalesces_progress_events_in_order(qapp):
    from macrotool.engine import PlayerEvent
    from macrotool.ui.main_window import QtBridge

    bridge = QtBridge()
    got: list[Any] = []
    bridge.player_event.connect(got.append)

    def worker() -> None:
        bridge.on_player_event(PlayerEvent("started", "m"))
        for i in range(20_000):  # retardo 0 ms: miles de pasos por segundo
            bridge.on_player_event(PlayerEvent("repeat" if i % 2 else "step", "m", step_index=i))
        bridge.on_player_event(PlayerEvent("paused", "m"))
        bridge.on_player_event(PlayerEvent("step", "m", step_index=99_999))
        bridge.on_player_event(PlayerEvent("finished", "m"))

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    assert wait_until(qapp, lambda: bool(got) and got[-1].kind == "finished")
    kinds = [e.kind for e in got]
    assert kinds[0] == "started" and kinds.count("finished") == 1
    assert len(got) < 50  # agrupados: la cola de Qt no se satura
    assert kinds.index("paused") < kinds.index("finished")
    # El progreso más reciente antes de cada evento se entrega, en orden.
    assert got[kinds.index("paused") - 1].step_index == 19_999
    assert got[-2].kind == "step" and got[-2].step_index == 99_999
    bridge.deleteLater()


def test_disabling_macros_stops_the_running_one(make_window, qapp):
    win = make_window()
    macro = select_example(win)
    macro.repeat_count = 0  # infinita en modo Alternar: su tecla ya no podría pararla
    macro.trigger_mode = "toggle"
    macro.enabled = True
    assert win.run_selected()
    assert wait_until(qapp, lambda: win.controller.is_running and win._running_step is not None)
    win.set_triggers_enabled(False)
    assert wait_until(qapp, lambda: not win.controller.is_running and not win._is_running)
    win.set_triggers_enabled(True)
    # Desactivar la macro en curso desde su tarjeta también la detiene.
    assert win.run_selected()
    assert wait_until(qapp, lambda: win.controller.is_running)
    win._on_macro_toggle_requested(win.macros.index(macro))
    assert wait_until(qapp, lambda: not win.controller.is_running)
    assert win.player.wait(2.0)


def test_f6_switches_to_the_selected_macro(make_window, qapp):
    backend = FakeBackend()
    win = make_window(backend)
    first = select_example(win)
    first.repeat_count = 0
    second = win.add_macro("Segunda")
    win.insert_steps([PressStep(inputs=["z"])])
    win.select_macro(first.id)
    assert win.run_selected()
    assert wait_until(qapp, lambda: win.controller.running_macro_id == first.id and bool(win._running_step))
    win.select_macro(second.id)
    win._on_trigger("control:run_selected", True)  # F6 con otra macro en marcha: cambia a ésta
    assert wait_until(qapp, lambda: ("press", ["z"]) in backend.calls)
    assert wait_until(qapp, lambda: not win.controller.is_running)
    # F6 sobre la macro que corre la detiene.
    win.select_macro(first.id)
    assert win.run_selected()
    assert wait_until(qapp, lambda: win.controller.running_macro_id == first.id)
    win._on_trigger("control:run_selected", True)
    assert wait_until(qapp, lambda: not win.controller.is_running)
    assert win.player.wait(2.0)


def test_paused_first_event_and_finished_message(make_window):
    from macrotool.engine import NO_STEPS_MESSAGE, PlayerEvent

    win = make_window()
    macro = win.add_macro("Vacía")
    win._on_player_event(PlayerEvent("paused", macro.id))  # pausa antes de la cuenta atrás
    assert win._is_running and win._is_paused
    assert win.pause_button.isEnabled() and win.stop_button.isEnabled()
    win._on_player_event(PlayerEvent("finished", macro.id, message=NO_STEPS_MESSAGE))
    assert not win._is_running
    assert NO_STEPS_MESSAGE in win.message_text()


def test_stop_hotkey_stays_active_during_capture_while_running(make_window):
    from macrotool.engine import PlayerEvent

    win = make_window()
    spy = SuspendSpy()
    win.triggers = spy
    try:
        macro = win.current_macro()
        win._on_player_event(PlayerEvent("started", macro.id))
        assert spy.exceptions[-1] == ("control:stop",)
        win._on_player_event(PlayerEvent("stopped", macro.id))
        assert spy.exceptions[-1] == ()
    finally:
        win.triggers = None


def test_rejected_releases_are_retried_by_the_window(make_window):
    from macrotool.engine import PlayerEvent

    backend = PendingBackend()
    win = make_window(backend)
    macro = win.current_macro()
    backend.pending = ["lshift", "w"]
    win._on_player_event(PlayerEvent("error", macro.id, message="Windows no dejó soltar algunas teclas"))
    assert win._release_retry.isActive()
    win._retry_pending_release()
    assert win._release_retry.isActive()  # sigue bloqueado
    backend.allow = True
    win._retry_pending_release()
    assert not win._release_retry.isActive()
    assert "Se soltaron" in win.message_text()


def test_session_end_saves_immediately_and_does_not_hide_to_tray(make_window, qapp):
    from macrotool import storage

    class Tray:
        def __init__(self) -> None:
            self.messages: list[str] = []

        def isVisible(self) -> bool:  # noqa: N802
            return True

        def showMessage(self, *args) -> None:  # noqa: N802
            self.messages.append(args[1])

        def hide(self) -> None:
            pass

    win = make_window()
    macro = win.add_macro("Recién editada")
    win.show()
    qapp.processEvents()
    win._tray = Tray()
    win.on_session_end()  # WM_QUERYENDSESSION (commitDataRequest)
    saved = storage.load_settings()
    assert saved.last_macro_id == macro.id and saved.window_geometry
    assert macro.id in {m.id for m in storage.load_macros()}  # sin esperar al autoguardado
    win.close()  # el quit() de Qt al terminar la sesión: no se desvía a la bandeja
    assert win._shut_down and win._tray.messages == []
    win._tray = None


def test_stop_recording_from_ui_passes_the_return_moment(make_window):
    class FakeRecorder:
        held_at_stop = frozenset()

        def __init__(self) -> None:
            self.stops: list[Any] = []

        def start(self, **_kw) -> None:
            pass

        def stop(self, **kw) -> list:
            self.stops.append(kw.get("ui_since"))
            return []

    win = make_window()
    win.recorder = FakeRecorder()
    try:
        assert win.start_recording()
        win._note_ui_return()  # se activó la ventana / se abrió el menú de la bandeja
        since = win._rec_return_t
        assert since is not None
        win.stop_recording(from_ui=True)
        assert win.recorder.stops == [since]
        assert win.start_recording()
        win._note_ui_return()
        win.stop_recording()  # con el atajo global: nada que recortar
        assert win.recorder.stops == [since, None]
    finally:
        win.recorder = None


def test_smoke_mode_suspends_triggers_before_registering_bindings(qapp, data_dir, monkeypatch):
    from macrotool import hooks
    from macrotool.ui.main_window import MainWindow

    monkeypatch.setattr(hooks.InputHook, "start", lambda self, timeout=5.0: None)  # sin hooks reales
    seen: list[bool] = []
    original = hooks.TriggerManager.set_bindings

    def spy(self, bindings):
        seen.append(self.suspended)
        return original(self, bindings)

    monkeypatch.setattr(hooks.TriggerManager, "set_bindings", spy)
    win = MainWindow(start_hooks=True, backend=FakeBackend(), suspend_triggers=True)
    try:
        assert seen and all(seen)
    finally:
        win._quitting = True
        win.close()


def test_main_smoke_test_uses_its_own_data_dir(monkeypatch, tmp_path):
    import main

    monkeypatch.delenv("MACROTOOL_DATA_DIR", raising=False)
    monkeypatch.setattr(main.tempfile, "gettempdir", lambda: str(tmp_path))
    old = tmp_path / main.SMOKE_DATA_DIR
    old.mkdir()
    (old / "macros.json").write_text("{}", encoding="utf-8")
    main._use_smoke_data_dir()
    assert os.environ["MACROTOOL_DATA_DIR"] == str(tmp_path / main.SMOKE_DATA_DIR)
    assert not (old / "macros.json").exists()  # cada prueba empieza de cero
    monkeypatch.setenv("MACROTOOL_DATA_DIR", str(tmp_path / "elegida"))
    main._use_smoke_data_dir()  # si ya se indicó una carpeta, se respeta
    assert os.environ["MACROTOOL_DATA_DIR"] == str(tmp_path / "elegida")


@pytest.mark.skipif(sys.platform != "win32", reason="mutex de Windows")
def test_single_instance_detects_elevated_first_instance():
    """Un mutex creado por un proceso elevado no se puede abrir: también es "ya en marcha"."""
    import ctypes
    import uuid
    from ctypes import wintypes

    import main

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class SECURITY_ATTRIBUTES(ctypes.Structure):  # noqa: N801
        _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                    ("bInheritHandle", wintypes.BOOL)]

    convert = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    convert.restype = wintypes.BOOL
    create = kernel32.CreateMutexW
    create.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    create.restype = wintypes.HANDLE
    close = kernel32.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p
    sd = ctypes.c_void_p()
    # Sólo SYSTEM: como la DACL por defecto de un proceso elevado frente a uno normal.
    assert convert("D:(A;;GA;;;SY)", 1, ctypes.byref(sd), None)
    sa = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), sd, False)
    name = "Local" + chr(92) + "MacroToolTest." + uuid.uuid4().hex
    first = create(ctypes.byref(sa), False, name)
    assert first
    try:
        assert main._acquire_single_instance(name) is None
    finally:
        close(first)
        local_free(sd)
    own = main._acquire_single_instance("Local" + chr(92) + "MacroToolTest." + uuid.uuid4().hex)
    assert own not in (None, True)
    close(own)


def test_theme_urls_survive_an_apostrophe_in_the_temp_folder(qapp, monkeypatch, tmp_path):
    from PySide6.QtCore import qInstallMessageHandler
    from PySide6.QtWidgets import QWidget

    from macrotool.ui import theme

    folder = tmp_path / "o'brien"
    folder.mkdir()
    monkeypatch.setattr(theme.tempfile, "gettempdir", lambda: str(folder))
    css = theme.stylesheet()
    assert "o'brien" in css and "url(" + chr(34) in css
    messages: list[str] = []
    qInstallMessageHandler(lambda _mode, _ctx, msg: messages.append(msg))
    try:
        widget = QWidget()
        widget.setStyleSheet(css)
        widget.ensurePolished()
    finally:
        qInstallMessageHandler(None)
    assert not [m for m in messages if "parse" in m.lower()]
