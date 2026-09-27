"""Pruebas de winput: estructuras ctypes, construcción de INPUT y backend con SendInput falso.

NUNCA se llama al SendInput real: un fixture autouse lo sustituye en todo el módulo.
"""
from __future__ import annotations

import ctypes
import subprocess
import sys
import threading
from ctypes import wintypes
from pathlib import Path

import pytest

from macrotool import keys, winput
from macrotool.winput import (
    INPUT,
    INPUT_KEYBOARD,
    KEYEVENTF_EXTENDEDKEY,
    KEYEVENTF_KEYUP,
    KEYEVENTF_SCANCODE,
    KEYEVENTF_UNICODE,
    MACROTOOL_EXTRA_INFO,
    MOUSEEVENTF_ABSOLUTE,
    MOUSEEVENTF_HWHEEL,
    MOUSEEVENTF_MOVE,
    MOUSEEVENTF_VIRTUALDESK,
    MOUSEEVENTF_WHEEL,
    InputError,
    WinInputBackend,
)


class FakeSendInput:
    """Sustituto de user32.SendInput: registra cada lote y devuelve lo que se le indique."""

    def __init__(self) -> None:
        self.calls: list[list[dict]] = []
        self.result: int | None = None  # None → inserta todo
        self.lock = threading.Lock()

    def __call__(self, n, array, size):
        assert size == ctypes.sizeof(INPUT) == 40
        batch = [_describe(array[i]) for i in range(n)]
        with self.lock:
            self.calls.append(batch)
        return n if self.result is None else self.result


def _describe(inp: INPUT) -> dict:
    if inp.type == INPUT_KEYBOARD:
        return {"type": "key", "vk": inp.ki.wVk, "scan": inp.ki.wScan, "flags": inp.ki.dwFlags,
                "extra": inp.ki.dwExtraInfo}
    return {"type": "mouse", "dx": inp.mi.dx, "dy": inp.mi.dy, "data": inp.mi.mouseData,
            "flags": inp.mi.dwFlags, "extra": inp.mi.dwExtraInfo}


@pytest.fixture(autouse=True)
def fake_send(monkeypatch) -> FakeSendInput:
    fake = FakeSendInput()
    monkeypatch.setattr(winput, "_SendInput", fake)
    return fake


# --- Estructuras y prototipos --------------------------------------------------------
def test_struct_sizes_x64():
    assert ctypes.sizeof(ctypes.c_void_p) == 8
    assert ctypes.sizeof(winput.MOUSEINPUT) == 32
    assert ctypes.sizeof(winput.KEYBDINPUT) == 24
    assert ctypes.sizeof(winput.HARDWAREINPUT) == 8
    assert ctypes.sizeof(INPUT) == 40
    assert INPUT.u.offset == 8
    assert winput.MOUSEINPUT.dwExtraInfo.offset == 24
    assert winput.KEYBDINPUT.dwExtraInfo.offset == 16
    assert ctypes.sizeof(winput.ULONG_PTR) == 8
    assert ctypes.sizeof(winput.LPARAM) == 8 and ctypes.sizeof(winput.WPARAM) == 8


def test_extra_info_is_full_pointer_width():
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.ki.dwExtraInfo = 0xFFFF_FFFF_FFFF_FFFF
    assert inp.ki.dwExtraInfo == 0xFFFF_FFFF_FFFF_FFFF


