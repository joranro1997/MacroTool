"""Inspector de origen de input.

Muestra EN VIVO cómo una aplicación ve cada evento de teclado/ratón a través de los
cuatro canales que Windows expone para distinguir input "real" de input inyectado.
Sirve como banco de pruebas: pulsa una tecla física y luego dispara la misma tecla
por SendInput, y compara.

Canales que se leen:
  1. Hook de bajo nivel (WH_KEYBOARD_LL / WH_MOUSE_LL) -> flag LLKHF_INJECTED /
     LLMHF_INJECTED (y la variante LOWER_IL). Es GLOBAL: ve todo el sistema.
  2. Raw Input (WM_INPUT) -> RAWINPUTHEADER.hDevice. SendInput llega con hDevice = 0;
     el hardware real llega con el handle de un dispositivo concreto. Es GLOBAL
     (registrado con RIDEV_INPUTSINK, llega aunque la ventana no tenga el foco).
  3. GetCurrentInputMessageSource -> originId (IMO_HARDWARE / IMO_INJECTED /
     IMO_SYSTEM). SOLO para los mensajes que procesa ESTA ventana: para verlo,
     enfoca esta ventana y pulsa/mueve encima.
  4. dwExtraInfo (leído en el hook) -> etiqueta cooperativa. MacroTool marca su input
     con MACROTOOL_EXTRA_INFO (0x4D43524F); aquí se resalta cuando aparece.

Resumen esperado:
  - Tecla/ratón FÍSICO  -> canal 1: NO inyectado · canal 2: hDevice real · canal 3: IMO_HARDWARE
  - Botón "SendInput"   -> canal 1: INYECTADO   · canal 2: hDevice 0     · canal 3: IMO_INJECTED
                           canal 4: 0x4D43524F ("MCRO")

Uso (desde la raíz del proyecto):
    python tools/input_inspector.py
"""
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

# --- Tipos con el tamaño correcto en x64 --------------------------------------
ULONG_PTR = ctypes.c_size_t
LRESULT = ctypes.c_ssize_t
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t
HHOOK = ctypes.c_void_p
HANDLE = ctypes.c_void_p

# La misma marca "MCRO" que usa macrotool/winput.py en dwExtraInfo.
MACROTOOL_EXTRA_INFO = 0x4D43524F

# --- Constantes ----------------------------------------------------------------
HC_ACTION = 0
WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14

LLKHF_INJECTED = 0x10
LLKHF_LOWER_IL_INJECTED = 0x02
LLMHF_INJECTED = 0x01
LLMHF_LOWER_IL_INJECTED = 0x02

WM_INPUT = 0x00FF
_KEY_MESSAGES = frozenset({0x0100, 0x0101, 0x0104, 0x0105})  # KEYDOWN/UP, SYSKEYDOWN/UP
_MOUSE_MESSAGES = frozenset(range(0x0200, 0x020F))  # WM_MOUSEMOVE .. WM_XBUTTONUP y rueda
_SOURCE_MESSAGES = _KEY_MESSAGES | _MOUSE_MESSAGES

# GetCurrentInputMessageSource -> INPUT_MESSAGE_SOURCE.originId
IMO_UNAVAILABLE = 0x00000000
IMO_HARDWARE = 0x00000001
IMO_INJECTED = 0x00000002
IMO_SYSTEM = 0x00000004
_ORIGIN_NAMES = {
    IMO_UNAVAILABLE: "IMO_UNAVAILABLE",
    IMO_HARDWARE: "IMO_HARDWARE",
    IMO_INJECTED: "IMO_INJECTED",
    IMO_SYSTEM: "IMO_SYSTEM",
}

# Raw Input
RID_INPUT = 0x10000003
RIDI_DEVICENAME = 0x20000007
RIDEV_INPUTSINK = 0x00000100
RIM_TYPEMOUSE = 0
RIM_TYPEKEYBOARD = 1
RIM_TYPEHID = 2
_RIM_NAMES = {RIM_TYPEMOUSE: "ratón", RIM_TYPEKEYBOARD: "teclado", RIM_TYPEHID: "HID"}

# SendInput
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
MOUSEEVENTF_MOVE = 0x0001
VK_SHIFT = 0x10  # tecla inofensiva para la prueba (pulsar/soltar Shift no escribe nada)


