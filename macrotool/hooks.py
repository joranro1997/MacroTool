"""Hooks de bajo nivel de teclado y ratón, disparadores y atajos globales.

``InputHook`` instala ``WH_KEYBOARD_LL`` y ``WH_MOUSE_LL`` en un hilo propio con
su bucle de mensajes y reparte cada evento (``RawEvent``) entre los listeners.
``TriggerManager`` decide, con lógica pura y rápida, qué eventos disparan un
binding y cuáles hay que bloquear.
"""
from __future__ import annotations

import atexit
import ctypes
import logging
import threading
import time
import weakref
from ctypes import wintypes
from dataclasses import dataclass
from types import ModuleType
from typing import Callable, Iterable, Optional

from .winput import LPARAM, LRESULT, MACROTOOL_EXTRA_INFO, ULONG_PTR, WPARAM

log = logging.getLogger(__name__)

# --- Constantes Win32 --------------------------------------------------------------
WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
HC_ACTION = 0

WM_QUIT = 0x0012
WM_USER = 0x0400
PM_NOREMOVE = 0x0000

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
WM_MOUSEWHEEL = 0x020A
WM_XBUTTONDOWN = 0x020B
WM_XBUTTONUP = 0x020C
WM_MOUSEHWHEEL = 0x020E

LLKHF_EXTENDED = 0x01
LLKHF_INJECTED = 0x10
LLMHF_INJECTED = 0x01
SC_ALTGR_FAKE_LCTRL = 0x21D  # Ctrl izquierdo sintetizado por AltGr
ALTGR_PAIR_S = 0.05  # el Ctrl ficticio y el Alt derecho de AltGr llegan casi a la vez

XBUTTON1 = 0x0001
XBUTTON2 = 0x0002

HHOOK = wintypes.HANDLE
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, WPARAM, LPARAM)


# --- Estructuras ---------------------------------------------------------------------
class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", WPARAM),
        ("lParam", LPARAM),
        ("time", wintypes.DWORD),
        ("pt", wintypes.POINT),
    ]


# --- Funciones Win32 (instancias propias de WinDLL: argtypes independientes) --------
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


def _declare(dll: ctypes.WinDLL, name: str, restype, *argtypes):
    fn = getattr(dll, name)
    fn.restype = restype
    fn.argtypes = list(argtypes)
    return fn


