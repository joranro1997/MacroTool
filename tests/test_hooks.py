"""Pruebas de hooks: estructuras, traducción de eventos, reparto a listeners, instalación breve
del hook real (sin inyectar nada) y la lógica pura de coincidencia de TriggerManager."""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

import pytest

from macrotool import hooks, winput
from macrotool.hooks import Binding, InputHook, RawEvent, TriggerManager
from macrotool.winput import MACROTOOL_EXTRA_INFO


@pytest.fixture(autouse=True)
def no_real_input(monkeypatch):
    """Salvaguarda: ninguna prueba de este módulo puede inyectar entrada real."""

    def forbidden(*args):
        raise AssertionError("SendInput real llamado desde una prueba")

    monkeypatch.setattr(winput, "_SendInput", forbidden)


# --- Estructuras y prototipos ----------------------------------------------------------
def test_struct_sizes_x64():
    assert ctypes.sizeof(hooks.KBDLLHOOKSTRUCT) == 24
    assert ctypes.sizeof(hooks.MSLLHOOKSTRUCT) == 32
    assert hooks.MSLLHOOKSTRUCT.dwExtraInfo.offset == 24
    assert ctypes.sizeof(hooks.MSG) == 48


def test_prototypes_declared():
    assert hooks._CallNextHookEx.argtypes == [wintypes.HANDLE, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t]
    assert hooks._CallNextHookEx.restype is ctypes.c_ssize_t
    assert hooks._SetWindowsHookExW.argtypes[1] is hooks.HOOKPROC
    assert hooks._SetWindowsHookExW.restype is wintypes.HANDLE
    assert hooks._UnhookWindowsHookEx.argtypes == [wintypes.HANDLE]
    assert hooks._PostThreadMessageW.argtypes == [wintypes.DWORD, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
    assert hooks._PeekMessageW.argtypes[0] == ctypes.POINTER(hooks.MSG)
    assert hooks._GetModuleHandleW.argtypes == [wintypes.LPCWSTR]


# --- Traducción de eventos ------------------------------------------------------------------
def test_parse_keyboard():
    ev = hooks.parse_keyboard(hooks.WM_KEYDOWN, 0x41, 0, 0, 1.5)
    assert ev == RawEvent(1.5, "key_down", "a", 0x41)
    assert hooks.parse_keyboard(hooks.WM_SYSKEYUP, 0xA4, 0x20 | 0x80, 0, 0).kind == "key_up"
    assert hooks.parse_keyboard(hooks.WM_KEYDOWN, 0x0D, hooks.LLKHF_EXTENDED, 0, 0).token == "num_enter"
    assert hooks.parse_keyboard(hooks.WM_KEYDOWN, 0x0D, 0, 0, 0).token == "enter"
    assert hooks.parse_keyboard(hooks.WM_KEYDOWN, 0xA2, 0, 0, 0).token == "ctrl"
    assert hooks.parse_keyboard(hooks.WM_KEYDOWN, 0xA3, hooks.LLKHF_EXTENDED, 0, 0).token == "rctrl"
    assert hooks.parse_keyboard(hooks.WM_KEYDOWN, 0xC0, 0, 0, 0).token == "vk_c0"
    assert hooks.parse_keyboard(0x0999, 0x41, 0, 0, 0) is None


def test_parse_keyboard_drops_altgr_fake_ctrl():
    assert hooks.parse_keyboard(hooks.WM_KEYDOWN, 0xA2, 0, 0, 0, scan_code=0x21D) is None
    assert hooks.parse_keyboard(hooks.WM_KEYDOWN, 0xA2, 0, 0, 0, scan_code=0x1D).token == "ctrl"
    altgr = hooks.parse_keyboard(hooks.WM_SYSKEYDOWN, 0xA5, hooks.LLKHF_EXTENDED, 0, 0, scan_code=0x38)
    assert altgr.token == "ralt"


def test_parse_keyboard_injected():
    assert not hooks.parse_keyboard(hooks.WM_KEYDOWN, 0x41, 0, 0, 0).injected
    assert hooks.parse_keyboard(hooks.WM_KEYDOWN, 0x41, hooks.LLKHF_INJECTED, 0, 0).injected
    assert hooks.parse_keyboard(hooks.WM_KEYDOWN, 0x41, 0, MACROTOOL_EXTRA_INFO, 0).injected


def test_parse_mouse_buttons():
    cases = {
        hooks.WM_LBUTTONDOWN: ("mouse_down", "mouse_left", 1),
        hooks.WM_LBUTTONUP: ("mouse_up", "mouse_left", 1),
        hooks.WM_RBUTTONDOWN: ("mouse_down", "mouse_right", 2),
        hooks.WM_RBUTTONUP: ("mouse_up", "mouse_right", 2),
        hooks.WM_MBUTTONDOWN: ("mouse_down", "mouse_middle", 4),
        hooks.WM_MBUTTONUP: ("mouse_up", "mouse_middle", 4),
    }
    for msg, (kind, token, vk) in cases.items():
        ev = hooks.parse_mouse(msg, 10, -20, 0, 0, 0, 2.0)
        assert (ev.kind, ev.token, ev.vk, ev.x, ev.y) == (kind, token, vk, 10, -20)
    x1 = hooks.parse_mouse(hooks.WM_XBUTTONDOWN, 0, 0, 0x0001 << 16, 0, 0, 0)
    x2 = hooks.parse_mouse(hooks.WM_XBUTTONUP, 0, 0, 0x0002 << 16, 0, 0, 0)
    assert (x1.kind, x1.token, x1.vk) == ("mouse_down", "mouse_x1", 5)
    assert (x2.kind, x2.token, x2.vk) == ("mouse_up", "mouse_x2", 6)
    assert hooks.parse_mouse(hooks.WM_XBUTTONDOWN, 0, 0, 0x0003 << 16, 0, 0, 0) is None


def test_parse_mouse_wheel_move_injected():
    up = hooks.parse_mouse(hooks.WM_MOUSEWHEEL, 5, 6, 120 << 16, 0, 0, 0)
    down = hooks.parse_mouse(hooks.WM_MOUSEWHEEL, 5, 6, ((-240) & 0xFFFF) << 16, 0, 0, 0)
    right = hooks.parse_mouse(hooks.WM_MOUSEHWHEEL, 5, 6, 120 << 16, 0, 0, 0)
    assert (up.kind, up.delta, up.token) == ("wheel", 120, "")
    assert down.delta == -240
    assert (right.kind, right.delta) == ("hwheel", 120)
    move = hooks.parse_mouse(hooks.WM_MOUSEMOVE, 300, 400, 0, 0, 0, 0)
    assert (move.kind, move.x, move.y) == ("move", 300, 400)
    assert hooks.parse_mouse(hooks.WM_LBUTTONDOWN, 0, 0, 0, hooks.LLMHF_INJECTED, 0, 0).injected
    assert hooks.parse_mouse(hooks.WM_MOUSEMOVE, 0, 0, 0, 0, MACROTOOL_EXTRA_INFO, 0).injected
    assert hooks.parse_mouse(0x02A3, 0, 0, 0, 0, 0, 0) is None


# --- Reparto a listeners ----------------------------------------------------------------------
def ev(kind: str, token: str = "", t: float = 0.0, **kw) -> RawEvent:
    return RawEvent(t, kind, token, **kw)


def test_dispatch_priority_and_blocking():
    hook = InputHook()
    order: list[str] = []
    hook.add_listener(lambda e: order.append(f"low:{e.consumed}"), priority=-10)
    hook.add_listener(lambda e: order.append("high") or True, priority=50)
    hook.add_listener(lambda e: order.append(f"mid:{e.consumed}"), priority=0)
    event = ev("key_down", "a")
    assert hook.dispatch(event) is True
    assert event.consumed
    assert order == ["high", "mid:True", "low:True"]


def test_dispatch_survives_listener_errors(caplog):
    hook = InputHook()
    seen: list[str] = []

    def broken(e):
        raise RuntimeError("fallo de prueba")

    hook.add_listener(broken, priority=10)
    hook.add_listener(lambda e: seen.append(e.token))
    assert hook.dispatch(ev("key_down", "b")) is False
    assert seen == ["b"]
    assert "listener" in caplog.text


def test_remove_listener_and_readd_updates_priority():
    hook = InputHook()
    order: list[str] = []
    a = lambda e: order.append("a")  # noqa: E731
    b = lambda e: order.append("b")  # noqa: E731
    hook.add_listener(a, priority=1)
    hook.add_listener(b, priority=2)
    hook.add_listener(a, priority=3)  # volver a añadir cambia la prioridad, no duplica
    hook.dispatch(ev("key_down", "x"))
    assert order == ["a", "b"]
    hook.remove_listener(a)
    hook.remove_listener(a)  # idempotente
    order.clear()
    hook.dispatch(ev("key_down", "x"))
    assert order == ["b"]


def test_moves_only_for_listeners_that_want_them():
    hook = InputHook()
    got: list[str] = []
    hook.add_listener(lambda e: got.append("no-moves:" + e.kind), wants_moves=False)
    assert not hook._want_moves
    hook.dispatch(ev("move", x=1, y=2))
    assert got == []
    hook.add_listener(lambda e: got.append("moves:" + e.kind))
    assert hook._want_moves
    hook.dispatch(ev("move"))
    assert got == ["moves:move"]


def test_hook_procs_without_installing(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(hooks, "_CallNextHookEx", lambda *a: calls.append(a) or 0)
    hook = InputHook()
    received: list[RawEvent] = []
    hook.add_listener(lambda e: received.append(e) or e.token == "q", wants_moves=False)

    kb = hooks.KBDLLHOOKSTRUCT(vkCode=0x51, scanCode=0x10, flags=0, time=0, dwExtraInfo=0)
    assert hook._keyboard_proc(hooks.HC_ACTION, hooks.WM_KEYDOWN, ctypes.addressof(kb)) == 1  # bloqueado
    assert calls == []
    kb.vkCode = 0x57  # "w": no se bloquea → CallNextHookEx
    assert hook._keyboard_proc(hooks.HC_ACTION, hooks.WM_KEYUP, ctypes.addressof(kb)) == 0
    assert len(calls) == 1
    assert [(e.kind, e.token) for e in received] == [("key_down", "q"), ("key_up", "w")]

    ms = hooks.MSLLHOOKSTRUCT(mouseData=0x0002 << 16, flags=0, time=0, dwExtraInfo=MACROTOOL_EXTRA_INFO)
    ms.pt.x, ms.pt.y = 7, 8
    hook._mouse_proc(hooks.HC_ACTION, hooks.WM_XBUTTONDOWN, ctypes.addressof(ms))
    assert received[-1] == RawEvent(received[-1].t, "mouse_down", "mouse_x2", 6, 7, 8, injected=True)
    n = len(received)
    hook._mouse_proc(hooks.HC_ACTION, hooks.WM_MOUSEMOVE, ctypes.addressof(ms))  # nadie quiere movimientos
    assert len(received) == n
    hook._keyboard_proc(-1, hooks.WM_KEYDOWN, ctypes.addressof(kb))  # nCode < 0: sólo se pasa
    assert len(received) == n and len(calls) == 4


# --- Instalación real (breve, sin inyectar nada) ---------------------------------------------------
def test_install_and_uninstall_real_hooks():
    hook = InputHook()
    hook.add_listener(lambda e: None, wants_moves=False)
    t0 = time.perf_counter()
    hook.start()
    try:
        assert hook.running
        hook.start()  # ya en marcha: no hace nada
        assert hook.running
    finally:
        hook.stop()
    assert not hook.running
    hook.stop()  # idempotente
    hook.start()
    assert hook.running
    hook.stop()
    assert not hook.running
    assert time.perf_counter() - t0 < 1.0


def test_install_failure_is_reported(monkeypatch):
    def failing(*args):
        ctypes.set_last_error(5)
        return None

    monkeypatch.setattr(hooks, "_SetWindowsHookExW", failing)
    hook = InputHook()
    with pytest.raises(RuntimeError, match="No se pudieron instalar"):
        hook.start()
    assert not hook.running
    hook.stop()


def test_atexit_cleanup_registered():
    hook = InputHook()
    assert hook in hooks._live_hooks
    hooks._stop_all_hooks()  # no falla aunque no estén en marcha


# --- TriggerManager -------------------------------------------------------------------------
class FakeHook:
    def __init__(self) -> None:
        self.listeners: list[tuple] = []
        self.running = True

    def add_listener(self, fn, priority=0, *, wants_moves=True):
        self.listeners.append((fn, priority, wants_moves))

    def remove_listener(self, fn):
        self.listeners = [entry for entry in self.listeners if entry[0] != fn]


class Harness:
    """TriggerManager con dobles de prueba para GetAsyncKeyState, primer plano y tecla de máscara."""

    def __init__(self, bindings: list[Binding]) -> None:
        self.hook = FakeHook()
        self.fired: list[tuple[str, bool]] = []
        self.released_vks: set[int] = set()  # VK que GetAsyncKeyState da por soltados
        self.own_foreground = False
        self.foreground_calls = 0
        self.masks = 0
        self.tm = TriggerManager(
            self.hook,
            lambda bid, pressed: self.fired.append((bid, pressed)),
            key_state=lambda vk: vk not in self.released_vks,
            foreground_check=self._foreground,
            mask_menu_key=self._mask,
        )
        self.errors = self.tm.set_bindings(bindings)

    def _foreground(self) -> bool:
        self.foreground_calls += 1
        return self.own_foreground

    def _mask(self) -> None:
        self.masks += 1

    def down(self, token: str, t: float = 0.0, **kw) -> bool:
        kind = "mouse_down" if token.startswith("mouse_") else "key_down"
        return self.tm.handle_event(RawEvent(t, kind, token, **kw))

    def up(self, token: str, t: float = 0.0, **kw) -> bool:
        kind = "mouse_up" if token.startswith("mouse_") else "key_up"
        return self.tm.handle_event(RawEvent(t, kind, token, **kw))

    def tap(self, token: str, t: float = 0.0) -> tuple[bool, bool]:
        return self.down(token, t), self.up(token, t + 0.05)


def test_registers_high_priority_listener_without_moves():
    h = Harness([])
    ((fn, priority, wants_moves),) = h.hook.listeners
    assert fn == h.tm.handle_event and priority == TriggerManager.PRIORITY and wants_moves is False
    h.tm.close()
    assert h.hook.listeners == []


def test_simple_trigger_blocks_down_and_up():
    h = Harness([Binding("macro:m1", "1")])
    assert h.errors == {}
    assert h.down("1") is True
    assert h.fired == [("macro:m1", True)]
    assert h.up("1") is True
    assert h.fired == [("macro:m1", True), ("macro:m1", False)]
    assert h.tap("2") == (False, False)
    assert len(h.fired) == 2


def test_non_blocking_binding_still_fires():
    h = Harness([Binding("macro:m1", "1", block=False)])
    assert h.tap("1") == (False, False)
    assert h.fired == [("macro:m1", True), ("macro:m1", False)]


def test_binding_without_modifiers_is_wildcard():
    h = Harness([Binding("macro:m1", "1")])
    h.down("shift")
    h.down("rctrl")
    assert h.down("1") is True
    assert h.fired == [("macro:m1", True)]


def test_modifier_bindings_require_exact_modifiers():
    h = Harness([Binding("macro:m1", "ctrl+1")])
    assert h.tap("1") == (False, False)  # sin Ctrl
    h.down("ctrl")  # Ctrl izquierdo (así lo entrega el hook)
    assert h.tap("1") == (True, True)
    h.down("shift")  # Ctrl+Mayús+1: modificador de más
    assert h.tap("1") == (False, False)
    h.up("shift")
    h.up("ctrl")
    h.down("rctrl")  # "ctrl" genérico acepta también el derecho
    assert h.tap("1") == (True, True)
    assert h.fired == [("macro:m1", True), ("macro:m1", False)] * 2


def test_specific_side_modifiers():
    h = Harness([Binding("macro:r", "rctrl+1"), Binding("macro:l", "lctrl+2")])
    assert h.errors == {}
    h.down("ctrl")  # izquierdo
    assert h.tap("1") == (False, False)
    assert h.tap("2") == (True, True)
    h.up("ctrl")
    h.down("rctrl")
    assert h.tap("1") == (True, True)
    assert h.tap("2") == (False, False)
    assert [f for f in h.fired if f[1]] == [("macro:l", True), ("macro:r", True)]


def test_most_specific_binding_wins():
    h = Harness([Binding("macro:plain", "1"), Binding("macro:ctrl", "ctrl+1")])
    h.down("ctrl")
    h.tap("1")
    h.up("ctrl")
    h.tap("1")
    assert [f for f in h.fired if f[1]] == [("macro:ctrl", True), ("macro:plain", True)]


def test_autorepeat_does_not_refire_but_is_blocked():
    h = Harness([Binding("macro:m1", "1")])
    assert h.down("1", t=0.0) is True
    assert h.down("1", t=0.5) is True
    assert h.down("1", t=0.53) is True
    assert h.up("1", t=0.6) is True
    assert h.fired == [("macro:m1", True), ("macro:m1", False)]


def test_autorepeat_of_non_blocking_binding_passes():
    h = Harness([Binding("macro:m1", "1", block=False)])
    assert [h.down("1", t=i * 0.03) for i in range(4)] == [False] * 4
    assert h.fired == [("macro:m1", True)]


def test_autorepeat_of_untracked_key_while_trigger_binding_added_later():
    h = Harness([])
    h.down("1", t=0.0)  # se pulsó antes de configurar el disparador
    h.tm.set_bindings([Binding("macro:m1", "1")])
    assert h.down("1", t=0.4) is False  # autorepetición: no dispara
    assert h.up("1", t=0.5) is False
    assert h.down("1", t=1.0) is True
    assert h.fired == [("macro:m1", True)]


def test_injected_events_are_ignored():
    h = Harness([Binding("macro:m1", "1"), Binding("macro:m2", "ctrl+2")])
    assert h.down("1", injected=True) is False
    assert h.up("1", injected=True) is False
    h.down("ctrl", injected=True)  # un Ctrl inyectado no cuenta como modificador pulsado
    assert h.tap("2") == (False, False)
    assert h.fired == []


def test_injected_up_does_not_end_physical_press():
    h = Harness([Binding("macro:m1", "1")])
    h.down("1")
    assert h.up("1", injected=True) is False  # la macro suelta "1": no es el up del usuario
    assert h.up("1") is True
    assert h.fired == [("macro:m1", True), ("macro:m1", False)]


def test_suspended_lets_everything_through():
    h = Harness([Binding("macro:m1", "1"), Binding("control:stop", "f7", control=True)])
    h.tm.set_suspended(True)
    assert h.tap("1") == (False, False)
    assert h.tap("f7") == (False, False)
    assert h.fired == []
    h.tm.set_suspended(False)
    assert h.tap("1") == (True, True)


def test_active_trigger_finishes_consistently_after_suspension():
    h = Harness([Binding("macro:m1", "1")])
    assert h.down("1") is True
    h.tm.set_suspended(True)
    assert h.down("1", t=0.3) is True  # autorepetición del disparador activo
    assert h.up("1", t=0.4) is True  # el up se bloquea igual que el down
    assert h.fired == [("macro:m1", True), ("macro:m1", False)]


def test_up_consistent_when_bindings_change_mid_press():
    h = Harness([Binding("macro:m1", "1")])
    assert h.down("1") is True
    h.tm.set_bindings([])
    assert h.up("1") is True
    assert h.fired == [("macro:m1", True), ("macro:m1", False)]


def test_macro_triggers_can_be_disabled_but_controls_work():
    h = Harness([Binding("macro:m1", "1"), Binding("control:enable", "f9", control=True)])
    h.tm.set_macro_triggers_enabled(False)
    assert h.tap("1") == (False, False)
    assert h.tap("f9") == (True, True)
    h.tm.set_macro_triggers_enabled(True)
    assert h.tap("1") == (True, True)
    assert [f[0] for f in h.fired if f[1]] == ["control:enable", "macro:m1"]


def test_ignore_when_own_process_in_foreground():
    h = Harness([Binding("macro:m1", "1"), Binding("control:stop", "f7", control=True)])
    h.tm.set_ignore_when_own_foreground(True)
    h.own_foreground = True
    assert h.tap("1") == (False, False)
    assert h.tap("f7") == (True, True)
    assert h.foreground_calls == 1  # sólo en el down de una macro candidata
    h.tap("a")  # sin binding: no se consulta el primer plano
    h.tm.handle_event(RawEvent(0, "move", x=1, y=1))
    assert h.foreground_calls == 1
    h.own_foreground = False
    assert h.tap("1") == (True, True)
    h.tm.set_ignore_when_own_foreground(False)
    h.own_foreground = True
    assert h.tap("1") == (True, True)
    assert h.foreground_calls == 2


def test_mouse_side_button_trigger():
    h = Harness([Binding("macro:m1", "mouse_x1")])
    assert h.tap("mouse_x1") == (True, True)
    assert h.tap("mouse_x2") == (False, False)
    assert h.fired == [("macro:m1", True), ("macro:m1", False)]


def test_mouse_left_with_modifier_is_allowed():
    h = Harness([Binding("macro:m1", "ctrl+mouse_left")])
    assert h.errors == {}
    assert h.tap("mouse_left") == (False, False)
    h.down("ctrl")
    assert h.tap("mouse_left") == (True, True)


def test_modifier_only_combo_uses_last_token_as_main():
    h = Harness([Binding("macro:m1", "ctrl+shift")])
    h.down("ctrl")
    assert h.down("shift") is True
    assert h.up("shift") is True
    h.up("ctrl")
    h.down("shift")
    assert h.down("ctrl") is False  # orden inverso: la principal es Mayús
    assert h.fired == [("macro:m1", True), ("macro:m1", False)]


def test_non_modifier_keys_in_combo_must_be_held():
    h = Harness([Binding("macro:m1", "a+b")])
    assert h.tap("b") == (False, False)
    h.down("a")
    assert h.tap("b") == (True, True)


def test_stale_modifier_is_pruned_with_async_state():
    h = Harness([Binding("macro:ctrl", "ctrl+1"), Binding("macro:shift", "shift+2")])
    h.down("ctrl")  # su up se pierde (p. ej. Win+L)
    h.released_vks.add(0xA2)  # Windows lo da por soltado
    assert h.tap("1") == (False, False)
    h.down("shift")
    assert h.tap("2") == (True, True)  # el Ctrl obsoleto no cuenta como modificador de más
    assert [f for f in h.fired if f[1]] == [("macro:shift", True)]


def test_blocked_modifier_main_is_trusted_over_async_state():
    h = Harness([Binding("macro:alt", "alt"), Binding("macro:alt1", "alt+1")])
    h.released_vks.add(0xA4)  # bloqueamos su down: GetAsyncKeyState no lo ve
    assert h.down("alt") is True
    assert h.tap("1") == (True, True)
    assert [f[0] for f in h.fired if f[1]] == ["macro:alt", "macro:alt1"]


def test_missed_up_of_trigger_does_not_disable_it():
    h = Harness([Binding("macro:m1", "1")])
    assert h.down("1", t=0.0) is True
    # el up se perdió; mucho después se vuelve a pulsar
    assert h.down("1", t=10.0) is True
    assert h.fired == [("macro:m1", True), ("macro:m1", False), ("macro:m1", True)]


def test_repeated_mouse_down_is_never_autorepeat():
    h = Harness([Binding("macro:m1", "mouse_x2")])
    h.down("mouse_x2", t=0.0)
    h.down("mouse_x2", t=0.01)  # sin up entre medias: se perdió el up
    assert h.fired == [("macro:m1", True), ("macro:m1", False), ("macro:m1", True)]


def test_menu_mask_for_alt_and_win_combos():
    h = Harness([
        Binding("macro:a", "alt+1"),
        Binding("macro:w", "win+2"),
        Binding("macro:c", "ctrl+3"),
        Binding("macro:nb", "alt+4", block=False),
    ])
    h.down("alt")
    h.tap("1")
    assert h.masks == 1
    h.tap("4")
    assert h.masks == 1  # no bloqueado: la aplicación ya ve Alt+4
    h.up("alt")
    h.down("win")
    h.tap("2")
    h.up("win")
    h.down("ctrl")
    h.tap("3")
    assert h.masks == 2


def test_consumed_events_do_not_fire():
    h = Harness([Binding("macro:m1", "1")])
    assert h.tm.handle_event(RawEvent(0, "key_down", "1", consumed=True)) is False
    assert h.fired == []


def test_on_trigger_errors_do_not_change_blocking():
    hook = FakeHook()

    def boom(bid, pressed):
        raise RuntimeError("fallo")

    tm = TriggerManager(hook, boom, key_state=lambda vk: True, foreground_check=lambda: False,
                        mask_menu_key=lambda: None)
    tm.set_bindings([Binding("macro:m1", "1")])
    assert tm.handle_event(RawEvent(0, "key_down", "1")) is True
    assert tm.handle_event(RawEvent(0, "key_up", "1")) is True


def test_non_press_events_ignored():
    h = Harness([Binding("macro:m1", "1")])
    for kind in ("move", "wheel", "hwheel"):
        assert h.tm.handle_event(RawEvent(0, kind, delta=120)) is False
    assert h.fired == []


# --- Validación de set_bindings -----------------------------------------------------------------
def test_set_bindings_validation_messages():
    h = Harness([
        Binding("macro:ok", "ctrl+1"),
        Binding("macro:bad", "ctrl+teclarara"),
        Binding("macro:dup", "1 + Ctrl"),
        Binding("macro:left", "mouse_left"),
        Binding("macro:right", "Clic derecho"),
        Binding("macro:rocker", "mouse_x1+mouse_left"),
        Binding("macro:empty", ""),
        Binding("macro:incomplete", "ctrl+"),
    ])
    assert set(h.errors) == {"macro:bad", "macro:dup", "macro:left", "macro:right", "macro:rocker", "macro:incomplete"}
    assert "no válido" in h.errors["macro:bad"]
    assert "ya lo usa otra macro" in h.errors["macro:dup"]
    assert "Ctrl + 1" in h.errors["macro:dup"]
    assert "clic izquierdo o derecho" in h.errors["macro:left"]
    assert "Clic derecho" in h.errors["macro:right"]
    # los válidos siguen funcionando
    h.down("ctrl")
    assert h.tap("1") == (True, True)
    assert [f for f in h.fired if f[1]] == [("macro:ok", True)]


def test_control_bindings_win_conflicts():
    h = Harness([Binding("macro:m1", "f7"), Binding("control:stop", "F7", control=True)])
    assert list(h.errors) == ["macro:m1"]
    assert "atajo global" in h.errors["macro:m1"]
    h.tap("f7")
    assert [f for f in h.fired if f[1]] == [("control:stop", True)]


def test_generic_and_specific_modifiers_are_not_conflicts():
    h = Harness([Binding("macro:g", "ctrl+1"), Binding("macro:l", "lctrl+1")])
    assert h.errors == {}
    h.down("ctrl")
    h.tap("1")
    h.up("ctrl")
    h.down("rctrl")
    h.tap("1")
    assert [f[0] for f in h.fired if f[1]] == ["macro:l", "macro:g"]


# --- Integración con InputHook.dispatch (sin instalar hooks) ------------------------------------------
def test_trigger_manager_with_real_dispatch():
    hook = InputHook()
    fired: list[tuple[str, bool]] = []
    tm = TriggerManager(hook, lambda b, p: fired.append((b, p)), key_state=lambda vk: True,
                        foreground_check=lambda: False, mask_menu_key=lambda: None)
    tm.set_bindings([Binding("macro:m1", "1")])
    later: list[RawEvent] = []
    hook.add_listener(lambda e: later.append(e), priority=-100)
    down = RawEvent(0, "key_down", "1", 0x31)
    assert hook.dispatch(down) is True
    assert later[-1].consumed
    assert hook.dispatch(RawEvent(0.05, "key_up", "1", 0x31)) is True
    assert hook.dispatch(RawEvent(0.1, "key_down", "2", 0x32)) is False
    assert fired == [("macro:m1", True), ("macro:m1", False)]


# --- AltGr, teclas "fantasma", teclado numérico y parada durante la captura -------------------------
def test_input_hook_marks_ralt_after_fake_ctrl_as_altgr():
    hook = InputHook()
    t = time.perf_counter()
    # Distribución con AltGr: Ctrl izquierdo ficticio (0x21D, se descarta) y Alt derecho.
    assert hook.keyboard_event(hooks.WM_KEYDOWN, 0xA2, 0, 0, t, hooks.SC_ALTGR_FAKE_LCTRL) is None
    altgr = hook.keyboard_event(hooks.WM_SYSKEYDOWN, 0xA5, hooks.LLKHF_EXTENDED, 0, t + 0.001, 0x38)
    assert altgr.token == "ralt" and altgr.altgr is True
    assert altgr.scan == 0xE038
    # Alt derecho sin el Ctrl ficticio (p. ej. teclado inglés): es un Alt normal.
    plain = hook.keyboard_event(hooks.WM_SYSKEYDOWN, 0xA5, hooks.LLKHF_EXTENDED, 0, t + 1.0, 0x38)
    assert plain.token == "ralt" and plain.altgr is False
    # El scancode identifica la tecla física (Num 1 / Fin comparten 0x4F; Fin es extendida).
    assert hook.keyboard_event(hooks.WM_KEYDOWN, 0x61, 0, 0, t, 0x4F).scan == 0x4F
    assert hook.keyboard_event(hooks.WM_KEYUP, 0x23, hooks.LLKHF_EXTENDED, 0, t, 0x4F).scan == 0xE04F


def test_generic_alt_trigger_ignores_altgr():
    """Teclado español: «Alt + 2» no debe saltar (ni tragarse la tecla) al escribir «@» con AltGr+2."""
    h = Harness([Binding("macro:m", "alt+2"), Binding("macro:alt", "alt")])
    assert h.down("ralt", 0.0, altgr=True) is False
    assert h.tap("2", 0.1) == (False, False)
    assert h.up("ralt", 0.2) is False
    assert h.fired == [] and h.masks == 0
    # Con Alt izquierdo sí.
    h.down("alt", 1.0)
    assert h.tap("2", 1.1) == (True, True)
    h.up("alt", 1.2)
    # Sin AltGr (teclado inglés) el Alt derecho es un Alt más.
    h.fired.clear()
    h.down("ralt", 2.0)
    assert h.tap("2", 2.1) == (True, True)
    assert [f[0] for f in h.fired if f[1]] == ["macro:alt", "macro:m"]


def test_explicit_altgr_trigger_still_works():
    h = Harness([Binding("macro:g", "ralt+2")])
    h.down("ralt", 0.0, altgr=True)
    assert h.tap("2", 0.1) == (True, True)
    assert h.fired == [("macro:g", True), ("macro:g", False)]


def test_required_key_with_lost_up_is_checked_against_windows():
    """Si se pierde el up de una tecla requerida (UAC, Ctrl+Alt+Supr), no queda "fantasma"."""
    h = Harness([Binding("macro:m", "w+mouse_x1")])
    h.down("w", 0.0)  # su up se pierde
    h.released_vks.add(0x57)  # Windows la da por soltada
    assert h.tap("mouse_x1", 30.0) == (False, False)
    assert h.fired == [] and "w" not in h.tm._pressed
    h.released_vks.clear()
    h.down("w", 31.0)
    assert h.tap("mouse_x1", 31.1) == (True, True)


def test_numpad_up_translated_to_another_vk_matches_its_down():
    """Num 1 con Bloq Num: si se suelta con Mayús pulsada, Windows entrega el up como Fin."""
    h = Harness([Binding("macro:m", "num1")])
    assert h.down("num1", 0.0, scan=0x4F) is True
    assert h.down("num1", 0.03, scan=0x4F) is True  # autorepetición
    assert h.down("end", 0.06, scan=0x4F) is True  # autorepetición traducida: misma tecla física
    h.down("shift", 0.1, scan=0x2A)
    h.up("shift", 0.2, scan=0x2A)  # Mayús ficticia que genera Windows
    assert h.up("end", 0.3, scan=0x4F) is True
    h.down("shift", 0.31, scan=0x2A)
    assert h.fired == [("macro:m", True), ("macro:m", False)]
    assert "num1" not in h.tm._pressed and h.tm._active == {}
    # Fin de verdad (tecla extendida, otro scancode) no se confunde con Num 1.
    assert h.down("end", 1.0, scan=0xE04F) is False
    assert h.up("end", 1.1, scan=0xE04F) is False


def test_stop_hotkey_can_stay_active_while_suspended():
    h = Harness([Binding("macro:m1", "1"), Binding("control:stop", "f7", control=True)])
    h.tm.set_suspended(True)
    assert h.tap("f7", 0.0) == (False, False)  # capturando una combinación: todo pasa
    h.tm.set_suspend_exceptions({"control:stop"})  # ...salvo la parada si corre una macro
    assert h.tap("f7", 1.0) == (True, True)
    assert h.tap("1", 2.0) == (False, False)
    assert h.fired == [("control:stop", True), ("control:stop", False)]
    h.tm.set_suspend_exceptions(())
    assert h.tap("f7", 3.0) == (False, False)
