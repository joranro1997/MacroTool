"""Tests de persistencia (macrotool.storage), siempre en una carpeta temporal."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from macrotool import storage
from macrotool.model import (
    Macro,
    MoveStep,
    PressStep,
    ScrollStep,
    Settings,
    TextStep,
    WaitStep,
    example_macros,
)


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Carpeta de datos aislada (nunca %APPDATA%\\MacroTool real)."""
    path = tmp_path / "datos MacroTool ñ"
    monkeypatch.setenv(storage.DATA_DIR_ENV, str(path))
    return path


def sample_macros() -> list[Macro]:
    return [
        Macro(
            name="Combo «especial» ñandú 日本 😀",
            trigger="ctrl+1",
            trigger_mode="toggle",
            default_delay_ms=175,
            jitter_ms=5,
            repeat_count=0,
            steps=[
                PressStep(inputs=["w", "mouse_right"], hold_ms=60, delay_after_ms=20),
                PressStep(inputs=["shift"], action="down", comment="correr"),
                TextStep(text="Hola\nqué tal\t✓", char_interval_ms=3),
                MoveStep(x=-10, y=15, relative=True, duration_ms=200),
                ScrollStep(amount=-2, x=100, y=100),
                WaitStep(ms=750, random_extra_ms=50, enabled=False),
            ],
        ),
        Macro(name="Vacía", enabled=False),
    ]


def leftovers(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir() if p.name.endswith(".tmp"))


# ---------------------------------------------------------------------------
# app_data_dir
# ---------------------------------------------------------------------------
def test_app_data_dir_uses_env_and_creates_it(data_dir: Path) -> None:
    assert not data_dir.exists()
    assert storage.app_data_dir() == data_dir
    assert data_dir.is_dir()
    assert storage.macros_path() == data_dir / "macros.json"
    assert storage.settings_path() == data_dir / "settings.json"