_SetWindowsHookExW = _declare(
    _user32, "SetWindowsHookExW", HHOOK, ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD
)
_CallNextHookEx = _declare(_user32, "CallNextHookEx", LRESULT, HHOOK, ctypes.c_int, WPARAM, LPARAM)
_UnhookWindowsHookEx = _declare(_user32, "UnhookWindowsHookEx", wintypes.BOOL, HHOOK)
_GetMessageW = _declare(_user32, "GetMessageW", wintypes.BOOL, ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
_PeekMessageW = _declare(
    _user32, "PeekMessageW", wintypes.BOOL,
    ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT,
)
_PostThreadMessageW = _declare(_user32, "PostThreadMessageW", wintypes.BOOL, wintypes.DWORD, wintypes.UINT, WPARAM, LPARAM)
_GetModuleHandleW = _declare(_kernel32, "GetModuleHandleW", wintypes.HMODULE, wintypes.LPCWSTR)
_GetCurrentThreadId = _declare(_kernel32, "GetCurrentThreadId", wintypes.DWORD)

_keys_mod: ModuleType | None = None


def _keys() -> ModuleType:
    global _keys_mod
    if _keys_mod is None:
        from . import keys as mod

        _keys_mod = mod
    return _keys_mod


# --- Eventos ---------------------------------------------------------------------------
@dataclass(slots=True)
class RawEvent:
    t: float  # time.perf_counter() al recibirlo
    kind: str  # "key_down" | "key_up" | "mouse_down" | "mouse_up" | "move" | "wheel" | "hwheel"
    token: str = ""  # token de keys.py (teclas y botones); "" para move/wheel
    vk: int = 0
    x: int = 0  # posición del cursor en píxeles físicos (sólo eventos de ratón)
    y: int = 0
    delta: int = 0  # rueda: múltiplos de 120 (positivo = arriba/derecha)
    injected: bool = False  # inyectado (LLKHF/LLMHF_INJECTED) o generado por MacroTool
    consumed: bool = False  # True si un listener anterior lo bloqueó
    # Teclado: scancode de la tecla física (| 0xE000 si es extendida; 0 = desconocido). Windows
    # traduce el VK según el estado (Bloq Num, Mayús): el scancode empareja el down con su up.
    scan: int = 0
    altgr: bool = False  # "ralt" que actúa como AltGr (llegó con el Ctrl ficticio 0x21D)


_KEY_KINDS = {WM_KEYDOWN: "key_down", WM_SYSKEYDOWN: "key_down", WM_KEYUP: "key_up", WM_SYSKEYUP: "key_up"}

_BUTTON_MESSAGES: dict[int, tuple[str, str, int]] = {
    WM_LBUTTONDOWN: ("mouse_down", "mouse_left", 0x01),
    WM_LBUTTONUP: ("mouse_up", "mouse_left", 0x01),
    WM_RBUTTONDOWN: ("mouse_down", "mouse_right", 0x02),
    WM_RBUTTONUP: ("mouse_up", "mouse_right", 0x02),
    WM_MBUTTONDOWN: ("mouse_down", "mouse_middle", 0x04),
    WM_MBUTTONUP: ("mouse_up", "mouse_middle", 0x04),
}
_XBUTTONS = {XBUTTON1: ("mouse_x1", 0x05), XBUTTON2: ("mouse_x2", 0x06)}


def _signed_hiword(value: int) -> int:
    hi = (value >> 16) & 0xFFFF
    return hi - 0x10000 if hi & 0x8000 else hi


def parse_keyboard(
    msg: int, vk: int, flags: int, extra_info: int, t: float, scan_code: int = 0, *, altgr: bool = False
) -> Optional[RawEvent]:
    """Traduce los datos de WH_KEYBOARD_LL a un RawEvent (None si no es relevante).

    Se descarta el Ctrl izquierdo ficticio que Windows genera al pulsar AltGr
    (scancode 0x21D): la tecla real es AltGr (``ralt``). ``altgr=True`` indica que ese
    Ctrl ficticio acompaña a la pulsación (distribuciones con AltGr, como la española).
    """
    kind = _KEY_KINDS.get(msg)
    if kind is None or not 0x01 <= vk <= 0xFF or scan_code == SC_ALTGR_FAKE_LCTRL:
        return None
    extended = bool(flags & LLKHF_EXTENDED)
    try:
        token = _keys().vk_to_token(vk, extended=extended)
    except ValueError:
        token = f"vk_{vk:02x}"
    injected = bool(flags & LLKHF_INJECTED) or extra_info == MACROTOOL_EXTRA_INFO
    scan = (int(scan_code) & 0xFFF) | (0xE000 if extended and scan_code else 0)
    return RawEvent(t, kind, token, vk, injected=injected, scan=scan, altgr=altgr and token == "ralt")


def parse_mouse(msg: int, x: int, y: int, mouse_data: int, flags: int, extra_info: int, t: float) -> Optional[RawEvent]:
    """Traduce los datos de WH_MOUSE_LL a un RawEvent (None si no es relevante)."""
    injected = bool(flags & LLMHF_INJECTED) or extra_info == MACROTOOL_EXTRA_INFO
    if msg == WM_MOUSEMOVE:
        return RawEvent(t, "move", x=x, y=y, injected=injected)
    button = _BUTTON_MESSAGES.get(msg)
    if button is not None:
        kind, token, vk = button
        return RawEvent(t, kind, token, vk, x, y, injected=injected)
    if msg in (WM_XBUTTONDOWN, WM_XBUTTONUP):
        xb = _XBUTTONS.get((mouse_data >> 16) & 0xFFFF)
        if xb is None:
            return None
        kind = "mouse_down" if msg == WM_XBUTTONDOWN else "mouse_up"
        return RawEvent(t, kind, xb[0], xb[1], x, y, injected=injected)
    if msg in (WM_MOUSEWHEEL, WM_MOUSEHWHEEL):
        kind = "wheel" if msg == WM_MOUSEWHEEL else "hwheel"
        return RawEvent(t, kind, x=x, y=y, delta=_signed_hiword(mouse_data), injected=injected)
    return None


# --- Hook -------------------------------------------------------------------------------
Listener = Callable[[RawEvent], Optional[bool]]


@dataclass(frozen=True)
class _ListenerEntry:
    fn: Listener
    priority: int
    seq: int
    wants_moves: bool


class _Worker:
    """Estado de un hilo de hooks (cada start() crea uno nuevo)."""

    def __init__(self) -> None:
        self.thread: threading.Thread | None = None
        self.thread_id = 0
        self.started = threading.Event()
        self.error: BaseException | None = None
        self.installed = False


_live_hooks: "weakref.WeakSet[InputHook]" = weakref.WeakSet()


class InputHook:
    """Hooks LL de teclado y ratón en un hilo dedicado con bucle de mensajes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._listeners: tuple[_ListenerEntry, ...] = ()  # copia inmutable: lectura sin lock
        self._want_moves = False
        self._seq = 0
        self._worker: _Worker | None = None
        self._fake_lctrl_t = float("-inf")  # último Ctrl ficticio de AltGr (sólo hilo del hook)
        # Referencias retenidas a los callbacks: si el GC los recoge, Windows saltaría a basura.
        self._kb_proc = HOOKPROC(self._keyboard_proc)
        self._ms_proc = HOOKPROC(self._mouse_proc)
        _live_hooks.add(self)

    # -- estado / ciclo de vida --
    @property
    def running(self) -> bool:
        worker = self._worker
        return worker is not None and worker.installed and worker.thread is not None and worker.thread.is_alive()

    def start(self, timeout: float = 5.0) -> None:
        """Instala los hooks en su hilo. RuntimeError (en español) si no es posible."""
        with self._lock:
            if self.running:
                return
            worker = _Worker()
            worker.thread = threading.Thread(
                target=self._run, args=(worker,), name="MacroTool-InputHook", daemon=True
            )
            self._worker = worker
            worker.thread.start()
        if not worker.started.wait(timeout):
            self.stop()
            raise RuntimeError("No se pudieron instalar los hooks de teclado y ratón (tiempo de espera agotado).")
        if worker.error is not None:
            worker.thread.join(1.0)
            with self._lock:
                if self._worker is worker:
                    self._worker = None
            raise RuntimeError(
                f"No se pudieron instalar los hooks de teclado y ratón: {worker.error}"
            ) from worker.error

    def stop(self, timeout: float = 2.0) -> None:
        """Desinstala los hooks y termina el hilo. Idempotente."""
        with self._lock:
            worker = self._worker
            self._worker = None
        if worker is None or worker.thread is None:
            return
        thread = worker.thread
        deadline = time.monotonic() + timeout
        while thread.is_alive():
            if worker.thread_id and _PostThreadMessageW(worker.thread_id, WM_QUIT, 0, 0):
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(0.005)  # la cola del hilo aún no existe: se reintenta
        if thread is not threading.current_thread():
            thread.join(max(0.0, deadline - time.monotonic()))

    def _run(self, worker: _Worker) -> None:
        msg = MSG()
        # Fuerza la creación de la cola de mensajes antes de publicar el id del hilo.
        _PeekMessageW(ctypes.byref(msg), None, WM_USER, WM_USER, PM_NOREMOVE)
        worker.thread_id = _GetCurrentThreadId()
        handles: list[int] = []
        try:
            hmod = _GetModuleHandleW(None)
            for hook_id, proc in ((WH_KEYBOARD_LL, self._kb_proc), (WH_MOUSE_LL, self._ms_proc)):
                handle = _SetWindowsHookExW(hook_id, proc, hmod, 0)
                if not handle:
                    raise ctypes.WinError(ctypes.get_last_error())
                handles.append(handle)
        except BaseException as exc:
            worker.error = exc
            _unhook_all(handles)
            worker.started.set()
            return
        worker.installed = True
        worker.started.set()
        try:
            while True:
                result = _GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result == 0:  # WM_QUIT
                    break
                if result == -1:
                    log.error("GetMessageW falló en el hilo del hook (error %d)", ctypes.get_last_error())
                    break
        finally:
            worker.installed = False
            _unhook_all(handles)

    # -- listeners --
    def add_listener(self, fn: Listener, priority: int = 0, *, wants_moves: bool = True) -> None:
        """Añade ``fn`` (mayor prioridad primero). Devolver True desde ``fn`` bloquea el evento.

        ``wants_moves=False`` evita recibir movimientos del ratón (y ahorra trabajo en el hook).
        """
        with self._lock:
            self._seq += 1
            entries = [e for e in self._listeners if e.fn != fn]
            entries.append(_ListenerEntry(fn, int(priority), self._seq, bool(wants_moves)))
            entries.sort(key=lambda e: (-e.priority, e.seq))
            self._set_listeners(entries)

    def remove_listener(self, fn: Listener) -> None:
        with self._lock:
            self._set_listeners([e for e in self._listeners if e.fn != fn])

    def _set_listeners(self, entries: list[_ListenerEntry]) -> None:
        self._listeners = tuple(entries)
        self._want_moves = any(e.wants_moves for e in entries)

    def dispatch(self, event: RawEvent) -> bool:
        """Entrega ``event`` a los listeners en orden. Devuelve True si hay que bloquearlo."""
        blocked = False
        is_move = event.kind == "move"
        for entry in self._listeners:
            if is_move and not entry.wants_moves:
                continue
            try:
                if entry.fn(event):
                    blocked = True
                    event.consumed = True
            except Exception:
                log.exception("Error en un listener del hook de entrada")
        return blocked

    def keyboard_event(self, msg: int, vk: int, flags: int, extra_info: int, t: float,
                       scan_code: int = 0) -> Optional[RawEvent]:
        """``parse_keyboard`` con el contexto del hook: marca como AltGr el "ralt" que llega
        justo después del Ctrl ficticio que genera Windows en las distribuciones con AltGr."""
        if scan_code == SC_ALTGR_FAKE_LCTRL and _KEY_KINDS.get(msg) == "key_down":
            self._fake_lctrl_t = t
        altgr = t - self._fake_lctrl_t <= ALTGR_PAIR_S
        return parse_keyboard(msg, vk, flags, extra_info, t, scan_code, altgr=altgr)

    # -- callbacks del hook (deben ser rapidísimos) --
    def _keyboard_proc(self, n_code: int, w_param: int, l_param: int) -> int:
        if n_code == HC_ACTION and self._listeners:
            try:
                kb = KBDLLHOOKSTRUCT.from_address(l_param)
                event = self.keyboard_event(
                    w_param, kb.vkCode, kb.flags, kb.dwExtraInfo, time.perf_counter(), kb.scanCode
                )
                if event is not None and self.dispatch(event):
                    return 1
            except Exception:
                log.exception("Error en el hook de teclado")
        return _CallNextHookEx(None, n_code, w_param, l_param)  # hhk se ignora desde Windows XP

    def _mouse_proc(self, n_code: int, w_param: int, l_param: int) -> int:
        if n_code == HC_ACTION and self._listeners and (w_param != WM_MOUSEMOVE or self._want_moves):
            try:
                ms = MSLLHOOKSTRUCT.from_address(l_param)
                event = parse_mouse(
                    w_param, ms.pt.x, ms.pt.y, ms.mouseData, ms.flags, ms.dwExtraInfo, time.perf_counter()
                )
                if event is not None and self.dispatch(event):
                    return 1
            except Exception:
                log.exception("Error en el hook de ratón")
        return _CallNextHookEx(None, n_code, w_param, l_param)


def _unhook_all(handles: list[int]) -> None:
    while handles:
        handle = handles.pop()
        if not _UnhookWindowsHookEx(handle):
            log.warning("UnhookWindowsHookEx falló (error %d)", ctypes.get_last_error())


@atexit.register
def _stop_all_hooks() -> None:
    for hook in list(_live_hooks):
        try:
            hook.stop(timeout=0.5)
        except Exception:
            pass


# --- Disparadores -----------------------------------------------------------------------
@dataclass
class Binding:
    id: str  # "control:stop", "control:record", "macro:<macro_id>"...
    combo: str  # "1", "ctrl+alt+f", "mouse_x1"...
    block: bool = True  # bloquear la entrada disparadora (down, up y autorepeticiones)
    control: bool = False  # funciona aunque los disparadores de macros estén desactivados


# Modificadores: familia genérica y lado físico. keys.vk_to_token devuelve el genérico
# para la tecla izquierda ("ctrl") y el específico para la derecha ("rctrl").
_GENERIC = {
    "ctrl": "ctrl", "lctrl": "ctrl", "rctrl": "ctrl",
    "shift": "shift", "lshift": "shift", "rshift": "shift",
    "alt": "alt", "lalt": "alt", "ralt": "alt",
    "win": "win", "lwin": "win", "rwin": "win",
}
_PHYSICAL = {"ctrl": "lctrl", "shift": "lshift", "alt": "lalt", "win": "lwin"}
_FAMILY = {
    "ctrl": frozenset({"lctrl", "rctrl"}),
    "shift": frozenset({"lshift", "rshift"}),
    "alt": frozenset({"lalt", "ralt"}),
    "win": frozenset({"lwin", "rwin"}),
}
_VK_OF_PHYSICAL = {
    "lctrl": 0xA2, "rctrl": 0xA3, "lshift": 0xA0, "rshift": 0xA1,
    "lalt": 0xA4, "ralt": 0xA5, "lwin": 0x5B, "rwin": 0x5C,
}
_MENU_FAMILIES = frozenset({"alt", "win"})  # soltarlas "solas" abre menús: hay que enmascararlas

# Una autorepetición llega como mucho cada ~1 s; un down repetido más tarde indica
# que se perdió el up (p. ej. Win+L o Ctrl+Alt+Supr cambian de escritorio).
STALE_REPEAT_S = 1.5


def _physical(token: str) -> str:
    return _PHYSICAL.get(token, token)


@dataclass(frozen=True)
class _CompiledBinding:
    id: str
    main: str  # entrada principal (token canónico, puede ser un modificador genérico)
    mods: frozenset[str]  # modificadores requeridos (genéricos o específicos)
    keys: frozenset[str]  # otras entradas no modificadoras que deben estar pulsadas
    block: bool
    control: bool
    score: tuple[int, int]  # especificidad: se prueba primero el binding más concreto


class TriggerManager:
    """Relaciona eventos del hook con bindings y decide qué bloquear.

    ``handle_event`` es lógica pura (salvo las consultas inyectables ``key_state`` y
    ``foreground_check``) y se ejecuta en el hilo del hook: debe ser rápida.
    """

    PRIORITY = 100

    def __init__(
        self,
        hook: InputHook,
        on_trigger: Callable[[str, bool], None],
        *,
        key_state: Callable[[int], bool] | None = None,
        foreground_check: Callable[[], bool] | None = None,
        mask_menu_key: Callable[[], None] | None = None,
    ) -> None:
        self._hook = hook
        self._on_trigger = on_trigger
        self._key_state = key_state
        self._foreground_check = foreground_check
        self._mask_menu_key = mask_menu_key
        self._index: dict[str, tuple[_CompiledBinding, ...]] = {}
        self._suspended = False
        self._suspend_exceptions: frozenset[str] = frozenset()
        self._macro_triggers_enabled = True
        self._ignore_own_foreground = False
        # Estado físico (sólo eventos no inyectados), por token físico ("lctrl", "a"...).
        self._pressed: set[str] = set()
        self._last_down: dict[str, float] = {}
        self._active: dict[str, tuple[str, bool]] = {}  # entrada principal → (binding_id, bloqueado)
        self._by_scan: dict[int, str] = {}  # tecla física (scancode) → token con el que se pulsó
        self._altgr = False  # "ralt" pulsada como AltGr: no cuenta como Alt
        try:
            hook.add_listener(self.handle_event, self.PRIORITY, wants_moves=False)
        except TypeError:  # hooks de prueba sin el parámetro wants_moves
            hook.add_listener(self.handle_event, self.PRIORITY)

    # -- configuración (hilo principal) --
    def set_bindings(self, bindings: list[Binding]) -> dict[str, str]:
        """Reemplaza los bindings de forma atómica. Devuelve {binding_id: error en español}."""
        keys = _keys()
        errors: dict[str, str] = {}
        owners: dict[tuple, str] = {}
        compiled: list[_CompiledBinding] = []
        # Los atajos de control tienen preferencia ante un conflicto.
        ordered = sorted(enumerate(bindings), key=lambda item: (not item[1].control, item[0]))
        for _, binding in ordered:
            combo = (binding.combo or "").strip()
            if not combo:
                continue
            try:
                tokens = keys.parse_combo(combo)
            except ValueError as exc:
                errors[binding.id] = f"Disparador no válido «{combo}»: {exc}"
                continue
            if not tokens:
                continue
            item = self._compile(binding, tokens)
            shown = keys.format_combo(keys.sort_combo(tokens))
            if item.main in ("mouse_left", "mouse_right") and not item.mods:
                errors[binding.id] = (
                    f"«{shown}» no puede usarse como disparador: el clic izquierdo o derecho debe "
                    "combinarse con Ctrl, Alt, Mayús o Win."
                )
                continue
            signature = (item.main, item.mods, item.keys)
            owner = owners.get(signature)
            if owner is not None:
                who = "un atajo global de MacroTool" if owner.startswith("control:") else "otra macro"
                errors[binding.id] = f"El disparador «{shown}» ya lo usa {who}."
                continue
            owners[signature] = binding.id
            compiled.append(item)
        index: dict[str, list[_CompiledBinding]] = {}
        for item in compiled:
            index.setdefault(item.main, []).append(item)
        self._index = {
            main: tuple(sorted(items, key=lambda b: b.score, reverse=True)) for main, items in index.items()
        }  # asignación atómica: el hilo del hook ve el diccionario viejo o el nuevo
        return errors

    @staticmethod
    def _compile(binding: Binding, tokens: list[str]) -> _CompiledBinding:
        """Separa la entrada principal (último token no modificador, o el último si todos
        lo son) de los modificadores y demás entradas requeridas."""
        main_index = len(tokens) - 1
        for i in range(len(tokens) - 1, -1, -1):
            if tokens[i] not in _GENERIC:
                main_index = i
                break
        main = tokens[main_index]
        rest = [t for i, t in enumerate(tokens) if i != main_index]
        mods = frozenset(t for t in rest if t in _GENERIC)
        others = frozenset(t for t in rest if t not in _GENERIC)
        specific = sum(1 for t in mods if t not in _FAMILY) + int(main in _GENERIC and main not in _FAMILY)
        return _CompiledBinding(
            binding.id, main, mods, others, bool(binding.block), bool(binding.control),
            (len(mods) + len(others), specific),
        )

    def set_suspended(self, suspended: bool) -> None:
        """Deja pasar todo y no dispara nada (p. ej. mientras la UI captura una combinación)."""
        self._suspended = bool(suspended)

    def set_suspend_exceptions(self, binding_ids: Iterable[str]) -> None:
        """Bindings que siguen funcionando durante la suspensión (p. ej. "control:stop"
        mientras hay una macro en marcha: el atajo de parada nunca debe quedar anulado)."""
        self._suspend_exceptions = frozenset(binding_ids or ())

    def set_macro_triggers_enabled(self, enabled: bool) -> None:
        self._macro_triggers_enabled = bool(enabled)

    def set_ignore_when_own_foreground(self, value: bool) -> None:
        """Si MacroTool está en primer plano no se disparan macros (los atajos de control sí)."""
        self._ignore_own_foreground = bool(value)

    @property
    def suspended(self) -> bool:
        return self._suspended

    @property
    def macro_triggers_enabled(self) -> bool:
        return self._macro_triggers_enabled

    def close(self) -> None:
        """Deja de escuchar el hook."""
        self._hook.remove_listener(self.handle_event)

    # -- lógica de coincidencia (hilo del hook) --
    def handle_event(self, event: RawEvent) -> bool:
        """Procesa un evento; devuelve True si hay que bloquearlo."""
        kind = event.kind
        if kind == "key_down" or kind == "mouse_down":
            return self._on_down(event)
        if kind == "key_up" or kind == "mouse_up":
            return self._on_up(event)
        return False

    def _event_token(self, event: RawEvent) -> str:
        """Token físico del evento. Windows traduce el VK de algunas teclas según el estado del
        momento (Num 1 llega como Fin si cambia Mayús o Bloq Num): el up y las autorepeticiones
        se identifican por el scancode con el token con el que se pulsó la tecla."""
        token = _physical(event.token)
        scan = event.scan
        if not scan or not event.kind.startswith("key_"):
            return token
        if event.kind == "key_up":
            return self._by_scan.pop(scan, token)
        previous = self._by_scan.get(scan)
        if (previous is not None and previous != token and previous in self._pressed
                and event.t - self._last_down.get(previous, float("-inf")) <= STALE_REPEAT_S):
            return previous  # autorepetición de la misma tecla física con otro VK
        self._by_scan[scan] = token
        return token

    def _on_down(self, event: RawEvent) -> bool:
        if event.injected or not event.token:
            return False
        token = self._event_token(event)
        if token == "ralt" and (event.altgr or token not in self._pressed):
            self._altgr = bool(event.altgr)  # una autorepetición sin el Ctrl ficticio no lo cambia
        last = self._last_down.get(token)
        self._last_down[token] = event.t
        active = self._active.get(token)
        if token in self._pressed:
            stale = event.kind == "mouse_down" or last is None or event.t - last > STALE_REPEAT_S
            if not stale:  # autorepetición: no vuelve a disparar, pero se bloquea si se bloqueó el down
                return active[1] if active is not None else False
            if active is not None:  # se perdió el up anterior: se cierra esa activación
                del self._active[token]
                self._fire(active[0], False)
        self._pressed.add(token)
        if event.consumed:
            return False
        if self._suspended:
            if not self._suspend_exceptions:
                return False
            match = self._match(token, only=self._suspend_exceptions)
        else:
            match = self._match(token)
        if match is None:
            return False
        binding, held = match
        self._active[token] = (binding.id, binding.block)
        if binding.block and any(_GENERIC[m] in _MENU_FAMILIES for m in held):
            # Alt/Win pulsadas y la tecla bloqueada: al soltarlas se abriría un menú.
            self._mask_menu()
        self._fire(binding.id, True)
        return binding.block

    def _on_up(self, event: RawEvent) -> bool:
        if event.injected or not event.token:
            return False
        token = self._event_token(event)
        if token == "ralt":
            self._altgr = False
        self._pressed.discard(token)
        self._last_down.pop(token, None)
        active = self._active.pop(token, None)
        if active is None:
            return False
        self._fire(active[0], False)
        return active[1]  # el up se bloquea si (y sólo si) se bloqueó el down

    def _generic(self, token: str) -> str:
        """Familia de un modificador físico. AltGr (distribuciones con AltGr) no es Alt:
        un disparador «Alt + 2» no debe saltar (ni tragarse la tecla) al escribir «@»."""
        if token == "ralt" and self._altgr:
            return "ralt"
        return _GENERIC.get(token, token)

    def _match(self, token: str, only: frozenset[str] | None = None) -> tuple[_CompiledBinding, frozenset[str]] | None:
        index = self._index
        candidates = index.get(token, ())
        generic = self._generic(token)
        if generic != token:
            extra = index.get(generic, ())
            if extra:
                candidates = tuple(sorted(candidates + extra, key=lambda b: b.score, reverse=True))
        if only is not None:
            candidates = tuple(b for b in candidates if b.id in only)
        if not candidates:
            return None
        held = self._held_modifiers(exclude=token)
        own_foreground: bool | None = None
        for binding in candidates:
            if not binding.control:
                if not self._macro_triggers_enabled:
                    continue
                if self._ignore_own_foreground:
                    if own_foreground is None:
                        own_foreground = self._is_own_foreground()
                    if own_foreground:
                        continue
            if self._requirements_met(binding, held):
                return binding, held
        return None

    def _requirements_met(self, binding: _CompiledBinding, held: frozenset[str]) -> bool:
        for mod in binding.mods:
            family = _FAMILY.get(mod)
            if family is not None:
                if not any(m in held and self._generic(m) == mod for m in family):
                    return False
            elif mod not in held:
                return False
        for key in binding.keys:
            if not self._key_still_down(key):
                return False
        if binding.mods:  # con modificadores: no puede haber modificadores de más
            for mod in held:
                if mod not in binding.mods and self._generic(mod) not in binding.mods:
                    return False
        return True  # sin modificadores: comodín (vale cualquier estado de modificadores)

    def _key_still_down(self, key: str) -> bool:
        """¿Sigue pulsada una entrada no modificadora requerida por un combo?

        Se contrasta con Windows: si se perdió su up (escritorio seguro, ventana elevada…),
        el hook la seguiría dando por pulsada indefinidamente."""
        if key not in self._pressed:
            return False
        if key in self._active or key.startswith("mouse_"):
            return True  # bloqueada (Windows no la ve) o botón: se confía en el hook
        try:
            vk = _keys().token_to_vk(key)
        except (ValueError, KeyError):
            return True
        if self._async_down(vk):
            return True
        self._pressed.discard(key)
        self._last_down.pop(key, None)
        return False

    def _held_modifiers(self, exclude: str) -> frozenset[str]:
        """Modificadores pulsados según el hook, descartando los que Windows ya da por soltados."""
        held = set()
        for token in self._pressed:
            if token == exclude or token not in _VK_OF_PHYSICAL:
                continue
            # Si bloqueamos su down, GetAsyncKeyState no lo refleja: se confía en el hook.
            if token in self._active or self._async_down(_VK_OF_PHYSICAL[token]):
                held.add(token)
        return frozenset(held)

    def _async_down(self, vk: int) -> bool:
        try:
            if self._key_state is None:
                from .winput import is_key_down

                self._key_state = is_key_down
            return bool(self._key_state(vk))
        except Exception:
            return True  # ante la duda, se confía en el estado rastreado

    def _is_own_foreground(self) -> bool:
        try:
            if self._foreground_check is None:
                from .winput import foreground_is_own_process

                self._foreground_check = foreground_is_own_process
            return bool(self._foreground_check())
        except Exception:
            return False

    def _mask_menu(self) -> None:
        try:
            if self._mask_menu_key is None:
                from .winput import send_mask_key

                self._mask_menu_key = send_mask_key
            self._mask_menu_key()
        except Exception:
            log.debug("No se pudo enviar la tecla de máscara", exc_info=True)

    def _fire(self, binding_id: str, pressed: bool) -> None:
        try:
            self._on_trigger(binding_id, pressed)
        except Exception:
            log.exception("Error en on_trigger(%r, %r)", binding_id, pressed)
