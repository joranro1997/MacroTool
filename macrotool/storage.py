"""Persistencia en disco: biblioteca de macros y ajustes (JSON en UTF-8).

Los ficheros viven en ``%APPDATA%\\MacroTool`` (o en ``MACROTOOL_DATA_DIR`` si
esa variable de entorno existe, p. ej. en los tests). Toda escritura es atómica
(fichero temporal + ``os.replace``) para que un corte nunca deje un JSON a medias,
y un fichero ilegible se aparta como copia ``.corrupt-FECHA`` en vez de perderse.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional, Union

from . import APP_NAME, keys
from .model import SCHEMA_VERSION, Macro, PressStep, Settings, example_macros, new_id

log = logging.getLogger(__name__)

DATA_DIR_ENV = "MACROTOOL_DATA_DIR"
MACROS_FILE = "macros.json"
SETTINGS_FILE = "settings.json"

PathLike = Union[str, "os.PathLike[str]"]


# ---------------------------------------------------------------------------
# Rutas
# ---------------------------------------------------------------------------
def app_data_dir() -> Path:
    """Carpeta de datos de la aplicación (se crea si no existe)."""
    override = os.environ.get(DATA_DIR_ENV, "").strip()
    if override:
        path = Path(override).expanduser()
    else:
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        path = Path(base) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def macros_path() -> Path:
    return app_data_dir() / MACROS_FILE


def settings_path() -> Path:
    return app_data_dir() / SETTINGS_FILE


# ---------------------------------------------------------------------------
# Utilidades de fichero
# ---------------------------------------------------------------------------
def _dumps(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _replace(src: str, dst: Path, attempts: int = 10) -> None:
    """``os.replace`` con reintentos: en Windows un antivirus o el indexador
    pueden tener el destino abierto durante unos milisegundos."""
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05)


def _atomic_write_text(path: Path, text: str) -> None:
    """Escribe ``text`` en UTF-8 de forma atómica (tmp en la misma carpeta + replace)."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        _replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json(path: Path) -> Any:
    """Lee un JSON UTF-8 (tolera BOM). ValueError si no es JSON válido."""
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _backup_corrupt(path: Path, *, keep_original: bool = False) -> Optional[Path]:
    """Aparta una copia ``<nombre>.corrupt-YYYYmmdd-HHMMSS`` del fichero.

    Por defecto lo renombra; con ``keep_original`` lo copia y deja el original.
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.corrupt-{stamp}")
    n = 1
    while target.exists():
        target = path.with_name(f"{path.name}.corrupt-{stamp}-{n}")
        n += 1
    try:
        if keep_original:
            shutil.copy2(path, target)
        else:
            os.replace(path, target)
    except OSError:
        if keep_original:
            log.exception("No se pudo copiar %s", path)
            return None
        try:  # p. ej. fichero bloqueado: al menos conservar una copia
            shutil.copy2(path, target)
        except OSError:
            log.exception("No se pudo apartar el fichero dañado %s", path)
            return None
    log.warning("Copia de seguridad del fichero dañado: %s", target)
    return target


# ---------------------------------------------------------------------------
# Conversión biblioteca <-> macros
# ---------------------------------------------------------------------------
def _library_dict(macros: Iterable[Macro]) -> dict[str, Any]:
    return {"version": SCHEMA_VERSION, "macros": [m.to_dict() for m in macros]}


def _raw_macro_list(data: Any) -> list[Any]:
    """Entradas de macro de un JSON: biblioteca, lista de macros o macro suelta."""
    if isinstance(data, dict) and "macros" in data:
        raw = data["macros"]
        if not isinstance(raw, list):
            raise ValueError("El campo «macros» del archivo no es una lista.")
        version = data.get("version")
        if isinstance(version, int) and version > SCHEMA_VERSION:
            log.warning("Biblioteca con versión %s (se esperaba %s)", version, SCHEMA_VERSION)
        return raw
    if isinstance(data, dict) and "steps" in data:
        return [data]
    if isinstance(data, list):
        return data
    raise ValueError("El archivo no contiene macros de MacroTool.")


def _normalize_macro(macro: Macro) -> None:
    """Pasa las teclas y el disparador a su forma canónica ("Ctrl" → "ctrl").

    Las entradas desconocidas se conservan tal cual (el motor avisará al ejecutarlas).
    """
    for step in macro.steps:
        if isinstance(step, PressStep):
            inputs: list[str] = []
            for token in step.inputs:
                try:
                    token = keys.normalize(token)
                except ValueError:
                    pass
                if token not in inputs:
                    inputs.append(token)
            step.inputs = inputs
    macro.trigger = macro.trigger.strip()
    if macro.trigger:
        try:
            macro.trigger = keys.combo_to_string(macro.trigger)
        except ValueError:
            pass


def _parse_macros(data: Any) -> tuple[list[Macro], int]:
    """(macros válidas, nº de macros o pasos descartados por estar dañados)."""
    macros: list[Macro] = []
    dropped = 0
    seen_ids: set[str] = set()
    for raw in _raw_macro_list(data):
        try:
            macro = Macro.from_dict(raw)
        except (ValueError, TypeError, KeyError):
            dropped += 1
            continue
        raw_steps = raw.get("steps")
        if isinstance(raw_steps, list):
            dropped += len(raw_steps) - len(macro.steps)
        if macro.id in seen_ids:
            macro.id = new_id()
        seen_ids.add(macro.id)
        _normalize_macro(macro)
        macros.append(macro)
    return macros, dropped


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------
def load_macros() -> list[Macro]:
    """Carga la biblioteca de macros.

    - Si no existe: crea y guarda las macros de ejemplo del primer arranque.
    - Si está dañada: la renombra a ``macros.json.corrupt-...`` y devuelve [].
    - Si sólo algunas macros/pasos están dañados: se cargan las válidas y se
      guarda una copia del fichero original por si acaso.
    Los errores de E/S (permisos...) se propagan para no sobrescribir datos.
    """
    path = macros_path()
    if not path.exists():
        macros = example_macros()
        try:
            save_macros(macros)
        except OSError:
            log.exception("No se pudieron guardar las macros de ejemplo")
        return macros
    try:
        macros, dropped = _parse_macros(_read_json(path))
    except ValueError:  # JSON inválido, UTF-8 inválido o estructura inesperada
        log.warning("Biblioteca de macros dañada: %s", path)
        _backup_corrupt(path)
        return []
    if dropped:
        log.warning("Se descartaron %d macros/pasos dañados de %s", dropped, path)
        _backup_corrupt(path, keep_original=True)
    return macros


def save_macros(macros: list[Macro]) -> None:
    """Guarda la biblioteca completa de forma atómica."""
    _atomic_write_text(macros_path(), _dumps(_library_dict(macros)))


def load_settings() -> Settings:
    """Carga los ajustes; si faltan o están dañados, devuelve los valores por defecto."""
    path = settings_path()
    if not path.exists():
        return Settings()
    try:
        return Settings.from_dict(_read_json(path))
    except ValueError:
        log.warning("Ajustes dañados, se usan los valores por defecto: %s", path)
        _backup_corrupt(path)
    except OSError:
        log.exception("No se pudieron leer los ajustes: %s", path)
    return Settings()


def save_settings(s: Settings) -> None:
    """Guarda los ajustes de forma atómica."""
    _atomic_write_text(settings_path(), _dumps({"version": SCHEMA_VERSION, **s.to_dict()}))


def export_macros(macros: list[Macro], path: PathLike) -> None:
    """Exporta macros con el mismo formato que ``macros.json``."""
    _atomic_write_text(Path(path), _dumps(_library_dict(macros)))


def import_macros(path: PathLike) -> list[Macro]:
    """Importa macros de un archivo (biblioteca, lista o macro suelta) con ids nuevos.

    ValueError con un mensaje en español si el archivo no se puede usar.
    """
    file = Path(path)
    try:
        text = file.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise ValueError(f"No se encontró el archivo «{file.name}».") from None
    except UnicodeDecodeError:
        raise ValueError("El archivo no está codificado en UTF-8.") from None
    except OSError as exc:
        raise ValueError(f"No se pudo leer el archivo «{file.name}»: {exc.strerror or exc}") from None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"El archivo no es un JSON válido (línea {exc.lineno}, columna {exc.colno})."
        ) from None
    macros, _ = _parse_macros(data)
    if not macros:
        raise ValueError("El archivo no contiene ninguna macro válida.")
    for macro in macros:
        macro.id = new_id()
    return macros