def test_app_data_dir_defaults_to_appdata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(storage.DATA_DIR_ENV, raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    assert storage.app_data_dir() == tmp_path / "Roaming" / "MacroTool"
    assert (tmp_path / "Roaming" / "MacroTool").is_dir()


def test_blank_env_var_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(storage.DATA_DIR_ENV, "   ")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert storage.app_data_dir() == tmp_path / "MacroTool"


def test_session_never_points_to_real_appdata() -> None:
    """conftest.py redirige los datos de toda la sesión a una carpeta temporal."""
    real = Path(os.environ.get("APPDATA", "")) / "MacroTool"
    assert os.environ.get(storage.DATA_DIR_ENV)
    assert storage.app_data_dir() != real


# ---------------------------------------------------------------------------
# Macros
# ---------------------------------------------------------------------------
def test_first_run_creates_example_macros(data_dir: Path) -> None:
    macros = storage.load_macros()
    expected = example_macros()
    assert [m.name for m in macros] == ["Combo 1", "Combo 2", "Combo 3"]
    assert [m.trigger for m in macros] == ["1", "2", "3"]
    assert all(m.enabled for m in macros)
    for got, want in zip(macros, expected):
        want.id = got.id
        assert got == want
    assert [s.describe() for s in macros[0].steps] == [
        "W + Clic derecho", "S + Q", "S + Clic izquierdo", "Espacio",
        "Clic izquierdo + Clic derecho",
    ]
    # se guardaron: el segundo arranque carga lo mismo (mismos ids)
    assert (data_dir / "macros.json").is_file()
    assert storage.load_macros() == macros


def test_save_and_load_round_trip(data_dir: Path) -> None:
    macros = sample_macros()
    storage.save_macros(macros)
    assert storage.load_macros() == macros
    assert leftovers(data_dir) == []


def test_saved_file_format(data_dir: Path) -> None:
    macros = sample_macros()
    storage.save_macros(macros)
    raw = (data_dir / "macros.json").read_bytes()
    text = raw.decode("utf-8")
    assert not raw.startswith(b"\xef\xbb\xbf")  # sin BOM
    assert "ñandú 日本 😀" in text  # ensure_ascii=False
    assert '\n  "macros": [' in text  # indent=2
    data = json.loads(text)
    assert data["version"] == 1
    assert data["macros"] == [m.to_dict() for m in macros]


def test_save_empty_library(data_dir: Path) -> None:
    storage.save_macros([])
    assert storage.load_macros() == []
    assert json.loads((data_dir / "macros.json").read_text("utf-8")) == {"version": 1, "macros": []}


def test_save_overwrites_previous(data_dir: Path) -> None:
    storage.save_macros(sample_macros())
    storage.save_macros([Macro(name="Única")])
    assert [m.name for m in storage.load_macros()] == ["Única"]


def test_atomic_save_keeps_old_file_on_failure(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage.save_macros([Macro(name="Original")])
    before = (data_dir / "macros.json").read_bytes()

    def broken_replace(src: str, dst: object) -> None:
        raise OSError("disco lleno")

    monkeypatch.setattr(storage.os, "replace", broken_replace)
    with pytest.raises(OSError):
        storage.save_macros([Macro(name="Nueva")])
    assert (data_dir / "macros.json").read_bytes() == before
    assert leftovers(data_dir) == []


def test_atomic_save_retries_when_file_is_locked(data_dir: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    real_replace = os.replace
    failures = {"left": 2}

    def flaky_replace(src: str, dst: object) -> None:
        if failures["left"]:
            failures["left"] -= 1
            raise PermissionError("en uso")
        real_replace(src, dst)

    monkeypatch.setattr(storage.os, "replace", flaky_replace)
    monkeypatch.setattr(storage.time, "sleep", lambda s: None)
    storage.save_macros([Macro(name="Tras reintentos")])
    assert [m.name for m in storage.load_macros()] == ["Tras reintentos"]
    assert failures["left"] == 0


@pytest.mark.parametrize("content", [
    "{esto no es json",
    "",
    '{"version": 1, "macros": 5}',
    '{"version": 1}',
    '"texto"',
    "42",
])
def test_corrupt_library_is_renamed(data_dir: Path, content: str) -> None:
    data_dir.mkdir(parents=True)
    path = data_dir / "macros.json"
    path.write_text(content, encoding="utf-8")
    assert storage.load_macros() == []
    assert not path.exists()
    backups = list(data_dir.glob("macros.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == content
    assert len(backups[0].name) == len("macros.json.corrupt-20260927-120000")
    # tras apartarlo, el siguiente guardado funciona con normalidad
    storage.save_macros([Macro(name="Nueva")])
    assert [m.name for m in storage.load_macros()] == ["Nueva"]


def test_invalid_utf8_is_treated_as_corrupt(data_dir: Path) -> None:
    data_dir.mkdir(parents=True)
    (data_dir / "macros.json").write_bytes(b'{"macros": ["\xff\xfe"]}')
    assert storage.load_macros() == []
    assert len(list(data_dir.glob("macros.json.corrupt-*"))) == 1


def test_two_corrupt_backups_in_the_same_second(data_dir: Path) -> None:
    data_dir.mkdir(parents=True)
    path = data_dir / "macros.json"
    for content in ("roto 1", "roto 2"):
        path.write_text(content, encoding="utf-8")
        assert storage.load_macros() == []
    contents = sorted(p.read_text("utf-8") for p in data_dir.glob("macros.json.corrupt-*"))
    assert contents == ["roto 1", "roto 2"]


def test_partially_damaged_library(data_dir: Path) -> None:
    data_dir.mkdir(parents=True)
    good = Macro(name="Buena", steps=[PressStep(inputs=["a"])])
    data = {"version": 1, "macros": [
        good.to_dict(),
        "basura",
        {"name": "Con paso roto", "steps": [{"type": "press", "inputs": ["b"]},
                                            {"type": "desconocido"}]},
    ]}
    path = data_dir / "macros.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    macros = storage.load_macros()
    assert [m.name for m in macros] == ["Buena", "Con paso roto"]
    assert macros[0] == good
    assert macros[1].steps == [PressStep(inputs=["b"])]
    # el original sigue ahí y además hay una copia de seguridad íntegra
    assert path.exists()
    backups = list(data_dir.glob("macros.json.corrupt-*"))
    assert len(backups) == 1 and json.loads(backups[0].read_text("utf-8")) == data


def test_load_accepts_bom_and_dirty_values(data_dir: Path) -> None:
    data_dir.mkdir(parents=True)
    data = {"macros": [{
        "id": "abc", "name": "Sucia", "enabled": "false", "default_delay_ms": "90",
        "trigger": " Control + 1 ",
        "steps": [{"type": "press", "inputs": ["W", "Clic derecho", "w", "tecla_rara"],
                   "hold_ms": "40"}],
    }]}
    (data_dir / "macros.json").write_bytes(b"\xef\xbb\xbf" + json.dumps(data).encode("utf-8"))
    [macro] = storage.load_macros()
    assert macro.id == "abc" and macro.enabled is False and macro.default_delay_ms == 90
    assert macro.trigger == "ctrl+1"  # disparador canónico
    step = macro.steps[0]
    assert isinstance(step, PressStep)
    assert step.inputs == ["w", "mouse_right", "tecla_rara"]  # canónicas, sin duplicados
    assert step.hold_ms == 40


def test_invalid_trigger_is_kept(data_dir: Path) -> None:
    storage.save_macros([Macro(name="x", trigger="ctrl+basura")])
    assert storage.load_macros()[0].trigger == "ctrl+basura"


def test_duplicate_ids_are_fixed(data_dir: Path) -> None:
    a, b = Macro(id="mismo", name="A"), Macro(id="mismo", name="B")
    storage.save_macros([a, b])
    loaded = storage.load_macros()
    assert [m.name for m in loaded] == ["A", "B"]
    assert loaded[0].id == "mismo" and loaded[1].id != "mismo"


def test_read_errors_propagate(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Un fichero ilegible (permisos) no se trata como vacío: así no se sobrescribe."""
    storage.save_macros(sample_macros())

    def denied(self: Path, *args: object, **kwargs: object) -> str:
        raise PermissionError("acceso denegado")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", denied)
        with pytest.raises(PermissionError):
            storage.load_macros()
    assert [m.name for m in storage.load_macros()] == [m.name for m in sample_macros()]
    assert list(data_dir.glob("*.corrupt-*")) == []


# ---------------------------------------------------------------------------
# Ajustes
# ---------------------------------------------------------------------------
def test_settings_missing_gives_defaults(data_dir: Path) -> None:
    assert storage.load_settings() == Settings()
    assert not (data_dir / "settings.json").exists()


def test_settings_round_trip(data_dir: Path) -> None:
    s = Settings(hotkey_stop="ctrl+f7", use_scancodes=False, always_on_top=True,
                 last_macro_id="xyz", window_geometry="AdnQywAD/w==",
                 record_click_positions=True, record_timing=False)
    storage.save_settings(s)
    assert storage.load_settings() == s
    data = json.loads((data_dir / "settings.json").read_text("utf-8"))
    assert data["version"] == 1 and data["hotkey_stop"] == "ctrl+f7"
    assert leftovers(data_dir) == []


@pytest.mark.parametrize("content", ["{roto", "[1, 2]", '"x"', "", "null"])
def test_corrupt_settings_give_defaults(data_dir: Path, content: str) -> None:
    data_dir.mkdir(parents=True)
    (data_dir / "settings.json").write_text(content, encoding="utf-8")
    assert storage.load_settings() == Settings()
    assert len(list(data_dir.glob("settings.json.corrupt-*"))) == 1
    storage.save_settings(Settings(hotkey_pause="f10"))
    assert storage.load_settings().hotkey_pause == "f10"


def test_settings_dirty_values(data_dir: Path) -> None:
    data_dir.mkdir(parents=True)
    (data_dir / "settings.json").write_text(
        json.dumps({"always_on_top": "true", "minimize_to_tray": 0, "hotkey_run_selected": None,
                    "desconocido": 1}), encoding="utf-8")
    s = storage.load_settings()
    assert s.always_on_top is True and s.minimize_to_tray is False
    assert s.hotkey_run_selected == "f6"


# ---------------------------------------------------------------------------
# Importar / exportar
# ---------------------------------------------------------------------------
def test_export_import_round_trip(data_dir: Path, tmp_path: Path) -> None:
    macros = sample_macros()
    target = tmp_path / "exportadas ñ.json"
    storage.export_macros(macros, target)
    assert json.loads(target.read_text("utf-8")) == {
        "version": 1, "macros": [m.to_dict() for m in macros]}
    imported = storage.import_macros(target)
    assert len(imported) == len(macros)
    for got, original in zip(imported, macros):
        assert got.id != original.id and len(got.id) == 32
        got.id = original.id
        assert got == original
    assert len({m.id for m in storage.import_macros(target)} | {m.id for m in imported}) == 4
    assert leftovers(tmp_path) == []


def test_export_accepts_str_path(data_dir: Path, tmp_path: Path) -> None:
    target = str(tmp_path / "salida.json")
    storage.export_macros([Macro(name="x")], target)
    assert [m.name for m in storage.import_macros(target)] == ["x"]


def test_export_uses_same_format_as_library(data_dir: Path, tmp_path: Path) -> None:
    macros = sample_macros()
    storage.save_macros(macros)
    storage.export_macros(macros, tmp_path / "copia.json")
    assert (tmp_path / "copia.json").read_bytes() == (data_dir / "macros.json").read_bytes()


def test_import_single_macro(tmp_path: Path) -> None:
    macro = sample_macros()[0]
    path = tmp_path / "una.json"
    path.write_text(json.dumps(macro.to_dict(), ensure_ascii=False), encoding="utf-8")
    [imported] = storage.import_macros(path)
    assert imported.id != macro.id
    imported.id = macro.id
    assert imported == macro


def test_import_minimal_single_macro(tmp_path: Path) -> None:
    path = tmp_path / "minima.json"
    path.write_text('{"steps": [{"type": "press", "inputs": "Ctrl+S"}]}', encoding="utf-8")
    [macro] = storage.import_macros(path)
    assert macro.name == "Nueva macro"
    assert macro.steps == [PressStep(inputs=["ctrl", "s"])]


def test_import_list_of_macros_with_bom(tmp_path: Path) -> None:
    path = tmp_path / "lista.json"
    payload = json.dumps([Macro(name="A").to_dict(), Macro(name="B").to_dict(), "basura"])
    path.write_bytes(b"\xef\xbb\xbf" + payload.encode("utf-8"))
    assert [m.name for m in storage.import_macros(path)] == ["A", "B"]


def test_import_assigns_fresh_unique_ids(tmp_path: Path) -> None:
    path = tmp_path / "dup.json"
    storage.export_macros([Macro(id="x", name="A"), Macro(id="x", name="B")], path)
    ids = [m.id for m in storage.import_macros(path)]
    assert len(set(ids)) == 2 and "x" not in ids


@pytest.mark.parametrize("content, message", [
    ("{no es json", "no es un JSON válido"),
    ("", "no es un JSON válido"),
    ('{"foo": 1}', "no contiene macros de MacroTool"),
    ("42", "no contiene macros de MacroTool"),
    ('"texto"', "no contiene macros de MacroTool"),
    ('{"macros": "abc"}', "no es una lista"),
    ('{"version": 1, "macros": []}', "no contiene ninguna macro"),
    ("[]", "no contiene ninguna macro"),
    ('["a", 1, null]', "no contiene ninguna macro"),
])
def test_import_invalid_files(tmp_path: Path, content: str, message: str) -> None:
    path = tmp_path / "malo.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        storage.import_macros(path)


def test_import_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="No se encontró el archivo «no_existe.json»"):
        storage.import_macros(tmp_path / "no_existe.json")


def test_import_non_utf8_file(tmp_path: Path) -> None:
    path = tmp_path / "latin1.json"
    path.write_bytes('{"name": "Canción", "steps": []}'.encode("latin-1"))
    with pytest.raises(ValueError, match="UTF-8"):
        storage.import_macros(path)


def test_import_directory_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="No se pudo leer el archivo"):
        storage.import_macros(tmp_path)


def test_import_json_error_mentions_position(tmp_path: Path) -> None:
    path = tmp_path / "linea.json"
    path.write_text('{\n  "macros": [\n    oops\n  ]\n}', encoding="utf-8")
    with pytest.raises(ValueError, match=r"línea 3, columna 5"):
        storage.import_macros(path)
