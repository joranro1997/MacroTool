"""Integración de extremo a extremo (sin inyectar nada real).

Cadena completa: ``InputHook.dispatch`` (hook NO instalado, como haría su callback) →
``TriggerManager`` → ``QtBridge`` (desde otro hilo) → ``MacroController`` →
``MacroPlayer`` → backend falso, con ``MainWindow(start_hooks=False)`` en Qt offscreen.
``winput._SendInput`` se sustituye por una función que falla: si algo intentase
inyectar de verdad, el test lo detectaría.
"""
from __future__ import annotations

import os

os.environ["QT_QPA_PLATFORM"] = "offscreen"  # antes de importar Qt: nada de ventanas visibles

import threading  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, Callable  # noqa: E402

import pytest  # noqa: E402

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from macrotool import winput  # noqa: E402
from macrotool.hooks import InputHook, RawEvent, TriggerManager  # noqa: E402

CHORDS = [["w", "mouse_right"], ["s", "q"], ["s", "mouse_left"], ["space"], ["mouse_left", "mouse_right"]]


class FakeBackend:
    """Backend falso con marcas de tiempo."""

    def __init__(self) -> None:
        self.calls: list[tuple[float, str, tuple[Any, ...]]] = []
        self._lock = threading.Lock()

    def _add(self, name: str, *args: Any) -> None:
        with self._lock:
            self.calls.append((time.perf_counter(), name, args))

    def press(self, tokens: list[str]) -> None:
        self._add("press", list(tokens))

    def release(self, tokens: list[str]) -> None:
        self._add("release", list(tokens))

    def type_char(self, ch: str) -> None:
        self._add("type_char", ch)

    def move_to(self, x: int, y: int) -> None:
        self._add("move_to", x, y)

    def move_rel(self, dx: int, dy: int) -> None:
        self._add("move_rel", dx, dy)

    def scroll(self, notches: int, horizontal: bool = False) -> None:
        self._add("scroll", notches, horizontal)

    def cursor_pos(self) -> tuple[int, int]:
        return (640, 480)

    def screen_size(self) -> tuple[int, int]:
        return (1920, 1080)

    def release_all(self) -> None:
        self._add("release_all")

    def log(self) -> list[tuple[Any, ...]]:
        with self._lock:
            return [(name, *args) for _, name, args in self.calls]


