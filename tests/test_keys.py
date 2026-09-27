"""Tests de tokens de teclado/ratón (macrotool.keys).

Sólo usan MapVirtualKeyW / GetKeyNameTextW (consultas de sólo lectura); nunca
se inyecta ninguna pulsación.
"""
from __future__ import annotations

import sys

import pytest

from macrotool import keys

ON_WINDOWS = sys.platform == "win32" and keys._user32 is not None

SPEC_TOKENS: list[str] = (
    [chr(c) for c in range(ord("a"), ord("z") + 1)]
    + [str(d) for d in range(10)]
    + [f"f{i}" for i in range(1, 25)]
    + ["enter", "esc", "tab", "space", "backspace", "delete", "insert", "home", "end",
       "pageup", "pagedown", "up", "down", "left", "right", "capslock", "numlock",
       "scrolllock", "printscreen", "pause", "apps"]
    + ["ctrl", "shift", "alt", "win", "lctrl", "rctrl", "lshift", "rshift",
       "lalt", "ralt", "lwin", "rwin"]
    + [f"num{i}" for i in range(10)]
    + ["num_multiply", "num_add", "num_subtract", "num_decimal", "num_divide", "num_enter"]
    + ["volume_up", "volume_down", "volume_mute", "media_next", "media_prev",
       "media_play_pause", "media_stop"]
    + ["mouse_left", "mouse_right", "mouse_middle", "mouse_x1", "mouse_x2"]
)

# VK esperados (tabla de Microsoft "Virtual-Key Codes").
EXPECTED_VK: dict[str, int] = {
    "a": 0x41, "m": 0x4D, "z": 0x5A, "0": 0x30, "5": 0x35, "9": 0x39,
    "f1": 0x70, "f12": 0x7B, "f13": 0x7C, "f24": 0x87,
    "enter": 0x0D, "esc": 0x1B, "tab": 0x09, "space": 0x20, "backspace": 0x08,
    "delete": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23, "pageup": 0x21,
    "pagedown": 0x22, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "capslock": 0x14, "numlock": 0x90, "scrolllock": 0x91, "printscreen": 0x2C,
    "pause": 0x13, "apps": 0x5D,
    "ctrl": 0xA2, "shift": 0xA0, "alt": 0xA4, "win": 0x5B,
    "lctrl": 0xA2, "rctrl": 0xA3, "lshift": 0xA0, "rshift": 0xA1,
    "lalt": 0xA4, "ralt": 0xA5, "lwin": 0x5B, "rwin": 0x5C,
    "num0": 0x60, "num9": 0x69, "num_multiply": 0x6A, "num_add": 0x6B,
    "num_subtract": 0x6D, "num_decimal": 0x6E, "num_divide": 0x6F, "num_enter": 0x0D,
    "volume_mute": 0xAD, "volume_down": 0xAE, "volume_up": 0xAF, "media_next": 0xB0,
    "media_prev": 0xB1, "media_stop": 0xB2, "media_play_pause": 0xB3,
    "mouse_left": 0x01, "mouse_right": 0x02, "mouse_middle": 0x04,
    "mouse_x1": 0x05, "mouse_x2": 0x06,
}

EXTENDED = ["up", "down", "left", "right", "insert", "delete", "home", "end", "pageup",
            "pagedown", "rctrl", "ralt", "win", "lwin", "rwin", "apps", "num_divide",
            "num_enter", "numlock", "printscreen", "volume_up", "volume_down",
            "volume_mute", "media_next", "media_prev", "media_play_pause", "media_stop"]


