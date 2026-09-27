"""Elevación (ejecutar como administrador) en Windows.

Los hooks globales de teclado/ratón y ``SendInput`` NO llegan a ventanas de mayor
integridad (apps que corren como administrador) cuando MacroTool va como usuario
normal (protección UIPI de Windows). Este módulo permite:

- ``is_elevated()``: saber si MacroTool ya va como administrador.
- ``foreground_needs_admin()``: detectar que la ventana en primer plano es de mayor
  integridad (y por tanto los disparadores no funcionarán ahí).
- ``relaunch_as_admin()``: reiniciar MacroTool con permisos de administrador.

Todo está fuertemente protegido: ante cualquier error se responde de forma conservadora
(sin avisos falsos y sin romper la aplicación).
"""
from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
from ctypes import wintypes

log = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"

# --- Constantes Win32 ---------------------------------------------------------
_TOKEN_QUERY = 0x0008
_TokenElevation = 20
_TokenIntegrityLevel = 25
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SECURITY_MANDATORY_MEDIUM_RID = 0x2000  # integridad "Media" = usuario normal
_SW_SHOWNORMAL = 1


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class _TOKEN_MANDATORY_LABEL(ctypes.Structure):
    _fields_ = [("Label", _SID_AND_ATTRIBUTES)]


if _IS_WINDOWS:
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)

    _advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    _advapi32.OpenProcessToken.restype = wintypes.BOOL
    _advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    _advapi32.GetTokenInformation.restype = wintypes.BOOL
    # restype PUNTERO obligatorio: si no, ctypes trunca el puntero a 32 bits en x64.
    _advapi32.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
    _advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
    _advapi32.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    _advapi32.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)

    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL

    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD

    _shell32.ShellExecuteW.argtypes = [
        wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int]
    _shell32.ShellExecuteW.restype = ctypes.c_void_p


def _token_is_elevated(handle) -> bool:
    token = wintypes.HANDLE()
    if not _advapi32.OpenProcessToken(handle, _TOKEN_QUERY, ctypes.byref(token)):
        return False
    try:
        elevated = wintypes.DWORD()
        size = wintypes.DWORD()
        if not _advapi32.GetTokenInformation(
                token, _TokenElevation, ctypes.byref(elevated), ctypes.sizeof(elevated), ctypes.byref(size)):
            return False
        return bool(elevated.value)
    finally:
        _kernel32.CloseHandle(token)


def is_elevated() -> bool:
    """True si el proceso actual se ejecuta como administrador (token elevado)."""
    if not _IS_WINDOWS:
        return False
    try:
        return _token_is_elevated(_kernel32.GetCurrentProcess())
    except Exception:  # noqa: BLE001
        return False


def _process_integrity_rid(pid: int):
    """RID de nivel de integridad del proceso ``pid`` (0x2000 medio, 0x3000 alto…) o None."""
    proc = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not proc:
        return None
    try:
        token = wintypes.HANDLE()
        if not _advapi32.OpenProcessToken(proc, _TOKEN_QUERY, ctypes.byref(token)):
            return None
        try:
            size = wintypes.DWORD()
            _advapi32.GetTokenInformation(token, _TokenIntegrityLevel, None, 0, ctypes.byref(size))
            if not size.value:
                return None
            buf = (ctypes.c_byte * size.value)()
            if not _advapi32.GetTokenInformation(token, _TokenIntegrityLevel, buf, size, ctypes.byref(size)):
                return None
            label = ctypes.cast(buf, ctypes.POINTER(_TOKEN_MANDATORY_LABEL)).contents
            sid = label.Label.Sid
            if not sid:
                return None
            count = _advapi32.GetSidSubAuthorityCount(sid)[0]
            return int(_advapi32.GetSidSubAuthority(sid, count - 1)[0])
        finally:
            _kernel32.CloseHandle(token)
    finally:
        _kernel32.CloseHandle(proc)


def foreground_needs_admin() -> bool:
    """True si NO vamos elevados y la ventana en primer plano es de mayor integridad.

    Conservador: ante cualquier duda devuelve False (no molesta con avisos falsos)."""
    if not _IS_WINDOWS or is_elevated():
        return False
    try:
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return False
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value or pid.value == os.getpid():
            return False
        rid = _process_integrity_rid(pid.value)
        return rid is not None and rid > _SECURITY_MANDATORY_MEDIUM_RID
    except Exception:  # noqa: BLE001
        return False


def relaunch_as_admin(extra_args: tuple[str, ...] = ("--relaunch",)) -> bool:
    """Lanza una nueva instancia de MacroTool pidiendo permisos de administrador (UAC).

    Devuelve True si el usuario aceptó y la instancia elevada se está lanzando (entonces
    conviene cerrar la instancia actual para liberar el mutex de instancia única).
    False si no se pudo (p. ej. el usuario canceló el aviso de UAC).
    """
    if not _IS_WINDOWS:
        return False
    try:
        if getattr(sys, "frozen", False):
            exe = sys.executable
            argv = list(sys.argv[1:])
        else:
            exe = sys.executable  # python.exe
            argv = [os.path.abspath(sys.argv[0])] + list(sys.argv[1:])
        argv = [a for a in argv if a != "--relaunch"] + list(extra_args)
        params = subprocess.list2cmdline(argv)
        rc = _shell32.ShellExecuteW(None, "runas", exe, params, None, _SW_SHOWNORMAL)
        return int(rc or 0) > 32  # ShellExecuteW: > 32 = éxito
    except Exception:  # noqa: BLE001
        log.exception("No se pudo reiniciar como administrador")
        return False