def wait_until(app: QApplication, predicate: Callable[[], bool], timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    app.processEvents()
    return predicate()


@pytest.fixture(autouse=True)
def no_real_input(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: Any) -> int:
        raise AssertionError("se intentó llamar a SendInput real")

    monkeypatch.setattr(winput, "_SendInput", forbidden)


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance() or QApplication([])
    from macrotool.ui import theme

    theme.apply_theme(app)
    return app


@pytest.fixture
def window(qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MACROTOOL_DATA_DIR", str(tmp_path))
    from macrotool.ui.main_window import MainWindow

    backend = FakeBackend()
    win = MainWindow(start_hooks=False, backend=backend)
    win.test_backend = backend  # type: ignore[attr-defined]
    yield win
    win._quitting = True
    win.close()
    qapp.processEvents()


def attach_trigger_manager(win: Any, fired: list[tuple[str, bool]], *,
                           own_foreground: Callable[[], bool] = lambda: False) -> InputHook:
    """TriggerManager real sobre un InputHook sin instalar, conectado al puente Qt de la ventana."""

    def on_trigger(binding_id: str, pressed: bool) -> None:  # hilo "del hook"
        fired.append((binding_id, pressed))
        win.bridge.on_trigger(binding_id, pressed)

    hook = InputHook()
    manager = TriggerManager(hook, on_trigger, key_state=lambda vk: False, foreground_check=own_foreground,
                             mask_menu_key=lambda: pytest.fail("no debe enviarse la tecla de máscara"))
    win.triggers = manager
    win._update_bindings()
    assert win.binding_errors() == {}
    return hook


def key(kind: str, token: str, vk: int, *, injected: bool = False) -> RawEvent:
    return RawEvent(time.perf_counter(), kind, token, vk, injected=injected)


def test_physical_key_1_runs_combo_1_end_to_end(window, qapp) -> None:
    win = window
    backend: FakeBackend = win.test_backend
    combo1 = next(m for m in win.macros if m.trigger == "1")
    assert combo1.name == "Combo 1" and combo1.enabled
    assert [s.inputs for s in combo1.steps] == CHORDS
    combo1.humanize = False  # secuencia y tiempos exactos
    combo1.default_delay_ms = 40  # más rápido; los 150 ms reales se comprueban en test_engine
    win.select_macro(combo1.id)

    fired: list[tuple[str, bool]] = []
    hook = attach_trigger_manager(win, fired)
    highlighted: list[tuple[int, list[int], str]] = []

    def observe(event: Any) -> None:  # se ejecuta tras el slot de la ventana (misma conexión en cola)
        if event.kind == "step":
            from macrotool.ui.widgets import ROLE_RUNNING

            tree = win.step_tree
            rows = [i for i in range(tree.topLevelItemCount()) if tree.topLevelItem(i).data(0, ROLE_RUNNING)]
            highlighted.append((event.step_index, rows, win.status_text()))

    win.bridge.player_event.connect(observe)

    blocked: dict[str, bool] = {}

    def physical_press() -> None:  # como el hilo del hook
        blocked["down"] = hook.dispatch(key("key_down", "1", 0x31))
        blocked["repeat"] = hook.dispatch(key("key_down", "1", 0x31))
        blocked["up"] = hook.dispatch(key("key_up", "1", 0x31))

    thread = threading.Thread(target=physical_press)
    thread.start()
    thread.join()
    assert blocked == {"down": True, "repeat": True, "up": True}
    assert fired == [(f"macro:{combo1.id}", True), (f"macro:{combo1.id}", False)]

    assert wait_until(qapp, lambda: backend.log()[-1:] == [("release_all",)] and not win._is_running)
    assert win.player.wait(2.0)
    expected: list[tuple[Any, ...]] = []
    for chord in CHORDS:
        expected += [("press", chord), ("release", list(reversed(chord)))]
    assert backend.log() == expected + [("release_all",)]
    t = [c[0] for c in backend.calls]
    for i in range(0, 10, 2):
        assert 47 <= (t[i + 1] - t[i]) * 1000 <= 90  # pulsación de 50 ms
    for i in range(1, 9, 2):
        assert 37 <= (t[i + 1] - t[i]) * 1000 <= 80  # retardo entre acciones de 40 ms

    assert [h[0] for h in highlighted] == [0, 1, 2, 3, 4]
    assert all(rows == [index] for index, rows, _ in highlighted)
    assert [h[2] for h in highlighted] == [f"Ejecutando paso {k}/5 · repetición 1/1" for k in range(1, 6)]
    assert win.status_text() == "Lista" and win._running_step is None
    assert "terminada" in win.message_text()


def test_injected_and_filtered_keys_do_not_trigger(window, qapp) -> None:
    win = window
    own = {"foreground": False}
    fired: list[tuple[str, bool]] = []
    hook = attach_trigger_manager(win, fired, own_foreground=lambda: own["foreground"])

    # Inyectada (p. ej. por la propia macro): ni se bloquea ni dispara.
    assert hook.dispatch(key("key_down", "1", 0x31, injected=True)) is False
    assert hook.dispatch(key("key_up", "1", 0x31, injected=True)) is False
    # Tecla sin disparador.
    assert hook.dispatch(key("key_down", "a", 0x41)) is False
    assert hook.dispatch(key("key_up", "a", 0x41)) is False
    assert fired == []

    # MacroTool en primer plano: las macros no se disparan; los atajos de control sí.
    own["foreground"] = True
    assert hook.dispatch(key("key_down", "1", 0x31)) is False
    hook.dispatch(key("key_up", "1", 0x31))
    assert fired == []
    assert hook.dispatch(key("key_down", "f7", 0x76)) is True
    assert hook.dispatch(key("key_up", "f7", 0x76)) is True
    assert fired == [("control:stop", True), ("control:stop", False)]
    own["foreground"] = False
    fired.clear()

    # Interruptor global apagado: la tecla 1 pasa sin disparar.
    win.set_triggers_enabled(False)
    assert hook.dispatch(key("key_down", "1", 0x31)) is False
    hook.dispatch(key("key_up", "1", 0x31))
    assert fired == []
    win.set_triggers_enabled(True)
    qapp.processEvents()
    assert not win.controller.is_running


class FakeTray:
    """Icono de bandeja visible (la plataforma offscreen no tiene bandeja)."""

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.visible = True

    def isVisible(self) -> bool:  # noqa: N802 - API de Qt
        return self.visible

    def showMessage(self, _title: str, text: str, *_args: Any) -> None:  # noqa: N802
        self.messages.append(text)

    def hide(self) -> None:
        self.visible = False

    def setToolTip(self, _text: str) -> None:  # noqa: N802
        pass


def test_close_hides_to_tray_and_quit_really_exits(window, qapp) -> None:
    win = window
    win.show()
    qapp.processEvents()
    win._tray = FakeTray()
    assert win.settings.minimize_to_tray
    win.close()  # botón cerrar: se oculta en la bandeja y avisa la primera vez
    qapp.processEvents()
    assert not win.isVisible() and not win._shut_down
    assert len(win._tray.messages) == 1
    win.show()
    win.close()
    assert len(win._tray.messages) == 1  # el aviso sólo la primera vez
    win.quit_app()  # «Salir» de la bandeja: cierre real
    assert win._shut_down and win._tray.visible is False
