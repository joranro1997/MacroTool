# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller: genera dist\\MacroTool.exe (un solo fichero, sin consola).

Uso: build_exe.bat, o bien ``python -m PyInstaller --noconfirm --clean MacroTool.spec``.

- Icono assets/icon.ico; la carpeta assets/ viaja dentro del .exe (``theme.assets_dir()`` la
  busca en ``sys._MEIPASS``).
- Versión y nombre de producto en las propiedades del .exe, leídos de macrotool/__init__.py.
- Para reducir tamaño solo se incluye lo que usa la aplicación de Qt: QtCore, QtGui y
  QtWidgets, los plugins de plataforma windows (normal) y offscreen (prueba de humo sin
  ventanas), el lector de .ico, el estilo de Windows y la traducción española de qtbase.
  Si en el futuro se usa otro módulo o plugin de Qt, hay que añadirlo en las listas de abajo.
"""
import re
from pathlib import Path, PurePath

from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)

ROOT = Path(SPECPATH)
APP_NAME = "MacroTool"
DESCRIPTION = "Macros de teclado y ratón para Windows"


# --- Versión (macrotool.__version__, sin importar el paquete) -------------------------------
def _read_version() -> str:
    text = (ROOT / "macrotool" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r"""^__version__\s*=\s*["']([^"']+)["']""", text, re.MULTILINE)
    if not match:
        raise SystemExit("No se encuentra __version__ en macrotool/__init__.py")
    return match.group(1)


def _version_tuple(version: str) -> tuple:
    """"1.2.3" -> (1, 2, 3, 0); un sufijo como "rc1" se ignora."""
    numbers = re.match(r"\d+(?:\.\d+){0,3}", version)
    parts = [int(n) for n in numbers.group(0).split(".")] if numbers else []
    return tuple(parts + [0] * (4 - len(parts)))


VERSION = _read_version()
VERSION_TUPLE = _version_tuple(VERSION)

version_info = VSVersionInfo(
    ffi=FixedFileInfo(
        filevers=VERSION_TUPLE,
        prodvers=VERSION_TUPLE,
        mask=0x3F,
        flags=0x0,
        OS=0x40004,  # VOS_NT_WINDOWS32
        fileType=0x1,  # VFT_APP
        subtype=0x0,
        date=(0, 0),
    ),
    kids=[
        StringFileInfo([
            StringTable("0C0A04B0", [  # español (alfabetización internacional), Unicode
                StringStruct("FileDescription", APP_NAME),  # nombre en el Administrador de tareas
                StringStruct("FileVersion", VERSION),
                StringStruct("InternalName", APP_NAME),
                StringStruct("OriginalFilename", f"{APP_NAME}.exe"),
                StringStruct("ProductName", APP_NAME),
                StringStruct("ProductVersion", VERSION),
                StringStruct("Comments", DESCRIPTION),
            ]),
        ]),
        VarFileInfo([VarStruct("Translation", [0x0C0A, 1200])]),
    ],
)

# --- Qué se excluye --------------------------------------------------------------------------
# Módulos de Python que la aplicación no usa (ni directa ni indirectamente).
EXCLUDED_MODULES = [
    # Qt: solo QtCore, QtGui y QtWidgets
    "PySide6.Qt3DAnimation", "PySide6.Qt3DCore", "PySide6.Qt3DExtras", "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic", "PySide6.Qt3DRender", "PySide6.QtAsyncio", "PySide6.QtAxContainer",
    "PySide6.QtBluetooth", "PySide6.QtCharts", "PySide6.QtConcurrent", "PySide6.QtDataVisualization",
    "PySide6.QtDBus", "PySide6.QtDesigner", "PySide6.QtGraphs", "PySide6.QtHelp", "PySide6.QtHttpServer",
    "PySide6.QtLocation", "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtNetwork",
    "PySide6.QtNetworkAuth", "PySide6.QtNfc", "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtPositioning", "PySide6.QtPrintSupport",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQuickControls2",
    "PySide6.QtQuickTest", "PySide6.QtQuickWidgets", "PySide6.QtRemoteObjects", "PySide6.QtScxml",
    "PySide6.QtSensors", "PySide6.QtSerialBus", "PySide6.QtSerialPort", "PySide6.QtSpatialAudio",
    "PySide6.QtSql", "PySide6.QtStateMachine", "PySide6.QtSvg", "PySide6.QtSvgWidgets",
    "PySide6.QtTest", "PySide6.QtTextToSpeech", "PySide6.QtUiTools", "PySide6.QtWebChannel",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineQuick", "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebSockets", "PySide6.QtWebView", "PySide6.QtXml",
    # Biblioteca estándar y paquetes de desarrollo
    "tkinter", "_tkinter", "turtle", "turtledemo", "idlelib", "lib2to3", "pydoc_data", "test",
    "unittest", "doctest", "pdb", "distutils", "setuptools", "pkg_resources", "pip", "venv",
    "ensurepip", "sqlite3", "_sqlite3", "xmlrpc", "ssl", "_ssl", "_hashlib",
    # (sin _hashlib, hashlib usa sus implementaciones internas: se ahorra libcrypto)
    "pytest", "_pytest", "numpy", "PIL", "matplotlib", "PyQt5", "PyQt6", "PySide2",
]

# DLL de Qt que se conservan (el resto de Qt6*.dll lo arrastran plugins que no se usan).
KEEP_QT_DLLS = {"qt6core.dll", "qt6gui.dll", "qt6widgets.dll"}
# Plugins de Qt que se conservan (<tipo>/<fichero>).
KEEP_QT_PLUGINS = {
    "platforms/qwindows.dll",  # plataforma normal
    "platforms/qoffscreen.dll",  # QT_QPA_PLATFORM=offscreen (prueba de humo sin ventanas)
    "imageformats/qico.dll",  # icono .ico (PNG va integrado en QtGui)
    "styles/qmodernwindowsstyle.dll",  # estilo nativo (la app usa Fusion, pero es pequeño)
}
# Otras DLL innecesarias: OpenGL por software y ANGLE/D3D (solo para QtQuick/OpenGL).
EXCLUDED_DLLS = {"opengl32sw.dll", "d3dcompiler_47.dll", "libegl.dll", "libglesv2.dll"}


def _qt_relative(dest: str):
    """Ruta dentro de PySide6/ ('plugins/…', 'translations/…', 'Qt6Core.dll') o None."""
    parts = PurePath(dest).parts
    if len(parts) >= 2 and parts[0].lower() == "pyside6":
        return "/".join(parts[1:])
    return None


def _keep(entry) -> bool:
    dest = entry[0]
    name = PurePath(dest).name.lower()
    if name in EXCLUDED_DLLS:
        return False
    rel = _qt_relative(dest)
    if rel is None:
        return True
    rel_lower = rel.lower()
    if rel_lower.startswith("plugins/"):
        return rel_lower[len("plugins/"):] in KEEP_QT_PLUGINS
    if rel_lower.startswith("translations/"):
        return name == "qtbase_es.qm"  # main._install_translations: textos estándar de Qt en español
    if name.startswith("qt6") and name.endswith(".dll"):
        return name in KEEP_QT_DLLS
    return True


def _filter(toc, label):
    kept = [entry for entry in toc if _keep(entry)]
    dropped = sorted(PurePath(entry[0]).as_posix() for entry in toc if not _keep(entry))
    if dropped:
        print(f"MacroTool.spec: {label}: {len(dropped)} ficheros de Qt/OpenGL excluidos")
    return kept


# --- Construcción ------------------------------------------------------------------------------
a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[(str(ROOT / "assets"), "assets")],
    hiddenimports=[
        # pyserial se importa de forma perezosa en macrotool/hidserial.py (backend HID);
        # el backend de puertos de Windows se carga dinámicamente, así que se declara aquí.
        "serial",
        "serial.tools.list_ports",
        "serial.tools.list_ports_windows",
        # elevation se importa de forma perezosa desde la UI (botón "Reiniciar como administrador").
        "macrotool.elevation",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDED_MODULES,
    noarchive=False,
    optimize=0,
)
a.binaries = _filter(a.binaries, "binarios")
a.datas = _filter(a.datas, "datos")

# Comprobación: lo imprescindible sigue dentro (si falta, el .exe no arrancaría).
_present = {PurePath(entry[0]).as_posix().lower() for entry in a.binaries + a.datas}
for _required in ("pyside6/qt6core.dll", "pyside6/qt6gui.dll", "pyside6/qt6widgets.dll",
                  "pyside6/plugins/platforms/qwindows.dll", "pyside6/plugins/platforms/qoffscreen.dll",
                  "pyside6/plugins/imageformats/qico.dll", "assets/icon.ico", "assets/icon.png"):
    if _required not in _present:
        raise SystemExit(f"MacroTool.spec: falta {_required} en el paquete")

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=APP_NAME,
    icon=[str(ROOT / "assets" / "icon.ico")],
    version=version_info,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX corrompe a veces las DLL de Qt y dispara falsos positivos de antivirus
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
