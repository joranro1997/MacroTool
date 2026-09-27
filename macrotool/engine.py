"""Reproductor de macros: ejecuta los pasos de una ``Macro`` en un hilo de trabajo.

La inyección real la hace un ``InputBackend`` (por defecto ``winput.WinInputBackend``,
creado de forma perezosa en el primer ``start``). Todas las esperas usan un deadline
absoluto con ``time.perf_counter`` y son interrumpibles (``stop``) y congelables
(``pause``: el tiempo no corre mientras la ejecución está en pausa).

Con ``Macro.humanize`` los retardos y las pulsaciones varían un poco al azar y las
entradas "a la vez" se pulsan con un desfase de pocos milisegundos, como lo haría una
persona. Toda la aleatoriedad sale del ``rng`` del reproductor (tests deterministas).
"""
from __future__ import annotations

import atexit
import logging
import math
import random
import threading
import time
import weakref
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Iterator, Optional

from .model import Macro, MoveStep, PressStep, ScrollStep, Step, TextStep, WaitStep

if TYPE_CHECKING:  # sólo para anotaciones: winput se importa de forma perezosa
    from .winput import InputBackend

log = logging.getLogger(__name__)

TERMINAL_EVENTS = frozenset({"finished", "stopped", "error"})
DEFAULT_STOP_REASON = "Detenida por el usuario"
FAILSAFE_MESSAGE = "Parada de emergencia: ratón en una esquina de la pantalla"
NO_STEPS_MESSAGE = "La macro no tiene pasos activos"
EXIT_STOP_REASON = "La aplicación se está cerrando"

_SLICE_S = 0.020  # rodaja máxima de cada espera (respuesta a stop/pausa)
_SPIN_S = 0.0015  # último tramo de cada espera: espera activa para afinar al milisegundo
_COUNTDOWN_TICK_S = 0.100  # frecuencia de los eventos "countdown"
_MOVE_SUBSTEP_MS = 10  # duración aproximada de cada subpaso del movimiento suave
_FAILSAFE_PX = 2  # distancia máxima a la esquina para la parada de emergencia
_JOIN_PREVIOUS_S = 1.0  # espera máxima al hilo de una ejecución ya terminada


@dataclass
class PlayerEvent:
    """Evento del reproductor. Se emite siempre desde el hilo de trabajo.

    Orden: countdown* → started → (repeat → step*)* → UN terminal (finished | stopped |
    error), con paused/resumed intercalados. Una macro sin pasos activos sólo emite
    "finished". Al entregarse el terminal, ``MacroPlayer.is_running`` ya es False.
    """

    kind: str  # "countdown" | "started" | "step" | "repeat" | "paused" | "resumed" | "finished" | "stopped" | "error"
    macro_id: str = ""
    step_index: int = -1  # índice en macro.steps (lista completa, incl. desactivados)
    repeat_index: int = 0  # 0-based
    total_repeats: int = 1  # 0 = infinito
    remaining_ms: int = 0  # para countdown
    message: str = ""  # motivo de stopped / texto de error (español)

    @property
    def is_terminal(self) -> bool:
        return self.kind in TERMINAL_EVENTS


# Reproductores vivos: al salir del intérprete los hilos daemon morirían sin pasar por
# su ``finally`` (release_all), dejando teclas pulsadas. ``_stop_players_at_exit`` lo evita.
_players: "weakref.WeakSet[MacroPlayer]" = weakref.WeakSet()


@atexit.register
def _stop_players_at_exit() -> None:
    for player in list(_players):
        player.stop(EXIT_STOP_REASON)
        player.wait(1.0)
        player.retry_pending_release()  # último intento de soltar lo que Windows rechazó


def _set_timer_resolution(enable: bool) -> None:
    """``winput.set_timer_resolution`` tolerando que no exista o falle."""
    try:
        from . import winput

        winput.set_timer_resolution(enable)
    except Exception:  # noqa: BLE001 - la resolución del temporizador es opcional
        log.debug("No se pudo ajustar la resolución del temporizador", exc_info=True)


def _pending_release(backend: object) -> list[str]:
    """Entradas que el backend no pudo soltar (vacío si no lo sabe)."""
    try:
        return list(getattr(backend, "pending_release", None) or [])
    except Exception:  # noqa: BLE001
        return []


