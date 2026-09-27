"""Tests del controlador de disparadores (once / toggle / hold) sin entrada real."""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from macrotool import engine  # noqa: E402
from macrotool.controller import (  # noqa: E402
    HOLD_RELEASE_REASON,
    SWITCH_REASON,
    MacroController,
)
from macrotool.engine import DEFAULT_STOP_REASON, MacroPlayer, PlayerEvent  # noqa: E402
from macrotool.model import Macro, PressStep, WaitStep  # noqa: E402


# --------------------------------------------------------------------- dobles
class FakeBackend:
    """Backend que sólo registra las llamadas (nunca inyecta nada)."""

    def __init__(self) -> None:
        self.calls: list[tuple[float, str, tuple[Any, ...]]] = []
        self._lock = threading.Lock()

    def _record(self, name: str, *args: Any) -> None:
        with self._lock:
            self.calls.append((time.perf_counter(), name, args))

    def press(self, tokens: list[str]) -> None:
        self._record("press", list(tokens))

    def release(self, tokens: list[str]) -> None:
        self._record("release", list(tokens))

    def type_char(self, ch: str) -> None:
        self._record("type_char", ch)

    def move_to(self, x: int, y: int) -> None:
        self._record("move_to", x, y)

    def move_rel(self, dx: int, dy: int) -> None:
        self._record("move_rel", dx, dy)

    def scroll(self, notches: int, horizontal: bool = False) -> None:
        self._record("scroll", notches, horizontal)

    def cursor_pos(self) -> tuple[int, int]:
        return (500, 400)

    def screen_size(self) -> tuple[int, int]:
        return (1920, 1080)

    def release_all(self) -> None:
        self._record("release_all")

    def presses(self) -> list[list[str]]:
        with self._lock:
            return [args[0] for _, name, args in self.calls if name == "press"]


class EventLog:
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

    def of(self, kind: str, macro_id: Optional[str] = None) -> list[PlayerEvent]:
        return [e for e in self.events
                if e.kind == kind and (macro_id is None or e.macro_id == macro_id)]

    def wait_for(self, kind: str, *, macro_id: Optional[str] = None, count: int = 1,
                 timeout: float = 3.0) -> PlayerEvent:
        def matches() -> list[PlayerEvent]:
            return [e for _, e in self.items
                    if e.kind == kind and (macro_id is None or e.macro_id == macro_id)]

        with self._cond:
            ok = self._cond.wait_for(lambda: len(matches()) >= count, timeout)
            assert ok, f"no llegó {kind!r} (recibidos: {[e.kind for _, e in self.items]})"
            return matches()[count - 1]


class Harness:
    """Controlador real + reproductor real + backend falso."""

    def __init__(self, *macros: Macro, failsafe: bool = False) -> None:
        self.macros = {m.id: m for m in macros}
        self.backend = FakeBackend()
        self.player = MacroPlayer(self.backend)
        self.log = EventLog()
        self.ctl = MacroController(self.player, self.macros.get, self.log,
                                   failsafe=lambda: failsafe)

    def finish(self, timeout: float = 2.0) -> None:
        assert self.player.wait(timeout)


class FakePlayer:
    """Reproductor falso: permite entregar eventos a mano y simular carreras."""

    def __init__(self) -> None:
        self.starts: list[SimpleNamespace] = []
        self.stops: list[str] = []
        self.waits: list[Optional[float]] = []
        self.running = False
        self.accept = True
        self.wait_result = True
        self.finish_synchronously = False
        self.toggles = 0

    @property
    def is_running(self) -> bool:
        return self.running

    def start(self, macro: Macro, on_event: Any, *, failsafe: bool = False,
              loop_until_stopped: bool = False) -> bool:
        if not self.accept:
            return False
        run = SimpleNamespace(macro=macro, on_event=on_event, failsafe=failsafe,
                              loop=loop_until_stopped)
        self.starts.append(run)
        if self.finish_synchronously:  # el hilo termina antes de que start() vuelva
            on_event(PlayerEvent("finished", macro_id=macro.id))
            return True
        self.running = True
        return True

    def stop(self, reason: str = DEFAULT_STOP_REASON) -> None:
        self.stops.append(reason)

    def wait(self, timeout: Optional[float] = None) -> bool:
        self.waits.append(timeout)
        if self.wait_result:
            self.running = False
        return self.wait_result

    def toggle_pause(self) -> None:
        self.toggles += 1

    def deliver(self, index: int, kind: str) -> None:
        run = self.starts[index]
        run.on_event(PlayerEvent(kind, macro_id=run.macro.id))


