"""Tokens de entrada: nombres canónicos de teclas y botones del ratón.

Un *token* es un string en minúsculas ("a", "f5", "ctrl", "num_enter",
"mouse_left", "vk_c0"...). Este módulo convierte entre tokens, códigos VK de
Windows y scancodes, interpreta combinaciones escritas por el usuario
("Ctrl + Shift+S") y genera nombres legibles en español.

Usa ``user32`` (ctypes) cuando está disponible; si no, recurre a tablas
estáticas (distribución QWERTY estándar) para que todo siga funcionando.
"""
from __future__ import annotations

import ctypes
import operator
import re
import sys
import unicodedata
from typing import Any, Iterable, Optional, Union

# ---------------------------------------------------------------------------
# Win32
# ---------------------------------------------------------------------------
MAPVK_VK_TO_VSC_EX = 4


def _load_user32() -> Any:
    """``user32`` con los prototipos declarados, o None fuera de Windows."""
    if sys.platform != "win32":
        return None
    try:
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
        user32.MapVirtualKeyW.restype = wintypes.UINT
        user32.GetKeyNameTextW.argtypes = [wintypes.LONG, wintypes.LPWSTR, ctypes.c_int]
        user32.GetKeyNameTextW.restype = ctypes.c_int
        return user32
    except (OSError, AttributeError):
        return None


_user32: Any = _load_user32()

# ---------------------------------------------------------------------------
# Tablas de tokens
# ---------------------------------------------------------------------------
_LETTERS = [chr(c) for c in range(ord("a"), ord("z") + 1)]
_DIGITS = [str(d) for d in range(10)]
_FKEYS = [f"f{i}" for i in range(1, 25)]
_EDITING = ["enter", "esc", "tab", "space", "backspace", "delete", "insert",
            "home", "end", "pageup", "pagedown", "up", "down", "left", "right"]
_SYSTEM = ["capslock", "numlock", "scrolllock", "printscreen", "pause", "apps"]
_MODIFIER_ORDER = ["ctrl", "lctrl", "rctrl", "alt", "lalt", "ralt",
                   "shift", "lshift", "rshift", "win", "lwin", "rwin"]
_NUMPAD = [f"num{i}" for i in range(10)] + [
    "num_multiply", "num_add", "num_subtract", "num_decimal", "num_divide", "num_enter"]
_MEDIA = ["volume_up", "volume_down", "volume_mute",
          "media_play_pause", "media_next", "media_prev", "media_stop"]

MOUSE_TOKENS: tuple[str, ...] = ("mouse_left", "mouse_right", "mouse_middle", "mouse_x1", "mouse_x2")
MODIFIERS: frozenset[str] = frozenset(_MODIFIER_ORDER)

# Token -> VK. Los modificadores genéricos se envían como la versión izquierda.
_VK: dict[str, int] = {}
_VK.update({c: ord(c.upper()) for c in _LETTERS})
_VK.update({d: ord(d) for d in _DIGITS})
_VK.update({f"f{i}": 0x6F + i for i in range(1, 25)})
_VK.update({
    "enter": 0x0D, "esc": 0x1B, "tab": 0x09, "space": 0x20, "backspace": 0x08,
    "delete": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "capslock": 0x14, "numlock": 0x90, "scrolllock": 0x91,
    "printscreen": 0x2C, "pause": 0x13, "apps": 0x5D,
    # genéricos primero: así el VK izquierdo se traduce de vuelta al genérico
    "ctrl": 0xA2, "alt": 0xA4, "shift": 0xA0, "win": 0x5B,
    "lctrl": 0xA2, "rctrl": 0xA3, "lalt": 0xA4, "ralt": 0xA5,
    "lshift": 0xA0, "rshift": 0xA1, "lwin": 0x5B, "rwin": 0x5C,
})
_VK.update({f"num{i}": 0x60 + i for i in range(10)})
_VK.update({
    "num_multiply": 0x6A, "num_add": 0x6B, "num_subtract": 0x6D,
    "num_decimal": 0x6E, "num_divide": 0x6F, "num_enter": 0x0D,
    "volume_mute": 0xAD, "volume_down": 0xAE, "volume_up": 0xAF,
    "media_next": 0xB0, "media_prev": 0xB1, "media_stop": 0xB2, "media_play_pause": 0xB3,
    "mouse_left": 0x01, "mouse_right": 0x02, "mouse_middle": 0x04,
    "mouse_x1": 0x05, "mouse_x2": 0x06,
})

