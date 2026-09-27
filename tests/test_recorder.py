"""Pruebas del grabador (con un hook falso) y de la conversión pura events_to_steps."""
from __future__ import annotations

import pytest

from macrotool import winput
from macrotool.hooks import RawEvent
from macrotool.model import MoveStep, PressStep, ScrollStep, example_macro
from macrotool.recorder import RECORDER_PRIORITY, Recorder, clean_events, events_to_steps


@pytest.fixture(autouse=True)
def no_real_input(monkeypatch):
    """Salvaguarda: ninguna prueba de este módulo puede inyectar entrada real."""

    def forbidden(*args):
        raise AssertionError("SendInput real llamado desde una prueba")

    monkeypatch.setattr(winput, "_SendInput", forbidden)


class Timeline:
    """Construye listas de RawEvent con tiempos en milisegundos."""

    def __init__(self) -> None:
        self.events: list[RawEvent] = []

    def _add(self, ms: float, kind: str, token: str = "", **kw) -> "Timeline":
        self.events.append(RawEvent(ms / 1000.0, kind, token, **kw))
        return self

    def down(self, ms: float, token: str, **kw) -> "Timeline":
        return self._add(ms, "mouse_down" if token.startswith("mouse_") else "key_down", token, **kw)

    def up(self, ms: float, token: str, **kw) -> "Timeline":
        return self._add(ms, "mouse_up" if token.startswith("mouse_") else "key_up", token, **kw)

    def wheel(self, ms: float, delta: int, horizontal: bool = False, **kw) -> "Timeline":
        return self._add(ms, "hwheel" if horizontal else "wheel", delta=delta, **kw)

    def move(self, ms: float, x: int, y: int) -> "Timeline":
        return self._add(ms, "move", x=x, y=y)

    def chord(self, start: float, tokens: list[str], hold: float = 50, gap: float = 10) -> "Timeline":
        t = start
        for tok in tokens:
            self.down(t, tok)
            t += gap
        t = t - gap + hold
        for tok in tokens:
            self.up(t, tok)
            t += gap
        return self


def press(inputs, action="tap", **kw) -> PressStep:
    return PressStep(inputs=list(inputs), action=action, **kw)


# --- Ejemplo del usuario ------------------------------------------------------------------------
def user_example_timeline() -> Timeline:
    tl = Timeline()
    tl.chord(0, ["w", "mouse_right"])
    tl.chord(300, ["s", "q"])
    tl.chord(600, ["s", "mouse_left"])
    tl.chord(900, ["space"])
    tl.chord(1200, ["mouse_left", "mouse_right"])
    return tl


def test_user_example_produces_example_macro_steps():
    steps = events_to_steps(user_example_timeline().events, record_timing=False)
    assert steps == example_macro().steps
    assert all(isinstance(s, PressStep) and s.action == "tap" and s.hold_ms == 50 for s in steps)


def test_user_example_with_timing():
    steps = events_to_steps(user_example_timeline().events)
    # acordes de dos: último up a +70 → siguiente a +300; "space" suelto: up a +50
    assert [s.delay_after_ms for s in steps] == [230, 230, 230, 250, None]
    assert [s.inputs for s in steps] == [s.inputs for s in example_macro().steps]


def test_min_delay_and_no_timing():
    tl = Timeline().chord(0, ["a"]).chord(55, ["b"])
    assert [s.delay_after_ms for s in events_to_steps(tl.events)] == [5, None]
    assert [s.delay_after_ms for s in events_to_steps(tl.events, min_delay_ms=30)] == [30, None]
    assert [s.delay_after_ms for s in events_to_steps(tl.events, record_timing=False)] == [None, None]


def test_empty_input():
    assert events_to_steps([]) == []