# --- Estructuras Win32 ---------------------------------------------------------
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


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [
        ("usUsagePage", wintypes.USHORT),
        ("usUsage", wintypes.USHORT),
        ("dwFlags", wintypes.DWORD),
        ("hwndTarget", wintypes.HWND),
    ]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [
        ("dwType", wintypes.DWORD),
        ("dwSize", wintypes.DWORD),
        ("hDevice", HANDLE),
        ("wParam", WPARAM),
    ]


class INPUT_MESSAGE_SOURCE(ctypes.Structure):
    _fields_ = [("deviceType", wintypes.DWORD), ("originId", wintypes.DWORD)]


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


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


HOOKPROC = ctypes.CFUNCTYPE(LRESULT, ctypes.c_int, WPARAM, LPARAM)

# --- Funciones Win32 -----------------------------------------------------------
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


def _declare(dll, name, restype, *argtypes):
    fn = getattr(dll, name)
    fn.restype = restype
    fn.argtypes = list(argtypes)
    return fn


_SetWindowsHookExW = _declare(user32, "SetWindowsHookExW", HHOOK,
                              ctypes.c_int, HOOKPROC, HANDLE, wintypes.DWORD)
_UnhookWindowsHookEx = _declare(user32, "UnhookWindowsHookEx", wintypes.BOOL, HHOOK)
_CallNextHookEx = _declare(user32, "CallNextHookEx", LRESULT,
                           HHOOK, ctypes.c_int, WPARAM, LPARAM)
_RegisterRawInputDevices = _declare(user32, "RegisterRawInputDevices", wintypes.BOOL,
                                    ctypes.POINTER(RAWINPUTDEVICE), wintypes.UINT, wintypes.UINT)
_GetRawInputData = _declare(user32, "GetRawInputData", wintypes.UINT,
                            HANDLE, wintypes.UINT, ctypes.c_void_p,
                            ctypes.POINTER(wintypes.UINT), wintypes.UINT)
_GetRawInputDeviceInfoW = _declare(user32, "GetRawInputDeviceInfoW", wintypes.UINT,
                                   HANDLE, wintypes.UINT, ctypes.c_void_p,
                                   ctypes.POINTER(wintypes.UINT))
_GetCurrentInputMessageSource = _declare(user32, "GetCurrentInputMessageSource",
                                         wintypes.BOOL, ctypes.POINTER(INPUT_MESSAGE_SOURCE))