# VK -> token (la primera aparición gana: "enter" antes que "num_enter", "ctrl" antes que "lctrl").
_TOKEN_BY_VK: dict[int, str] = {}
for _tok, _vk in _VK.items():
    _TOKEN_BY_VK.setdefault(_vk, _tok)
# VK genéricos (los que entrega WM_KEYDOWN / Qt nativeVirtualKey()).
_TOKEN_BY_VK.update({0x10: "shift", 0x11: "ctrl", 0x12: "alt"})
# Con la marca "extendida" algunos VK corresponden a otra tecla física.
_EXTENDED_VARIANTS: dict[int, str] = {0x0D: "num_enter", 0x11: "rctrl", 0x12: "ralt"}

# Teclas que necesitan KEYEVENTF_EXTENDEDKEY (prefijo E0 en el scancode).
_EXTENDED_TOKENS: frozenset[str] = frozenset({
    "up", "down", "left", "right", "insert", "delete", "home", "end", "pageup", "pagedown",
    "rctrl", "ralt", "win", "lwin", "rwin", "apps", "num_divide", "num_enter",
    "numlock", "printscreen", *_MEDIA,
})
# Lo mismo por VK (para vk_xx y consultas con un entero). VK_RETURN (0x0D) es
# ambiguo y se considera no extendido; los genéricos 0x11/0x12 son los izquierdos.
_EXTENDED_VKS: frozenset[int] = frozenset({
    0x03,  # Ctrl+Pausa (Interrumpir)
    0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28,  # RePág…↓
    0x2C, 0x2D, 0x2E,  # Impr Pant, Insert, Supr
    0x5B, 0x5C, 0x5D, 0x5F,  # Win izq./der., Menú, Suspender
    0x6F, 0x90,  # Num /, Bloq Num
    0xA3, 0xA5,  # Ctrl der., AltGr
    *range(0xA6, 0xB8),  # navegador, volumen, multimedia, lanzadores
})

