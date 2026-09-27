"""Configuración común de pytest.

- Inserta la raíz del proyecto en ``sys.path`` (``import macrotool`` funciona con
  ``python -m pytest`` y con ``pytest`` a secas).
- Protege los datos reales del usuario: si ``MACROTOOL_DATA_DIR`` no está
  definida, apunta a una carpeta temporal para toda la sesión, de modo que ningún
  test pueda escribir en ``%APPDATA%\\MacroTool``.
- Fuerza Qt sin ventanas visibles (``QT_QPA_PLATFORM=offscreen``) salvo que ya
  se haya elegido otra plataforma.
"""
from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if not os.environ.get("MACROTOOL_DATA_DIR"):
    _session_data_dir = tempfile.mkdtemp(prefix="macrotool-tests-")
    os.environ["MACROTOOL_DATA_DIR"] = _session_data_dir
    atexit.register(shutil.rmtree, _session_data_dir, ignore_errors=True)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
