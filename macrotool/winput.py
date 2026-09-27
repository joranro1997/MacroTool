"""Inyección de entrada en Windows (SendInput) y utilidades Win32 relacionadas.

Todo lo que inyecta MacroTool lleva ``MACROTOOL_EXTRA_INFO`` en ``dwExtraInfo``
para que los hooks reconozcan (e ignoren) la entrada generada por las macros.

Las funciones ``*_input(s)`` sólo *construyen* estructuras ``INPUT`` (se pueden
probar sin tocar el escritorio); el envío real se hace en ``send_inputs``.
"""
from __future__ import annotations

import ctypes
import logging
import os
import threading
from ctypes import wintypes
from types import ModuleType
from typing import Iterable, Protocol, Sequence, runtime_checkable

log = logging.getLogger(__name__)

# --- Tipos Win32 con el tamaño exacto en x64 ---------------------------------
ULONG_PTR = ctypes.c_size_t
LONG_PTR = ctypes.c_ssize_t
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t
LRESULT = ctypes.c_ssize_t
HANDLE = wintypes.HANDLE  # c_void_p

# Valor mágico ("MCRO") en dwExtraInfo de todo lo que inyectamos.
MACROTOOL_EXTRA_INFO = 0x4D43524F

BLOCKED_MESSAGE = "Windows bloqueó la entrada (¿la ventana destino se ejecuta como administrador?)"

# --- Constantes -----------------------------------------------------------------
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
INPUT_HARDWARE = 2

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_XDOWN = 0x0080
MOUSEEVENTF_XUP = 0x0100
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_ABSOLUTE = 0x8000

XBUTTON1 = 0x0001
XBUTTON2 = 0x0002
WHEEL_DELTA = 120

SM_CXSCREEN = 0
SM_CYSCREEN = 1
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

# VK sin asignar que se usa para "enmascarar" Alt/Win (evita que se abra el menú
# de la ventana o el menú Inicio al bloquear un disparador como Alt+1 o Win+1).
VK_MASK_KEY = 0xE8

# (flag down, flag up, mouseData) de cada botón del ratón.
_MOUSE_BUTTONS: dict[str, tuple[int, int, int]] = {
    "mouse_left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP, 0),
    "mouse_right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP, 0),
    "mouse_middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP, 0),
    "mouse_x1": (MOUSEEVENTF_XDOWN, MOUSEEVENTF_XUP, XBUTTON1),
    "mouse_x2": (MOUSEEVENTF_XDOWN, MOUSEEVENTF_XUP, XBUTTON2),
}

# Los modificadores genéricos se envían como la versión izquierda.
_SEND_AS = {"ctrl": "lctrl", "shift": "lshift", "alt": "lalt", "win": "lwin"}


# --- Estructuras ------------------------------------------------------------------
class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


# --- Funciones Win32 ----------------------------------------------------------------
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
winmm = ctypes.WinDLL("winmm", use_last_error=True)


def _declare(dll: ctypes.WinDLL, name: str, restype, *argtypes):
    fn = getattr(dll, name)
    fn.restype = restype
    fn.argtypes = list(argtypes)
    return fn