# Scancodes (byte bajo, "make code") de una distribución QWERTY estándar, sólo
# como respaldo cuando user32 no está disponible.
_STATIC_SCAN: dict[str, int] = {
    "esc": 0x01, "1": 0x02, "2": 0x03, "3": 0x04, "4": 0x05, "5": 0x06, "6": 0x07,
    "7": 0x08, "8": 0x09, "9": 0x0A, "0": 0x0B, "backspace": 0x0E, "tab": 0x0F,
    "q": 0x10, "w": 0x11, "e": 0x12, "r": 0x13, "t": 0x14, "y": 0x15, "u": 0x16,
    "i": 0x17, "o": 0x18, "p": 0x19, "enter": 0x1C, "ctrl": 0x1D, "lctrl": 0x1D,
    "a": 0x1E, "s": 0x1F, "d": 0x20, "f": 0x21, "g": 0x22, "h": 0x23, "j": 0x24,
    "k": 0x25, "l": 0x26, "shift": 0x2A, "lshift": 0x2A, "z": 0x2C, "x": 0x2D,
    "c": 0x2E, "v": 0x2F, "b": 0x30, "n": 0x31, "m": 0x32, "rshift": 0x36,
    "num_multiply": 0x37, "alt": 0x38, "lalt": 0x38, "space": 0x39, "capslock": 0x3A,
    "f1": 0x3B, "f2": 0x3C, "f3": 0x3D, "f4": 0x3E, "f5": 0x3F, "f6": 0x40,
    "f7": 0x41, "f8": 0x42, "f9": 0x43, "f10": 0x44, "numlock": 0x45,
    "scrolllock": 0x46, "num7": 0x47, "num8": 0x48, "num9": 0x49, "num_subtract": 0x4A,
    "num4": 0x4B, "num5": 0x4C, "num6": 0x4D, "num_add": 0x4E, "num1": 0x4F,
    "num2": 0x50, "num3": 0x51, "num0": 0x52, "num_decimal": 0x53,
    "f11": 0x57, "f12": 0x58, "f13": 0x64, "f14": 0x65, "f15": 0x66, "f16": 0x67,
    "f17": 0x68, "f18": 0x69, "f19": 0x6A, "f20": 0x6B, "f21": 0x6C, "f22": 0x6D,
    "f23": 0x6E, "f24": 0x76,
    # extendidas (E0 xx)
    "num_enter": 0x1C, "rctrl": 0x1D, "num_divide": 0x35, "printscreen": 0x37,
    "ralt": 0x38, "home": 0x47, "up": 0x48, "pageup": 0x49, "left": 0x4B,
    "right": 0x4D, "end": 0x4F, "down": 0x50, "pagedown": 0x51, "insert": 0x52,
    "delete": 0x53, "win": 0x5B, "lwin": 0x5B, "rwin": 0x5C, "apps": 0x5D,
    "volume_mute": 0x20, "volume_down": 0x2E, "volume_up": 0x30,
    "media_next": 0x19, "media_prev": 0x10, "media_stop": 0x24, "media_play_pause": 0x22,
}
# Correcciones sobre MapVirtualKeyW: VK_SNAPSHOT devuelve 0x54 (Pet Sis, Alt+Impr
# Pant) en lugar de E0 37; Pausa es una secuencia E1 1D 45 que no puede enviarse
# como scancode simple (0 = enviar por VK).
_SCAN_OVERRIDES: dict[str, int] = {"printscreen": 0x37, "pause": 0}

# Nombres en español.
_DISPLAY: dict[str, str] = {}
_DISPLAY.update({c: c.upper() for c in _LETTERS})
_DISPLAY.update({d: d for d in _DIGITS})
_DISPLAY.update({f: f.upper() for f in _FKEYS})
_DISPLAY.update({
    "enter": "Intro", "esc": "Esc", "tab": "Tab", "space": "Espacio",
    "backspace": "Retroceso", "delete": "Supr", "insert": "Insert", "home": "Inicio",
    "end": "Fin", "pageup": "RePág", "pagedown": "AvPág",
    "up": "↑", "down": "↓", "left": "←", "right": "→",
    "capslock": "Bloq Mayús", "numlock": "Bloq Num", "scrolllock": "Bloq Despl",
    "printscreen": "Impr Pant", "pause": "Pausa", "apps": "Menú",
    "ctrl": "Ctrl", "shift": "Mayús", "alt": "Alt", "win": "Win",
    "lctrl": "Ctrl izq.", "rctrl": "Ctrl der.", "lshift": "Mayús izq.",
    "rshift": "Mayús der.", "lalt": "Alt izq.", "ralt": "AltGr",
    "lwin": "Win izq.", "rwin": "Win der.",
    "num_multiply": "Num *", "num_add": "Num +", "num_subtract": "Num -",
    "num_decimal": "Num .", "num_divide": "Num /", "num_enter": "Num Intro",
    "volume_up": "Subir volumen", "volume_down": "Bajar volumen", "volume_mute": "Silenciar",
    "media_next": "Pista siguiente", "media_prev": "Pista anterior",
    "media_play_pause": "Reproducir/Pausa", "media_stop": "Detener reproducción",
    "mouse_left": "Clic izquierdo", "mouse_right": "Clic derecho",
    "mouse_middle": "Clic central", "mouse_x1": "Botón lateral 1",
    "mouse_x2": "Botón lateral 2",
})
_DISPLAY.update({f"num{i}": f"Num {i}" for i in range(10)})