# --- Acordes y pulsaciones solapadas ----------------------------------------------------------------
def test_shift_held_while_typing_two_letters_gives_down_up_steps():
    tl = Timeline()
    tl.down(0, "shift").down(100, "h").up(150, "h").down(200, "i").up(250, "i").up(300, "shift")
    steps = events_to_steps(tl.events, record_timing=False)
    assert steps == [
        press(["shift"], "down"),
        press(["h"], "down"),
        press(["h"], "up"),
        press(["i"], "down"),
        press(["i"], "up"),
        press(["shift"], "up"),
    ]
    timed = events_to_steps(tl.events)
    assert [s.delay_after_ms for s in timed] == [100, 50, 50, 50, 50, None]


def test_chord_order_and_hold():
    tl = Timeline().down(0, "ctrl").down(30, "shift").down(45, "s").up(120, "shift").up(125, "s").up(130, "ctrl")
    (step,) = events_to_steps(tl.events)
    assert step == press(["ctrl", "shift", "s"], hold_ms=75)


def test_minimum_hold():
    tl = Timeline().down(0, "a").up(2, "a")
    assert events_to_steps(tl.events)[0].hold_ms == 10


def test_autorepeat_downs_are_dropped():
    tl = Timeline().down(0, "a").down(500, "a").down(533, "a").up(600, "a").chord(700, ["b"])
    steps = events_to_steps(tl.events)
    assert steps == [press(["a"], hold_ms=600, delay_after_ms=100), press(["b"])]


def test_initial_ups_and_final_downs_are_trimmed():
    # Ctrl+F8 inicia la grabación: su up llega ya grabando; al terminar, Ctrl queda pulsado.
    tl = Timeline().up(0, "ctrl").up(5, "f8")
    tl.chord(100, ["x"])
    tl.down(400, "ctrl")
    steps = events_to_steps(tl.events)
    assert steps == [press(["x"])]


def test_down_without_up_in_middle_is_not_trimmed():
    tl = Timeline().down(0, "a").down(50, "b").up(100, "b").down(150, "c").up(200, "c").up(250, "a")
    steps = events_to_steps(tl.events, record_timing=False)
    assert [(s.action, s.inputs) for s in steps] == [
        ("down", ["a"]), ("down", ["b"]), ("up", ["b"]), ("down", ["c"]), ("up", ["c"]), ("up", ["a"]),
    ]


def test_injected_and_consumed_events_are_ignored():
    tl = Timeline().chord(0, ["a"])
    tl.down(100, "1", consumed=True).up(150, "1", consumed=True)
    tl.down(200, "z", injected=True).up(250, "z", injected=True)
    assert events_to_steps(tl.events) == [press(["a"])]


def test_unsorted_input_is_sorted():
    tl = Timeline().chord(0, ["a"]).chord(100, ["b"])
    steps = events_to_steps(list(reversed(tl.events)), record_timing=False)
    assert [s.inputs for s in steps] == [["a"], ["b"]]


def test_clean_events_does_not_mutate_input():
    tl = Timeline().up(0, "x").chord(10, ["a"]).down(100, "b")
    original = list(tl.events)
    cleaned = clean_events(tl.events)
    assert tl.events == original
    assert [(e.kind, e.token) for e in cleaned] == [("key_down", "a"), ("key_up", "a")]


# --- Ratón ----------------------------------------------------------------------------------------------
def test_double_click():
    tl = Timeline().chord(0, ["mouse_left"], hold=40).chord(150, ["mouse_left"], hold=40).chord(600, ["a"])
    steps = events_to_steps(tl.events)
    assert steps == [press(["mouse_left"], hold_ms=40, count=2, delay_after_ms=410), press(["a"])]


