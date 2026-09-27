"""Punto de entrada de MacroTool.

Uso:
    pythonw main.py                 # normal (sin consola; ver run.bat)
    python main.py --minimized      # arranca oculto en la bandeja del sistema
    python main.py --smoke-test     # construye todo, espera ~1,5 s y sale con código 0
                                    # (con QT_QPA_PLATFORM=offscreen sirve para validar el .exe;
                                    # sin MACROTOOL_DATA_DIR usa %TEMP%/MacroTool-smoke-test)
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import shutil
import sys
import tempfile
import threading
import traceback
from pathlib import Path
from types import TracebackType
from typing import Any, Optional

MUTEX_NAME = "Local\\MacroTool.SingleInstance"
APP_USER_MODEL_ID = "MacroTool.MacroTool"
ERROR_ACCESS_DENIED = 5
ERROR_ALREADY_EXISTS = 183
SMOKE_TEST_MS = 1500
SMOKE_DATA_DIR = "MacroTool-smoke-test"  # en %TEMP%: la prueba de humo no toca los datos reales

log = logging.getLogger("macrotool")
_log_path: Optional[Path] = None
_smoke_test = False
_unhandled_errors = 0


# --- Registro y errores no controlados ----------------------------------------------------
def _setup_logging() -> Optional[Path]:
    """Registro en app_data_dir()/macrotool.log (rotativo). Devuelve la ruta o None."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s")
    path: Optional[Path] = None
    try:
        from macrotool import storage

        path = storage.app_data_dir() / "macrotool.log"
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    except Exception:  # noqa: BLE001 - sin carpeta de datos: al menos no romper
        handler = logging.StreamHandler(sys.stderr) if sys.stderr else logging.NullHandler()
    handler.setFormatter(fmt)
    root.addHandler(handler)
    if sys.stderr is not None and _smoke_test:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(fmt)
        console.setLevel(logging.WARNING)
        root.addHandler(console)
    return path


def _show_error_dialog(exc_type: type[BaseException], exc: BaseException) -> None:
    if _smoke_test:
        return
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox

        if QApplication.instance() is None:
            return
        box = QMessageBox(QMessageBox.Critical, "MacroTool", "Se produjo un error inesperado.")
        details = f"{exc_type.__name__}: {exc}"
        if _log_path is not None:
            details += f"\n\nLos detalles se han guardado en:\n{_log_path}"
        box.setInformativeText(details)
        box.exec()
    except Exception:  # noqa: BLE001 - nunca fallar mientras se informa de un fallo
        log.exception("No se pudo mostrar el diálogo de error")


def _excepthook(exc_type: type[BaseException], exc: BaseException, tb: Optional[TracebackType]) -> None:
    global _unhandled_errors
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return
    _unhandled_errors += 1
    log.critical("Excepción no controlada", exc_info=(exc_type, exc, tb))
    if _smoke_test and sys.stderr is not None:
        traceback.print_exception(exc_type, exc, tb)
    _show_error_dialog(exc_type, exc)


