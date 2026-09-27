"""Tests del modelo de datos (macrotool.model)."""
from __future__ import annotations

import json

import pytest

from macrotool.model import (
    PRESS_ACTIONS,
    SCHEMA_VERSION,
    STEP_TYPES,
    TRIGGER_MODES,
    Macro,
    MoveStep,
    PressStep,
    ScrollStep,
    Settings,
    Step,
    TextStep,
    WaitStep,
    example_macro,
    example_macros,
    new_id,
)

# Un ejemplar de cada tipo con valores distintos de los por defecto.
SAMPLE_STEPS: list[Step] = [
    PressStep(inputs=["ctrl", "shift", "s"], action="down", hold_ms=75, count=3,
              x=10, y=-20, enabled=False, delay_after_ms=250, comment="guardar"),
    PressStep(inputs=["mouse_left", "mouse_right"]),
    TextStep(text="Hola, ñandú ✓\nsegunda\tlínea 😀", char_interval_ms=5,
             delay_after_ms=0, comment="texto"),
    MoveStep(x=-150, y=300, relative=True, duration_ms=400, delay_after_ms=30),
    ScrollStep(amount=5, horizontal=True, x=100, y=200, comment="rueda"),
    WaitStep(ms=1234, random_extra_ms=66, enabled=False, delay_after_ms=10),
]


def full_macro() -> Macro:
    return Macro(
        name="Mi macro «especial» ñ",
        steps=[s.clone() for s in SAMPLE_STEPS],
        enabled=False,
        trigger="ctrl+1",
        trigger_mode="hold",
        block_trigger=False,
        default_delay_ms=175,
        jitter_ms=12,
        humanize=False,
        hold_jitter_ms=7,
        chord_stagger_ms=0,
        repeat_count=0,
        repeat_delay_ms=500,
        start_delay_ms=3000,
    )


# ---------------------------------------------------------------------------
# Registro de tipos
# ---------------------------------------------------------------------------
def test_step_types_registry() -> None:
    assert STEP_TYPES == {
        "press": PressStep, "text": TextStep, "move": MoveStep,
        "scroll": ScrollStep, "wait": WaitStep,
    }
    for type_name, cls in STEP_TYPES.items():
        assert cls.TYPE == type_name
        assert cls.LABEL  # etiqueta en español para la UI


def test_constants() -> None:
    assert PRESS_ACTIONS == ("tap", "down", "up")
    assert TRIGGER_MODES == ("once", "toggle", "hold")
    assert SCHEMA_VERSION == 1


def test_new_id_is_unique_hex() -> None:
    ids = {new_id() for _ in range(100)}
    assert len(ids) == 100
    assert all(len(i) == 32 and int(i, 16) >= 0 for i in ids)


# ---------------------------------------------------------------------------
# Round-trip de pasos
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("step", SAMPLE_STEPS, ids=lambda s: s.TYPE)
def test_step_round_trip(step: Step) -> None:
    data = step.to_dict()
    assert data["type"] == step.TYPE
    restored = Step.from_dict(data)
    assert type(restored) is type(step)
    assert restored == step


@pytest.mark.parametrize("step", SAMPLE_STEPS, ids=lambda s: s.TYPE)
def test_step_round_trip_through_json(step: Step) -> None:
    text = json.dumps(step.to_dict(), ensure_ascii=False)
    assert Step.from_dict(json.loads(text)) == step


@pytest.mark.parametrize("cls", list(STEP_TYPES.values()), ids=lambda c: c.TYPE)
def test_default_steps_round_trip(cls: type[Step]) -> None:
    step = cls()
    assert Step.from_dict(step.to_dict()) == step
    # sólo con el tipo también se obtiene el paso por defecto
    assert Step.from_dict({"type": cls.TYPE}) == step


def test_step_to_dict_contains_all_fields() -> None:
    data = PressStep().to_dict()
    assert set(data) == {"type", "enabled", "delay_after_ms", "comment",
                         "inputs", "action", "hold_ms", "count", "x", "y"}
    assert "TYPE" not in data and "LABEL" not in data


