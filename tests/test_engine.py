"""Tests del reproductor de macros con un backend falso (nunca se inyecta entrada real)."""
from __future__ import annotations

import random
import sys
import threading
import time
import types
from pathlib import Path
from typing import Any, Optional

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import macrotool  # noqa: E402
from macrotool import engine  # noqa: E402
from macrotool.engine import (  # noqa: E402
    DEFAULT_STOP_REASON,
    FAILSAFE_MESSAGE,
    NO_STEPS_MESSAGE,
    TERMINAL_EVENTS,
    MacroPlayer,
    PlayerEvent,
)
from macrotool import model  # noqa: E402
from macrotool.model import (  # noqa: E402
    MoveStep,
    PressStep,
    ScrollStep,
    TextStep,
    WaitStep,
    example_macro,
)


def Macro(**kwargs: Any) -> model.Macro:  # noqa: N802 - sustituye al constructor en los tests
    """Macro exacta (sin humanización) salvo que el test la pida: los tiempos se comprueban al ms."""
    kwargs.setdefault("humanize", False)
    return model.Macro(**kwargs)

_ORIGINAL_SET_TIMER_RESOLUTION = engine._set_timer_resolution

# Tolerancias de tiempo (ms): las esperas nunca terminan antes de tiempo, pero pueden
# retrasarse por la planificación del sistema.
EARLY_MS = 3
LATE_MS = 40


# --------------------------------------------------------------------- dobles
class FakeBackend:
    """Backend que sólo registra las llamadas (con marca de tiempo)."""

    def __init__(self, cursor: tuple[int, int] = (500, 400),
                 screen: tuple[int, int] = (1920, 1080), fail_on: Optional[str] = None) -> None:
        self.calls: list[tuple[float, str, tuple[Any, ...]]] = []
        self.cursor = cursor
        self.screen = screen
        self.fail_on = fail_on
        self._lock = threading.Lock()

    def _record(self, name: str, *args: Any) -> None:
        with self._lock:
            self.calls.append((time.perf_counter(), name, args))
        if self.fail_on == name:
            raise RuntimeError("fallo simulado")

    def press(self, tokens: list[str]) -> None:
        self._record("press", list(tokens))

    def release(self, tokens: list[str]) -> None:
        self._record("release", list(tokens))

    def type_char(self, ch: str) -> None:
        self._record("type_char", ch)

    def move_to(self, x: int, y: int) -> None:
        self._record("move_to", x, y)
        self.cursor = (x, y)

    def move_rel(self, dx: int, dy: int) -> None:
        self._record("move_rel", dx, dy)
        self.cursor = (self.cursor[0] + dx, self.cursor[1] + dy)

    def scroll(self, notches: int, horizontal: bool = False) -> None:
        self._record("scroll", notches, horizontal)

    def cursor_pos(self) -> tuple[int, int]:
        return self.cursor

    def screen_size(self) -> tuple[int, int]:
        return self.screen

    def release_all(self) -> None:
        self._record("release_all")

    # utilidades de los tests
    def log(self, *names: str) -> list[tuple[Any, ...]]:
        with self._lock:
            return [(name, *args) for _, name, args in self.calls if not names or name in names]

    def times(self, *names: str) -> list[float]:
        with self._lock:
            return [t for t, name, _ in self.calls if not names or name in names]


class EventLog:
    """Receptor de eventos que guarda (instante, evento) y permite esperar a uno."""

    def __init__(self) -> None:
        self.items: list[tuple[float, PlayerEvent]] = []
        self._cond = threading.Condition()

    def __call__(self, event: PlayerEvent) -> None:
        with self._cond:
            self.items.append((time.perf_counter(), event))
            self._cond.notify_all()

    @property
    def events(self) -> list[PlayerEvent]:
        with self._cond:
            return [e for _, e in self.items]

    def kinds(self) -> list[str]:
        return [e.kind for e in self.events]

    def of(self, kind: str) -> list[PlayerEvent]:
        return [e for e in self.events if e.kind == kind]

    def time_of(self, kind: str, nth: int = 0) -> float:
        with self._cond:
            return [t for t, e in self.items if e.kind == kind][nth]

    def wait_for(self, kind: str, *, step_index: Optional[int] = None, count: int = 1,
                 timeout: float = 3.0) -> PlayerEvent:
        def matches() -> list[PlayerEvent]:
            return [e for _, e in self.items
                    if e.kind == kind and (step_index is None or e.step_index == step_index)]

        with self._cond:
            ok = self._cond.wait_for(lambda: len(matches()) >= count, timeout)
            assert ok, f"no llegó el evento {kind!r} (recibidos: {[e.kind for _, e in self.items]})"
            return matches()[count - 1]


class ScriptedRng:
    """Sustituto de ``random.Random`` que devuelve valores prefijados y registra las llamadas."""

    def __init__(self, values: list[float]) -> None:
        self.values = list(values)
        self.calls: list[tuple[str, float, float]] = []

    def randint(self, a: int, b: int) -> float:
        self.calls.append(("randint", a, b))
        return self.values.pop(0)

    def gauss(self, mu: float, sigma: float) -> float:
        self.calls.append(("gauss", mu, sigma))
        return self.values.pop(0)