_SendInput = _declare(user32, "SendInput", wintypes.UINT, wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
_GetCursorPos = _declare(user32, "GetCursorPos", wintypes.BOOL, ctypes.POINTER(wintypes.POINT))
_SetCursorPos = _declare(user32, "SetCursorPos", wintypes.BOOL, ctypes.c_int, ctypes.c_int)
_GetSystemMetrics = _declare(user32, "GetSystemMetrics", ctypes.c_int, ctypes.c_int)
_GetAsyncKeyState = _declare(user32, "GetAsyncKeyState", ctypes.c_short, ctypes.c_int)
_GetForegroundWindow = _declare(user32, "GetForegroundWindow", wintypes.HWND)
_GetWindowThreadProcessId = _declare(
    user32, "GetWindowThreadProcessId", wintypes.DWORD, wintypes.HWND, ctypes.POINTER(wintypes.DWORD)
)
_WindowFromPoint = _declare(user32, "WindowFromPoint", wintypes.HWND, wintypes.POINT)
_timeBeginPeriod = _declare(winmm, "timeBeginPeriod", wintypes.UINT, wintypes.UINT)
_timeEndPeriod = _declare(winmm, "timeEndPeriod", wintypes.UINT, wintypes.UINT)

# keys.py se importa de forma perezosa (y se cachea): usa ctypes y es de otro módulo.
_keys_mod: ModuleType | None = None


def _keys() -> ModuleType:
    global _keys_mod
    if _keys_mod is None:
        from . import keys as mod

        _keys_mod = mod
    return _keys_mod


class InputError(RuntimeError):
    """Windows no insertó todos los eventos pedidos (p. ej. bloqueo de UIPI)."""

    def __init__(self, message: str = BLOCKED_MESSAGE, *, inserted: int = 0, expected: int = 0, winerror: int = 0):
        super().__init__(message)
        self.inserted = inserted
        self.expected = expected
        self.winerror = winerror


# --- Construcción de INPUT (sin enviar nada) --------------------------------------
def send_token(token: str) -> str:
    """Token normalizado tal y como se envía (``ctrl`` → ``lctrl``...)."""
    tok = _keys().normalize(token)
    return _SEND_AS.get(tok, tok)


def mouse_button_input(token: str, up: bool = False) -> INPUT:
    """INPUT de pulsar/soltar un botón del ratón (``mouse_left``...)."""
    try:
        down_flag, up_flag, data = _MOUSE_BUTTONS[token]
    except KeyError:
        raise ValueError(f"No es un botón del ratón: {token!r}") from None
    inp = INPUT(type=INPUT_MOUSE)
    inp.mi.mouseData = data
    inp.mi.dwFlags = up_flag if up else down_flag
    inp.mi.dwExtraInfo = MACROTOOL_EXTRA_INFO
    return inp


def key_input(token: str, up: bool = False, *, use_scancodes: bool = True) -> INPUT:
    """INPUT de una tecla o botón del ratón (los botones se delegan en ``mouse_button_input``).

    Con ``use_scancodes`` y un scancode conocido se envía con KEYEVENTF_SCANCODE
    (lo que leen los juegos con DirectInput); si no, por código virtual.
    """
    k = _keys()
    tok = k.normalize(token)
    tok = _SEND_AS.get(tok, tok)
    if tok in _MOUSE_BUTTONS:
        return mouse_button_input(tok, up)
    vk = int(k.token_to_vk(tok))
    sc = int(k.scan_code(tok) or 0)
    prefix = sc >> 8
    extended = bool(k.is_extended_key(tok)) or prefix == 0xE0
    flags = KEYEVENTF_KEYUP if up else 0
    inp = INPUT(type=INPUT_KEYBOARD)
    # Pausa (prefijo E1) no se puede reproducir con un único scancode: va por VK.
    if use_scancodes and sc and prefix != 0xE1:
        inp.ki.wVk = 0
        inp.ki.wScan = sc & 0xFF
        flags |= KEYEVENTF_SCANCODE
    else:
        inp.ki.wVk = vk
        inp.ki.wScan = sc & 0xFF  # informativo: algunas apps leen el scancode del lParam
    if extended:
        flags |= KEYEVENTF_EXTENDEDKEY
    inp.ki.dwFlags = flags
    inp.ki.dwExtraInfo = MACROTOOL_EXTRA_INFO
    return inp


def vk_input(vk: int, up: bool = False) -> INPUT:
    """INPUT de un código virtual "en bruto" (sin scancode)."""
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.ki.wVk = vk & 0xFFFF
    inp.ki.dwFlags = KEYEVENTF_KEYUP if up else 0
    inp.ki.dwExtraInfo = MACROTOOL_EXTRA_INFO
    return inp


def build_inputs(tokens: Iterable[str], up: bool = False, *, use_scancodes: bool = True) -> list[INPUT]:
    """Lista de INPUT para pulsar (``up=False``) o soltar todas las entradas, en el orden dado."""
    return [key_input(t, up, use_scancodes=use_scancodes) for t in tokens]


def unicode_inputs(ch: str) -> list[INPUT]:
    """down+up KEYEVENTF_UNICODE de un carácter (dos unidades UTF-16 si es > U+FFFF)."""
    if len(ch) != 1:
        raise ValueError(f"Se esperaba un único carácter, no {ch!r}")
    raw = ch.encode("utf-16-le", "surrogatepass")
    units = [int.from_bytes(raw[i : i + 2], "little") for i in range(0, len(raw), 2)]
    result: list[INPUT] = []
    for unit in units:
        for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
            inp = INPUT(type=INPUT_KEYBOARD)
            inp.ki.wVk = 0
            inp.ki.wScan = unit
            inp.ki.dwFlags = flags
            inp.ki.dwExtraInfo = MACROTOOL_EXTRA_INFO
            result.append(inp)
    return result


def move_rel_input(dx: int, dy: int) -> INPUT:
    """Movimiento relativo (sujeto a la aceleración del puntero; los juegos leen el delta en bruto)."""
    inp = INPUT(type=INPUT_MOUSE)
    inp.mi.dx = int(dx)
    inp.mi.dy = int(dy)
    inp.mi.dwFlags = MOUSEEVENTF_MOVE
    inp.mi.dwExtraInfo = MACROTOOL_EXTRA_INFO
    return inp


def move_abs_input(x: int, y: int, virtual_screen: tuple[int, int, int, int] | None = None) -> INPUT:
    """Movimiento absoluto normalizado (0–65535) sobre el escritorio virtual."""
    vx, vy, vw, vh = virtual_screen or _virtual_screen()
    inp = INPUT(type=INPUT_MOUSE)
    inp.mi.dx = round((x - vx) * 65535 / max(1, vw - 1))
    inp.mi.dy = round((y - vy) * 65535 / max(1, vh - 1))
    inp.mi.dwFlags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
    inp.mi.dwExtraInfo = MACROTOOL_EXTRA_INFO
    return inp


def scroll_inputs(notches: int, horizontal: bool = False) -> list[INPUT]:
    """Un evento de ±WHEEL_DELTA por muesca (máxima compatibilidad con juegos)."""
    notches = int(notches)
    step = WHEEL_DELTA if notches > 0 else -WHEEL_DELTA
    flag = MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL
    result: list[INPUT] = []
    for _ in range(abs(notches)):
        inp = INPUT(type=INPUT_MOUSE)
        inp.mi.mouseData = step & 0xFFFFFFFF  # DWORD con signo en complemento a dos
        inp.mi.dwFlags = flag
        inp.mi.dwExtraInfo = MACROTOOL_EXTRA_INFO
        result.append(inp)
    return result


# --- Envío -----------------------------------------------------------------------
def send_inputs(inputs: Sequence[INPUT], *, quiet: bool = False) -> None:
    """Envía todos los eventos en UNA llamada a SendInput. ``InputError`` si Windows no los inserta.

    ``quiet`` registra el fallo sólo en modo depuración (reintentos periódicos).
    """
    n = len(inputs)
    if n == 0:
        return
    array = (INPUT * n)(*inputs)
    inserted = _SendInput(n, array, ctypes.sizeof(INPUT))
    if inserted != n:
        err = ctypes.get_last_error()
        (log.debug if quiet else log.warning)("SendInput insertó %d de %d eventos (error Win32 %d)",
                                              inserted, n, err)
        raise InputError(BLOCKED_MESSAGE, inserted=max(0, min(int(inserted), n)), expected=n, winerror=err)


def send_mask_key() -> None:
    """Pulsa y suelta una tecla sin asignar para que Windows no active menús al soltar Alt/Win."""
    send_inputs([vk_input(VK_MASK_KEY), vk_input(VK_MASK_KEY, up=True)])


# --- Consultas -----------------------------------------------------------------------
def get_cursor_pos() -> tuple[int, int]:
    """Posición del cursor (píxeles físicos si el proceso es consciente de DPI)."""
    pt = wintypes.POINT()
    if not _GetCursorPos(ctypes.byref(pt)):
        raise ctypes.WinError(ctypes.get_last_error())
    return pt.x, pt.y


def set_cursor_pos(x: int, y: int) -> bool:
    return bool(_SetCursorPos(int(x), int(y)))


def screen_size() -> tuple[int, int]:
    """Tamaño de la pantalla principal."""
    return _GetSystemMetrics(SM_CXSCREEN), _GetSystemMetrics(SM_CYSCREEN)


def _virtual_screen() -> tuple[int, int, int, int]:
    return (
        _GetSystemMetrics(SM_XVIRTUALSCREEN),
        _GetSystemMetrics(SM_YVIRTUALSCREEN),
        _GetSystemMetrics(SM_CXVIRTUALSCREEN),
        _GetSystemMetrics(SM_CYVIRTUALSCREEN),
    )


def is_key_down(vk: int) -> bool:
    """Estado asíncrono (GetAsyncKeyState) de una tecla o botón del ratón."""
    return bool(_GetAsyncKeyState(int(vk)) & 0x8000)


def _window_pid(hwnd) -> int:
    pid = wintypes.DWORD(0)
    _GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def foreground_is_own_process() -> bool:
    """True si la ventana en primer plano pertenece a este proceso."""
    hwnd = _GetForegroundWindow()
    return bool(hwnd) and _window_pid(hwnd) == os.getpid()


def window_is_own_process_at(x: int, y: int) -> bool:
    """True si la ventana bajo el punto (x, y) pertenece a este proceso.

    No llamar desde un callback de hook: WindowFromPoint puede enviar mensajes.
    """
    hwnd = _WindowFromPoint(wintypes.POINT(int(x), int(y)))
    return bool(hwnd) and _window_pid(hwnd) == os.getpid()


# --- Ajustes del proceso ---------------------------------------------------------------
_timer_lock = threading.Lock()
_timer_active = False


def set_timer_resolution(enable: bool) -> None:
    """timeBeginPeriod(1)/timeEndPeriod(1) para esperas precisas. Idempotente."""
    global _timer_active
    with _timer_lock:
        if bool(enable) == _timer_active:
            return
        try:
            if enable:
                _timer_active = _timeBeginPeriod(1) == 0
            else:
                _timeEndPeriod(1)
                _timer_active = False
        except OSError:
            log.debug("No se pudo cambiar la resolución del temporizador", exc_info=True)


def timer_resolution_active() -> bool:
    return _timer_active


def enable_dpi_awareness() -> None:
    """Hace el proceso consciente de DPI por monitor (V2), con alternativas. Silencioso si falla."""
    try:
        fn = user32.SetProcessDpiAwarenessContext
        fn.restype = wintypes.BOOL
        fn.argtypes = [HANDLE]
        if fn(HANDLE(-4)):  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
            return
        if ctypes.get_last_error() == 5:  # ERROR_ACCESS_DENIED: ya estaba establecido
            return
    except (AttributeError, OSError):
        pass
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        fn = shcore.SetProcessDpiAwareness
        fn.restype = ctypes.c_long  # HRESULT
        fn.argtypes = [ctypes.c_int]
        if fn(2) == 0:  # PROCESS_PER_MONITOR_DPI_AWARE
            return
    except (AttributeError, OSError):
        pass
    try:
        fn = user32.SetProcessDPIAware
        fn.restype = wintypes.BOOL
        fn.argtypes = []
        fn()
    except (AttributeError, OSError):
        pass


# --- Backends ---------------------------------------------------------------------------
@runtime_checkable
class InputBackend(Protocol):
    def press(self, tokens: list[str]) -> None: ...
    def release(self, tokens: list[str]) -> None: ...
    def type_char(self, ch: str) -> None: ...
    def move_to(self, x: int, y: int) -> None: ...
    def move_rel(self, dx: int, dy: int) -> None: ...
    def scroll(self, notches: int, horizontal: bool = False) -> None: ...
    def cursor_pos(self) -> tuple[int, int]: ...
    def screen_size(self) -> tuple[int, int]: ...
    def release_all(self) -> bool | None: ...  # False si algo quedó sin soltar


class WinInputBackend:
    """Backend real basado en SendInput. Thread-safe.

    Si Windows rechaza una suelta (UIPI con una ventana elevada en primer plano, escritorio
    seguro de UAC o Ctrl+Alt+Supr), las entradas no se olvidan: quedan como *sueltas
    pendientes* y se reintentan en la siguiente llamada (``press``, ``release``,
    ``release_all``) o con ``retry_pending_release``.
    """

    def __init__(self, use_scancodes: bool = True) -> None:
        self.use_scancodes = use_scancodes
        self._lock = threading.Lock()
        # Entradas pulsadas con press() y no soltadas (dict = conjunto ordenado).
        self._pressed: dict[str, None] = {}
        # Entradas cuya suelta rechazó Windows (en el orden en que hay que soltarlas): siguen
        # pulsadas en el sistema.
        self._pending: dict[str, None] = {}

    @property
    def pressed(self) -> list[str]:
        """Entradas actualmente pulsadas por este backend, en orden de pulsación."""
        with self._lock:
            return list(self._pressed)

    @property
    def pending_release(self) -> list[str]:
        """Entradas que se intentaron soltar sin éxito (se reintentarán)."""
        with self._lock:
            return list(self._pending)

    def press(self, tokens: Iterable[str]) -> None:
        toks = [send_token(t) for t in tokens]  # ValueError antes de enviar nada
        inputs = build_inputs(toks, up=False, use_scancodes=self.use_scancodes)
        with self._lock:
            for t in toks:
                self._pending.pop(t, None)  # se vuelve a pulsar: ya no está pendiente de soltar
            self._flush_pending_locked()
            for t in toks:
                self._pressed.setdefault(t, None)
            send_inputs(inputs)

    def release(self, tokens: Iterable[str]) -> None:
        toks = [send_token(t) for t in tokens]
        inputs = build_inputs(toks, up=True, use_scancodes=self.use_scancodes)
        with self._lock:
            self._flush_pending_locked()
            tracked = {t for t in toks if t in self._pressed or t in self._pending}
            for t in toks:
                self._pressed.pop(t, None)
                self._pending.pop(t, None)
            try:
                send_inputs(inputs)
            except Exception as exc:
                # Windows inserta los eventos en orden: los primeros ``inserted`` sí se soltaron.
                inserted = exc.inserted if isinstance(exc, InputError) else 0
                for t in toks[inserted:]:
                    if t in tracked:
                        self._pending.setdefault(t, None)
                raise

    def release_all(self) -> bool:
        """Suelta (en orden inverso) todo lo pulsado y lo pendiente.

        Nunca lanza excepciones (se llama desde bloques ``finally``): devuelve False si
        Windows rechazó alguna suelta; esas entradas quedan en ``pending_release``.
        """
        with self._lock:
            # Primero lo que ya se intentó soltar (en su orden) y luego lo pulsado, al revés.
            toks = list(self._pending) + [t for t in reversed(self._pressed) if t not in self._pending]
            self._pressed.clear()
            self._pending.clear()
            return self._send_releases_locked(toks, quiet=False)

    def retry_pending_release(self) -> bool:
        """Reintenta las sueltas pendientes. True si ya no queda ninguna."""
        with self._lock:
            if not self._pending:
                return True
            toks = list(self._pending)
            self._pending.clear()
            return self._send_releases_locked(toks, quiet=True)

    def _flush_pending_locked(self) -> None:
        if self._pending:
            toks = list(self._pending)
            self._pending.clear()
            self._send_releases_locked(toks, quiet=True)

    def _send_releases_locked(self, toks: list[str], *, quiet: bool) -> bool:
        if not toks:
            return True
        try:
            send_inputs(build_inputs(toks, up=True, use_scancodes=self.use_scancodes), quiet=quiet)
            return True
        except Exception as exc:  # noqa: BLE001 - nunca romper a quien llama (finally, temporizadores)
            inserted = exc.inserted if isinstance(exc, InputError) else 0
            for t in toks[inserted:]:
                self._pending.setdefault(t, None)
            if not quiet:
                log.warning("No se pudieron soltar las entradas pulsadas %s: %s", toks[inserted:], exc)
            return False

    def type_char(self, ch: str) -> None:
        inputs = unicode_inputs(ch)
        with self._lock:
            send_inputs(inputs)

    def move_to(self, x: int, y: int) -> None:
        if set_cursor_pos(x, y):
            return
        with self._lock:  # p. ej. escritorio no interactivo: se intenta con SendInput
            send_inputs([move_abs_input(x, y)])

    def move_rel(self, dx: int, dy: int) -> None:
        if not dx and not dy:
            return
        with self._lock:
            send_inputs([move_rel_input(dx, dy)])

    def scroll(self, notches: int, horizontal: bool = False) -> None:
        inputs = scroll_inputs(notches, horizontal)
        with self._lock:
            send_inputs(inputs)

    def cursor_pos(self) -> tuple[int, int]:
        return get_cursor_pos()

    def screen_size(self) -> tuple[int, int]:
        return screen_size()