def test_double_click_needs_short_gap_same_button_and_position():
    slow = Timeline().chord(0, ["mouse_left"]).chord(500, ["mouse_left"])
    assert [s.count for s in events_to_steps(slow.events)] == [1, 1]
    mixed = Timeline().chord(0, ["mouse_left"]).chord(100, ["mouse_right"])
    assert [s.count for s in events_to_steps(mixed.events)] == [1, 1]
    far = Timeline().down(0, "mouse_left", x=10, y=10).up(40, "mouse_left", x=10, y=10)
    far.down(100, "mouse_left", x=20, y=10).up(140, "mouse_left", x=20, y=10)
    assert [s.count for s in events_to_steps(far.events)] == [1, 1]
    near = Timeline().down(0, "mouse_left", x=10, y=10).up(40, "mouse_left", x=10, y=10)
    near.down(100, "mouse_left", x=12, y=11).up(140, "mouse_left", x=12, y=11)
    assert [s.count for s in events_to_steps(near.events)] == [2]
    chord = Timeline().chord(0, ["ctrl", "mouse_left"]).chord(150, ["ctrl", "mouse_left"])
    assert [s.count for s in events_to_steps(chord.events)] == [1, 1]  # sólo botones sueltos


def test_triple_click_is_double_plus_single():
    tl = Timeline().chord(0, ["mouse_left"]).chord(100, ["mouse_left"]).chord(200, ["mouse_left"])
    assert [s.count for s in events_to_steps(tl.events)] == [2, 1]


def test_double_click_ignores_tiny_moves_between_clicks():
    tl = Timeline().move(0, 100, 100)
    tl.down(50, "mouse_left", x=100, y=100).up(90, "mouse_left", x=100, y=100)
    tl.move(120, 101, 100)
    tl.down(150, "mouse_left", x=101, y=100).up(190, "mouse_left", x=101, y=100)
    steps = events_to_steps(tl.events, record_timing=False, record_click_positions=True)
    assert steps == [MoveStep(x=100, y=100), press(["mouse_left"], hold_ms=40, count=2, x=100, y=100)]


def test_click_positions():
    tl = Timeline().down(0, "s").down(10, "mouse_left", x=300, y=200).up(60, "s").up(65, "mouse_left", x=300, y=200)
    tl.chord(200, ["a"])
    with_pos = events_to_steps(tl.events, record_timing=False, record_click_positions=True)
    assert with_pos == [press(["s", "mouse_left"], x=300, y=200), press(["a"])]
    without = events_to_steps(tl.events, record_timing=False)
    assert (without[0].x, without[0].y) == (None, None)


def test_tiny_jitter_during_click_keeps_tap():
    tl = Timeline().move(0, 50, 50)
    tl.down(100, "mouse_left", x=50, y=50).move(120, 51, 51).up(160, "mouse_left", x=51, y=51)
    steps = events_to_steps(tl.events, record_timing=False)
    assert steps == [MoveStep(x=50, y=50), press(["mouse_left"], hold_ms=60)]


def test_drag_becomes_down_moves_up():
    tl = Timeline().down(0, "mouse_left", x=10, y=10).move(50, 60, 10).move(100, 200, 50).up(150, "mouse_left", x=200, y=50)
    steps = events_to_steps(tl.events, record_timing=False, record_click_positions=True)
    assert steps == [
        press(["mouse_left"], "down", x=10, y=10),
        MoveStep(x=60, y=10),
        MoveStep(x=200, y=50),
        press(["mouse_left"], "up", x=200, y=50),
    ]


def test_moves_between_steps_and_timing():
    tl = Timeline().move(0, 10, 20).move(40, 30, 40).chord(100, ["mouse_left"]).move(300, 5, 5)
    steps = events_to_steps(tl.events)
    assert steps == [
        MoveStep(x=10, y=20, delay_after_ms=40),
        MoveStep(x=30, y=40, delay_after_ms=60),
        press(["mouse_left"], delay_after_ms=150),
        MoveStep(x=5, y=5),
    ]


# --- Rueda ----------------------------------------------------------------------------------------------
def test_wheel_is_aggregated():
    tl = Timeline().wheel(0, -120).wheel(50, -120).wheel(100, -120).chord(400, ["a"])
    steps = events_to_steps(tl.events)
    assert steps == [ScrollStep(amount=-3, delay_after_ms=300), press(["a"])]