def test_prototypes_declared():
    send = winput.user32.SendInput  # el objeto real (no se llama): sólo se comprueban los tipos
    assert send.restype is wintypes.UINT
    assert send.argtypes == [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    assert winput._GetCursorPos.argtypes == [ctypes.POINTER(wintypes.POINT)]
    assert winput._SetCursorPos.argtypes == [ctypes.c_int, ctypes.c_int]
    assert winput._GetSystemMetrics.restype is ctypes.c_int
    assert winput._GetAsyncKeyState.restype is ctypes.c_short
    assert winput._GetForegroundWindow.restype is wintypes.HWND
    assert winput._GetWindowThreadProcessId.argtypes == [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    assert winput._timeBeginPeriod.argtypes == [wintypes.UINT]
    assert winput._timeEndPeriod.restype is wintypes.UINT


# --- Teclas ----------------------------------------------------------------------------
def test_key_by_scancode():
    down = _describe(winput.key_input("a"))
    up = _describe(winput.key_input("a", up=True))
    assert down == {"type": "key", "vk": 0, "scan": keys.scan_code("a"), "flags": KEYEVENTF_SCANCODE,
                    "extra": MACROTOOL_EXTRA_INFO}
    assert up["flags"] == KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP
    assert down["scan"] != 0


@pytest.mark.parametrize("token", ["up", "delete", "home", "rctrl", "ralt", "num_divide", "num_enter", "apps"])
def test_extended_keys_by_scancode(token):
    d = _describe(winput.key_input(token))
    assert d["flags"] & KEYEVENTF_SCANCODE
    assert d["flags"] & KEYEVENTF_EXTENDEDKEY
    assert d["scan"] == keys.scan_code(token)


def test_generic_modifiers_sent_as_left():
    ctrl = _describe(winput.key_input("ctrl"))
    lctrl = _describe(winput.key_input("lctrl"))
    rctrl = _describe(winput.key_input("rctrl"))
    assert ctrl == lctrl
    assert not ctrl["flags"] & KEYEVENTF_EXTENDEDKEY
    assert rctrl["scan"] == ctrl["scan"] and rctrl["flags"] & KEYEVENTF_EXTENDEDKEY
    win = _describe(winput.key_input("win"))
    assert win == _describe(winput.key_input("lwin"))
    assert win["flags"] & KEYEVENTF_EXTENDEDKEY
    assert winput.send_token("Control") == "lctrl"
    assert winput.send_token("shift") == "lshift"


def test_key_by_vk_when_scancodes_disabled():
    d = _describe(winput.key_input("a", use_scancodes=False))
    assert d["vk"] == keys.token_to_vk("a")
    assert not d["flags"] & KEYEVENTF_SCANCODE
    ext = _describe(winput.key_input("delete", up=True, use_scancodes=False))
    assert ext["vk"] == 0x2E
    assert ext["flags"] == KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP


def test_pause_always_by_vk():
    d = _describe(winput.key_input("pause"))
    assert d["vk"] == 0x13
    assert not d["flags"] & KEYEVENTF_SCANCODE


def test_invalid_token_raises():
    with pytest.raises(ValueError):
        winput.key_input("no_existe")


# --- Ratón --------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "token, down_flag, up_flag, data",
    [
        ("mouse_left", 0x0002, 0x0004, 0),
        ("mouse_right", 0x0008, 0x0010, 0),
        ("mouse_middle", 0x0020, 0x0040, 0),
        ("mouse_x1", 0x0080, 0x0100, 1),
        ("mouse_x2", 0x0080, 0x0100, 2),
    ],
)
def test_mouse_buttons(token, down_flag, up_flag, data):
    down = _describe(winput.key_input(token))
    up = _describe(winput.key_input(token, up=True))
    assert down == {"type": "mouse", "dx": 0, "dy": 0, "data": data, "flags": down_flag, "extra": MACROTOOL_EXTRA_INFO}
    assert up["flags"] == up_flag and up["data"] == data


def test_mouse_alias_tokens():
    assert _describe(winput.key_input("lmb"))["flags"] == 0x0002
    with pytest.raises(ValueError):
        winput.mouse_button_input("a")


def test_build_inputs_keeps_order_and_mixes_devices():
    batch = [_describe(i) for i in winput.build_inputs(["w", "mouse_right"])]
    assert [b["type"] for b in batch] == ["key", "mouse"]
    assert batch[0]["scan"] == keys.scan_code("w")
    assert batch[1]["flags"] == 0x0008
    ups = [_describe(i) for i in winput.build_inputs(["mouse_right", "w"], up=True)]
    assert ups[0]["flags"] == 0x0010 and ups[1]["flags"] & KEYEVENTF_KEYUP


def test_unicode_bmp_and_surrogates():
    bmp = [_describe(i) for i in winput.unicode_inputs("ñ")]
    assert [(b["vk"], b["scan"], b["flags"]) for b in bmp] == [
        (0, 0xF1, KEYEVENTF_UNICODE),
        (0, 0xF1, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
    ]
    emoji = [_describe(i) for i in winput.unicode_inputs("\U0001F600")]
    assert [b["scan"] for b in emoji] == [0xD83D, 0xD83D, 0xDE00, 0xDE00]
    assert [b["flags"] & KEYEVENTF_KEYUP for b in emoji] == [0, KEYEVENTF_KEYUP, 0, KEYEVENTF_KEYUP]
    assert all(b["extra"] == MACROTOOL_EXTRA_INFO for b in emoji)
    with pytest.raises(ValueError):
        winput.unicode_inputs("ab")
    with pytest.raises(ValueError):
        winput.unicode_inputs("")


def test_scroll_inputs():
    up = [_describe(i) for i in winput.scroll_inputs(3)]
    assert len(up) == 3 and all(b["data"] == 120 and b["flags"] == MOUSEEVENTF_WHEEL for b in up)
    down = [_describe(i) for i in winput.scroll_inputs(-2)]
    assert len(down) == 2 and all(b["data"] == (-120) & 0xFFFFFFFF for b in down)
    assert ctypes.c_int32(down[0]["data"]).value == -120
    left = [_describe(i) for i in winput.scroll_inputs(-1, horizontal=True)]
    assert left[0]["flags"] == MOUSEEVENTF_HWHEEL
    assert winput.scroll_inputs(0) == []


def test_move_inputs():
    rel = _describe(winput.move_rel_input(-5, 7))
    assert (rel["dx"], rel["dy"], rel["flags"]) == (-5, 7, MOUSEEVENTF_MOVE)
    flags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
    a = _describe(winput.move_abs_input(0, 0, virtual_screen=(0, 0, 1920, 1080)))
    b = _describe(winput.move_abs_input(1919, 1079, virtual_screen=(0, 0, 1920, 1080)))
    c = _describe(winput.move_abs_input(-1920, 0, virtual_screen=(-1920, 0, 3840, 1080)))
    assert (a["dx"], a["dy"], a["flags"]) == (0, 0, flags)
    assert (b["dx"], b["dy"]) == (65535, 65535)
    assert c["dx"] == 0


# --- Envío ------------------------------------------------------------------------------------
def test_send_inputs_checks_result(fake_send):
    fake_send.result = 0
    with pytest.raises(InputError) as info:
        winput.send_inputs(winput.build_inputs(["a"]))
    assert str(info.value) == winput.BLOCKED_MESSAGE
    assert "administrador" in str(info.value)
    assert info.value.expected == 1 and info.value.inserted == 0
    fake_send.result = None
    winput.send_inputs([])  # lote vacío: no llama a SendInput
    assert len(fake_send.calls) == 1


def test_send_mask_key(fake_send):
    winput.send_mask_key()
    (batch,) = fake_send.calls
    assert [(b["vk"], b["flags"]) for b in batch] == [(0xE8, 0), (0xE8, KEYEVENTF_KEYUP)]
    assert all(b["extra"] == MACROTOOL_EXTRA_INFO for b in batch)


# --- Backend -------------------------------------------------------------------------------
def test_backend_press_release_single_batch(fake_send):
    b = WinInputBackend()
    b.press(["w", "mouse_right"])
    b.release(reversed(["w", "mouse_right"]))
    assert len(fake_send.calls) == 2
    down, up = fake_send.calls
    assert [d["type"] for d in down] == ["key", "mouse"]
    assert down[1]["flags"] == 0x0008
    assert [d["type"] for d in up] == ["mouse", "key"]
    assert up[1]["flags"] & KEYEVENTF_KEYUP
    assert b.pressed == []


def test_backend_uses_vk_when_configured(fake_send):
    WinInputBackend(use_scancodes=False).press(["a"])
    assert fake_send.calls[0][0]["vk"] == keys.token_to_vk("a")


def test_backend_release_all_reverse_order(fake_send):
    b = WinInputBackend()
    b.press(["ctrl"])
    b.press(["s", "mouse_left"])
    b.release(["s"])
    assert b.pressed == ["lctrl", "mouse_left"]
    b.release_all()
    batch = fake_send.calls[-1]
    assert [x["type"] for x in batch] == ["mouse", "key"]
    assert batch[0]["flags"] == 0x0004  # LEFTUP
    assert batch[1]["scan"] == keys.scan_code("lctrl") and batch[1]["flags"] & KEYEVENTF_KEYUP
    assert b.pressed == []
    n = len(fake_send.calls)
    b.release_all()  # nada pulsado: no envía nada
    assert len(fake_send.calls) == n


def test_backend_generic_and_specific_modifier_tracked_together(fake_send):
    b = WinInputBackend()
    b.press(["ctrl"])
    b.release(["lctrl"])
    assert b.pressed == []


def test_backend_invalid_token_sends_nothing(fake_send):
    b = WinInputBackend()
    with pytest.raises(ValueError):
        b.press(["a", "tecla_rara"])
    assert fake_send.calls == [] and b.pressed == []


def test_backend_blocked_raises_but_release_all_does_not(fake_send):
    b = WinInputBackend()
    fake_send.result = 0
    with pytest.raises(InputError):
        b.press(["a"])
    assert b.pressed == ["a"]  # se intentará soltar igualmente
    assert b.release_all() is False  # no lanza aunque Windows bloquee, pero lo indica
    assert b.pressed == [] and b.pending_release == ["a"]  # no se olvida: queda pendiente
    fake_send.result = None
    assert b.release_all() is True
    assert b.pending_release == []


def test_backend_rejected_release_is_kept_pending_and_retried(fake_send):
    """UIPI / escritorio seguro: si Windows no inserta las sueltas, no se olvidan."""
    b = WinInputBackend()
    b.press(["shift", "w"])
    fake_send.result = 0
    with pytest.raises(InputError):
        b.release(["w", "shift"])
    assert b.pressed == [] and b.pending_release == ["w", "lshift"]
    assert b.release_all() is False  # el finally del motor tampoco puede: siguen pendientes
    assert b.pending_release == ["w", "lshift"]
    assert b.retry_pending_release() is False
    fake_send.result = None
    n = len(fake_send.calls)
    assert b.retry_pending_release() is True
    assert b.pending_release == []
    ups = fake_send.calls[n]
    assert [u["scan"] for u in ups] == [keys.scan_code("w"), keys.scan_code("lshift")]  # orden de suelta
    assert all(u["flags"] & KEYEVENTF_KEYUP for u in ups)
    n = len(fake_send.calls)
    assert b.retry_pending_release() is True and len(fake_send.calls) == n  # nada más que enviar


def test_backend_partial_release_keeps_only_what_was_not_inserted(fake_send):
    b = WinInputBackend()
    b.press(["a", "b", "c"])
    fake_send.result = 1  # Windows sólo insertó el primer up
    with pytest.raises(InputError) as info:
        b.release(["a", "b", "c"])
    assert info.value.inserted == 1
    assert b.pending_release == ["b", "c"]
    fake_send.result = None
    b.release(["x"])  # la siguiente llamada reintenta primero lo pendiente
    pending_ups, x_up = fake_send.calls[-2:]
    assert [u["scan"] for u in pending_ups] == [keys.scan_code("b"), keys.scan_code("c")]
    assert [u["scan"] for u in x_up] == [keys.scan_code("x")]
    assert b.pending_release == []


def test_backend_press_retries_pending_release_first(fake_send):
    b = WinInputBackend()
    b.press(["shift"])
    fake_send.result = 0
    assert b.release_all() is False
    fake_send.result = None
    b.press(["a"])
    up, down = fake_send.calls[-2:]
    assert up[0]["flags"] & KEYEVENTF_KEYUP and up[0]["scan"] == keys.scan_code("lshift")
    assert down[0]["scan"] == keys.scan_code("a") and not down[0]["flags"] & KEYEVENTF_KEYUP
    assert b.pending_release == [] and b.pressed == ["a"]
    # Volver a pulsar una entrada pendiente la saca de la lista (ya no hay que soltarla aparte).
    fake_send.result = 0
    with pytest.raises(InputError):
        b.release(["a"])
    fake_send.result = None
    b.press(["a"])
    assert fake_send.calls[-1][0]["scan"] == keys.scan_code("a")
    assert b.pending_release == [] and b.pressed == ["a"]


def test_backend_type_char_scroll_move_rel(fake_send):
    b = WinInputBackend()
    b.type_char("\U0001F600")
    b.scroll(-3)
    b.move_rel(10, -4)
    b.move_rel(0, 0)  # sin movimiento: no envía nada
    assert [len(c) for c in fake_send.calls] == [4, 3, 1]


def test_backend_thread_safety(fake_send):
    b = WinInputBackend()
    tokens = ["a", "b", "c", "d", "mouse_x1"]

    def worker(tok: str) -> None:
        for _ in range(200):
            b.press([tok, "shift"])
            b.release([tok])

    threads = [threading.Thread(target=worker, args=(t,)) for t in tokens]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert b.pressed == ["lshift"]
    b.release_all()
    assert b.pressed == []
    # Cada llamada a SendInput es un lote completo (2 downs o 1 up), nunca mezclado.
    assert all(len(c) in (1, 2) for c in fake_send.calls)
    assert len(fake_send.calls) == len(tokens) * 400 + 1


def test_backend_satisfies_protocol():
    assert isinstance(WinInputBackend(), winput.InputBackend)


# --- Consultas del sistema (seguras) -----------------------------------------------------------
def test_cursor_and_screen_queries():
    x, y = winput.get_cursor_pos()
    assert isinstance(x, int) and isinstance(y, int)
    w, h = WinInputBackend().screen_size()
    assert w > 0 and h > 0
    assert isinstance(winput.foreground_is_own_process(), bool)
    assert isinstance(winput.window_is_own_process_at(x, y), bool)
    assert isinstance(winput.is_key_down(0x41), bool)


def test_set_cursor_pos_restores_position(fake_send):
    backend = WinInputBackend()
    original = backend.cursor_pos()
    try:
        backend.move_to(*original)  # misma posición: no molesta al usuario
    finally:
        winput.set_cursor_pos(*original)
    if fake_send.calls:
        # SetCursorPos puede fallar (p. ej. ventana elevada en primer plano): se recurre a
        # un movimiento absoluto con SendInput (aquí falso) con las mismas coordenadas.
        ((move,),) = fake_send.calls
        assert move == _describe(winput.move_abs_input(*original))
        assert move["flags"] & MOUSEEVENTF_ABSOLUTE


def test_move_to_falls_back_to_absolute_sendinput(fake_send, monkeypatch):
    monkeypatch.setattr(winput, "_SetCursorPos", lambda x, y: 0)
    WinInputBackend().move_to(100, 200)
    ((move,),) = fake_send.calls
    assert move == _describe(winput.move_abs_input(100, 200))


def test_timer_resolution_idempotent(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(winput, "_timer_active", False)
    monkeypatch.setattr(winput, "_timeBeginPeriod", lambda p: calls.append(f"begin{p}") or 0)
    monkeypatch.setattr(winput, "_timeEndPeriod", lambda p: calls.append(f"end{p}") or 0)
    winput.set_timer_resolution(True)
    winput.set_timer_resolution(True)
    assert winput.timer_resolution_active()
    winput.set_timer_resolution(False)
    winput.set_timer_resolution(False)
    assert calls == ["begin1", "end1"]


def test_timer_resolution_real_calls():
    winput.set_timer_resolution(True)
    winput.set_timer_resolution(False)
    assert not winput.timer_resolution_active()


def test_enable_dpi_awareness_is_silent():
    # En un proceso aparte: no altera el DPI del proceso de pruebas.
    code = "from macrotool import winput; winput.enable_dpi_awareness(); winput.enable_dpi_awareness(); print('ok')"
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, cwd=root)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