@pytest.fixture(autouse=True)
def no_timer_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine, "_set_timer_resolution", lambda enable: None)


def long_macro(name: str, mode: str = "once", **kwargs: Any) -> Macro:
    """Pulsa ``name`` y luego espera mucho (se detiene a mano)."""
    return Macro(name=name, trigger_mode=mode, default_delay_ms=0, steps=[
        PressStep(inputs=[name], hold_ms=0), WaitStep(ms=10_000)], **kwargs)


def short_macro(name: str, mode: str = "once", **kwargs: Any) -> Macro:
    return Macro(name=name, trigger_mode=mode, default_delay_ms=0,
                 steps=[PressStep(inputs=[name], hold_ms=0)], **kwargs)


# ----------------------------------------------------------------------- once
def test_once_starts_and_ignores_retrigger_while_running() -> None:
    macro = Macro(name="a", trigger_mode="once", default_delay_ms=0, steps=[
        PressStep(inputs=["a"], hold_ms=0), WaitStep(ms=200), PressStep(inputs=["b"], hold_ms=0)])
    h = Harness(macro)
    h.ctl.handle_trigger(macro.id, True)
    assert h.ctl.running_macro_id == macro.id and h.ctl.is_running
    h.log.wait_for("step")
    h.ctl.handle_trigger(macro.id, False)  # soltar: nada
    h.ctl.handle_trigger(macro.id, True)  # re-disparo: se ignora
    h.ctl.handle_trigger(macro.id, False)
    h.log.wait_for("finished")
    h.finish()
    assert len(h.log.of("started")) == 1
    assert h.backend.presses() == [["a"], ["b"]]
    assert h.log.of("stopped") == []
    assert h.ctl.running_macro_id is None and not h.ctl.is_running


def test_once_can_run_again_after_finishing() -> None:
    macro = short_macro("a")
    h = Harness(macro)
    for _ in range(3):
        h.ctl.handle_trigger(macro.id, True)
        h.finish()
        assert h.ctl.running_macro_id is None
    assert h.backend.presses() == [["a"]] * 3


def test_once_switches_from_other_macro() -> None:
    first, second = long_macro("x"), short_macro("y")
    h = Harness(first, second)
    h.ctl.handle_trigger(first.id, True)
    h.log.wait_for("step", macro_id=first.id)
    t0 = time.perf_counter()
    h.ctl.handle_trigger(second.id, True)
    assert (time.perf_counter() - t0) < 0.2
    stopped = h.log.wait_for("stopped", macro_id=first.id)
    assert stopped.message == SWITCH_REASON
    h.log.wait_for("finished", macro_id=second.id)
    h.finish()
    kinds = [(e.kind, e.macro_id) for e in h.log.events]
    assert kinds.index(("stopped", first.id)) < kinds.index(("started", second.id))
    assert h.backend.presses() == [["x"], ["y"]]
    assert h.ctl.running_macro_id is None


# --------------------------------------------------------------------- toggle
def test_toggle_starts_and_stops() -> None:
    macro = long_macro("t", mode="toggle")
    h = Harness(macro)
    h.ctl.handle_trigger(macro.id, True)
    h.ctl.handle_trigger(macro.id, False)
    h.log.wait_for("step")
    assert h.ctl.running_macro_id == macro.id
    t0 = time.perf_counter()
    h.ctl.handle_trigger(macro.id, True)
    h.log.wait_for("stopped")
    assert (time.perf_counter() - t0) < 0.1
    h.finish()
    assert h.log.of("stopped")[0].message == DEFAULT_STOP_REASON
    assert h.ctl.running_macro_id is None