# Nombres fijos para VK sin token propio (GetKeyNameTextW no los conoce o da basura).
_VK_NAMES: dict[int, str] = {
    0x03: "Interrumpir", 0x0C: "Borrar", 0x2F: "Ayuda", 0x5F: "Suspender",
    0x6C: "Num Separador",
    0xA6: "Navegador: atrás", 0xA7: "Navegador: adelante", 0xA8: "Navegador: actualizar",
    0xA9: "Navegador: detener", 0xAA: "Navegador: buscar", 0xAB: "Navegador: favoritos",
    0xAC: "Navegador: inicio", 0xB4: "Correo", 0xB5: "Seleccionar multimedia",
    0xB6: "Aplicación 1", 0xB7: "Aplicación 2",
}
# Teclas de símbolos que dependen de la distribución (Ñ, ´, ç, º...).
_OEM_VKS: tuple[int, ...] = (0xBA, 0xBB, 0xBC, 0xBD, 0xBE, 0xBF, 0xC0,
                             0xDB, 0xDC, 0xDD, 0xDE, 0xDF, 0xE2)

_ALIASES: dict[str, str] = {
    # alias de la especificación
    "control": "ctrl", "escape": "esc", "return": "enter", "del": "delete", "supr": "delete",
    "ins": "insert", "pgup": "pageup", "pgdn": "pagedown", "spacebar": "space",
    "espacio": "space", "windows": "win", "meta": "win", "super": "win", "menu": "apps",
    "lmb": "mouse_left", "click_izquierdo": "mouse_left", "rmb": "mouse_right",
    "mmb": "mouse_middle", "altgr": "ralt", "intro": "enter",
    # otros habituales (inglés / español)
    "ctl": "ctrl", "strg": "ctrl", "cmd": "win", "opt": "alt", "option": "alt",
    "mayus": "shift", "mayusculas": "shift", "lcontrol": "lctrl", "rcontrol": "rctrl",
    "lmenu": "lalt", "rmenu": "ralt", "left_ctrl": "lctrl", "right_ctrl": "rctrl",
    "left_shift": "lshift", "right_shift": "rshift", "left_alt": "lalt", "right_alt": "ralt",
    "left_win": "lwin", "right_win": "rwin",
    "esp": "space", "barra_espaciadora": "space", "tabulador": "tab",
    "back": "backspace", "bksp": "backspace", "retroceso": "backspace",
    "suprimir": "delete", "insertar": "insert", "inicio": "home", "fin": "end",
    "pageup": "pageup", "page_up": "pageup", "pgdown": "pagedown", "page_down": "pagedown",
    "repag": "pageup", "avpag": "pagedown",
    "arriba": "up", "abajo": "down", "izquierda": "left", "derecha": "right",
    "up_arrow": "up", "down_arrow": "down", "left_arrow": "left", "right_arrow": "right",
    "caps": "capslock", "caps_lock": "capslock", "bloq_mayus": "capslock",
    "num_lock": "numlock", "bloq_num": "numlock", "scroll_lock": "scrolllock",
    "bloq_despl": "scrolllock", "prtsc": "printscreen", "prtscn": "printscreen",
    "print": "printscreen", "print_screen": "printscreen", "impr_pant": "printscreen",
    "break": "pause", "pausa": "pause", "application": "apps", "context_menu": "apps",
    "enter_num": "num_enter", "numpad_enter": "num_enter", "kp_enter": "num_enter",
    "multiply": "num_multiply", "add": "num_add", "subtract": "num_subtract",
    "decimal": "num_decimal", "divide": "num_divide",
    "mute": "volume_mute", "silenciar": "volume_mute", "vol_up": "volume_up",
    "vol_down": "volume_down", "play_pause": "media_play_pause", "next_track": "media_next",
    "prev_track": "media_prev", "previous_track": "media_prev",
    "left_click": "mouse_left", "right_click": "mouse_right", "middle_click": "mouse_middle",
    "click_derecho": "mouse_right", "click_central": "mouse_middle",
    "clic_izquierdo": "mouse_left", "clic_derecho": "mouse_right", "clic_central": "mouse_middle",
    "clic_izq": "mouse_left", "clic_der": "mouse_right", "clic_izq.": "mouse_left",
    "clic_der.": "mouse_right",
    "lbutton": "mouse_left", "rbutton": "mouse_right", "mbutton": "mouse_middle",
    "mouse1": "mouse_left", "mouse2": "mouse_right", "mouse3": "mouse_middle",
    "mouse4": "mouse_x1", "mouse5": "mouse_x2", "xbutton1": "mouse_x1", "xbutton2": "mouse_x2",
    "x1": "mouse_x1", "x2": "mouse_x2", "mouse_back": "mouse_x1", "mouse_forward": "mouse_x2",
    "lateral_1": "mouse_x1", "lateral_2": "mouse_x2",
}