def test_step_to_dict_is_a_deep_copy() -> None:
    step = PressStep(inputs=["a"])
    data = step.to_dict()
    data["inputs"].append("b")
    assert step.inputs == ["a"]


def test_step_clone_is_independent() -> None:
    step = PressStep(inputs=["w", "mouse_right"], hold_ms=80)
    copy = step.clone()
    assert copy == step and copy is not step
    copy.inputs.append("q")
    assert step.inputs == ["w", "mouse_right"]


def test_default_inputs_not_shared() -> None:
    a, b = PressStep(), PressStep()
    a.inputs.append("x")
    assert b.inputs == ["enter"]


@pytest.mark.parametrize("bad", [
    {"type": "nope"}, {}, {"type": None}, {"type": 5}, {"type": ["press"]},
])
def test_step_from_dict_unknown_type(bad: dict) -> None:
    with pytest.raises(ValueError):
        Step.from_dict(bad)


@pytest.mark.parametrize("bad", ["press", 42, None, ["press"]])
def test_step_from_dict_not_a_dict(bad: object) -> None:
    with pytest.raises(ValueError):
        Step.from_dict(bad)  # type: ignore[arg-type]


def test_step_from_dict_ignores_unknown_fields() -> None:
    step = Step.from_dict({"type": "wait", "ms": 10, "foo": "bar", "TYPE": "press"})
    assert step == WaitStep(ms=10)


# ---------------------------------------------------------------------------
# Coerción desde JSON "sucio"
# ---------------------------------------------------------------------------
def test_coerce_numbers_from_strings_and_floats() -> None:
    step = Step.from_dict({"type": "press", "hold_ms": "120", "count": 2.6,
                           "x": "15", "y": 30.4, "delay_after_ms": " 200 "})
    assert step == PressStep(hold_ms=120, count=3, x=15, y=30, delay_after_ms=200)
    assert Step.from_dict({"type": "wait", "ms": "1.5e3"}).ms == 1500


@pytest.mark.parametrize("raw, expected", [
    ("false", False), ("False", False), ("0", False), ("no", False), ("", False),
    ("off", False), (0, False), (0.0, False),
    ("true", True), ("TRUE", True), ("1", True), ("sí", True), ("yes", True), (1, True), (2, True),
])
def test_coerce_booleans(raw: object, expected: bool) -> None:
    assert Step.from_dict({"type": "wait", "enabled": raw}).enabled is expected
    assert Settings.from_dict({"use_scancodes": raw}).use_scancodes is expected


def test_coerce_invalid_values_fall_back_to_defaults() -> None:
    step = Step.from_dict({"type": "press", "hold_ms": "mucho", "count": [1],
                           "enabled": "quizá", "comment": {"a": 1}, "inputs": 7,
                           "action": 3})
    assert step == PressStep()


def test_coerce_null_in_required_fields_uses_defaults() -> None:
    step = Step.from_dict({"type": "press", "hold_ms": None, "inputs": None,
                           "comment": None, "enabled": None, "action": None})
    assert step == PressStep()
    macro = Macro.from_dict({"name": None, "trigger": None, "default_delay_ms": None})
    assert macro.name == "Nueva macro"
    assert macro.trigger == ""
    assert macro.default_delay_ms == 100


def test_coerce_optional_ints() -> None:
    step = Step.from_dict({"type": "scroll", "x": None, "y": "abc", "delay_after_ms": "x"})
    assert step.x is None and step.y is None and step.delay_after_ms is None
    step = Step.from_dict({"type": "scroll", "x": "7", "y": 8.0, "delay_after_ms": "0"})
    assert (step.x, step.y, step.delay_after_ms) == (7, 8, 0)
    assert isinstance(step.x, int) and isinstance(step.y, int)


def test_coerce_nan_and_infinity() -> None:
    step = Step.from_dict({"type": "wait", "ms": float("nan"), "random_extra_ms": float("inf")})
    assert step == WaitStep()


def test_coerce_inputs() -> None:
    assert Step.from_dict({"type": "press", "inputs": "ctrl + s"}).inputs == ["ctrl", "s"]
    assert Step.from_dict({"type": "press", "inputs": ["w", 1, None, "", " q ", True, ["x"]]}).inputs \
        == ["w", "1", "q"]
    assert Step.from_dict({"type": "press", "inputs": ("a", "b")}).inputs == ["a", "b"]
    assert Step.from_dict({"type": "press", "inputs": []}).inputs == []