def _thread_excepthook(args: threading.ExceptHookArgs) -> None:
    global _unhandled_errors
    _unhandled_errors += 1
    name = args.thread.name if args.thread is not None else "?"
    log.critical("Excepción no controlada en el hilo %s", name,
                 exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


# --- Integración con Windows --------------------------------------------------------------
def _acquire_single_instance(name: str = MUTEX_NAME) -> Any:
    """Mutex con nombre. Devuelve su handle (mantenerlo vivo) o None si ya hay otra instancia."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.CreateMutexW(None, False, name)
        if not handle:
            if ctypes.get_last_error() == ERROR_ACCESS_DENIED:
                # Existe, pero lo creó una instancia ejecutada como administrador (su DACL no
                # deja abrirlo a un proceso normal): también es "ya se está ejecutando".
                return None
            return True  # si no se puede crear, mejor arrancar que bloquear al usuario
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return None
        return handle
    except Exception:  # noqa: BLE001
        log.exception("No se pudo comprobar la instancia única")
        return True


def _set_app_user_model_id() -> None:
    """Icono propio en la barra de tareas (en vez del de python.exe)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        shell32 = ctypes.WinDLL("shell32")
        fn = shell32.SetCurrentProcessExplicitAppUserModelID
        fn.argtypes = [wintypes.LPCWSTR]
        fn.restype = ctypes.c_long
        fn(APP_USER_MODEL_ID)
    except Exception:  # noqa: BLE001
        log.debug("SetCurrentProcessExplicitAppUserModelID no disponible")


def _install_translations(app: Any) -> None:
    """Textos estándar de Qt (botones de diálogos, menús de los campos…) en español."""
    try:
        from PySide6.QtCore import QLibraryInfo, QLocale, QTranslator

        translator = QTranslator(app)
        path = QLibraryInfo.path(QLibraryInfo.TranslationsPath)
        if translator.load(QLocale(QLocale.Spanish, QLocale.Spain), "qtbase", "_", path):
            app.installTranslator(translator)
    except Exception:  # noqa: BLE001 - sin traducciones la app sigue funcionando
        log.debug("No se pudieron cargar las traducciones de Qt")


class _NullBackend:
    """Backend que no hace nada (prueba de humo: nunca se inyecta nada)."""

    def press(self, tokens: list[str]) -> None: ...
    def release(self, tokens: list[str]) -> None: ...
    def type_char(self, ch: str) -> None: ...
    def move_to(self, x: int, y: int) -> None: ...
    def move_rel(self, dx: int, dy: int) -> None: ...
    def scroll(self, notches: int, horizontal: bool = False) -> None: ...
    def cursor_pos(self) -> tuple[int, int]: return (0, 0)
    def screen_size(self) -> tuple[int, int]: return (1920, 1080)
    def release_all(self) -> None: ...


# --- Arranque -----------------------------------------------------------------------------
def _parse_args(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(prog="MacroTool", description="Macros de teclado y ratón para Windows.")
    parser.add_argument("--smoke-test", action="store_true",
                        help="construye la aplicación, espera ~1,5 s y sale (validación del ejecutable)")
    parser.add_argument("--no-hooks", action="store_true",
                        help="no instala los hooks globales de teclado/ratón (pruebas)")
    parser.add_argument("--minimized", action="store_true", help="arranca oculto en la bandeja del sistema")
    return parser.parse_known_args(argv)


def _use_smoke_data_dir() -> None:
    """Prueba de humo sin ``MACROTOOL_DATA_DIR``: carpeta temporal propia (vaciada en cada
    prueba) para no crear, apartar ni modificar nada en la carpeta de datos real. El registro
    queda en ``%TEMP%\\MacroTool-smoke-test\\macrotool.log`` para consultarlo si falla."""
    if os.environ.get("MACROTOOL_DATA_DIR", "").strip():
        return
    folder = Path(tempfile.gettempdir()) / SMOKE_DATA_DIR
    shutil.rmtree(folder, ignore_errors=True)
    os.environ["MACROTOOL_DATA_DIR"] = str(folder)


def main(argv: Optional[list[str]] = None) -> int:
    global _log_path, _smoke_test
    args, qt_args = _parse_args(sys.argv[1:] if argv is None else argv)
    _smoke_test = args.smoke_test
    if _smoke_test:
        _use_smoke_data_dir()
    _log_path = _setup_logging()
    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook
    _set_app_user_model_id()

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QMessageBox

    from macrotool import APP_NAME, __version__
    from macrotool.ui import theme

    app = QApplication.instance() or QApplication([sys.argv[0], *qt_args])
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setOrganizationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)  # la bandeja del sistema mantiene viva la app
    _install_translations(app)
    theme.apply_theme(app)
    app.setWindowIcon(theme.app_icon())

    mutex = None
    if not args.smoke_test:
        mutex = _acquire_single_instance()
        if mutex is None:
            QMessageBox.information(None, APP_NAME, "MacroTool ya se está ejecutando (quizá como administrador).\n\n"
                                    "Búscalo en la bandeja del sistema, junto al reloj.")
            return 0

    log.info("Iniciando %s %s", APP_NAME, __version__)
    from macrotool.ui.main_window import MainWindow, SettingsDialog
    from macrotool.ui.step_dialog import StepDialog

    try:
        # En la prueba de humo los hooks se instalan (se validan) con los disparadores ya
        # suspendidos ANTES de registrar ningún binding: nunca se traga una tecla del usuario ni
        # envía la tecla de máscara de Alt/Win.
        window = MainWindow(start_hooks=not args.no_hooks, backend=_NullBackend() if args.smoke_test else None,
                            suspend_triggers=args.smoke_test)
    except Exception:  # noqa: BLE001
        log.exception("No se pudo crear la ventana principal")
        if args.smoke_test:
            traceback.print_exc()
            return 1
        raise
    # Apagado o cierre de sesión de Windows: Qt sólo emite commitDataRequest (WM_QUERYENDSESSION)
    # y aboutToQuit (WM_ENDSESSION), y el proceso puede terminar sin que app.exec() vuelva.
    commit_data = getattr(app, "commitDataRequest", None)  # sólo si Qt tiene gestión de sesión
    if commit_data is not None:
        commit_data.connect(lambda _manager: window.on_session_end())
    app.aboutToQuit.connect(window.shutdown)
    if args.smoke_test:
        window.save_on_exit = False  # no tocar los ajustes (geometría, etc.)
        # Construir también los diálogos valida que el ejecutable incluye todo lo necesario.
        StepDialog(window).deleteLater()
        SettingsDialog(window, window.settings).deleteLater()
    if args.minimized and not args.smoke_test:
        window.hide()
    else:
        window.show()
    if args.smoke_test:
        QTimer.singleShot(SMOKE_TEST_MS, window.quit_app)
        QTimer.singleShot(SMOKE_TEST_MS + 3000, lambda: app.exit(2))  # red de seguridad
    code = app.exec()
    window.shutdown()
    log.info("MacroTool cerrado (código %s)", code)
    del mutex
    if args.smoke_test and _unhandled_errors:
        return 1
    return int(code)


if __name__ == "__main__":
    sys.exit(main())