def test_wheel_splits_on_direction_gap_and_axis():
    tl = Timeline().wheel(0, 120).wheel(50, 120).wheel(100, -120).wheel(400, -120).wheel(420, 120, horizontal=True)
    steps = events_to_steps(tl.events, record_timing=False)
    assert steps == [
        ScrollStep(amount=2),
        ScrollStep(amount=-1),
        ScrollStep(amount=-1),
        ScrollStep(amount=1, horizontal=True),
    ]


def test_wheel_small_deltas_and_positions():
    tl = Timeline().wheel(0, 40, x=7, y=8).wheel(20, 40, x=7, y=8).wheel(40, 50, x=7, y=8)
    assert events_to_steps(tl.events, record_click_positions=True) == [ScrollStep(amount=1, x=7, y=8)]
    assert events_to_steps(tl.events) == [ScrollStep(amount=1)]
    tiny = Timeline().wheel(0, -10)
    assert events_to_steps(tiny.events) == [ScrollStep(amount=-1)]
    zero = Timeline().wheel(0, 0)
    assert events_to_steps(zero.events) == []


def test_wheel_while_key_held():
    tl = Timeline().down(0, "ctrl").wheel(50, 120).wheel(80, 120).up(200, "ctrl")
    steps = events_to_steps(tl.events, record_timing=False)
    assert steps == [press(["ctrl"], "down"), ScrollStep(amount=2), press(["ctrl"], "up")]


# --- Recorder con hook falso ------------------------------------------------------------------------------
class FakeHook:
    def __init__(self, running: bool = True) -> None:
        self.listeners: list[tuple] = []
        self.running = running
        self.started = 0
        self.stopped = 0

    def add_listener(self, fn, priority=0, *, wants_moves=True):
        self.listeners.append((fn, priority, wants_moves))

    def remove_listener(self, fn):
        self.listeners = [entry for entry in self.listeners if entry[0] != fn]

    def start(self):
        self.started += 1
        self.running = True

    def stop(self):
        self.stopped += 1
        self.running = False

    def emit(self, event: RawEvent):
        for fn, _, wants_moves in list(self.listeners):
            if event.kind == "move" and not wants_moves:
                continue
            assert fn(event) is None  # el grabador nunca bloquea


def test_recorder_collects_physical_events():
    hook = FakeHook()
    seen: list[RawEvent] = []
    rec = Recorder(hook, own_window_at=lambda x, y: False)
    rec.start(on_event=seen.append)
    assert rec.recording
    ((_, priority, wants_moves),) = hook.listeners
    assert priority == RECORDER_PRIORITY and wants_moves is False
    hook.emit(RawEvent(0.0, "key_down", "a"))
    hook.emit(RawEvent(0.01, "key_down", "b", injected=True))
    hook.emit(RawEvent(0.02, "key_down", "1", consumed=True))
    hook.emit(RawEvent(0.05, "key_up", "a"))
    events = rec.stop()
    # Lo bloqueado por un atajo global se conserva sólo como marca (consumed): no genera pasos.
    assert [(e.kind, e.token, e.consumed) for e in events] == [
        ("key_down", "a", False), ("key_down", "1", True), ("key_up", "a", False)]
    assert seen == [e for e in events if not e.consumed]
    assert [s.inputs for s in events_to_steps(events)] == [["a"]]
    assert not rec.recording and hook.listeners == []
    hook.emit(RawEvent(0.1, "key_down", "c"))
    assert rec.stop() == []