def test_coerce_text_values() -> None:
    step = Step.from_dict({"type": "text", "text": 123, "comment": 4.5})
    assert step.text == "123" and step.comment == "4.5"


def test_sanitize_out_of_range_values() -> None:
    press = Step.from_dict({"type": "press", "action": "smash", "hold_ms": -5, "count": 0,
                            "delay_after_ms": -100})
    assert press.action == "tap" and press.hold_ms == 0 and press.count == 1
    assert press.delay_after_ms == 0
    for action in PRESS_ACTIONS:
        assert Step.from_dict({"type": "press", "action": action}).action == action
    wait = Step.from_dict({"type": "wait", "ms": -1, "random_extra_ms": -2})
    assert wait.ms == 0 and wait.random_extra_ms == 0
    assert Step.from_dict({"type": "text", "char_interval_ms": -3}).char_interval_ms == 0
    assert Step.from_dict({"type": "move", "duration_ms": -3, "x": -5}).duration_ms == 0
    # las coordenadas relativas negativas sí son válidas
    assert Step.from_dict({"type": "move", "x": -5, "relative": True}).x == -5


# ---------------------------------------------------------------------------
# describe()
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("step, text", [
    (PressStep(inputs=["w", "mouse_right"]), "W + Clic derecho"),
    (PressStep(inputs=["ctrl", "shift", "s"]), "Ctrl + Mayús + S"),
    (PressStep(inputs=["space"]), "Espacio"),
    (PressStep(inputs=["enter"], action="down"), "Mantener Intro"),
    (PressStep(inputs=["ctrl", "c"], action="up"), "Soltar Ctrl + C"),
    (PressStep(inputs=["mouse_left"], count=2), "Clic izquierdo ×2"),
    (PressStep(inputs=["mouse_left"], count=2, x=5, y=6), "Clic izquierdo ×2 en (5, 6)"),
    (PressStep(inputs=["mouse_left"], action="down", count=3), "Mantener Clic izquierdo"),
    (PressStep(inputs=["mouse_middle"], x=0, y=0), "Clic central en (0, 0)"),
    (PressStep(inputs=["mouse_x1"], x=5, y=None), "Botón lateral 1"),
    (PressStep(inputs=[]), "(vacío)"),
    (PressStep(inputs=["num5", "num_enter", "f5"]), "Num 5 + Num Intro + F5"),
    (PressStep(inputs=["alt", "f4"]), "Alt + F4"),
    (PressStep(inputs=["ralt", "e"]), "AltGr + E"),
    (PressStep(inputs=["Ctrl", "ESPACIO"]), "Ctrl + Espacio"),
    (PressStep(inputs=["tecla_rara"]), "tecla_rara"),
])
def test_press_describe(step: PressStep, text: str) -> None:
    assert step.describe() == text


@pytest.mark.parametrize("step, text", [
    (TextStep(text="hola"), "Escribir «hola»"),
    (TextStep(text=""), "Escribir «»"),
    (TextStep(text="a\r\nb\nc\td"), "Escribir «a ⏎ b ⏎ c ⇥ d»"),
    (TextStep(text="x" * 40), "Escribir «" + "x" * 40 + "»"),
    (TextStep(text="x" * 41), "Escribir «" + "x" * 39 + "…»"),
    (MoveStep(x=100, y=200), "Mover ratón a (100, 200)"),
    (MoveStep(x=100, y=200, duration_ms=250), "Mover ratón a (100, 200) en 250 ms"),
    (MoveStep(x=10, y=-5, relative=True), "Mover ratón +10, -5 (relativo)"),
    (MoveStep(x=0, y=0, relative=True, duration_ms=100), "Mover ratón +0, +0 (relativo) en 100 ms"),
    (ScrollStep(amount=3), "Rueda ↑ arriba ×3"),
    (ScrollStep(amount=-3), "Rueda ↓ abajo ×3"),
    (ScrollStep(amount=2, horizontal=True), "Rueda → derecha ×2"),
    (ScrollStep(amount=-1, horizontal=True, x=4, y=5), "Rueda ← izquierda ×1 en (4, 5)"),
    (WaitStep(ms=500), "Esperar 500 ms"),
    (WaitStep(ms=250, random_extra_ms=100), "Esperar 250 ms (+0–100 ms aleatorio)"),
])
def test_other_describe(step: Step, text: str) -> None:
    assert step.describe() == text