_SendInput = _declare(user32, "SendInput", wintypes.UINT,
                      wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
_GetModuleHandleW = _declare(kernel32, "GetModuleHandleW", HANDLE, wintypes.LPCWSTR)


# --- Estado compartido ---------------------------------------------------------
class Channels:
    """Última lectura de cada canal (la escriben el hook y el filtro de mensajes)."""

    def __init__(self) -> None:
        self.hook = "—"          # canal 1
        self.raw = "—"           # canal 2
        self.source = "—"        # canal 3
        self.extra = "—"         # canal 4
        self.log: list[str] = []

    def add_log(self, line: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log.append(f"[{stamp}] {line}")
        del self.log[:-200]  # conserva las últimas 200 líneas


def _device_name(hdevice: int) -> str:
    """Nombre del dispositivo de Raw Input (recortado). '' si no se puede."""
    if not hdevice:
        return ""
    size = wintypes.UINT(0)
    if _GetRawInputDeviceInfoW(hdevice, RIDI_DEVICENAME, None, ctypes.byref(size)) != 0:
        return ""
    if size.value == 0 or size.value > 4096:
        return ""
    buf = ctypes.create_unicode_buffer(size.value)
    if _GetRawInputDeviceInfoW(hdevice, RIDI_DEVICENAME, buf, ctypes.byref(size)) == 0xFFFFFFFF:
        return ""
    name = buf.value or ""
    return name.rsplit("#", 1)[0].strip("\\?").strip() or name


def _fmt_extra(value: int) -> str:
    if value == MACROTOOL_EXTRA_INFO:
        return f"0x{value:08X} (MCRO ← MacroTool)"
    return "0" if value == 0 else f"0x{value:X}"


def main() -> int:
    if sys.platform != "win32":
        print("Este inspector solo funciona en Windows.")
        return 2

    from PySide6 import QtCore, QtWidgets

    ch = Channels()

    # --- Hooks de bajo nivel (canal 1 + canal 4) ------------------------------
    def _on_keyboard(n_code, w_param, l_param):
        if n_code == HC_ACTION:
            kb = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT))[0]
            injected = bool(kb.flags & LLKHF_INJECTED)
            lower = bool(kb.flags & LLKHF_LOWER_IL_INJECTED)
            ch.hook = ("INYECTADO" if injected else "hardware (no inyectado)") + \
                      (" [IL baja]" if lower else "")
            ch.extra = _fmt_extra(kb.dwExtraInfo)
            ch.add_log(f"teclado vk=0x{kb.vkCode:02X} · canal1={ch.hook} · dwExtraInfo={ch.extra}")
        return _CallNextHookEx(None, n_code, w_param, l_param)

    def _on_mouse(n_code, w_param, l_param):
        if n_code == HC_ACTION and w_param != 0x0200:  # ignora WM_MOUSEMOVE (ruido)
            ms = ctypes.cast(l_param, ctypes.POINTER(MSLLHOOKSTRUCT))[0]
            injected = bool(ms.flags & LLMHF_INJECTED)
            lower = bool(ms.flags & LLMHF_LOWER_IL_INJECTED)
            ch.hook = ("INYECTADO" if injected else "hardware (no inyectado)") + \
                      (" [IL baja]" if lower else "")
            ch.extra = _fmt_extra(ms.dwExtraInfo)
            ch.add_log(f"ratón msg=0x{w_param:04X} · canal1={ch.hook} · dwExtraInfo={ch.extra}")
        return _CallNextHookEx(None, n_code, w_param, l_param)

    kb_proc = HOOKPROC(_on_keyboard)  # se guardan para que no los recoja el GC
    ms_proc = HOOKPROC(_on_mouse)
    hmod = _GetModuleHandleW(None)
    kb_hook = _SetWindowsHookExW(WH_KEYBOARD_LL, kb_proc, hmod, 0)
    ms_hook = _SetWindowsHookExW(WH_MOUSE_LL, ms_proc, hmod, 0)
    if not kb_hook or not ms_hook:
        err = ctypes.get_last_error()
        print(f"No se pudieron instalar los hooks de bajo nivel (error {err}).")

    # --- Filtro de eventos nativos (canal 2 + canal 3) ------------------------
    def _on_raw_input(lparam: int) -> None:
        size = wintypes.UINT(0)
        if _GetRawInputData(lparam, RID_INPUT, None, ctypes.byref(size),
                            ctypes.sizeof(RAWINPUTHEADER)) != 0 or size.value == 0:
            return
        buf = ctypes.create_string_buffer(size.value)
        if _GetRawInputData(lparam, RID_INPUT, buf, ctypes.byref(size),
                            ctypes.sizeof(RAWINPUTHEADER)) == 0xFFFFFFFF:
            return
        header = RAWINPUTHEADER.from_buffer_copy(buf, 0)
        hdev = int(header.hDevice or 0)
        kind = _RIM_NAMES.get(header.dwType, f"tipo {header.dwType}")
        if hdev == 0:
            ch.raw = f"{kind}: hDevice = 0  → inyectado (SendInput)"
        else:
            name = _device_name(hdev)
            ch.raw = f"{kind}: hDevice = 0x{hdev:X}" + (f"  ({name})" if name else "  → hardware real")

    class Filter(QtCore.QAbstractNativeEventFilter):
        def nativeEventFilter(self, event_type, message):
            if event_type == b"windows_generic_MSG":
                msg = wintypes.MSG.from_address(int(message))
                if msg.message == WM_INPUT:
                    _on_raw_input(msg.lParam)
                elif msg.message in _SOURCE_MESSAGES:
                    src = INPUT_MESSAGE_SOURCE()
                    if _GetCurrentInputMessageSource(ctypes.byref(src)):
                        ch.source = _ORIGIN_NAMES.get(src.originId, f"0x{src.originId:X}")
            return False, 0

    app = QtWidgets.QApplication(sys.argv)
    win = QtWidgets.QWidget()
    win.setWindowTitle("Inspector de origen de input — 4 canales")
    win.resize(720, 480)

    grid = QtWidgets.QGridLayout()
    labels: dict[str, QtWidgets.QLabel] = {}
    rows = [
        ("hook", "1 · Hook bajo nivel (global)", "flag INYECTADO / hardware"),
        ("raw", "2 · Raw Input hDevice (global)", "0 = inyectado · handle = hardware"),
        ("source", "3 · GetCurrentInputMessageSource", "solo con ESTA ventana enfocada"),
        ("extra", "4 · dwExtraInfo (hook)", "etiqueta cooperativa"),
    ]
    for r, (key, title, hint) in enumerate(rows):
        name = QtWidgets.QLabel(f"<b>{title}</b><br><span style='color:gray'>{hint}</span>")
        value = QtWidgets.QLabel("—")
        value.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        value.setStyleSheet("font-family: Consolas, monospace; font-size: 13px;")
        value.setWordWrap(True)
        grid.addWidget(name, r, 0)
        grid.addWidget(value, r, 1)
        labels[key] = value
    grid.setColumnStretch(1, 1)

    log_box = QtWidgets.QPlainTextEdit()
    log_box.setReadOnly(True)
    log_box.setStyleSheet("font-family: Consolas, monospace; font-size: 12px;")

    btn_key = QtWidgets.QPushButton("Disparar TECLA por SendInput  (Shift)")
    btn_mouse = QtWidgets.QPushButton("Disparar RATÓN por SendInput  (mover 1px)")

    def _send_key() -> None:
        arr = (INPUT * 2)()
        for i, up in enumerate((0, KEYEVENTF_KEYUP)):
            arr[i].type = INPUT_KEYBOARD
            arr[i].ki.wVk = VK_SHIFT
            arr[i].ki.dwFlags = up
            arr[i].ki.dwExtraInfo = MACROTOOL_EXTRA_INFO
        _SendInput(2, arr, ctypes.sizeof(INPUT))
        ch.add_log(">> SendInput: TECLA Shift (marcada con MCRO)")

    def _send_mouse() -> None:
        arr = (INPUT * 2)()
        for i, dx in enumerate((1, -1)):
            arr[i].type = INPUT_MOUSE
            arr[i].mi.dx = dx
            arr[i].mi.dwFlags = MOUSEEVENTF_MOVE
            arr[i].mi.dwExtraInfo = MACROTOOL_EXTRA_INFO
        _SendInput(2, arr, ctypes.sizeof(INPUT))
        ch.add_log(">> SendInput: RATÓN mover 1px (marcado con MCRO)")

    btn_key.clicked.connect(_send_key)
    btn_mouse.clicked.connect(_send_mouse)

    buttons = QtWidgets.QHBoxLayout()
    buttons.addWidget(btn_key)
    buttons.addWidget(btn_mouse)

    intro = QtWidgets.QLabel(
        "Pulsa una tecla o mueve/clica el ratón FÍSICO y observa los canales. "
        "Luego pulsa un botón de SendInput y compara: el hardware sale como "
        "<b>no inyectado / IMO_HARDWARE / hDevice real</b>; SendInput sale como "
        "<b>INYECTADO / IMO_INJECTED / hDevice 0</b>.  (El canal 3 solo cambia con esta "
        "ventana enfocada.)")
    intro.setWordWrap(True)

    layout = QtWidgets.QVBoxLayout(win)
    layout.addWidget(intro)
    layout.addLayout(grid)
    layout.addWidget(QtWidgets.QLabel("<b>Registro</b>"))
    layout.addWidget(log_box, 1)
    layout.addLayout(buttons)

    # Registrar Raw Input para teclado (01:06) y ratón (01:02) con INPUTSINK (global).
    win.show()
    hwnd = int(win.winId())
    devices = (RAWINPUTDEVICE * 2)(
        RAWINPUTDEVICE(0x01, 0x06, RIDEV_INPUTSINK, hwnd),
        RAWINPUTDEVICE(0x01, 0x02, RIDEV_INPUTSINK, hwnd),
    )
    if not _RegisterRawInputDevices(devices, 2, ctypes.sizeof(RAWINPUTDEVICE)):
        ch.add_log(f"AVISO: RegisterRawInputDevices falló (error {ctypes.get_last_error()})")

    native_filter = Filter()
    app.installNativeEventFilter(native_filter)

    def _refresh() -> None:
        for key, lbl in labels.items():
            lbl.setText(getattr(ch, key))
        if ch.log:
            text = "\n".join(ch.log)
            if text != log_box.toPlainText():
                log_box.setPlainText(text)
                log_box.verticalScrollBar().setValue(log_box.verticalScrollBar().maximum())

    timer = QtCore.QTimer()
    timer.timeout.connect(_refresh)
    timer.start(50)

    def _cleanup() -> None:
        if kb_hook:
            _UnhookWindowsHookEx(kb_hook)
        if ms_hook:
            _UnhookWindowsHookEx(ms_hook)

    app.aboutToQuit.connect(_cleanup)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