def test_recorder_throttles_moves_but_keeps_last_position_before_click():
    hook = FakeHook()
    rec = Recorder(hook, own_window_at=lambda x, y: False)
    rec.start(record_moves=True, move_interval_ms=40)
    assert hook.listeners[0][2] is True
    for i in range(10):  # un movimiento cada 10 ms
        hook.emit(RawEvent(i * 0.01, "move", x=i, y=0))
    hook.emit(RawEvent(0.095, "mouse_down", "mouse_left", 1, x=9, y=0))
    hook.emit(RawEvent(0.12, "mouse_up", "mouse_left", 1, x=9, y=0))
    hook.emit(RawEvent(0.13, "move", x=50, y=0))  # demasiado pronto: queda pendiente
    events = rec.stop()
    moves = [e.x for e in events if e.kind == "move"]
    assert moves == [0, 4, 8, 9, 50]
    assert [e.kind for e in events][-3:] == ["mouse_down", "mouse_up", "move"]


def test_recorder_without_moves_ignores_them():
    hook = FakeHook()
    rec = Recorder(hook, own_window_at=lambda x, y: False)
    rec.start()
    rec._listener(RawEvent(0, "move", x=1, y=1))  # aunque llegara alguno
    assert rec.stop() == []


def test_recorder_trims_final_click_on_own_window():
    hook = FakeHook()
    own = {(900, 50)}
    rec = Recorder(hook, own_window_at=lambda x, y: (x, y) in own)
    rec.start(record_moves=True, move_interval_ms=0)
    hook.emit(RawEvent(0.0, "mouse_down", "mouse_left", 1, x=10, y=10))
    hook.emit(RawEvent(0.05, "mouse_up", "mouse_left", 1, x=10, y=10))
    hook.emit(RawEvent(0.2, "move", x=500, y=40))
    hook.emit(RawEvent(0.3, "move", x=900, y=50))
    hook.emit(RawEvent(0.4, "mouse_down", "mouse_left", 1, x=900, y=50))
    hook.emit(RawEvent(0.45, "mouse_up", "mouse_left", 1, x=900, y=50))
    events = rec.stop()
    assert [(e.kind, e.x) for e in events] == [("mouse_down", 10), ("mouse_up", 10)]


def test_recorder_keeps_trailing_moves_without_own_click():
    hook = FakeHook()
    rec = Recorder(hook, own_window_at=lambda x, y: True)
    rec.start(record_moves=True, move_interval_ms=0)
    hook.emit(RawEvent(0.0, "key_down", "a"))
    hook.emit(RawEvent(0.05, "key_up", "a"))
    hook.emit(RawEvent(0.2, "move", x=5, y=5))
    events = rec.stop()
    assert [e.kind for e in events] == ["key_down", "key_up", "move"]
    rec.start(record_moves=True, move_interval_ms=0)
    hook.emit(RawEvent(0.0, "mouse_down", "mouse_left", x=1, y=1))
    assert len(rec.stop(trim_own_clicks=False)) == 1


def test_recorder_starts_and_stops_hook_if_needed():
    hook = FakeHook(running=False)
    rec = Recorder(hook, own_window_at=lambda x, y: False)
    rec.start()
    assert hook.started == 1
    rec.stop()
    assert hook.stopped == 1
    running = FakeHook(running=True)
    rec2 = Recorder(running, own_window_at=lambda x, y: False)
    rec2.start()
    rec2.stop()
    assert running.started == 0 and running.stopped == 0


def test_recorder_restart_discards_previous():
    hook = FakeHook()
    rec = Recorder(hook, own_window_at=lambda x, y: False)
    rec.start()
    hook.emit(RawEvent(0.0, "key_down", "a"))
    rec.start()
    assert len(hook.listeners) == 1
    hook.emit(RawEvent(0.1, "key_down", "b"))
    assert [e.token for e in rec.stop()] == ["b"]


def test_recorder_callback_errors_are_contained():
    hook = FakeHook()
    rec = Recorder(hook, own_window_at=lambda x, y: False)

    def boom(event):
        raise RuntimeError("fallo")

    rec.start(on_event=boom)
    hook.emit(RawEvent(0.0, "key_down", "a"))
    assert len(rec.stop()) == 1