def _stuck_message(backend: object) -> str:
    """Aviso en español cuando Windows rechazó soltar alguna entrada."""
    tokens = _pending_release(backend)
    names = ""
    if tokens:
        try:
            from . import keys

            names = " (" + ", ".join(keys.display_name(t) for t in tokens) + ")"
        except Exception:  # noqa: BLE001
            names = " (" + ", ".join(tokens) + ")"
    return (f"Windows no dejó soltar algunas teclas o botones{names}. Se reintentará automáticamente; "
            "si alguno sigue pulsado, púlsalo y suéltalo")


def _ease(t: float) -> float:
    """Curva suave (ease-in-out senoidal) de [0, 1] en [0, 1]."""
    return 0.5 - 0.5 * math.cos(math.pi * t)


class _Stopped(Exception):
    """Señal interna: se ha pedido detener la ejecución."""


class _Run:
    """Estado de una ejecución concreta (una por cada ``start`` aceptado)."""

    def __init__(self, macro: Macro, on_event: Callable[[PlayerEvent], None],
                 backend: Optional["InputBackend"], *, failsafe: bool, loop: bool) -> None:
        self.macro = macro
        self.on_event = on_event
        self.backend = backend
        self.failsafe = failsafe
        self.total_repeats = 0 if loop else max(0, int(macro.repeat_count))
        self.thread: Optional[threading.Thread] = None

        self.stop_event = threading.Event()
        self.unpaused = threading.Event()  # activo = en marcha; limpio = en pausa
        self.unpaused.set()
        self.wake = threading.Event()  # despierta las esperas ante stop/pausa
        self.stop_reason = ""
        self.done = False  # True justo antes de emitir el evento terminal
        self._lock = threading.Lock()

        # Estado que sólo toca el hilo de trabajo.
        self.step_index = -1
        self.repeat_index = 0
        self.held: list[str] = []  # entradas pulsadas por la macro y aún no soltadas
        self.engine_pos: Optional[tuple[int, int]] = None  # última posición puesta por el motor
        self.screen: Optional[tuple[int, int]] = None

    def request_stop(self, reason: str) -> None:
        with self._lock:
            if self.stop_event.is_set():
                return  # idempotente: prevalece el primer motivo
            self.stop_reason = reason
            self.stop_event.set()
        self.unpaused.set()
        self.wake.set()

    def request_pause(self) -> None:
        with self._lock:
            if self.stop_event.is_set():
                return
            self.unpaused.clear()
        self.wake.set()

    def request_resume(self) -> None:
        self.unpaused.set()

    @property
    def paused(self) -> bool:
        return not self.unpaused.is_set() and not self.stop_event.is_set()