@pytest.fixture
def no_user32(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simula un sistema sin user32 (se usan las tablas de respaldo)."""
    monkeypatch.setattr(keys, "_user32", None)


# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------
def test_constants() -> None:
    assert keys.MOUSE_TOKENS == ("mouse_left", "mouse_right", "mouse_middle",
                                 "mouse_x1", "mouse_x2")
    assert keys.MODIFIERS == frozenset({"ctrl", "shift", "alt", "win", "lctrl", "rctrl",
                                        "lshift", "rshift", "lalt", "ralt", "lwin", "rwin"})
    assert keys.MAPVK_VK_TO_VSC_EX == 4


@pytest.mark.skipif(not ON_WINDOWS, reason="requiere user32")
def test_win32_prototypes_declared() -> None:
    from ctypes import c_int, wintypes

    assert keys._user32.MapVirtualKeyW.argtypes == [wintypes.UINT, wintypes.UINT]
    assert keys._user32.MapVirtualKeyW.restype is wintypes.UINT
    assert keys._user32.GetKeyNameTextW.argtypes == [wintypes.LONG, wintypes.LPWSTR, c_int]
    assert keys._user32.GetKeyNameTextW.restype is c_int


# ---------------------------------------------------------------------------
# normalize
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("token", SPEC_TOKENS)
def test_all_spec_tokens_are_canonical(token: str) -> None:
    assert keys.normalize(token) == token
    assert keys.normalize(token.upper()) == token
    assert keys.normalize(f"  {token} ") == token


@pytest.mark.parametrize("alias, token", [
    # alias de la especificación
    ("control", "ctrl"), ("escape", "esc"), ("return", "enter"), ("del", "delete"),
    ("supr", "delete"), ("ins", "insert"), ("pgup", "pageup"), ("pgdn", "pagedown"),
    ("spacebar", "space"), ("espacio", "space"), ("windows", "win"), ("meta", "win"),
    ("super", "win"), ("menu", "apps"), ("lmb", "mouse_left"),
    ("click_izquierdo", "mouse_left"), ("rmb", "mouse_right"), ("mmb", "mouse_middle"),
    ("altgr", "ralt"), ("intro", "enter"),
    # mayúsculas / espacios / tildes
    ("CONTROL", "ctrl"), ("Escape", "esc"), ("AltGr", "ralt"), ("Espacio", "space"),
    ("LMB", "mouse_left"), ("Click Izquierdo", "mouse_left"), ("Menú", "apps"),
    # nombres legibles (lo que muestra display_name) también se aceptan
    ("Intro", "enter"), ("Retroceso", "backspace"), ("RePág", "pageup"), ("AvPág", "pagedown"),
    ("Inicio", "home"), ("Fin", "end"), ("↑", "up"), ("→", "right"),
    ("Mayús", "shift"), ("mayus", "shift"), ("Bloq Mayús", "capslock"),
    ("Clic derecho", "mouse_right"), ("clic central", "mouse_middle"),
    ("Botón lateral 1", "mouse_x1"), ("Botón lateral 2", "mouse_x2"),
    ("Num 5", "num5"), ("Num Intro", "num_enter"), ("Num -", "num_subtract"),
    ("Ctrl der.", "rctrl"), ("Win izq.", "lwin"), ("Impr Pant", "printscreen"),
    # otros alias
    ("numpad7", "num7"), ("kp_3", "num3"), ("mouse4", "mouse_x1"), ("mouse5", "mouse_x2"),
    ("prtsc", "printscreen"), ("caps", "capslock"), ("page up", "pageup"),
])
def test_normalize_aliases(alias: str, token: str) -> None:
    assert keys.normalize(alias) == token


@pytest.mark.parametrize("text, token", [
    ("vk_c0", "vk_c0"), ("VK_C0", "vk_c0"), ("vk_0xba", "vk_ba"), ("vk_7", "vk_07"),
    ("vk_ff", "vk_ff"), ("vk_41", "a"), ("vk_0d", "enter"), ("vk_11", "ctrl"),
    ("vk_a3", "rctrl"), ("vk_02", "mouse_right"), ("vk_70", "f1"),
])
def test_normalize_vk_tokens(text: str, token: str) -> None:
    assert keys.normalize(text) == token


@pytest.mark.parametrize("bad", [
    "", "   ", "foo", "f25", "f0", "num10", "vk_00", "vk_100", "vk_zz", "vk_",
    "ctrl+s", "mouse_x3", "ñ", "++", None, 65, ["a"],
])
def test_normalize_rejects_unknown(bad: object) -> None:
    with pytest.raises(ValueError):
        keys.normalize(bad)  # type: ignore[arg-type]


def test_normalize_error_message_is_spanish() -> None:
    with pytest.raises(ValueError, match="Tecla desconocida: «foo»"):
        keys.normalize("foo")
    with pytest.raises(ValueError, match="Tecla vacía"):
        keys.normalize(" ")


# ---------------------------------------------------------------------------
# is_mouse / is_modifier / generic_modifier
# ---------------------------------------------------------------------------
def test_is_mouse() -> None:
    for token in keys.MOUSE_TOKENS:
        assert keys.is_mouse(token)
    assert keys.is_mouse("LMB") and keys.is_mouse("Clic derecho") and keys.is_mouse("vk_05")
    for token in ("a", "ctrl", "space", "vk_c0", "", "basura", None, 1):
        assert not keys.is_mouse(token)  # type: ignore[arg-type]


def test_is_modifier() -> None:
    for token in keys.MODIFIERS:
        assert keys.is_modifier(token)
    assert keys.is_modifier("Control") and keys.is_modifier("AltGr") and keys.is_modifier("vk_10")
    for token in ("a", "mouse_left", "capslock", "apps", "", "basura", None):
        assert not keys.is_modifier(token)  # type: ignore[arg-type]


def test_generic_modifier() -> None:
    assert [keys.generic_modifier(t) for t in
            ("ctrl", "lctrl", "rctrl", "alt", "lalt", "ralt", "shift", "lshift", "rshift",
             "win", "lwin", "rwin")] == ["ctrl"] * 3 + ["alt"] * 3 + ["shift"] * 3 + ["win"] * 3
    assert keys.generic_modifier("A") == "a"
    assert keys.generic_modifier("Clic derecho") == "mouse_right"
    with pytest.raises(ValueError):
        keys.generic_modifier("basura")


# ---------------------------------------------------------------------------
# token_to_vk / vk_to_token
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("token, vk", list(EXPECTED_VK.items()))
def test_token_to_vk(token: str, vk: int) -> None:
    assert keys.token_to_vk(token) == vk


def test_token_to_vk_covers_every_spec_token() -> None:
    for token in SPEC_TOKENS:
        vk = keys.token_to_vk(token)
        assert 0x01 <= vk <= 0xFE, token
    for c in "abcdefghijklmnopqrstuvwxyz":
        assert keys.token_to_vk(c) == ord(c.upper())
    for i in range(1, 25):
        assert keys.token_to_vk(f"f{i}") == 0x6F + i
    for i in range(10):
        assert keys.token_to_vk(str(i)) == 0x30 + i
        assert keys.token_to_vk(f"num{i}") == 0x60 + i


def test_token_to_vk_aliases_and_vk_tokens() -> None:
    assert keys.token_to_vk("Control") == 0xA2
    assert keys.token_to_vk("lmb") == 0x01
    assert keys.token_to_vk("vk_c0") == 0xC0
    assert keys.token_to_vk("VK_BA") == 0xBA
    with pytest.raises(ValueError):
        keys.token_to_vk("basura")


@pytest.mark.parametrize("token", SPEC_TOKENS)
def test_vk_round_trip(token: str) -> None:
    vk = keys.token_to_vk(token)
    back = keys.vk_to_token(vk, extended=keys.is_extended_key(token))
    # las variantes izquierdas se envían con el VK izquierdo, que vuelve como genérico
    expected = {"lctrl": "ctrl", "lshift": "shift", "lalt": "alt", "lwin": "win"}.get(token, token)
    assert back == expected


@pytest.mark.parametrize("vk, extended, token", [
    (0x0D, False, "enter"), (0x0D, True, "num_enter"),
    (0x10, False, "shift"), (0x10, True, "shift"),
    (0x11, False, "ctrl"), (0x11, True, "rctrl"),
    (0x12, False, "alt"), (0x12, True, "ralt"),
    (0xA0, False, "shift"), (0xA1, False, "rshift"), (0xA1, True, "rshift"),
    (0xA2, False, "ctrl"), (0xA3, True, "rctrl"), (0xA3, False, "rctrl"),
    (0xA4, False, "alt"), (0xA5, True, "ralt"),
    (0x5B, True, "win"), (0x5C, True, "rwin"),
    (0x24, True, "home"), (0x24, False, "home"), (0x26, True, "up"),
    (0x41, False, "a"), (0x31, False, "1"), (0x67, False, "num7"), (0x6F, True, "num_divide"),
    (0x01, False, "mouse_left"), (0x02, False, "mouse_right"), (0x04, False, "mouse_middle"),
    (0x05, False, "mouse_x1"), (0x06, False, "mouse_x2"),
    (0xC0, False, "vk_c0"), (0xBA, False, "vk_ba"), (0x03, True, "vk_03"),
    (0x07, False, "vk_07"), (0xFF, False, "vk_ff"), (0xA6, True, "vk_a6"),
])
def test_vk_to_token(vk: int, extended: bool, token: str) -> None:
    assert keys.vk_to_token(vk, extended) == token


def test_vk_to_token_default_not_extended() -> None:
    assert keys.vk_to_token(0x0D) == "enter"
    assert keys.vk_to_token(0x11) == "ctrl"


@pytest.mark.parametrize("bad", [0, -1, 0x100, 1000, "0x41", None, 1.5])
def test_vk_to_token_rejects_invalid(bad: object) -> None:
    with pytest.raises(ValueError):
        keys.vk_to_token(bad)  # type: ignore[arg-type]


def test_every_vk_has_a_valid_token() -> None:
    for vk in range(0x01, 0x100):
        for extended in (False, True):
            token = keys.vk_to_token(vk, extended)
            assert keys.normalize(token) == token
            assert keys.token_to_vk(token) in (vk, {0x10: 0xA0, 0x11: 0xA2, 0x12: 0xA4}.get(vk),
                                               {0x11: 0xA3, 0x12: 0xA5}.get(vk) if extended else None)


# ---------------------------------------------------------------------------
# is_extended_key
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("token", EXTENDED)
def test_extended_tokens(token: str) -> None:
    assert keys.is_extended_key(token) is True


@pytest.mark.parametrize("token", [
    "pause", "enter", "ctrl", "lctrl", "shift", "lshift", "rshift", "alt", "lalt",
    "space", "esc", "tab", "backspace", "capslock", "scrolllock", "a", "1", "f1", "f24",
    "num0", "num5", "num_multiply", "num_add", "num_subtract", "num_decimal",
    *keys.MOUSE_TOKENS,
])
def test_not_extended_tokens(token: str) -> None:
    assert keys.is_extended_key(token) is False


@pytest.mark.parametrize("vk, extended", [
    (0x25, True), (0x26, True), (0x27, True), (0x28, True), (0x2D, True), (0x2E, True),
    (0x24, True), (0x23, True), (0x21, True), (0x22, True), (0xA3, True), (0xA5, True),
    (0x5B, True), (0x5C, True), (0x5D, True), (0x6F, True), (0x90, True), (0x2C, True),
    (0xAD, True), (0xB3, True), (0xA6, True), (0x03, True),
    (0x13, False), (0x0D, False), (0x11, False), (0x12, False), (0x10, False),
    (0xA2, False), (0xA4, False), (0xA0, False), (0xA1, False), (0x41, False),
    (0x20, False), (0x60, False), (0x01, False), (0xC0, False),
])
def test_extended_by_vk(vk: int, extended: bool) -> None:
    assert keys.is_extended_key(vk) is extended


def test_extended_vk_tokens_and_aliases() -> None:
    assert keys.is_extended_key("vk_a6") is True  # Navegador: atrás
    assert keys.is_extended_key("vk_c0") is False
    assert keys.is_extended_key("Supr") is True
    assert keys.is_extended_key("AltGr") is True
    assert keys.is_extended_key("Num Intro") is True
    with pytest.raises(ValueError):
        keys.is_extended_key("basura")


def test_extended_without_user32(no_user32: None) -> None:
    for token in EXTENDED:
        assert keys.is_extended_key(token)
    assert not keys.is_extended_key("pause") and not keys.is_extended_key("vk_c0")
    assert keys.is_extended_key(0xB0)


@pytest.mark.skipif(not ON_WINDOWS, reason="requiere user32")
def test_extended_table_agrees_with_windows() -> None:
    """Si Windows da un scancode E0 para la tecla, la tabla debe marcarla como extendida.

    (Lo contrario no siempre se cumple: flechas, Inicio, Bloq Num... comparten scancode
    con el teclado numérico y MapVirtualKeyW no devuelve el prefijo.)
    """
    for token in SPEC_TOKENS:
        if keys.is_mouse(token):
            continue
        raw = keys._user32.MapVirtualKeyW(keys.token_to_vk(token), keys.MAPVK_VK_TO_VSC_EX)
        if raw >> 8 == 0xE0:
            assert keys.is_extended_key(token), token


# ---------------------------------------------------------------------------
# scan_code
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("token", keys.MOUSE_TOKENS)
def test_mouse_has_no_scan_code(token: str) -> None:
    assert keys.scan_code(token) == 0


@pytest.mark.skipif(not ON_WINDOWS, reason="requiere user32")
@pytest.mark.parametrize("token, scan", [
    # teclas cuya posición no depende de la distribución
    ("esc", 0x01), ("1", 0x02), ("0", 0x0B), ("backspace", 0x0E), ("tab", 0x0F),
    ("enter", 0x1C), ("num_enter", 0x1C), ("ctrl", 0x1D), ("rctrl", 0x1D),
    ("shift", 0x2A), ("lshift", 0x2A), ("rshift", 0x36), ("alt", 0x38), ("ralt", 0x38),
    ("space", 0x39), ("capslock", 0x3A), ("f1", 0x3B), ("f10", 0x44), ("f11", 0x57),
    ("f12", 0x58), ("numlock", 0x45), ("scrolllock", 0x46), ("home", 0x47), ("up", 0x48),
    ("pageup", 0x49), ("left", 0x4B), ("right", 0x4D), ("end", 0x4F), ("down", 0x50),
    ("pagedown", 0x51), ("insert", 0x52), ("delete", 0x53), ("num7", 0x47), ("num0", 0x52),
    ("num_divide", 0x35), ("num_multiply", 0x37), ("win", 0x5B), ("lwin", 0x5B),
    ("rwin", 0x5C), ("apps", 0x5D), ("printscreen", 0x37), ("volume_mute", 0x20),
    ("media_play_pause", 0x22),
])
def test_scan_codes_on_windows(token: str, scan: int) -> None:
    assert keys.scan_code(token) == scan


def test_pause_is_sent_by_vk() -> None:
    assert keys.scan_code("pause") == 0


@pytest.mark.skipif(not ON_WINDOWS, reason="requiere user32")
def test_scan_codes_are_single_bytes_and_match_windows() -> None:
    for token in SPEC_TOKENS:
        scan = keys.scan_code(token)
        assert 0 <= scan <= 0xFF, token
        if keys.is_mouse(token) or token in ("pause", "printscreen"):
            continue
        raw = keys._user32.MapVirtualKeyW(keys.token_to_vk(token), keys.MAPVK_VK_TO_VSC_EX)
        assert scan == raw & 0xFF, token
    # las letras tienen scancode en cualquier distribución
    assert all(keys.scan_code(c) for c in "abcdefghijklmnopqrstuvwxyz")


def test_scan_code_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        keys.scan_code("basura")


def test_scan_code_fallback_without_user32(no_user32: None) -> None:
    assert keys.scan_code("a") == 0x1E
    assert keys.scan_code("w") == 0x11
    assert keys.scan_code("esc") == 0x01
    assert keys.scan_code("rctrl") == 0x1D
    assert keys.scan_code("up") == 0x48
    assert keys.scan_code("printscreen") == 0x37
    assert keys.scan_code("pause") == 0
    assert keys.scan_code("mouse_left") == 0
    assert keys.scan_code("vk_c0") == 0
    for token in SPEC_TOKENS:
        if not keys.is_mouse(token) and token != "pause":
            assert keys.scan_code(token) > 0, token


# ---------------------------------------------------------------------------
# display_name
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("token, name", [
    ("enter", "Intro"), ("esc", "Esc"), ("space", "Espacio"), ("backspace", "Retroceso"),
    ("delete", "Supr"), ("insert", "Insert"), ("home", "Inicio"), ("end", "Fin"),
    ("pageup", "RePág"), ("pagedown", "AvPág"), ("up", "↑"), ("down", "↓"),
    ("left", "←"), ("right", "→"), ("ctrl", "Ctrl"), ("shift", "Mayús"), ("alt", "Alt"),
    ("ralt", "AltGr"), ("win", "Win"), ("capslock", "Bloq Mayús"),
    ("mouse_left", "Clic izquierdo"), ("mouse_right", "Clic derecho"),
    ("mouse_middle", "Clic central"), ("mouse_x1", "Botón lateral 1"),
    ("mouse_x2", "Botón lateral 2"), ("num5", "Num 5"), ("num_enter", "Num Intro"),
    ("f5", "F5"), ("f24", "F24"), ("w", "W"), ("7", "7"), ("tab", "Tab"),
    # alias y mayúsculas
    ("Control", "Ctrl"), ("lmb", "Clic izquierdo"), ("ESPACIO", "Espacio"),
])
def test_display_names(token: str, name: str) -> None:
    assert keys.display_name(token) == name


def test_every_token_has_unique_spanish_name() -> None:
    names = [keys.display_name(t) for t in SPEC_TOKENS]
    assert all(names)
    assert len(set(names)) == len(names)
    # y el nombre legible vuelve al mismo token
    for token, name in zip(SPEC_TOKENS, names):
        assert keys.normalize(name) == token


def test_display_name_unknown_token_is_returned_as_is() -> None:
    assert keys.display_name("basura") == "basura"
    assert keys.display_name("") == ""


def test_display_name_vk_fallback_without_user32(no_user32: None) -> None:
    assert keys.display_name("vk_c0") == "VK 0xC0"
    assert keys.display_name("vk_07") == "VK 0x07"
    assert keys.display_name("vk_a6") == "Navegador: atrás"


@pytest.mark.skipif(not ON_WINDOWS, reason="requiere user32")
def test_display_name_vk_uses_keyboard_layout() -> None:
    # VK_OEM_3 es "`" en EE. UU. y "Ñ" en España: sólo comprobamos que Windows da un nombre
    name = keys.display_name("vk_c0")
    assert name and not name.startswith("VK 0x")
    assert name == name.upper()
    # sin scancode en ninguna distribución → respaldo
    assert keys.display_name("vk_07") == "VK 0x07"
    # nombres fijos en español para teclas especiales sin token propio
    assert keys.display_name("vk_a6") == "Navegador: atrás"


def test_key_name_text_lparam(monkeypatch: pytest.MonkeyPatch) -> None:
    """GetKeyNameTextW recibe scancode << 16 | extendida << 24."""
    calls: list[int] = []

    class FakeUser32:
        def MapVirtualKeyW(self, vk: int, kind: int) -> int:
            return {0xC0: 0x27, 0xE9: 0xE05A}.get(vk, 0)

        def GetKeyNameTextW(self, lparam: int, buf: object, size: int) -> int:
            calls.append(lparam)
            buf.value = "ñ"  # type: ignore[attr-defined]
            return 1

    monkeypatch.setattr(keys, "_user32", FakeUser32())
    assert keys.display_name("vk_c0") == "Ñ"
    assert keys.display_name("vk_e9") == "Ñ"
    assert calls == [0x27 << 16, (0x5A << 16) | (1 << 24)]
    # sin nombre (0) o secuencia E1 → respaldo sin llamar a GetKeyNameTextW
    assert keys.display_name("vk_07") == "VK 0x07"
    assert len(calls) == 2


def test_key_choices_disambiguates_duplicate_symbol_names(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeUser32:
        def MapVirtualKeyW(self, vk: int, kind: int) -> int:
            return {0xDC: 0x2B, 0xE2: 0x56}.get(vk, 0x10)

        def GetKeyNameTextW(self, lparam: int, buf: object, size: int) -> int:
            buf.value = "\\" if (lparam >> 16) & 0xFF in (0x2B, 0x56) else "x"  # type: ignore[attr-defined]
            return 1

    monkeypatch.setattr(keys, "_user32", FakeUser32())
    choices = dict(keys.key_choices())
    assert choices["vk_dc"] == "\\"
    assert choices["vk_e2"] == "\\ (VK 0xE2)"


# ---------------------------------------------------------------------------
# Combinaciones
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text, tokens", [
    ("Ctrl + Shift+S", ["ctrl", "shift", "s"]),
    ("", []), ("   ", []),
    ("1", ["1"]),
    ("W + Clic derecho", ["w", "mouse_right"]),
    ("s+q", ["s", "q"]),
    ("S + Clic izquierdo", ["s", "mouse_left"]),
    ("Espacio", ["space"]),
    ("lmb+rmb", ["mouse_left", "mouse_right"]),
    ("control+CTRL+ctrl+s", ["ctrl", "s"]),
    ("ctrl + lctrl", ["ctrl", "lctrl"]),
    ("  alt +   f4 ", ["alt", "f4"]),
    ("AltGr+vk_C0", ["ralt", "vk_c0"]),
    ("mouse_x1", ["mouse_x1"]),
])
def test_parse_combo(text: str, tokens: list[str]) -> None:
    assert keys.parse_combo(text) == tokens


def test_parse_combo_accepts_sequences() -> None:
    assert keys.parse_combo(["Control", "S", "ctrl"]) == ["ctrl", "s"]
    assert keys.parse_combo(()) == []


@pytest.mark.parametrize("bad", ["ctrl+", "+s", "ctrl++s", "+", "ctrl+basura", "foo", "f99+a"])
def test_parse_combo_errors(bad: str) -> None:
    with pytest.raises(ValueError):
        keys.parse_combo(bad)


def test_parse_combo_error_messages() -> None:
    with pytest.raises(ValueError, match="Tecla desconocida: «basura»"):
        keys.parse_combo("ctrl + basura")
    with pytest.raises(ValueError, match="Combinación incompleta"):
        keys.parse_combo("ctrl+")


@pytest.mark.parametrize("tokens, text", [
    (["w", "mouse_right"], "W + Clic derecho"),
    (["s", "q"], "S + Q"),
    (["s", "mouse_left"], "S + Clic izquierdo"),
    (["space"], "Espacio"),
    (["mouse_left", "mouse_right"], "Clic izquierdo + Clic derecho"),
    (["ctrl", "shift", "s"], "Ctrl + Mayús + S"),
    ([], "—"), ("", "—"),
    ("ctrl+1", "Ctrl + 1"),
    (["lmb"], "Clic izquierdo"),
    (["ctrl", "basura"], "Ctrl + basura"),
])
def test_format_combo(tokens: object, text: str) -> None:
    assert keys.format_combo(tokens) == text  # type: ignore[arg-type]


def test_format_parse_round_trip() -> None:
    tokens = [t for t in SPEC_TOKENS if t != "num_add"]
    for i in range(0, len(tokens), 3):
        combo = tokens[i:i + 3]
        assert keys.parse_combo(keys.format_combo(combo)) == combo


@pytest.mark.parametrize("tokens, text", [
    (["ctrl", "shift", "s"], "ctrl+shift+s"),
    ("Ctrl + Shift + S", "ctrl+shift+s"),
    (["Control", "ESPACIO", "ctrl"], "ctrl+space"),
    ([], ""), ("", ""),
    (["vk_C0"], "vk_c0"),
])
def test_combo_to_string(tokens: object, text: str) -> None:
    assert keys.combo_to_string(tokens) == text  # type: ignore[arg-type]


def test_combo_to_string_round_trip() -> None:
    for text in ("ctrl+shift+s", "1", "mouse_x1", "ralt+vk_c0", "w+mouse_right"):
        assert keys.combo_to_string(keys.parse_combo(text)) == text
    with pytest.raises(ValueError):
        keys.combo_to_string(["basura"])


@pytest.mark.parametrize("tokens, expected", [
    (["s", "shift", "ctrl"], ["ctrl", "shift", "s"]),
    (["w", "win", "alt", "shift", "ctrl", "q"], ["ctrl", "alt", "shift", "win", "w", "q"]),
    (["mouse_left", "rshift", "lctrl", "ralt"], ["lctrl", "ralt", "rshift", "mouse_left"]),
    (["b", "a"], ["b", "a"]),
    ([], []),
    ("s + Ctrl", ["ctrl", "s"]),
    (["Mayús", "W"], ["shift", "w"]),
])
def test_sort_combo(tokens: object, expected: list[str]) -> None:
    assert keys.sort_combo(tokens) == expected  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# key_choices
# ---------------------------------------------------------------------------
def test_key_choices() -> None:
    choices = keys.key_choices()
    tokens = [t for t, _ in choices]
    names = [n for _, n in choices]
    assert len(tokens) == len(set(tokens))
    assert len(names) == len(set(names))  # sin nombres repetidos en el desplegable
    assert set(SPEC_TOKENS) <= set(tokens)
    for token, name in choices:
        assert keys.normalize(token) == token
        assert name.startswith(keys.display_name(token))
        if not token.startswith("vk_"):
            assert name == keys.display_name(token)
    # agrupado: letras, números, F, navegación, modificadores, numpad, multimedia, ratón
    order = [tokens.index(t) for t in ("a", "1", "f1", "enter", "ctrl", "num0",
                                        "volume_up", "mouse_left")]
    assert order == sorted(order)
    assert tokens[:26] == [chr(c) for c in range(ord("a"), ord("z") + 1)]
    assert tokens[-5:] == list(keys.MOUSE_TOKENS)


def test_key_groups() -> None:
    groups = keys.key_groups()
    titles = [title for title, _ in groups]
    assert titles[0] == "Letras" and titles[-1] == "Ratón"
    assert "Modificadores" in titles and "Teclado numérico" in titles
    flat = [item for _, items in groups for item in items]
    assert flat == keys.key_choices()


def test_key_choices_without_user32(no_user32: None) -> None:
    tokens = [t for t, _ in keys.key_choices()]
    assert "vk_c0" in tokens and set(SPEC_TOKENS) <= set(tokens)