def test_recorder_start_failure_cleans_up():
    class BrokenHook(FakeHook):
        def start(self):
            raise RuntimeError("No se pudieron instalar los hooks")

    hook = BrokenHook(running=False)
    rec = Recorder(hook, own_window_at=lambda x, y: False)
    with pytest.raises(RuntimeError):
        rec.start()
    assert not rec.recording and hook.listeners == []


def test_record_then_convert_end_to_end():
    hook = FakeHook()
    rec = Recorder(hook, own_window_at=lambda x, y: False)
    rec.start()
    for e in user_example_timeline().events:
        hook.emit(RawEvent(e.t, e.kind, e.token, e.vk, e.x, e.y, e.delta))
    steps = events_to_steps(rec.stop(), record_timing=False)
    assert steps == example_macro().steps


# --- Revisión final: arrastres, gesto de parada, ups perdidos, teclas mantenidas... ---------------
def test_drag_without_moves_keeps_release_position():
    """Con posiciones y sin movimientos grabados, un arrastre no se convierte en un clic."""
    tl = Timeline().down(0, "mouse_left", x=10, y=10).up(300, "mouse_left", x=500, y=400)
    steps = events_to_steps(tl.events, record_click_positions=True, record_timing=False)
    assert steps == [press(["mouse_left"], "down", x=10, y=10), press(["mouse_left"], "up", x=500, y=400)]
    # Un clic con un temblor mínimo sigue siendo un clic.
    tl = Timeline().down(0, "mouse_left", x=10, y=10).up(80, "mouse_left", x=11, y=10)
    assert events_to_steps(tl.events, record_click_positions=True, record_timing=False) == [
        press(["mouse_left"], hold_ms=80, x=10, y=10)]
    # Sin posiciones, un tap y un down/up se reproducen igual: se mantiene el tap.
    tl = Timeline().down(0, "mouse_left", x=10, y=10).up(300, "mouse_left", x=500, y=400)
    assert events_to_steps(tl.events, record_timing=False) == [press(["mouse_left"], hold_ms=300)]


def test_trim_ui_return_drops_the_gesture_that_brought_the_user_back():
    from macrotool.recorder import trim_ui_return

    def game() -> Timeline:
        return Timeline().chord(0, ["a"]).chord(500, ["mouse_left"], hold=40)

    # Bandeja: clic derecho en el icono (el menú se abre al soltar) y clic en «Detener macro».
    tl = game().down(2000, "mouse_right", x=1800, y=1060).up(2080, "mouse_right", x=1800, y=1060)
    tl.down(2600, "mouse_left", x=1750, y=980).up(2650, "mouse_left", x=1750, y=980)
    kept = trim_ui_return(tl.events, 2.085)
    assert [s.inputs for s in events_to_steps(kept, record_timing=False)] == [["a"], ["mouse_left"]]
    # Barra de tareas: el clic que restaura la ventana (se activa al soltar).
    tl = game().down(3000, "mouse_left", x=300, y=1060).up(3070, "mouse_left", x=300, y=1060)
    tl.down(3500, "mouse_left", x=900, y=50).up(3550, "mouse_left", x=900, y=50)
    assert len(trim_ui_return(tl.events, 3.09)) == 4
    # Alt+Tab: la ventana se activa al soltar Alt.
    tl = game().down(4000, "alt").down(4050, "tab").up(4100, "tab").up(4200, "alt")
    tl.down(4600, "space").up(4650, "space")  # Espacio sobre el botón con el foco
    assert len(trim_ui_return(tl.events, 4.21)) == 4
    # Clic directo en la ventana inactiva: se activa durante el propio clic.
    tl = game().down(5000, "mouse_left", x=900, y=50)
    tl.up(5050, "mouse_left", x=900, y=50)
    assert len(trim_ui_return(tl.events, 5.001)) == 4
    # Si lo último del juego es muy anterior, no se toca.
    assert len(trim_ui_return(game().events, 5.0)) == 4