def test_base_step_describe_not_implemented() -> None:
    with pytest.raises(NotImplementedError):
        Step().describe()


# ---------------------------------------------------------------------------
# Macro
# ---------------------------------------------------------------------------
def test_macro_defaults() -> None:
    macro = Macro()
    assert macro.name == "Nueva macro" and macro.steps == []
    assert macro.enabled is True and macro.trigger == "" and macro.trigger_mode == "once"
    assert macro.block_trigger is True
    assert macro.default_delay_ms == 100
    assert macro.repeat_count == 1 and macro.repeat_delay_ms == 0 and macro.start_delay_ms == 0
    # Humanización activada por defecto (requisito del usuario).
    assert macro.humanize is True
    assert (macro.jitter_ms, macro.hold_jitter_ms, macro.chord_stagger_ms) == (15, 10, 8)
    assert Macro().id != Macro().id


def test_macro_round_trip() -> None:
    macro = full_macro()
    data = macro.to_dict()
    assert [s["type"] for s in data["steps"]] == ["press", "press", "text", "move", "scroll", "wait"]
    restored = Macro.from_dict(json.loads(json.dumps(data, ensure_ascii=False)))
    assert restored == macro
    assert restored.to_dict() == data


def test_macro_to_dict_keys() -> None:
    assert set(Macro().to_dict()) == {
        "id", "name", "steps", "enabled", "trigger", "trigger_mode", "block_trigger",
        "default_delay_ms", "repeat_count", "repeat_delay_ms", "start_delay_ms",
        "humanize", "jitter_ms", "hold_jitter_ms", "chord_stagger_ms",
    }


def test_macro_clone() -> None:
    macro = full_macro()
    same = macro.clone()
    assert same == macro and same is not macro
    same.steps[0].inputs.append("x")  # type: ignore[attr-defined]
    assert macro.steps[0].inputs == ["ctrl", "shift", "s"]  # type: ignore[attr-defined]
    other = macro.clone(new_identity=True)
    assert other.id != macro.id
    other.id = macro.id
    assert other == macro


def test_macro_from_dict_skips_bad_steps() -> None:
    data = full_macro().to_dict()
    data["steps"] = [{"type": "press", "inputs": ["a"]}, {"type": "teleport"}, "basura",
                     None, 3, {"no_type": True}, {"type": "wait", "ms": 5}]
    macro = Macro.from_dict(data)
    assert macro.steps == [PressStep(inputs=["a"]), WaitStep(ms=5)]


@pytest.mark.parametrize("steps", [None, "abc", 5, {"type": "press"}])
def test_macro_from_dict_steps_not_a_list(steps: object) -> None:
    assert Macro.from_dict({"name": "x", "steps": steps}).steps == []


@pytest.mark.parametrize("bad", [None, "macro", 3, [1, 2]])
def test_macro_from_dict_not_a_dict(bad: object) -> None:
    with pytest.raises(ValueError):
        Macro.from_dict(bad)  # type: ignore[arg-type]


def test_macro_from_dict_fixes_id_and_mode() -> None:
    for bad_id in ("", "   ", None):
        macro = Macro.from_dict({"id": bad_id})
        assert len(macro.id) == 32
    assert Macro.from_dict({"id": 123}).id == "123"
    assert Macro.from_dict({"trigger_mode": "forever"}).trigger_mode == "once"
    for mode in TRIGGER_MODES:
        assert Macro.from_dict({"trigger_mode": mode}).trigger_mode == mode