_VK_RE = re.compile(r"vk_?(?:0x)?([0-9a-f]{1,2})")
_NUMPAD_RE = re.compile(r"(?:num|numpad|kp)_?([0-9])")

TokenOrVk = Union[str, int]


# Acentos agudo, grave y diéresis. La virgulilla NO: la Ñ es otra tecla distinta de la N.
_STRIPPED_MARKS = frozenset({"\u0300", "\u0301", "\u0308"})


def _simplify(text: str) -> str:
    """Minúsculas, sin tildes ("Mayús" → "mayus") y con los espacios convertidos en "_"."""
    decomposed = unicodedata.normalize("NFD", text.strip().lower())
    plain = unicodedata.normalize("NFC", "".join(c for c in decomposed if c not in _STRIPPED_MARKS))
    return re.sub(r"\s+", "_", plain)


# Los nombres legibles también se aceptan al parsear ("Clic derecho", "Bloq Mayús"...).
_LOOKUP: dict[str, str] = {}
for _tok, _name in _DISPLAY.items():
    _LOOKUP.setdefault(_simplify(_name), _tok)
for _alias, _tok in _ALIASES.items():
    _LOOKUP[_simplify(_alias)] = _tok
for _tok in _VK:
    _LOOKUP[_tok] = _tok


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------
def normalize(token: str) -> str:
    """Token canónico a partir de un nombre, alias o ``vk_xx``; ValueError si no es válido."""
    if not isinstance(token, str):
        raise ValueError(f"Tecla no válida: {token!r}")
    raw = token.strip()
    if not raw:
        raise ValueError("Tecla vacía")
    key = raw.lower()
    if key in _VK:
        return key
    simple = _simplify(raw)
    found = _LOOKUP.get(simple)
    if found:
        return found
    match = _VK_RE.fullmatch(simple)
    if match:
        vk = int(match.group(1), 16)
        if 0x01 <= vk <= 0xFF:
            return vk_to_token(vk)
    match = _NUMPAD_RE.fullmatch(simple)
    if match:
        return f"num{match.group(1)}"
    raise ValueError(f"Tecla desconocida: «{raw}»")


def _try_normalize(token: Any) -> Optional[str]:
    try:
        return normalize(token)
    except ValueError:
        return None


def is_mouse(token: str) -> bool:
    """True si es un botón del ratón (acepta alias como "lmb")."""
    return _try_normalize(token) in MOUSE_TOKENS


def is_modifier(token: str) -> bool:
    """True para ctrl/shift/alt/win y sus variantes izquierda/derecha."""
    return _try_normalize(token) in MODIFIERS