def test_recorder_stop_applies_ui_return_and_reports_held_keys():
    hook = FakeHook()
    rec = Recorder(hook, own_window_at=lambda x, y: False, key_state=lambda vk: vk == 0x57)
    rec.start()
    for e in (Timeline().down(0, "w").chord(100, ["mouse_left"]).chord(300, ["space"])
              .down(2000, "mouse_right").up(2050, "mouse_right").events):
        hook.emit(e)
    events = rec.stop(ui_since=2.06)
    assert rec.held_at_stop == frozenset({"w"})  # W seguía pulsada al parar
    steps = events_to_steps(events, record_timing=False, held_at_end=rec.held_at_stop)
    assert steps[0] == press(["w"], "down")
    assert [s.inputs for s in steps] == [["w"], ["mouse_left"], ["mouse_left"], ["space"], ["space"]]


def test_key_held_while_stopping_is_kept_only_if_still_pressed():
    tl = Timeline().down(0, "w").chord(100, ["mouse_left"]).chord(300, ["space"])
    assert [s.inputs for s in events_to_steps(tl.events, record_timing=False)] == [["mouse_left"], ["space"]]
    kept = events_to_steps(tl.events, record_timing=False, held_at_end={"w"})
    assert kept[0] == press(["w"], "down")
    assert [s.action for s in kept] == ["down", "down", "up", "down", "up"]
    # El modificador del atajo de parada (lo último, sin nada después) se descarta siempre.
    tl = Timeline().chord(0, ["a"]).down(500, "ctrl")
    assert events_to_steps(tl.events, record_timing=False, held_at_end={"ctrl"}) == [press(["a"], hold_ms=50)]


def test_lost_up_is_not_taken_for_autorepeat():
    """Ctrl+Alt+Supr se come los ups: el siguiente Ctrl no debe mantener Ctrl todo el intervalo."""
    tl = Timeline().down(0, "ctrl").down(10, "alt").down(20, "delete")
    tl.chord(8000, ["a"])
    tl.down(9000, "ctrl").chord(9050, ["c"]).up(9200, "ctrl")
    steps = events_to_steps(tl.events, record_timing=False)
    assert steps[0] == press(["a"], hold_ms=50)
    assert [s.inputs for s in steps] == [["a"], ["ctrl", "c"]]  # «a», no Ctrl+A
    # Un segundo mouse_down del mismo botón siempre indica un up perdido.
    tl = Timeline().down(0, "mouse_left").down(200, "mouse_left").up(250, "mouse_left")
    assert events_to_steps(tl.events, record_timing=False) == [press(["mouse_left"], hold_ms=50)]


def test_global_hotkey_modifiers_are_not_recorded():
    tl = Timeline().chord(0, ["a"])
    tl.down(200, "alt").down(250, "p", consumed=True).up(300, "p", consumed=True).up(350, "alt")
    tl.chord(600, ["b"])
    assert [s.inputs for s in events_to_steps(tl.events, record_timing=False)] == [["a"], ["b"]]
    # Un modificador que también acompañó a otras teclas grabadas se conserva.
    tl = Timeline().down(0, "shift").chord(50, ["w"]).down(200, "p", consumed=True)
    tl.up(250, "p", consumed=True).up(400, "shift")
    assert [s.inputs for s in events_to_steps(tl.events, record_timing=False)] == [["shift", "w"]]


def test_numpad_key_released_as_other_vk_pairs_by_scancode():
    tl = Timeline().down(0, "num1", scan=0x4F).down(30, "end", scan=0x4F)  # autorepetición traducida
    tl.up(100, "end", scan=0x4F)
    assert events_to_steps(tl.events, record_timing=False) == [press(["num1"], hold_ms=100)]
