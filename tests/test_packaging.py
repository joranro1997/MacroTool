"""Comprobaciones de empaquetado sin compilar (la compilación real la hace build_exe.bat).

- MacroTool.spec toma la versión de ``macrotool.__version__`` y sus filtros conservan lo que la
  aplicación necesita de Qt (y descartan lo que no).
- El código solo usa módulos de Qt que el .spec incluye.
- ``theme.assets_dir()`` encuentra ``assets`` dentro del ejecutable (``sys._MEIPASS``).
"""
from __future__ import annotations

import re
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest

import macrotool

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "MacroTool.spec"


@lru_cache(maxsize=1)
def _spec_namespace() -> dict[str, Any]:
    """Ejecuta la parte declarativa del .spec (todo lo anterior a la construcción)."""
    source = SPEC.read_text(encoding="utf-8")
    head, marker, _ = source.partition("# --- Construcción")
    assert marker, "MacroTool.spec debe tener la sección '# --- Construcción'"
    namespace: dict[str, Any] = {"SPECPATH": str(ROOT), "__name__": "macrotool_spec"}
    exec(compile(head, str(SPEC), "exec"), namespace)  # noqa: S102 - fichero propio del proyecto
    return namespace


@pytest.fixture()
def spec() -> dict[str, Any]:
    pytest.importorskip("PyInstaller")
    return _spec_namespace()


def test_spec_version_comes_from_package(spec: dict[str, Any]) -> None:
    assert spec["VERSION"] == macrotool.__version__
    assert spec["APP_NAME"] == macrotool.APP_NAME
    numbers = [int(n) for n in macrotool.__version__.split(".")[:4]]
    assert list(spec["VERSION_TUPLE"][:len(numbers)]) == numbers
    assert len(spec["VERSION_TUPLE"]) == 4


@pytest.mark.parametrize(("text", "expected"), [
    ("1.0.0", (1, 0, 0, 0)),
    ("2.3", (2, 3, 0, 0)),
    ("7", (7, 0, 0, 0)),
    ("1.2.3.4.5", (1, 2, 3, 4)),
    ("1.2.0rc1", (1, 2, 0, 0)),
    ("dev", (0, 0, 0, 0)),
])
def test_spec_version_tuple(spec: dict[str, Any], text: str, expected: tuple[int, ...]) -> None:
    assert spec["_version_tuple"](text) == expected


@pytest.mark.parametrize(("dest", "kept"), [
    # imprescindible
    (r"PySide6\Qt6Core.dll", True),
    (r"PySide6\Qt6Gui.dll", True),
    (r"PySide6\Qt6Widgets.dll", True),
    (r"PySide6\QtWidgets.pyd", True),
    (r"PySide6\pyside6.abi3.dll", True),
    (r"shiboken6\shiboken6.abi3.dll", True),
    (r"PySide6\MSVCP140.dll", True),
    (r"python311.dll", True),
    (r"PySide6\plugins\platforms\qwindows.dll", True),
    (r"PySide6\plugins\platforms\qoffscreen.dll", True),
    (r"PySide6\plugins\imageformats\qico.dll", True),
    (r"PySide6\plugins\styles\qmodernwindowsstyle.dll", True),
    (r"PySide6\translations\qtbase_es.qm", True),
    (r"assets\icon.ico", True),
    (r"assets\icon.png", True),
    # innecesario
    (r"PySide6\translations\qtbase_de.qm", False),
    (r"PySide6\plugins\platforms\qdirect2d.dll", False),
    (r"PySide6\plugins\imageformats\qpdf.dll", False),
    (r"PySide6\plugins\imageformats\qjpeg.dll", False),
    (r"PySide6\plugins\iconengines\qsvgicon.dll", False),
    (r"PySide6\plugins\generic\qtuiotouchplugin.dll", False),
    (r"PySide6\plugins\tls\qschannelbackend.dll", False),
    (r"PySide6\Qt6Pdf.dll", False),
    (r"PySide6\Qt6Svg.dll", False),
    (r"PySide6\Qt6Network.dll", False),
    (r"PySide6\opengl32sw.dll", False),
])
def test_spec_filter_keeps_only_needed_files(spec: dict[str, Any], dest: str, kept: bool) -> None:
    assert spec["_keep"]((dest, "origen", "BINARY")) is kept


def test_code_only_uses_qt_modules_included_in_the_exe(spec: dict[str, Any]) -> None:
    """Si se empieza a usar otro módulo de Qt hay que incluirlo en MacroTool.spec."""
    allowed = {"QtCore", "QtGui", "QtWidgets"}
    excluded = set(spec["EXCLUDED_MODULES"])
    used: dict[str, set[str]] = {}
    for path in [ROOT / "main.py", *sorted((ROOT / "macrotool").rglob("*.py"))]:
        for module in re.findall(r"\bPySide6\.(Qt\w+)", path.read_text(encoding="utf-8")):
            used.setdefault(module, set()).add(path.relative_to(ROOT).as_posix())
    assert used, "no se encontró ningún import de PySide6"
    assert set(used) <= allowed, {m: files for m, files in used.items() if m not in allowed}
    assert not {f"PySide6.{m}" for m in used} & excluded


def test_spec_bundles_existing_assets() -> None:
    for name in ("icon.ico", "icon.png"):
        assert (ROOT / "assets" / name).is_file(), name


def test_assets_dir_inside_frozen_exe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from macrotool.ui import theme

    assert theme.assets_dir() == ROOT / "assets"
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert theme.assets_dir() == tmp_path / "assets"