def test_toggle_quick_third_press_restarts() -> None:
    macro = long_macro("t", mode="toggle")
    h = Harness(macro)
    h.ctl.handle_trigger(macro.id, True)  # empieza
    h.ctl.handle_trigger(macro.id, True)  # pide parar
    h.ctl.handle_trigger(macro.id, True)  # antes del evento terminal: vuelve a empezar
    h.log.wait_for("started", count=2)
    assert h.ctl.running_macro_id == macro.id
    assert h.player.is_running
    h.ctl.handle_trigger(macro.id, True)
    h.finish()
    assert len(h.log.of("stopped")) == 2
    assert h.ctl.running_macro_id is None


def test_toggle_switches_from_other_macro() -> None:
    other, macro = long_macro("o"), long_macro("t", mode="toggle")
    h = Harness(other, macro)
    h.ctl.run(other.id)
    h.ctl.handle_trigger(macro.id, True)
    assert h.ctl.running_macro_id == macro.id
    h.log.wait_for("step", macro_id=macro.id)
    assert h.log.of("stopped", other.id)[0].message == SWITCH_REASON
    h.ctl.stop()
    h.finish()


# ----------------------------------------------------------------------- hold
def test_hold_repeats_until_release() -> None:
    macro = Macro(name="h", trigger_mode="hold", repeat_count=1, default_delay_ms=15,
                  steps=[PressStep(inputs=["h"], hold_ms=0)])
    h = Harness(macro)
    h.ctl.handle_trigger(macro.id, True)
    h.log.wait_for("repeat", count=4)
    h.ctl.handle_trigger(macro.id, True)  # autorepetición: se ignora
    assert len(h.log.of("started")) == 1
    t0 = time.perf_counter()
    h.ctl.handle_trigger(macro.id, False)
    stopped = h.log.wait_for("stopped")
    assert (time.perf_counter() - t0) < 0.1
    assert stopped.message == HOLD_RELEASE_REASON
    h.finish()
    presses = len(h.backend.presses())
    assert presses >= 4
    assert all(e.total_repeats == 0 for e in h.log.of("repeat"))
    time.sleep(0.05)
    assert len(h.backend.presses()) == presses, "no debe pulsar nada tras soltar"
    assert h.ctl.running_macro_id is None


def test_hold_release_stops_even_if_macro_was_disabled_or_deleted() -> None:
    macro = long_macro("h", mode="hold")
    h = Harness(macro)
    h.ctl.handle_trigger(macro.id, True)
    h.log.wait_for("step")
    macro.enabled = False
    del h.macros[macro.id]
    h.ctl.handle_trigger(macro.id, False)
    h.finish()
    assert h.log.of("stopped")[0].message == HOLD_RELEASE_REASON


def test_release_of_other_trigger_or_non_hold_run_is_ignored() -> None:
    held, other = long_macro("h", mode="hold"), long_macro("o", mode="hold")
    h = Harness(held, other)
    h.ctl.handle_trigger(held.id, True)
    h.ctl.handle_trigger(other.id, False)  # soltar otra tecla: nada
    time.sleep(0.05)
    assert h.ctl.running_macro_id == held.id and h.player.is_running
    h.ctl.stop()
    h.finish()
    # Macro "hold" ejecutada a mano (como once): soltar su disparador no la para.
    h.ctl.run(held.id)
    h.ctl.handle_trigger(held.id, False)
    time.sleep(0.05)
    assert h.player.is_running
    h.ctl.stop()
    h.finish()