@pytest.fixture(autouse=True)
def timer_calls(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Evita tocar la resolución real del temporizador y registra las llamadas."""
    calls: list[bool] = []
    monkeypatch.setattr(engine, "_set_timer_resolution", calls.append)
    return calls


def play(macro: Macro, backend: Optional[FakeBackend] = None, *, rng: Any = None,
         timeout: float = 5.0, **kwargs: Any) -> tuple[FakeBackend, EventLog, MacroPlayer]:
    backend = backend or FakeBackend()
    player = MacroPlayer(backend, rng=rng)
    log = EventLog()
    assert player.start(macro, log, **kwargs)
    assert player.wait(timeout), "la macro no terminó a tiempo"
    return backend, log, player


def start(macro: Macro, backend: Optional[FakeBackend] = None, *, rng: Any = None,
          **kwargs: Any) -> tuple[FakeBackend, EventLog, MacroPlayer]:
    backend = backend or FakeBackend()
    player = MacroPlayer(backend, rng=rng)
    log = EventLog()
    assert player.start(macro, log, **kwargs)
    return backend, log, player


def ms(seconds: float) -> float:
    return seconds * 1000.0


def assert_ms(elapsed_s: float, expected_ms: float, early: float = EARLY_MS,
              late: float = LATE_MS) -> None:
    got = ms(elapsed_s)
    assert expected_ms - early <= got <= expected_ms + late, f"{got:.1f} ms, esperado {expected_ms} ms"


def assert_single_terminal(log: EventLog, kind: str) -> PlayerEvent:
    kinds = log.kinds()
    terminals = [k for k in kinds if k in TERMINAL_EVENTS]
    assert terminals == [kind], kinds
    assert kinds[-1] == kind, "el evento terminal debe ser el último"
    return log.events[-1]


def taps(macro_steps: list[str], hold: int = 0) -> list[PressStep]:
    return [PressStep(inputs=[t], hold_ms=hold) for t in macro_steps]


# ------------------------------------------------------------ macro de ejemplo
def test_example_macro_sequence_and_delays(timer_calls: list[bool]) -> None:
    macro = example_macro()
    macro.humanize = False  # tiempos exactos y cada combinación en un único lote
    backend, log, player = play(macro)

    assert backend.log() == [
        ("press", ["w", "mouse_right"]), ("release", ["mouse_right", "w"]),
        ("press", ["s", "q"]), ("release", ["q", "s"]),
        ("press", ["s", "mouse_left"]), ("release", ["mouse_left", "s"]),
        ("press", ["space"]), ("release", ["space"]),
        ("press", ["mouse_left", "mouse_right"]), ("release", ["mouse_right", "mouse_left"]),
        ("release_all",),
    ]
    t = backend.times()
    for i in range(0, 10, 2):  # hold_ms por defecto = 50
        assert_ms(t[i + 1] - t[i], 50)
    for i in range(1, 9, 2):  # retardo entre acciones de la macro = 150
        assert_ms(t[i + 1] - t[i], 150)
    assert ms(t[10] - t[9]) < 20, "no debe esperarse tras el último paso"

    assert log.kinds() == ["started", "repeat"] + ["step"] * 5 + ["finished"]
    assert [e.step_index for e in log.of("step")] == [0, 1, 2, 3, 4]
    assert log.of("repeat")[0].total_repeats == 1
    assert {e.macro_id for e in log.events} == {macro.id}
    assert timer_calls == [True, False]
    assert not player.is_running and player.current_macro_id is None


def test_example_macro_humanized_keeps_order_and_bounds() -> None:
    macro = example_macro()  # humanize=True por defecto: ±15 ms, pulsación ±10 ms, desfase 0–8 ms
    assert macro.humanize and (macro.jitter_ms, macro.hold_jitter_ms, macro.chord_stagger_ms) == (15, 10, 8)
    backend, log, _ = play(macro, rng=random.Random(7))

    # Las entradas "a la vez" se pulsan una a una en orden y se sueltan en orden inverso.
    chords = [["w", "mouse_right"], ["s", "q"], ["s", "mouse_left"], ["space"], ["mouse_left", "mouse_right"]]
    expected: list[tuple[Any, ...]] = []
    for chord in chords:
        expected += [("press", [t]) for t in chord] + [("release", [t]) for t in reversed(chord)]
    assert backend.log() == expected + [("release_all",)]
    assert_single_terminal(log, "finished")

    calls = backend.calls[:-1]
    i = 0
    last_release: Optional[float] = None
    for chord in chords:
        n = len(chord)
        presses = [t for t, _, _ in calls[i:i + n]]
        releases = [t for t, _, _ in calls[i + n:i + 2 * n]]
        if last_release is not None:  # retardo entre acciones: 150 ± 15 ms
            assert_ms(presses[0] - last_release, 150, early=15 + EARLY_MS, late=15 + LATE_MS)
        for a, b in zip(presses, presses[1:]):  # desfase 0–8 ms
            assert_ms(b - a, 0, early=1, late=8 + LATE_MS)
        assert_ms(releases[0] - presses[-1], 50, early=10 + EARLY_MS, late=10 + LATE_MS)  # 50 ± 10
        last_release = releases[-1]
        i += 2 * n


def test_events_carry_macro_id_and_step_is_emitted_before_executing() -> None:
    macro = Macro(steps=taps(["a", "b"]), default_delay_ms=0)
    backend, log, _ = play(macro)
    assert {e.macro_id for e in log.events} == {macro.id}
    first_step_t = log.time_of("step", 0)
    assert first_step_t <= backend.times("press")[0]


# ----------------------------------------------------------------- repeticiones
def test_repeats_with_repeat_delay_and_no_wait_after_last() -> None:
    macro = Macro(steps=taps(["a", "b"]), default_delay_ms=20, repeat_count=3,
                  repeat_delay_ms=60)
    backend, log, _ = play(macro)

    assert [c[1] for c in backend.log("press")] == [["a"], ["b"]] * 3
    presses = backend.times("press")
    for i in range(0, 6, 2):
        assert_ms(presses[i + 1] - presses[i], 20)
    for i in (1, 3):  # entre repeticiones: retardo del paso + repeat_delay_ms
        assert_ms(presses[i + 1] - presses[i], 80)
    last_release = backend.times("release")[-1]
    assert ms(backend.times("release_all")[0] - last_release) < 20

    repeats = log.of("repeat")
    assert [e.repeat_index for e in repeats] == [0, 1, 2]
    assert all(e.total_repeats == 3 for e in log.events)
    assert_single_terminal(log, "finished")


def test_infinite_repeats_until_stop() -> None:
    macro = Macro(steps=taps(["a"]), default_delay_ms=5, repeat_count=0)
    backend, log, player = start(macro)
    log.wait_for("repeat", count=5)
    player.stop()
    assert player.wait(1.0)
    assert all(e.total_repeats == 0 for e in log.events)
    assert len(backend.log("press")) >= 5
    ev = assert_single_terminal(log, "stopped")
    assert ev.message == DEFAULT_STOP_REASON


def test_loop_until_stopped_ignores_repeat_count() -> None:
    macro = Macro(steps=taps(["a"]), default_delay_ms=5, repeat_count=2)
    backend, log, player = start(macro, loop_until_stopped=True)
    log.wait_for("repeat", count=4)
    player.stop("fin")
    assert player.wait(1.0)
    assert log.of("repeat")[0].total_repeats == 0
    assert assert_single_terminal(log, "stopped").message == "fin"


# --------------------------------------------------------------- pasos activos
def test_disabled_steps_are_skipped_and_indices_refer_to_full_list() -> None:
    steps = taps(["a", "b", "c", "d"])
    steps[1].enabled = False
    steps[3].enabled = False  # el último activo es "c": sin espera tras él
    macro = Macro(steps=steps, default_delay_ms=30)
    backend, log, _ = play(macro)
    assert [c[1] for c in backend.log("press")] == [["a"], ["c"]]
    assert [e.step_index for e in log.of("step")] == [0, 2]
    assert ms(backend.times("release_all")[0] - backend.times("release")[-1]) < 20


@pytest.mark.parametrize("repeat_count, loop", [(1, False), (0, False), (3, True)])
def test_macro_without_enabled_steps_finishes_immediately(repeat_count: int, loop: bool) -> None:
    steps = taps(["a"])
    steps[0].enabled = False
    for macro in (Macro(steps=steps, repeat_count=repeat_count, start_delay_ms=2000),
                  Macro(steps=[], repeat_count=repeat_count)):
        t0 = time.perf_counter()
        backend, log, _ = play(macro, timeout=1.0, loop_until_stopped=loop)
        assert ms(time.perf_counter() - t0) < 200
        assert log.kinds() == ["finished"]
        assert log.events[0].message == NO_STEPS_MESSAGE
        assert backend.log() == [("release_all",)]


# ------------------------------------------------------------------- parada
def test_stop_during_long_wait_is_fast() -> None:
    macro = Macro(steps=[PressStep(inputs=["a"], hold_ms=0), WaitStep(ms=10_000),
                         PressStep(inputs=["b"])], default_delay_ms=0)
    backend, log, player = start(macro)
    log.wait_for("step", step_index=1)
    time.sleep(0.05)
    assert player.is_running and player.current_macro_id == macro.id
    t0 = time.perf_counter()
    player.stop()
    player.stop("otro motivo")  # idempotente: prevalece el primer motivo
    log.wait_for("stopped", timeout=1.0)
    assert ms(log.time_of("stopped") - t0) < 100
    assert player.wait(1.0)
    assert ms(time.perf_counter() - t0) < 100
    ev = assert_single_terminal(log, "stopped")
    assert ev.message == DEFAULT_STOP_REASON
    assert backend.log()[-1] == ("release_all",)
    assert ["b"] not in [c[1] for c in backend.log("press")]
    assert not player.is_running


def test_stop_during_hold_releases_everything() -> None:
    macro = Macro(steps=[PressStep(inputs=["w", "mouse_right"], hold_ms=5000)])
    backend, log, player = start(macro)
    log.wait_for("step")
    time.sleep(0.03)
    t0 = time.perf_counter()
    player.stop()
    assert player.wait(1.0)
    assert ms(time.perf_counter() - t0) < 100
    assert backend.log() == [("press", ["w", "mouse_right"]), ("release_all",)]
    assert_single_terminal(log, "stopped")


def test_stop_when_idle_is_noop() -> None:
    player = MacroPlayer(FakeBackend())
    player.stop()
    player.pause()
    player.resume()
    player.toggle_pause()
    assert not player.is_running and not player.is_paused
    assert player.wait(0.1)


def test_start_returns_false_while_running_and_macro_is_cloned() -> None:
    macro = Macro(steps=[WaitStep(ms=150), PressStep(inputs=["a"], hold_ms=0)],
                  default_delay_ms=0)
    backend, log, player = start(macro)
    assert player.start(macro, EventLog()) is False
    macro.steps.clear()  # las ediciones durante la ejecución no afectan
    macro.steps.append(PressStep(inputs=["z"]))
    assert player.wait(2.0)
    assert [c[1] for c in backend.log("press")] == [["a"]]
    assert_single_terminal(log, "finished")
    # Terminada, se puede volver a empezar.
    log2 = EventLog()
    assert player.start(Macro(steps=taps(["x"])), log2)
    assert player.wait(1.0)
    assert_single_terminal(log2, "finished")


def test_terminal_event_is_seen_as_not_running_and_can_chain_start() -> None:
    backend = FakeBackend()
    player = MacroPlayer(backend)
    seen: dict[str, Any] = {}
    second = EventLog()

    def on_event(event: PlayerEvent) -> None:
        if event.kind == "finished":
            seen["running"] = player.is_running
            seen["restarted"] = player.start(Macro(steps=taps(["b"])), second)

    assert player.start(Macro(steps=taps(["a"])), on_event)
    second.wait_for("finished")
    assert player.wait(1.0)
    assert seen == {"running": False, "restarted": True}
    assert [c[1] for c in backend.log("press")] == [["a"], ["b"]]


# ------------------------------------------------------------------- pausa
def test_pause_freezes_time() -> None:
    macro = Macro(steps=[PressStep(inputs=["a"], hold_ms=0), WaitStep(ms=200),
                         PressStep(inputs=["b"], hold_ms=0)], default_delay_ms=0)
    backend, log, player = start(macro)
    log.wait_for("step", step_index=1)
    time.sleep(0.05)
    t_pause = time.perf_counter()
    player.pause()
    assert player.is_paused
    log.wait_for("paused")
    time.sleep(0.3)
    assert backend.log("press") == [("press", ["a"])], "en pausa no debe avanzar"
    t_resume = time.perf_counter()
    player.toggle_pause()  # reanuda
    assert not player.is_paused
    log.wait_for("resumed")
    assert player.wait(2.0)

    ta, tb = backend.times("press")
    assert_ms(tb - ta, 200 + ms(t_resume - t_pause), early=10)
    kinds = log.kinds()
    assert kinds.index("paused") < kinds.index("resumed") < kinds.index("finished")
    assert log.of("paused")[0].step_index == 1
    assert_single_terminal(log, "finished")


def test_pause_releases_held_inputs_and_resume_restores_them() -> None:
    macro = Macro(steps=[PressStep(inputs=["shift", "mouse_left"], action="down"),
                         WaitStep(ms=300),
                         PressStep(inputs=["shift", "mouse_left"], action="up")],
                  default_delay_ms=0)
    backend, log, player = start(macro)
    log.wait_for("step", step_index=1)
    player.pause()
    log.wait_for("paused")
    assert backend.log()[-1] == ("release", ["mouse_left", "shift"])
    player.resume()
    assert player.wait(2.0)
    assert backend.log() == [
        ("press", ["shift", "mouse_left"]),
        ("release", ["mouse_left", "shift"]),  # al pausar
        ("press", ["shift", "mouse_left"]),  # al reanudar
        ("release", ["shift", "mouse_left"]),
        ("release_all",),
    ]


def test_stop_while_paused() -> None:
    macro = Macro(steps=[WaitStep(ms=1000)])
    backend, log, player = start(macro)
    log.wait_for("step")
    player.pause()
    log.wait_for("paused")
    t0 = time.perf_counter()
    player.stop()
    assert player.wait(1.0)
    assert ms(time.perf_counter() - t0) < 100
    assert "resumed" not in log.kinds()
    assert_single_terminal(log, "stopped")
    assert not player.is_paused


# ------------------------------------------------------------- cuenta atrás
def test_countdown_events_and_start_delay() -> None:
    macro = Macro(steps=taps(["a"]), start_delay_ms=250)
    t0 = time.perf_counter()
    backend, log, _ = play(macro)
    countdowns = log.of("countdown")
    assert [e.remaining_ms for e in countdowns] == pytest.approx([250, 150, 50], abs=20)
    assert log.kinds()[:len(countdowns) + 1] == ["countdown"] * len(countdowns) + ["started"]
    assert_ms(backend.times("press")[0] - t0, 250)


def test_pause_freezes_countdown() -> None:
    macro = Macro(steps=taps(["a"]), start_delay_ms=200)
    backend, log, player = start(macro)
    t0 = time.perf_counter()
    log.wait_for("countdown")
    time.sleep(0.05)
    t_pause = time.perf_counter()
    player.pause()
    log.wait_for("paused")
    time.sleep(0.2)
    t_resume = time.perf_counter()
    player.resume()
    assert player.wait(2.0)
    assert_ms(backend.times("press")[0] - t0, 200 + ms(t_resume - t_pause), early=10)


def test_stop_during_countdown() -> None:
    macro = Macro(steps=taps(["a"]), start_delay_ms=5000)
    backend, log, player = start(macro)
    log.wait_for("countdown")
    t0 = time.perf_counter()
    player.stop()
    assert player.wait(1.0)
    assert ms(time.perf_counter() - t0) < 100
    assert backend.log() == [("release_all",)]
    assert "started" not in log.kinds()
    assert_single_terminal(log, "stopped")


# ------------------------------------------------------------ humanización
def humanized(**kwargs: Any) -> model.Macro:
    """Macro humanizada; por defecto sólo con la variación que el test indique."""
    for name in ("jitter_ms", "hold_jitter_ms", "chord_stagger_ms"):
        kwargs.setdefault(name, 0)
    return Macro(humanize=True, **kwargs)


def test_jitter_is_normal_clipped_and_not_applied_after_last_step() -> None:
    rng = ScriptedRng([-80, 200])  # 200 se recorta a +80
    macro = humanized(steps=taps(["a", "b", "c"]), default_delay_ms=100, jitter_ms=80)
    backend, _, _ = play(macro, rng=rng)
    assert rng.calls == [("gauss", 0.0, 40.0)] * 2  # normal(0, jitter/2), nada tras el último
    ta, tb, tc = backend.times("press")
    assert_ms(tb - ta, 20)
    assert_ms(tc - tb, 180)


def test_jitter_is_clamped_to_zero() -> None:
    rng = ScriptedRng([-50])
    macro = humanized(steps=taps(["a", "b"]), default_delay_ms=10, jitter_ms=50)
    backend, _, _ = play(macro, rng=rng)
    ta, tb = backend.times("press")
    assert ms(tb - ta) < 15


def test_zero_bases_are_never_varied() -> None:
    rng = ScriptedRng([])  # cualquier llamada al rng fallaría
    macro = humanized(steps=[PressStep(inputs=["a"], hold_ms=0), TextStep(text="xy", char_interval_ms=0)],
                      default_delay_ms=0, jitter_ms=50, hold_jitter_ms=50)
    backend, log, _ = play(macro, rng=rng)
    assert_single_terminal(log, "finished")
    assert rng.calls == []


def test_humanize_off_is_exact_and_uses_no_randomness() -> None:
    rng = ScriptedRng([])
    macro = Macro(steps=[PressStep(inputs=["w", "mouse_right"], hold_ms=30), TextStep(text="ab", char_interval_ms=20)],
                  default_delay_ms=40, jitter_ms=80, hold_jitter_ms=50, chord_stagger_ms=30)
    backend, log, _ = play(macro, rng=rng)
    assert_single_terminal(log, "finished")
    assert rng.calls == []
    assert backend.log() == [("press", ["w", "mouse_right"]), ("release", ["mouse_right", "w"]),
                             ("type_char", "a"), ("type_char", "b"), ("release_all",)]
    t = backend.times()
    assert_ms(t[1] - t[0], 30)
    assert_ms(t[2] - t[1], 40)
    assert_ms(t[3] - t[2], 20)


def test_jitter_with_seeded_rng_stays_within_bounds() -> None:
    macro = humanized(steps=taps(list("abcdef")), default_delay_ms=60, jitter_ms=30)
    backend, _, _ = play(macro, rng=random.Random(1234))
    expected_rng = random.Random(1234)
    expected = [round(60 + max(-30.0, min(30.0, expected_rng.gauss(0.0, 15.0)))) for _ in range(5)]
    t = backend.times("press")
    for i, exp in enumerate(expected):
        gap = t[i + 1] - t[i]
        assert 30 - EARLY_MS <= ms(gap) <= 90 + LATE_MS
        assert_ms(gap, exp)


def test_repeat_delay_is_humanized_too() -> None:
    rng = ScriptedRng([4, 8])  # retardo del paso 20+4, pausa entre repeticiones 60+8
    macro = humanized(steps=taps(["a"]), repeat_count=2, default_delay_ms=20, repeat_delay_ms=60,
                      jitter_ms=10)
    backend, _, _ = play(macro, rng=rng)
    assert rng.calls == [("gauss", 0.0, 5.0)] * 2
    ta, tb = backend.times("press")
    assert_ms(tb - ta, 24 + 68)


def test_chord_stagger_presses_in_order_and_releases_in_reverse() -> None:
    rng = ScriptedRng([5, 0, 3, 6])  # desfases: 2 al pulsar y 2 (independientes) al soltar
    macro = humanized(steps=[PressStep(inputs=["w", "mouse_right", "q"], hold_ms=40)], chord_stagger_ms=8)
    backend, _, _ = play(macro, rng=rng)
    assert rng.calls == [("randint", 0, 8)] * 4
    assert backend.log() == [
        ("press", ["w"]), ("press", ["mouse_right"]), ("press", ["q"]),
        ("release", ["q"]), ("release", ["mouse_right"]), ("release", ["w"]),
        ("release_all",),
    ]
    t = backend.times()
    for i, expected in enumerate([5, 0, 40, 3, 6]):
        assert_ms(t[i + 1] - t[i], expected)


def test_chord_stagger_applies_to_down_and_up_actions() -> None:
    rng = ScriptedRng([2, 4])
    macro = humanized(default_delay_ms=0, chord_stagger_ms=8, steps=[
        PressStep(inputs=["ctrl", "shift"], action="down"),
        PressStep(inputs=["shift", "ctrl"], action="up"),
    ])
    backend, _, _ = play(macro, rng=rng)
    assert backend.log() == [("press", ["ctrl"]), ("press", ["shift"]),
                             ("release", ["shift"]), ("release", ["ctrl"]), ("release_all",)]


def test_hold_jitter_varies_each_hold_with_floor_of_one_ms() -> None:
    rng = ScriptedRng([4, -100, 2])  # 20+4, 20-10 (recortado), 20+2
    macro = humanized(steps=[PressStep(inputs=["mouse_left"], hold_ms=20, count=2)], hold_jitter_ms=10,
                      chord_stagger_ms=8)  # una sola entrada: sin desfase
    backend, _, _ = play(macro, rng=rng)
    assert rng.calls == [("gauss", 0.0, 5.0)] * 3
    t = backend.times("press", "release")
    for i, expected in enumerate([24, 10, 22]):
        assert_ms(t[i + 1] - t[i], expected)

    rng = ScriptedRng([-10])  # 5 - 10 → nunca menos de 1 ms
    backend, _, _ = play(humanized(steps=[PressStep(inputs=["a"], hold_ms=5)], hold_jitter_ms=10), rng=rng)
    press_t, release_t = backend.times("press", "release")
    assert ms(release_t - press_t) < 15


def test_text_interval_is_humanized_with_hold_jitter() -> None:
    rng = ScriptedRng([10, -10])
    macro = humanized(steps=[TextStep(text="abc", char_interval_ms=30)], hold_jitter_ms=10)
    backend, _, _ = play(macro, rng=rng)
    t = backend.times("type_char")
    assert_ms(t[1] - t[0], 40)
    assert_ms(t[2] - t[1], 20)


def test_step_delay_override_and_wait_step_random_extra() -> None:
    rng = ScriptedRng([37])
    macro = Macro(default_delay_ms=500, steps=[
        PressStep(inputs=["a"], hold_ms=0, delay_after_ms=20),
        WaitStep(ms=50, random_extra_ms=100),  # delay_after None → 0 en esperas
        PressStep(inputs=["b"], hold_ms=0),
    ])
    backend, _, _ = play(macro, rng=rng)
    assert rng.calls == [("randint", 0, 100)]
    ta, tb = backend.times("press")
    assert_ms(tb - ta, 20 + 50 + 37)


# ---------------------------------------------------------------- failsafe
@pytest.mark.parametrize("cursor", [(0, 0), (1919, 0), (0, 1079), (1919, 1079),
                                    (2, 2), (1917, 1077)])
def test_failsafe_in_corner_stops(cursor: tuple[int, int]) -> None:
    backend, log, _ = play(Macro(steps=taps(["a"])), FakeBackend(cursor=cursor), failsafe=True)
    ev = assert_single_terminal(log, "stopped")
    assert ev.message == FAILSAFE_MESSAGE
    assert backend.log() == [("release_all",)]


@pytest.mark.parametrize("cursor, failsafe", [((3, 0), True), ((960, 540), True),
                                              ((0, 0), False)])
def test_failsafe_not_triggered(cursor: tuple[int, int], failsafe: bool) -> None:
    backend, log, _ = play(Macro(steps=taps(["a"])), FakeBackend(cursor=cursor),
                           failsafe=failsafe)
    assert_single_terminal(log, "finished")
    assert backend.log("press") == [("press", ["a"])]


def test_failsafe_ignores_corner_set_by_the_engine() -> None:
    macro = Macro(default_delay_ms=0, steps=[MoveStep(x=0, y=0), *taps(["a", "b"]),
                                             PressStep(inputs=["mouse_left"], x=1919, y=0),
                                             *taps(["c"])])
    backend, log, _ = play(macro, failsafe=True)
    assert_single_terminal(log, "finished")
    assert [c[1] for c in backend.log("press")] == [["a"], ["b"], ["mouse_left"], ["c"]]


def test_failsafe_when_user_moves_to_corner_mid_macro() -> None:
    backend = FakeBackend()
    macro = Macro(default_delay_ms=0, steps=[PressStep(inputs=["a"], hold_ms=0),
                                             WaitStep(ms=150), PressStep(inputs=["b"])])
    _, log, player = start(macro, backend, failsafe=True)
    log.wait_for("step", step_index=1)
    backend.cursor = (1919, 1079)
    assert player.wait(2.0)
    assert assert_single_terminal(log, "stopped").message == FAILSAFE_MESSAGE
    assert [e.step_index for e in log.of("step")] == [0, 1]
    assert backend.log("press") == [("press", ["a"])]


# ------------------------------------------------------------------ errores
def test_backend_exception_emits_error_and_releases() -> None:
    macro = Macro(steps=taps(["a", "b"]), default_delay_ms=0)
    backend, log, player = play(macro, FakeBackend(fail_on="press"))
    ev = assert_single_terminal(log, "error")
    assert "paso 1" in ev.message and "fallo simulado" in ev.message
    assert backend.log() == [("press", ["a"]), ("release_all",)]
    assert not player.is_running


def test_release_all_failure_becomes_error() -> None:
    backend, log, _ = play(Macro(steps=taps(["a"])), FakeBackend(fail_on="release_all"))
    ev = assert_single_terminal(log, "error")
    assert "soltar" in ev.message


def test_failing_event_receiver_does_not_break_playback() -> None:
    backend = FakeBackend()
    player = MacroPlayer(backend)
    kinds: list[str] = []

    def on_event(event: PlayerEvent) -> None:
        kinds.append(event.kind)
        raise ValueError("receptor roto")

    assert player.start(Macro(steps=taps(["a", "b"]), default_delay_ms=0), on_event)
    assert player.wait(1.0)
    assert kinds[-1] == "finished"
    assert len(backend.log("press")) == 2


# ------------------------------------------------------------ tipos de paso
def test_press_step_count_hold_and_position() -> None:
    macro = Macro(steps=[PressStep(inputs=["mouse_left"], hold_ms=20, count=2, x=10, y=20)])
    backend, _, _ = play(macro)
    assert backend.log() == [
        ("move_to", 10, 20),
        ("press", ["mouse_left"]), ("release", ["mouse_left"]),
        ("press", ["mouse_left"]), ("release", ["mouse_left"]),
        ("release_all",),
    ]
    t = backend.times("press", "release")
    for i in range(3):
        assert_ms(t[i + 1] - t[i], 20)


def test_press_down_and_up_actions() -> None:
    macro = Macro(default_delay_ms=0, steps=[
        PressStep(inputs=["ctrl", "shift"], action="down"),
        PressStep(inputs=["s"]),
        PressStep(inputs=["shift", "ctrl"], action="up"),
        PressStep(inputs=[]),  # vacío: no hace nada
    ])
    backend, _, _ = play(macro)
    assert backend.log() == [
        ("press", ["ctrl", "shift"]),
        ("press", ["s"]), ("release", ["s"]),
        ("release", ["shift", "ctrl"]),
        ("release_all",),
    ]


def test_text_step() -> None:
    macro = Macro(steps=[TextStep(text="a\r\nb\tñ\U0001F600", char_interval_ms=10)])
    backend, _, _ = play(macro)
    assert backend.log() == [
        ("type_char", "a"),
        ("press", ["enter"]), ("release", ["enter"]),
        ("type_char", "b"),
        ("press", ["tab"]), ("release", ["tab"]),
        ("type_char", "ñ"),
        ("type_char", "\U0001F600"),  # fuera del BMP: un único carácter
        ("release_all",),
    ]
    t = backend.times("type_char")
    assert_ms(t[-1] - t[0], 50)  # 5 intervalos entre 6 caracteres


def test_move_step_absolute_instant() -> None:
    backend, _, _ = play(Macro(steps=[MoveStep(x=300, y=200)]))
    assert backend.log() == [("move_to", 300, 200), ("release_all",)]


def test_move_step_absolute_smooth() -> None:
    backend = FakeBackend(cursor=(100, 100))
    macro = Macro(steps=[MoveStep(x=300, y=200, duration_ms=100)])
    _, log, _ = play(macro, backend)
    moves = backend.log("move_to")
    assert 5 <= len(moves) <= 10
    assert moves[-1] == ("move_to", 300, 200)
    xs = [m[1] for m in moves]
    ys = [m[2] for m in moves]
    assert xs == sorted(xs) and ys == sorted(ys)
    assert all(100 <= x <= 300 for x in xs)
    # Easing: los primeros subpasos avanzan menos que los centrales.
    assert xs[0] - 100 < xs[len(xs) // 2] - xs[len(xs) // 2 - 1]
    t = backend.times("move_to")
    assert_ms(t[-1] - log.time_of("step"), 100)


def test_move_step_relative_instant_and_smooth_sum_exact() -> None:
    macro = Macro(default_delay_ms=0, steps=[
        MoveStep(x=15, y=-7, relative=True),
        MoveStep(x=37, y=-23, relative=True, duration_ms=80),
    ])
    backend, log, _ = play(macro)
    rel = backend.log("move_rel")
    assert rel[0] == ("move_rel", 15, -7)
    smooth = rel[1:]
    assert 2 <= len(smooth) <= 8
    assert sum(m[1] for m in smooth) == 37
    assert sum(m[2] for m in smooth) == -23
    assert all(m[1] or m[2] for m in smooth)
    t = backend.times("move_rel")
    assert_ms(t[-1] - log.time_of("step", 1), 80)


def test_scroll_step() -> None:
    macro = Macro(default_delay_ms=0, steps=[
        ScrollStep(amount=-3, x=10, y=20),
        ScrollStep(amount=2, horizontal=True),
    ])
    backend, _, _ = play(macro)
    assert backend.log() == [
        ("move_to", 10, 20), ("scroll", -3, False), ("scroll", 2, True), ("release_all",),
    ]


# ------------------------------------------------------------------ backend
def test_backend_is_created_lazily_from_winput(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[FakeBackend] = []

    class LazyBackend(FakeBackend):
        def __init__(self) -> None:
            super().__init__()
            created.append(self)

    fake = types.ModuleType("macrotool.winput")
    fake.WinInputBackend = LazyBackend  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "macrotool.winput", fake)
    monkeypatch.setattr(macrotool, "winput", fake, raising=False)

    player = MacroPlayer()
    assert created == []
    for _ in range(2):
        log = EventLog()
        assert player.start(Macro(steps=taps(["a"])), log)
        assert player.wait(1.0)
        assert_single_terminal(log, "finished")
    assert len(created) == 1
    assert created[0].log("press") == [("press", ["a"])] * 2


def test_backend_import_failure_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "macrotool.winput", None)
    player = MacroPlayer()
    log = EventLog()
    assert player.start(Macro(steps=taps(["a"])), log)
    assert player.wait(1.0)
    ev = assert_single_terminal(log, "error")
    assert ev.message.startswith("No se pudo preparar")


def test_set_backend_applies_to_next_run() -> None:
    first, second = FakeBackend(), FakeBackend()
    player = MacroPlayer(first)
    player.start(Macro(steps=[WaitStep(ms=100), *taps(["a"])], default_delay_ms=0), EventLog())
    player.set_backend(second)  # la ejecución en curso conserva su backend
    assert player.wait(1.0)
    player.start(Macro(steps=taps(["b"])), EventLog())
    assert player.wait(1.0)
    assert first.log("press") == [("press", ["a"])]
    assert second.log("press") == [("press", ["b"])]


def test_set_timer_resolution_tolerates_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    broken = types.ModuleType("macrotool.winput")

    def boom(enable: bool) -> None:
        raise OSError("sin winmm")

    broken.set_timer_resolution = boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "macrotool.winput", broken)
    monkeypatch.setattr(macrotool, "winput", broken, raising=False)
    _ORIGINAL_SET_TIMER_RESOLUTION(True)  # no lanza
    monkeypatch.setitem(sys.modules, "macrotool.winput", None)
    monkeypatch.delattr(macrotool, "winput", raising=False)
    _ORIGINAL_SET_TIMER_RESOLUTION(False)  # tampoco si winput no se puede importar


def test_running_macros_are_stopped_at_interpreter_exit() -> None:
    macro = Macro(steps=[PressStep(inputs=["w"], action="down"), WaitStep(ms=10_000)],
                  default_delay_ms=0)
    backend, log, player = start(macro)
    log.wait_for("step", step_index=1)
    engine._stop_players_at_exit()  # lo que haría atexit
    assert not player.is_running
    assert assert_single_terminal(log, "stopped").message == engine.EXIT_STOP_REASON
    assert backend.log()[-1] == ("release_all",)


def test_thread_start_failure_leaves_player_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    player = MacroPlayer(FakeBackend())

    def boom(self: threading.Thread) -> None:
        raise RuntimeError("sin hilos")

    with monkeypatch.context() as m:
        m.setattr(engine.threading.Thread, "start", boom)
        with pytest.raises(RuntimeError):
            player.start(Macro(steps=taps(["a"])), EventLog())
    assert not player.is_running
    log = EventLog()
    assert player.start(Macro(steps=taps(["a"])), log)
    assert player.wait(1.0)
    assert_single_terminal(log, "finished")


def test_player_event_defaults_and_terminal_flag() -> None:
    ev = PlayerEvent("step")
    assert (ev.macro_id, ev.step_index, ev.repeat_index, ev.total_repeats,
            ev.remaining_ms, ev.message) == ("", -1, 0, 1, 0, "")
    assert not ev.is_terminal
    assert all(PlayerEvent(k).is_terminal for k in ("finished", "stopped", "error"))


# ------------------------------------------------------ sueltas rechazadas por Windows
class _BlockableSendInput:
    """Sustituto de user32.SendInput: registra los lotes y puede rechazarlos (UIPI, UAC...)."""

    def __init__(self) -> None:
        self.blocked = False
        self.batches: list[list[tuple[int, int]]] = []
        self._lock = threading.Lock()

    def __call__(self, n, array, size):
        with self._lock:
            self.batches.append([(array[i].ki.wScan, array[i].ki.dwFlags) for i in range(n)])
        return 0 if self.blocked else n


def test_rejected_release_is_reported_and_retried_later(monkeypatch: pytest.MonkeyPatch) -> None:
    """Si Windows rechaza soltar (ventana elevada, escritorio seguro), las teclas no se olvidan:
    el evento terminal lo avisa y ``retry_pending_release`` las suelta cuando vuelve a poder."""
    from macrotool import keys, winput

    fake = _BlockableSendInput()
    monkeypatch.setattr(winput, "_SendInput", fake)
    backend = winput.WinInputBackend()
    macro = Macro(steps=[PressStep(inputs=["shift", "w"], action="down"), WaitStep(ms=150),
                         PressStep(inputs=["shift", "w"], action="up")], default_delay_ms=0)
    player = MacroPlayer(backend)
    log = EventLog()
    assert player.start(macro, log)
    log.wait_for("step", step_index=1)
    fake.blocked = True  # p. ej. Alt+Tab al Administrador de tareas (elevado)
    assert player.wait(3.0)
    ev = assert_single_terminal(log, "error")
    assert "Error en el paso 3" in ev.message
    assert "no dejó soltar" in ev.message and "W" in ev.message and "Mayús" in ev.message
    assert sorted(player.pending_release) == ["lshift", "w"]
    assert player.retry_pending_release() is False  # sigue bloqueado: siguen pendientes
    fake.blocked = False
    n = len(fake.batches)
    assert player.retry_pending_release() is True
    assert player.pending_release == []
    ups = [item for batch in fake.batches[n:] for item in batch]
    assert sorted(scan for scan, _ in ups) == sorted([keys.scan_code("lshift"), keys.scan_code("w")])
    assert all(flags & winput.KEYEVENTF_KEYUP for _, flags in ups)


def test_stop_while_blocked_warns_instead_of_silent_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    from macrotool import winput

    fake = _BlockableSendInput()
    monkeypatch.setattr(winput, "_SendInput", fake)
    player = MacroPlayer(winput.WinInputBackend())
    log = EventLog()
    macro = Macro(steps=[PressStep(inputs=["mouse_left"], action="down"), WaitStep(ms=5000)])
    assert player.start(macro, log)
    log.wait_for("step", step_index=1)
    fake.blocked = True
    player.stop()
    assert player.wait(3.0)
    ev = assert_single_terminal(log, "error")  # antes: "stopped" sin ningún aviso
    assert "Clic izquierdo" in ev.message
    assert player.pending_release == ["mouse_left"]
    fake.blocked = False
    assert player.retry_pending_release() is True and player.pending_release == []