def test_macro_from_dict_dirty_values() -> None:
    macro = Macro.from_dict({
        "enabled": "false", "block_trigger": "0", "default_delay_ms": "250",
        "jitter_ms": -4, "repeat_count": "-2", "repeat_delay_ms": 10.7,
        "start_delay_ms": "nada", "name": 7, "trigger": " 1 ",
        "humanize": "no", "hold_jitter_ms": "-3", "chord_stagger_ms": None,
    })
    assert macro.enabled is False and macro.block_trigger is False
    assert macro.default_delay_ms == 250
    assert macro.jitter_ms == 0 and macro.repeat_count == 0
    assert macro.humanize is False and macro.hold_jitter_ms == 0 and macro.chord_stagger_ms == 8
    assert macro.repeat_delay_ms == 11 and macro.start_delay_ms == 0
    assert macro.name == "7"


def test_macro_from_empty_dict() -> None:
    macro = Macro.from_dict({})
    template = Macro()
    macro.id = template.id
    assert macro == template


def test_effective_delay_after() -> None:
    macro = Macro(default_delay_ms=120)
    assert macro.effective_delay_after(PressStep()) == 120
    assert macro.effective_delay_after(PressStep(delay_after_ms=0)) == 0
    assert macro.effective_delay_after(PressStep(delay_after_ms=33)) == 33
    assert macro.effective_delay_after(PressStep(delay_after_ms=-5)) == 0
    assert macro.effective_delay_after(TextStep()) == 120
    # en WaitStep, None significa 0 (la espera ya es el propio paso)
    assert macro.effective_delay_after(WaitStep(ms=1000)) == 0
    assert macro.effective_delay_after(WaitStep(ms=1000, delay_after_ms=50)) == 50
    assert Macro(default_delay_ms=-10).effective_delay_after(PressStep()) == 0


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def test_settings_defaults() -> None:
    s = Settings()
    assert (s.hotkey_run_selected, s.hotkey_stop, s.hotkey_record, s.hotkey_enable,
            s.hotkey_pause) == ("f6", "f7", "f8", "f9", "")
    assert s.triggers_enabled and s.ignore_triggers_when_focused and s.use_scancodes
    assert s.minimize_to_tray and s.record_timing
    assert not (s.minimize_on_run or s.always_on_top or s.failsafe_corner
                or s.record_mouse_moves or s.record_click_positions)
    assert s.last_macro_id == "" and s.window_geometry == ""


def test_settings_round_trip() -> None:
    s = Settings(hotkey_run_selected="ctrl+f6", hotkey_stop="", hotkey_pause="f10",
                 triggers_enabled=False, ignore_triggers_when_focused=False,
                 use_scancodes=False, minimize_on_run=True, minimize_to_tray=False,
                 always_on_top=True, failsafe_corner=True, record_mouse_moves=True,
                 record_click_positions=True, record_timing=False,
                 last_macro_id="abc", window_geometry="AdnQywADAAA=")
    data = json.loads(json.dumps(s.to_dict()))
    assert Settings.from_dict(data) == s


def test_settings_from_dirty_dict() -> None:
    s = Settings.from_dict({"always_on_top": "true", "use_scancodes": "false",
                            "hotkey_stop": None, "last_macro_id": 12, "unknown": 1,
                            "failsafe_corner": "tal vez"})
    assert s.always_on_top is True and s.use_scancodes is False
    assert s.hotkey_stop == "f7" and s.last_macro_id == "12"
    assert s.failsafe_corner is False
    assert Settings.from_dict({}) == Settings()


@pytest.mark.parametrize("bad", [None, [], "x", 1])
def test_settings_from_dict_not_a_dict(bad: object) -> None:
    with pytest.raises(ValueError):
        Settings.from_dict(bad)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Macros de ejemplo
# ---------------------------------------------------------------------------
def test_example_macro_matches_user_request() -> None:
    macro = example_macro()
    assert macro.trigger == "1" and macro.trigger_mode == "once"
    assert macro.enabled is False and macro.block_trigger is True
    assert macro.default_delay_ms == 150
    assert all(isinstance(s, PressStep) and s.action == "tap" for s in macro.steps)
    assert [s.describe() for s in macro.steps] == [
        "W + Clic derecho",
        "S + Q",
        "S + Clic izquierdo",
        "Espacio",
        "Clic izquierdo + Clic derecho",
    ]
    assert Macro.from_dict(macro.to_dict()) == macro