def test_hold_quick_repress_after_release_restarts() -> None:
    macro = long_macro("h", mode="hold")
    h = Harness(macro)
    h.ctl.handle_trigger(macro.id, True)
    h.ctl.handle_trigger(macro.id, False)
    h.ctl.handle_trigger(macro.id, True)  # antes del evento terminal de la primera
    h.log.wait_for("started", count=2)
    assert h.ctl.running_macro_id == macro.id and h.player.is_running
    h.ctl.handle_trigger(macro.id, False)
    h.finish()
    assert h.ctl.running_macro_id is None


# ------------------------------------------------------------ casos ignorados
def test_unknown_or_disabled_macro_is_ignored() -> None:
    disabled = short_macro("d", enabled=False)
    h = Harness(disabled)
    h.ctl.handle_trigger("no-existe", True)
    h.ctl.handle_trigger("no-existe", False)
    h.ctl.handle_trigger(disabled.id, True)
    time.sleep(0.05)
    assert not h.player.is_running and h.ctl.running_macro_id is None
    assert h.log.events == []
    assert h.ctl.run("no-existe") is False


# ------------------------------------------------------------ ejecución manual
def test_run_toggle_run_stop_and_pause() -> None:
    macro = long_macro("m", enabled=False)  # a mano funciona aunque el disparador esté desactivado
    h = Harness(macro)
    assert h.ctl.run(macro.id) is True
    assert h.ctl.run(macro.id) is False  # ya en marcha
    h.log.wait_for("step")
    h.ctl.toggle_pause()
    h.log.wait_for("paused")
    assert h.player.is_paused
    h.ctl.toggle_pause()
    h.log.wait_for("resumed")
    h.ctl.toggle_run(macro.id)  # corre → detener
    h.finish()
    assert h.log.of("stopped")[0].message == DEFAULT_STOP_REASON
    assert h.ctl.running_macro_id is None
    h.ctl.toggle_run(macro.id)  # parada → ejecutar
    assert h.ctl.running_macro_id == macro.id
    h.ctl.stop("Motivo propio")
    h.finish()
    assert h.log.of("stopped")[1].message == "Motivo propio"
    assert not h.ctl.is_running


def test_running_macro_id_cleared_when_macro_ends_by_itself() -> None:
    macro = short_macro("s")
    player = MacroPlayer(FakeBackend())
    seen: list[Optional[str]] = []

    def on_event(event: PlayerEvent) -> None:
        if event.is_terminal:
            seen.append(ctl.running_macro_id)

    ctl = MacroController(player, {macro.id: macro}.get, on_event)
    assert ctl.run(macro.id)
    assert player.wait(2.0)
    assert seen == [None], "al recibir el evento terminal el estado ya está limpio"
    assert ctl.running_macro_id is None and not ctl.is_running


def test_failsafe_flag_is_passed_to_player() -> None:
    macro = short_macro("f")
    h = Harness(macro, failsafe=True)  # el cursor falso no está en una esquina
    h.ctl.run(macro.id)
    h.finish()
    assert h.log.of("finished")

    fake = FakePlayer()
    ctl = MacroController(fake, {macro.id: macro}.get, lambda ev: None,
                          failsafe=lambda: True)
    ctl.run(macro.id)
    assert fake.starts[-1].failsafe is True and fake.starts[-1].loop is False

    def broken() -> bool:
        raise RuntimeError("ajustes rotos")

    fake2 = FakePlayer()
    ctl2 = MacroController(fake2, {macro.id: macro}.get, lambda ev: None, failsafe=broken)
    ctl2.run(macro.id)
    assert fake2.starts[-1].failsafe is False


def test_hold_mode_passes_loop_until_stopped() -> None:
    hold, once, toggle = (short_macro("h", mode="hold"), short_macro("o"),
                          short_macro("t", mode="toggle"))
    macros = {m.id: m for m in (hold, once, toggle)}
    fake = FakePlayer()
    ctl = MacroController(fake, macros.get, lambda ev: None)
    for macro in (hold, once, toggle):
        ctl.handle_trigger(macro.id, True)
    assert [s.loop for s in fake.starts] == [True, False, False]