class MacroPlayer:
    """Reproduce una macro cada vez, en un hilo daemon por ejecución."""

    def __init__(self, backend: Optional["InputBackend"] = None, *,
                 rng: Optional[random.Random] = None) -> None:
        self._backend = backend
        self._rng = rng if rng is not None else random.Random()
        self._lock = threading.Lock()
        self._run: Optional[_Run] = None
        _players.add(self)

    # ------------------------------------------------------------------ API pública
    def set_backend(self, backend: Optional["InputBackend"]) -> None:
        """Cambia el backend. Una ejecución en curso conserva el suyo; el nuevo se usa
        a partir del siguiente ``start`` (None = ``WinInputBackend`` perezoso)."""
        with self._lock:
            self._backend = backend

    def start(self, macro: Macro, on_event: Callable[[PlayerEvent], None], *,
              failsafe: bool = False, loop_until_stopped: bool = False) -> bool:
        """Empieza a reproducir una copia de ``macro``. False si ya hay una ejecución en curso."""
        with self._lock:
            previous = self._run
            if previous is not None and not previous.done:
                return False
        # La ejecución anterior ya está emitiendo su evento terminal: esperar a que su
        # hilo salga para que sus eventos nunca se mezclen con los de la nueva.
        if (previous is not None and previous.thread is not None
                and previous.thread is not threading.current_thread()):
            previous.thread.join(_JOIN_PREVIOUS_S)
        run = _Run(macro.clone(), on_event, None, failsafe=bool(failsafe),
                   loop=bool(loop_until_stopped))
        with self._lock:
            if self._run is not previous:
                return False  # otro hilo se adelantó
            run.backend = self._backend
            run.thread = threading.Thread(target=self._worker, args=(run,),
                                          name="MacroPlayer", daemon=True)
            self._run = run
            try:
                run.thread.start()
            except BaseException:
                self._run = previous
                raise
        return True

    def stop(self, reason: str = DEFAULT_STOP_REASON) -> None:
        """Pide detener la ejecución en curso. No bloquea; idempotente."""
        run = self._run
        if run is not None and not run.done:
            run.request_stop(reason)

    def pause(self) -> None:
        run = self._run
        if run is not None and not run.done:
            run.request_pause()

    def resume(self) -> None:
        run = self._run
        if run is not None and not run.done:
            run.request_resume()

    def toggle_pause(self) -> None:
        if self.is_paused:
            self.resume()
        else:
            self.pause()

    def wait(self, timeout: Optional[float] = None) -> bool:
        """Espera a que termine el hilo de la ejecución actual. True si ya no queda ninguno."""
        run = self._run
        if run is None or run.thread is None:
            return True
        if run.thread is threading.current_thread():
            return False
        run.thread.join(timeout)
        return not run.thread.is_alive()

    @property
    def is_running(self) -> bool:
        run = self._run
        return run is not None and not run.done

    @property
    def is_paused(self) -> bool:
        run = self._run
        return run is not None and not run.done and run.paused

    @property
    def current_macro_id(self) -> Optional[str]:
        run = self._run
        return run.macro.id if run is not None and not run.done else None

    @property
    def pending_release(self) -> list[str]:
        """Entradas que Windows no dejó soltar al terminar una ejecución (siguen pulsadas)."""
        run = self._run
        backends = [self._backend] + ([run.backend] if run is not None else [])
        pending: list[str] = []
        for backend in backends:
            if backend is not None:
                pending += [t for t in _pending_release(backend) if t not in pending]
        return pending

    def retry_pending_release(self) -> bool:
        """Reintenta soltar lo pendiente (seguro en cualquier momento). True si no queda nada."""
        run = self._run
        backends = {id(b): b for b in (self._backend, run.backend if run is not None else None)
                    if b is not None}
        ok = True
        for backend in backends.values():
            retry = getattr(backend, "retry_pending_release", None)
            if retry is None:
                continue
            try:
                ok = retry() is not False and ok
            except Exception:  # noqa: BLE001
                log.debug("No se pudieron reintentar las sueltas pendientes", exc_info=True)
                ok = False
        return ok

    # ------------------------------------------------------------ hilo de trabajo
    def _worker(self, run: _Run) -> None:
        kind, message = "error", "La ejecución se interrumpió de forma inesperada"
        timer = False
        try:
            self._resolve_backend(run)
            _set_timer_resolution(True)
            timer = True
            message = self._play(run)
            kind = "finished"
        except _Stopped:
            kind, message = "stopped", run.stop_reason or DEFAULT_STOP_REASON
        except Exception as exc:  # noqa: BLE001 - cualquier fallo acaba en evento "error"
            log.exception("Error al ejecutar la macro %r", run.macro.name)
            kind, message = "error", self._describe_error(run, exc)
        finally:
            # Nunca dejar teclas ni botones pulsados.
            if run.backend is not None:
                problem = ""
                try:
                    if run.backend.release_all() is False:
                        problem = _stuck_message(run.backend)
                except Exception as exc:  # noqa: BLE001
                    log.exception("No se pudieron soltar las entradas pulsadas")
                    problem = f"No se pudieron soltar las teclas pulsadas: {exc}"
                if problem:
                    if kind == "error" and message:
                        message = f"{message.rstrip('.')}. {problem}"
                    else:
                        kind, message = "error", problem
            run.held.clear()
            if timer:
                _set_timer_resolution(False)
            with self._lock:
                run.done = True
            self._emit(run, kind, message=message)

    def _resolve_backend(self, run: _Run) -> None:
        if run.backend is not None:
            return
        with self._lock:
            if self._backend is None:
                try:
                    from .winput import WinInputBackend

                    self._backend = WinInputBackend()
                except Exception as exc:
                    raise RuntimeError(
                        f"No se pudo preparar la simulación de teclado y ratón: {exc}") from exc
            run.backend = self._backend

    @staticmethod
    def _describe_error(run: _Run, exc: BaseException) -> str:
        detail = str(exc).strip() or type(exc).__name__
        if run.backend is None:
            return detail
        if run.step_index >= 0:
            return f"Error en el paso {run.step_index + 1}: {detail}"
        return f"Error durante la ejecución: {detail}"

    def _emit(self, run: _Run, kind: str, *, step_index: Optional[int] = None,
              remaining_ms: int = 0, message: str = "") -> None:
        event = PlayerEvent(
            kind=kind,
            macro_id=run.macro.id,
            step_index=run.step_index if step_index is None else step_index,
            repeat_index=run.repeat_index,
            total_repeats=run.total_repeats,
            remaining_ms=remaining_ms,
            message=message,
        )
        try:
            run.on_event(event)
        except Exception:  # noqa: BLE001 - un receptor defectuoso no debe romper la macro
            log.exception("Error en el receptor de eventos del reproductor")

    def _play(self, run: _Run) -> str:
        """Bucle principal. Devuelve el mensaje del evento "finished"."""
        macro = run.macro
        active = [i for i, step in enumerate(macro.steps) if step.enabled]
        if not active:
            return NO_STEPS_MESSAGE  # también con repeticiones infinitas: nada que repetir
        if macro.start_delay_ms > 0:
            self._countdown(run, int(macro.start_delay_ms))
        self._emit(run, "started", step_index=-1)

        repeats = run.total_repeats
        rep = 0
        while repeats == 0 or rep < repeats:
            self._checkpoint(run)
            run.repeat_index = rep
            self._emit(run, "repeat", step_index=-1)
            last_rep = repeats != 0 and rep == repeats - 1
            for n, index in enumerate(active):
                step = macro.steps[index]
                self._checkpoint(run)
                if run.failsafe:
                    self._check_failsafe(run)
                run.step_index = index
                self._emit(run, "step")
                self._execute(run, step)
                last_step = n == len(active) - 1
                if last_step and last_rep:
                    break  # sin espera tras el último paso de la última repetición
                delay = self._delay_after(macro, step)
                if last_step:
                    delay += self._vary(macro, macro.repeat_delay_ms, macro.jitter_ms)
                self._sleep_ms(run, delay)
            rep += 1
        return ""

    # ------------------------------------------------------------- humanización
    def _vary(self, macro: Macro, base: int, spread: int, *, floor: int = 0) -> int:
        """``base`` ± una variación normal(0, spread/2) recortada a ±``spread``.

        Sólo con ``macro.humanize``. Una base de 0 se respeta (0 = sin espera) y el
        resultado nunca baja de ``floor``.
        """
        base = max(0, int(base))
        spread = max(0, int(spread))
        if not macro.humanize or spread == 0 or base == 0:
            return base
        offset = max(-spread, min(spread, self._rng.gauss(0.0, spread / 2.0)))
        return max(floor, round(base + offset))

    def _delay_after(self, macro: Macro, step: Step) -> int:
        return self._vary(macro, macro.effective_delay_after(step), macro.jitter_ms)

    def _hold_ms(self, macro: Macro, hold_ms: int) -> int:
        """Duración de una pulsación: ≥ 1 ms si la base es > 0."""
        return self._vary(macro, hold_ms, macro.hold_jitter_ms, floor=1)

    @staticmethod
    def _stagger_ms(macro: Macro) -> int:
        return max(0, int(macro.chord_stagger_ms)) if macro.humanize else 0

    # ------------------------------------------------------------------- esperas
    def _checkpoint(self, run: _Run) -> float:
        """Lanza ``_Stopped`` si se pidió parar; si hay pausa, la atiende.

        Devuelve los segundos pasados en pausa (para desplazar los deadlines)."""
        if run.stop_event.is_set():
            raise _Stopped()
        if run.unpaused.is_set():
            return 0.0
        started = time.perf_counter()
        # Soltar lo que la macro mantiene pulsado mientras dure la pausa.
        held = list(run.held)
        if held:
            run.backend.release(held[::-1])
        self._emit(run, "paused")
        while not run.unpaused.wait(_SLICE_S):
            if run.stop_event.is_set():
                break
        if run.stop_event.is_set():
            raise _Stopped()
        run.wake.clear()
        if held:
            run.backend.press(held)
        self._emit(run, "resumed")
        return time.perf_counter() - started

    def _wait_until(self, run: _Run, deadline: float,
                    on_tick: Optional[Callable[[float], None]] = None) -> float:
        """Espera hasta ``deadline`` (reloj ``perf_counter``) en rodajas ≤ 20 ms.

        El deadline se desplaza lo que dure cada pausa. ``on_tick(restante_s)`` se llama
        cada ~100 ms. Devuelve el tiempo total pasado en pausa."""
        paused_total = 0.0
        next_tick = time.perf_counter()
        while True:
            paused = self._checkpoint(run)
            if paused:
                paused_total += paused
                deadline += paused
                next_tick += paused
            now = time.perf_counter()
            left = deadline - now
            if left <= 0:
                return paused_total
            if on_tick is not None:
                if now >= next_tick:
                    on_tick(left)
                    next_tick += _COUNTDOWN_TICK_S
                    if next_tick <= now:
                        next_tick = now + _COUNTDOWN_TICK_S
                left = min(left, max(0.0, next_tick - now))
            if left > _SPIN_S:
                if run.wake.wait(min(left - _SPIN_S, _SLICE_S)):
                    run.wake.clear()  # el estado se vuelve a comprobar arriba
            else:
                time.sleep(0)

    def _sleep_ms(self, run: _Run, ms: float) -> float:
        if ms <= 0:
            # Sin espera: al menos ceder el GIL, para que con retardos de 0 ms el hilo principal
            # (suelta del disparador, atajo de parada) no quede esperando a este bucle.
            time.sleep(0)
        return self._wait_until(run, time.perf_counter() + max(0.0, ms) / 1000.0)

    def _countdown(self, run: _Run, ms: int) -> None:
        def tick(left: float) -> None:
            self._emit(run, "countdown", step_index=-1,
                       remaining_ms=max(1, int(round(left * 1000))))

        self._wait_until(run, time.perf_counter() + ms / 1000.0, on_tick=tick)

    def _timeline(self, run: _Run, duration_ms: int) -> Iterator[float]:
        """Reparte ``duration_ms`` en subpasos de ~10 ms. Espera antes de cada uno y
        devuelve la fracción suavizada recorrida (la última es exactamente 1.0)."""
        n = max(1, round(duration_ms / _MOVE_SUBSTEP_MS))
        dt = duration_ms / 1000.0 / n
        t0 = time.perf_counter()
        shift = 0.0
        for i in range(1, n + 1):
            shift += self._wait_until(run, t0 + i * dt + shift)
            yield 1.0 if i == n else _ease(i / n)

    # --------------------------------------------------------------- failsafe
    def _check_failsafe(self, run: _Run) -> None:
        x, y = run.backend.cursor_pos()
        if run.screen is None:
            run.screen = tuple(run.backend.screen_size())  # type: ignore[assignment]
        w, h = run.screen

        def near(px: int, py: int) -> bool:
            return abs(x - px) <= _FAILSAFE_PX and abs(y - py) <= _FAILSAFE_PX

        if not any(near(cx, cy) for cx, cy in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1))):
            return
        if run.engine_pos is not None and near(*run.engine_pos):
            return  # la puso la propia macro
        run.request_stop(FAILSAFE_MESSAGE)
        raise _Stopped()

    # ------------------------------------------------------------------- pasos
    def _execute(self, run: _Run, step: Step) -> None:
        if isinstance(step, PressStep):
            self._do_press(run, step)
        elif isinstance(step, TextStep):
            self._do_text(run, step)
        elif isinstance(step, MoveStep):
            self._do_move(run, step)
        elif isinstance(step, ScrollStep):
            if step.x is not None and step.y is not None:
                self._move_to(run, int(step.x), int(step.y))
            if step.amount:
                run.backend.scroll(int(step.amount), bool(step.horizontal))
        elif isinstance(step, WaitStep):
            extra = max(0, int(step.random_extra_ms))
            ms = max(0, int(step.ms)) + (self._rng.randint(0, extra) if extra else 0)
            self._sleep_ms(run, ms)
        else:
            log.warning("Tipo de paso no soportado: %r", step)

    def _press(self, run: _Run, tokens: list[str]) -> None:
        run.backend.press(list(tokens))
        for token in tokens:
            if token not in run.held:
                run.held.append(token)

    def _release(self, run: _Run, tokens: list[str]) -> None:
        run.backend.release(list(tokens))
        run.held = [t for t in run.held if t not in tokens]

    def _press_chord(self, run: _Run, tokens: list[str]) -> None:
        """Pulsa entradas "a la vez": en un único lote o, humanizado, una a una en orden
        con un desfase aleatorio uniforme de 0..chord_stagger_ms entre cada una."""
        stagger = self._stagger_ms(run.macro)
        if stagger == 0 or len(tokens) < 2:
            self._press(run, tokens)
            return
        for i, token in enumerate(tokens):
            if i:
                self._sleep_ms(run, self._rng.randint(0, stagger))
            self._press(run, [token])

    def _release_chord(self, run: _Run, tokens: list[str]) -> None:
        """Como ``_press_chord`` pero soltando (en el orden dado, desfases independientes)."""
        stagger = self._stagger_ms(run.macro)
        if stagger == 0 or len(tokens) < 2:
            self._release(run, tokens)
            return
        for i, token in enumerate(tokens):
            if i:
                self._sleep_ms(run, self._rng.randint(0, stagger))
            self._release(run, [token])

    def _move_to(self, run: _Run, x: int, y: int) -> None:
        run.backend.move_to(x, y)
        run.engine_pos = (x, y)

    def _do_press(self, run: _Run, step: PressStep) -> None:
        if step.x is not None and step.y is not None:
            self._move_to(run, int(step.x), int(step.y))
        inputs = list(step.inputs)
        if not inputs:
            return
        if step.action == "down":
            self._press_chord(run, inputs)
        elif step.action == "up":
            self._release_chord(run, inputs)
        else:  # tap
            hold = max(0, int(step.hold_ms))
            count = max(1, int(step.count))
            for i in range(count):
                self._press_chord(run, inputs)
                self._sleep_ms(run, self._hold_ms(run.macro, hold))
                self._release_chord(run, inputs[::-1])
                if i < count - 1:
                    self._sleep_ms(run, self._hold_ms(run.macro, hold))

    def _do_text(self, run: _Run, step: TextStep) -> None:
        text = step.text.replace("\r\n", "\n").replace("\r", "\n")
        interval = max(0, int(step.char_interval_ms))
        for i, ch in enumerate(text):
            if i:
                self._sleep_ms(run, self._vary(run.macro, interval, run.macro.hold_jitter_ms))
            if ch == "\n":
                self._press(run, ["enter"])
                self._release(run, ["enter"])
            elif ch == "\t":
                self._press(run, ["tab"])
                self._release(run, ["tab"])
            else:
                run.backend.type_char(ch)

    def _do_move(self, run: _Run, step: MoveStep) -> None:
        x, y = int(step.x), int(step.y)
        duration = max(0, int(step.duration_ms))
        if not step.relative:
            if duration:
                self._smooth_move_to(run, x, y, duration)
            else:
                self._move_to(run, x, y)
            return
        if duration:
            done_x = done_y = 0
            for frac in self._timeline(run, duration):
                tx, ty = round(x * frac), round(y * frac)
                if tx != done_x or ty != done_y:
                    run.backend.move_rel(tx - done_x, ty - done_y)
                    done_x, done_y = tx, ty
        elif x or y:
            run.backend.move_rel(x, y)
        if run.failsafe:  # la posición resultante la ha puesto el motor
            run.engine_pos = tuple(run.backend.cursor_pos())  # type: ignore[assignment]

    def _smooth_move_to(self, run: _Run, x: int, y: int, duration_ms: int) -> None:
        sx, sy = run.backend.cursor_pos()
        last: tuple[int, int] = (sx, sy)
        for frac in self._timeline(run, duration_ms):
            if frac >= 1.0:
                self._move_to(run, x, y)  # posición final exacta
                return
            pos = (round(sx + (x - sx) * frac), round(sy + (y - sy) * frac))
            if pos != last:
                run.backend.move_to(*pos)
                run.engine_pos = pos
                last = pos