def test_example_macros_first_run_combos() -> None:
    macros = example_macros()
    assert [m.name for m in macros] == ["Combo 1", "Combo 2", "Combo 3"]
    assert [m.trigger for m in macros] == ["1", "2", "3"]
    assert all(m.enabled and m.trigger_mode == "once" and m.block_trigger for m in macros)
    assert len({m.id for m in macros}) == 3
    descriptions = [[s.describe() for s in m.steps] for m in macros]
    assert descriptions[0] == [
        "W + Clic derecho", "S + Q", "S + Clic izquierdo", "Espacio",
        "Clic izquierdo + Clic derecho",
    ]
    assert descriptions[1] == [
        "Clic derecho", "F", "S + C", "Mayús + Clic derecho", "Clic izquierdo", "S + F",
    ]
    assert macros[1].steps[4].hold_ms == 1000  # type: ignore[attr-defined]
    assert descriptions[2] == [
        "Clic izquierdo + Clic derecho", "Mayús + Clic derecho", "S + Clic izquierdo",
        "F", "S + Clic derecho", "Mayús + Q",
    ]
    for macro in macros:
        assert Macro.from_dict(json.loads(json.dumps(macro.to_dict()))) == macro


def test_example_steps_use_valid_tokens() -> None:
    from macrotool import keys

    for macro in [example_macro(), *example_macros()]:
        for step in macro.steps:
            assert isinstance(step, PressStep)
            assert keys.parse_combo(step.inputs) == step.inputs


def test_huge_values_are_clamped_to_the_ui_ranges():
    """Un JSON editado a mano con valores absurdos no desborda Qt ni desincroniza panel y motor."""
    from macrotool import model

    macro = Macro.from_dict({
        "default_delay_ms": 5_000_000_000, "repeat_count": 10**12, "repeat_delay_ms": 10**10,
        "start_delay_ms": 10**10, "jitter_ms": 900_000, "hold_jitter_ms": -5, "chord_stagger_ms": 900_000,
        "steps": [
            {"type": "press", "inputs": ["a"], "hold_ms": 10**10, "count": 10**9, "x": 10**9, "y": -10**9,
             "delay_after_ms": 10**10},
            {"type": "text", "text": "x", "char_interval_ms": 10**9},
            {"type": "move", "x": 10**9, "y": -10**9, "duration_ms": 10**9},
            {"type": "scroll", "amount": -10**9, "x": 10**9, "y": 5},
            {"type": "wait", "ms": 3_000_000_000, "random_extra_ms": 3_000_000_000},
        ],
    })
    assert (macro.default_delay_ms, macro.repeat_count, macro.repeat_delay_ms, macro.start_delay_ms) == (
        model.MAX_DELAY_MS, model.MAX_REPEATS, model.MAX_REPEAT_DELAY_MS, model.MAX_DELAY_MS)
    assert (macro.jitter_ms, macro.hold_jitter_ms, macro.chord_stagger_ms) == (
        model.MAX_JITTER_MS, 0, model.MAX_STAGGER_MS)
    press, text, move, scroll, wait = macro.steps
    assert (press.hold_ms, press.count, press.x, press.y, press.delay_after_ms) == (
        model.MAX_DELAY_MS, model.MAX_COUNT, model.COORD_LIMIT, -model.COORD_LIMIT, model.MAX_DELAY_MS)
    assert text.char_interval_ms == model.MAX_CHAR_INTERVAL_MS
    assert (move.x, move.y, move.duration_ms) == (model.COORD_LIMIT, -model.COORD_LIMIT, model.MAX_DELAY_MS)
    assert (scroll.amount, scroll.x, scroll.y) == (-model.MAX_SCROLL_NOTCHES, model.COORD_LIMIT, 5)
    assert (wait.ms, wait.random_extra_ms) == (model.MAX_WAIT_MS, model.MAX_WAIT_MS)
    # Los valores normales no cambian.
    example = example_macro()
    assert Macro.from_dict(example.to_dict()).to_dict() == example.to_dict()