# ----------------------------------------------------- carreras (FakePlayer)
def test_stale_terminal_event_does_not_clear_new_run() -> None:
    a, b = short_macro("a"), short_macro("b")
    macros = {m.id: m for m in (a, b)}
    events: list[PlayerEvent] = []
    fake = FakePlayer()
    ctl = MacroController(fake, macros.get, events.append)

    ctl.handle_trigger(a.id, True)
    # El hilo de "a" ya acabó (is_running False) pero su evento terminal aún no llegó.
    fake.running = False
    ctl.handle_trigger(b.id, True)
    assert ctl.running_macro_id == b.id
    fake.deliver(0, "finished")  # evento terminal tardío de la ejecución anterior
    assert ctl.running_macro_id == b.id
    assert [e.macro_id for e in events] == [a.id], "el evento se reenvía igualmente"
    fake.deliver(1, "stopped")
    assert ctl.running_macro_id is None


def test_terminal_event_before_start_returns() -> None:
    macro = short_macro("s")
    fake = FakePlayer()
    fake.finish_synchronously = True
    ctl = MacroController(fake, {macro.id: macro}.get, lambda ev: None)
    assert ctl.run(macro.id) is True
    assert ctl.running_macro_id is None


def test_non_terminal_events_keep_state() -> None:
    macro = short_macro("s")
    fake = FakePlayer()
    ctl = MacroController(fake, {macro.id: macro}.get, lambda ev: None)
    ctl.run(macro.id)
    for kind in ("countdown", "started", "repeat", "step", "paused", "resumed"):
        fake.deliver(0, kind)
        assert ctl.running_macro_id == macro.id
    fake.deliver(0, "error")
    assert ctl.running_macro_id is None


def test_switch_stops_and_waits_before_starting() -> None:
    a, b = short_macro("a"), short_macro("b")
    fake = FakePlayer()
    ctl = MacroController(fake, {m.id: m for m in (a, b)}.get, lambda ev: None)
    ctl.handle_trigger(a.id, True)
    ctl.handle_trigger(b.id, True)
    assert fake.stops == [SWITCH_REASON]
    assert fake.waits == [MacroController.SWITCH_TIMEOUT_S]
    assert [s.macro.id for s in fake.starts] == [a.id, b.id]
    assert ctl.running_macro_id == b.id


def test_switch_aborted_if_previous_does_not_stop() -> None:
    a, b = short_macro("a"), short_macro("b")
    fake = FakePlayer()
    ctl = MacroController(fake, {m.id: m for m in (a, b)}.get, lambda ev: None)
    ctl.handle_trigger(a.id, True)
    fake.wait_result = False  # el hilo anterior no termina a tiempo
    ctl.handle_trigger(b.id, True)
    assert [s.macro.id for s in fake.starts] == [a.id]
    assert ctl.running_macro_id == a.id


def test_failed_start_leaves_controller_idle() -> None:
    macro = short_macro("s")
    fake = FakePlayer()
    fake.accept = False
    ctl = MacroController(fake, {macro.id: macro}.get, lambda ev: None)
    assert ctl.run(macro.id) is False
    ctl.handle_trigger(macro.id, True)
    assert ctl.running_macro_id is None and not ctl.is_running


def test_user_event_callback_errors_are_contained() -> None:
    macro = short_macro("s")
    fake = FakePlayer()

    def broken(event: PlayerEvent) -> None:
        raise ValueError("receptor roto")

    ctl = MacroController(fake, {macro.id: macro}.get, broken)
    ctl.run(macro.id)
    fake.deliver(0, "step")
    fake.deliver(0, "finished")
    assert ctl.running_macro_id is None


def test_stop_and_toggle_pause_delegate() -> None:
    fake = FakePlayer()
    ctl = MacroController(fake, lambda _id: None, lambda ev: None)
    ctl.stop()
    ctl.stop("Otra razón")
    ctl.toggle_pause()
    assert fake.stops == [DEFAULT_STOP_REASON, "Otra razón"]
    assert fake.toggles == 1