def generic_modifier(token: str) -> str:
    """Modificador genérico de un token ("rctrl" → "ctrl"); los demás tokens, normalizados."""
    tok = normalize(token)
    if tok in MODIFIERS and tok[0] in "lr":
        return tok[1:]
    return tok


def token_to_vk(token: str) -> int:
    """Código VK de Windows del token (los modificadores genéricos → versión izquierda)."""
    tok = normalize(token)
    if tok.startswith("vk_"):
        return int(tok[3:], 16)
    return _VK[tok]


def vk_to_token(vk: int, extended: bool = False) -> str:
    """Token para un VK. ``extended`` distingue Num Intro, Ctrl der. y AltGr.

    El VK izquierdo de un modificador (0xA2) y el genérico (0x11) dan el token
    genérico ("ctrl"); el derecho, el específico ("rctrl"). VK sin nombre → "vk_xx".
    """
    try:
        code = operator.index(vk)
    except TypeError:
        raise ValueError(f"Código VK no válido: {vk!r}") from None
    if not 0x01 <= code <= 0xFF:
        raise ValueError(f"Código VK fuera de rango: {code}")
    if extended and code in _EXTENDED_VARIANTS:
        return _EXTENDED_VARIANTS[code]
    return _TOKEN_BY_VK.get(code) or f"vk_{code:02x}"


def _map_scan(vk: int) -> int:
    """MapVirtualKeyW(vk, MAPVK_VK_TO_VSC_EX) o 0 si no hay user32."""
    if _user32 is None:
        return 0
    try:
        return int(_user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC_EX))
    except (OSError, ctypes.ArgumentError):
        return 0


def is_extended_key(token_or_vk: TokenOrVk) -> bool:
    """True si la tecla necesita KEYEVENTF_EXTENDEDKEY (prefijo E0)."""
    if isinstance(token_or_vk, str):
        tok = normalize(token_or_vk)
        if tok in _EXTENDED_TOKENS:
            return True
        if not tok.startswith("vk_"):
            return False
        vk = int(tok[3:], 16)
    else:
        try:
            vk = operator.index(token_or_vk)
        except TypeError:
            raise ValueError(f"Tecla no válida: {token_or_vk!r}") from None
    return vk in _EXTENDED_VKS or (_map_scan(vk) >> 8) == 0xE0


def scan_code(token: str) -> int:
    """Scancode (byte bajo, "make code") de la tecla según la distribución actual.

    Se obtiene con MapVirtualKeyW(vk, MAPVK_VK_TO_VSC_EX); el prefijo E0 se
    expresa con :func:`is_extended_key`. Devuelve 0 para los botones del ratón,
    las teclas sin scancode y Pausa (secuencia E1: hay que enviarla por VK).
    """
    tok = normalize(token)
    if tok in MOUSE_TOKENS:
        return 0
    if tok in _SCAN_OVERRIDES:
        return _SCAN_OVERRIDES[tok]
    if _user32 is None:
        return _STATIC_SCAN.get(tok, 0)
    raw = _map_scan(token_to_vk(tok))
    if raw >> 8 == 0xE1:
        return 0
    return raw & 0xFF


def _key_name_text(vk: int) -> str:
    """Nombre de la tecla según la distribución (GetKeyNameTextW) o ""."""
    if _user32 is None:
        return ""
    raw = _map_scan(vk)
    if not raw or raw >> 8 == 0xE1:
        return ""
    extended = 1 if raw >> 8 == 0xE0 else 0
    lparam = ((raw & 0xFF) << 16) | (extended << 24)
    buf = ctypes.create_unicode_buffer(64)
    try:
        length = _user32.GetKeyNameTextW(lparam, buf, len(buf))
    except (OSError, ctypes.ArgumentError):
        return ""
    name = buf.value[:length].strip() if length > 0 else ""
    return name.upper() if len(name) == 1 else name


def display_name(token: str) -> str:
    """Nombre legible en español ("Intro", "Clic derecho", "Num 5", "W"...).

    Nunca lanza excepciones: un token desconocido se devuelve tal cual.
    """
    tok = _try_normalize(token)
    if tok is None:
        return str(token)
    name = _DISPLAY.get(tok)
    if name:
        return name
    vk = int(tok[3:], 16)
    return _VK_NAMES.get(vk) or _key_name_text(vk) or f"VK 0x{vk:02X}"


def _as_items(tokens: Union[str, Iterable[str]]) -> list[str]:
    """Una combinación como lista de trozos sin normalizar."""
    if isinstance(tokens, str):
        return [part.strip() for part in tokens.split("+") if part.strip()]
    return [t for t in tokens]


def parse_combo(text: Union[str, Iterable[str]]) -> list[str]:
    """"Ctrl + Shift+S" → ["ctrl", "shift", "s"]. Sin duplicados; ValueError si hay errores."""
    if isinstance(text, str):
        if not text.strip():
            return []
        parts = [part.strip() for part in text.split("+")]
        if any(not part for part in parts):
            raise ValueError(f"Combinación incompleta: «{text.strip()}» (falta una tecla junto a «+»)")
    else:
        parts = list(text)
    result: list[str] = []
    for part in parts:
        tok = normalize(part)
        if tok not in result:
            result.append(tok)
    return result


def format_combo(tokens: Union[str, Iterable[str]]) -> str:
    """Nombres legibles unidos por " + " ("W + Clic derecho"); vacío → "—"."""
    items = _as_items(tokens)
    if not items:
        return "—"
    return " + ".join(display_name(t) for t in items)


def combo_to_string(tokens: Union[str, Iterable[str]]) -> str:
    """Forma canónica para guardar: "ctrl+shift+s" (mismo orden, sin duplicados)."""
    return "+".join(parse_combo(tokens))


def sort_combo(tokens: Union[str, Iterable[str]]) -> list[str]:
    """Modificadores primero (ctrl, alt, shift, win), luego el resto en su orden."""
    items = parse_combo(tokens)
    rank = {tok: i for i, tok in enumerate(_MODIFIER_ORDER)}
    return sorted(items, key=lambda t: rank.get(t, len(rank)))


def key_groups() -> list[tuple[str, list[tuple[str, str]]]]:
    """Teclas agrupadas para menús: [(nombre del grupo, [(token, nombre)...])...].

    Si dos teclas de símbolos se llaman igual en la distribución actual (p. ej.
    dos «\\» en EE. UU.), la segunda se distingue con su código: «\\ (VK 0xE2)».
    """
    oem = [f"vk_{vk:02x}" for vk in _OEM_VKS if _user32 is None or _map_scan(vk)]
    groups: list[tuple[str, list[str]]] = [
        ("Letras", _LETTERS),
        ("Números", [*_DIGITS[1:], _DIGITS[0]]),
        ("Teclas de función", _FKEYS),
        ("Edición y navegación", _EDITING),
        ("Bloqueo y sistema", _SYSTEM),
        ("Modificadores", _MODIFIER_ORDER),
        ("Teclado numérico", _NUMPAD),
        ("Multimedia", _MEDIA),
        ("Símbolos (según la distribución)", oem),
        ("Ratón", list(MOUSE_TOKENS)),
    ]
    seen: set[str] = set()
    result: list[tuple[str, list[tuple[str, str]]]] = []
    for title, toks in groups:
        items: list[tuple[str, str]] = []
        for tok in toks:
            name = display_name(tok)
            if name in seen:
                name = f"{name} (VK 0x{token_to_vk(tok):02X})"
            seen.add(name)
            items.append((tok, name))
        if items:
            result.append((title, items))
    return result


def key_choices() -> list[tuple[str, str]]:
    """Lista plana (token, nombre) para desplegables, agrupada lógicamente."""
    return [item for _, items in key_groups() for item in items]
