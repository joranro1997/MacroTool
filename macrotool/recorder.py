"""Grabación de teclado/ratón y conversión de los eventos grabados en pasos de macro.

``Recorder`` escucha el ``InputHook`` (sin bloquear nada) y guarda ``RawEvent``.
``events_to_steps`` es una función pura que convierte esa lista en pasos:
acordes (varias entradas a la vez) → un ``PressStep`` "tap"; pulsaciones que se
solapan → pasos "down"/"up"; dobles clics, rueda agregada y movimientos.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, replace
from typing import Callable, Collection, Iterable, Optional

from .hooks import InputHook, RawEvent
from .keys import MODIFIERS
from .model import MoveStep, PressStep, ScrollStep, Step

log = logging.getLogger(__name__)

RECORDER_PRIORITY = -100  # después de los disparadores: lo que bloquean sólo se guarda como marca

DOUBLE_CLICK_S = 0.4  # separación máxima (up → down) entre los dos clics de un doble clic
CLICK_SLOP_PX = 4  # distancia máxima para doble clic / temblor del ratón durante un clic
WHEEL_GAP_S = 0.2  # muescas de rueda más próximas se suman en un único paso
MIN_HOLD_MS = 10
# Un down repetido de una entrada que sigue abierta más tarde que esto no es una
# autorepetición: se perdió el up (Ctrl+Alt+Supr, Win+L, UAC...). Como hooks.STALE_REPEAT_S.
STALE_DOWN_S = 1.5
UI_GESTURE_S = 1.0  # gesto (clic, Alt+Tab...) que devolvió al usuario a MacroTool para parar

_DOWN_KINDS = frozenset({"key_down", "mouse_down"})
_UP_KINDS = frozenset({"key_up", "mouse_up"})
_PRESS_KINDS = _DOWN_KINDS | _UP_KINDS
_WHEEL_KINDS = frozenset({"wheel", "hwheel"})
_POSITIONAL_KINDS = frozenset({"mouse_down", "mouse_up", "move", "wheel", "hwheel"})


# --- Grabador -------------------------------------------------------------------------
class Recorder:
    """Graba los eventos físicos (no inyectados) del hook.

    Los eventos que bloqueó un atajo global se guardan marcados (``consumed``): no generan
    pasos, pero permiten quitar los modificadores de ese atajo (ver ``clean_events``).
    """

    def __init__(self, hook: InputHook, *, own_window_at: Callable[[int, int], bool] | None = None,
                 key_state: Callable[[int], bool] | None = None) -> None:
        self._hook = hook
        # ¿La ventana en (x, y) es de MacroTool? Sirve para quitar el clic final en "Detener".
        self._own_window_at = own_window_at
        # GetAsyncKeyState: al parar, distingue una tecla mantenida de un up perdido.
        self._key_state = key_state
        self._lock = threading.Lock()
        self._events: list[RawEvent] = []
        self._recording = False
        self._record_moves = False
        self._move_interval = 0.04
        self._last_move_t = float("-inf")
        self._pending_move: RawEvent | None = None
        self._on_event: Callable[[RawEvent], None] | None = None
        self._started_hook = False
        # Entradas que seguían pulsadas físicamente al detener la última grabación.
        self.held_at_stop: frozenset[str] = frozenset()

    @property
    def recording(self) -> bool:
        return self._recording

    def start(
        self,
        *,
        record_moves: bool = False,
        move_interval_ms: int = 40,
        on_event: Callable[[RawEvent], None] | None = None,
    ) -> None:
        """Empieza a grabar (si ya grababa, descarta lo anterior). ``on_event`` se llama en el hilo del hook."""
        if self._recording:
            self.stop(trim_own_clicks=False)
        with self._lock:
            self._events = []
            self._record_moves = bool(record_moves)
            self._move_interval = max(0, int(move_interval_ms)) / 1000.0
            self._last_move_t = float("-inf")
            self._pending_move = None
            self._on_event = on_event
            self._recording = True
        try:
            try:
                self._hook.add_listener(self._listener, RECORDER_PRIORITY, wants_moves=self._record_moves)
            except TypeError:  # hooks de prueba sin el parámetro wants_moves
                self._hook.add_listener(self._listener, RECORDER_PRIORITY)
            if not getattr(self._hook, "running", True):
                self._hook.start()
                self._started_hook = True
        except BaseException:
            self._recording = False
            self._hook.remove_listener(self._listener)
            raise

    def stop(self, *, trim_own_clicks: bool = True, ui_since: Optional[float] = None) -> list[RawEvent]:
        """Deja de grabar y devuelve los eventos grabados ([] si no estaba grabando).

        Con ``trim_own_clicks`` se descartan los clics finales sobre ventanas de
        MacroTool (p. ej. el botón "Detener") y los movimientos hacia ellos.
        ``ui_since`` (``perf_counter``) es el momento en que el usuario volvió a MacroTool para
        detener la grabación (ventana activada, menú de la bandeja...): se descarta todo lo
        posterior y el gesto que lo provocó (ver ``trim_ui_return``).
        ``held_at_stop`` queda con las entradas que seguían pulsadas físicamente.
        """
        self._hook.remove_listener(self._listener)
        with self._lock:
            if not self._recording:
                return []
            self._recording = False
            if self._pending_move is not None:
                self._events.append(self._pending_move)
                self._pending_move = None
            events = self._events
            self._events = []
            self._on_event = None
        if self._started_hook:
            self._started_hook = False
            self._hook.stop()
        if ui_since is not None:
            events = trim_ui_return(events, ui_since)
        if trim_own_clicks:
            events = self._trim_own_clicks(events)
        self.held_at_stop = self._still_pressed(events)
        return events

    def _still_pressed(self, events: list[RawEvent]) -> frozenset[str]:
        """Teclas con down sin up que Windows sigue dando por pulsadas (los botones del ratón
        no: al detener con un clic, el propio clic aún está en curso)."""
        open_tokens: set[str] = set()
        for e in events:
            if e.injected or e.consumed or not e.token or e.kind not in ("key_down", "key_up"):
                continue
            if e.kind in _DOWN_KINDS:
                open_tokens.add(e.token)
            elif e.kind in _UP_KINDS:
                open_tokens.discard(e.token)
        if not open_tokens:
            return frozenset()
        check = self._key_state
        if check is None:
            from .winput import is_key_down

            check = is_key_down
        from .keys import token_to_vk

        held = set()
        for token in open_tokens:
            try:
                if check(token_to_vk(token)):
                    held.add(token)
            except Exception:  # noqa: BLE001 - ante la duda, no se da por mantenida
                continue
        return frozenset(held)

    def _listener(self, event: RawEvent) -> None:
        if event.injected or not self._recording:
            return None
        if event.consumed:
            # Lo bloqueó un atajo global: se guarda sólo como marca (no genera pasos).
            if event.kind in _PRESS_KINDS:
                with self._lock:
                    if self._recording:
                        self._events.append(event)
            return None
        added: list[RawEvent] = []
        with self._lock:
            if not self._recording:
                return None
            if event.kind == "move":
                if not self._record_moves:
                    return None
                if event.t - self._last_move_t < self._move_interval:
                    # Se guarda aparte: si luego llega un clic, ésta es la posición real previa.
                    self._pending_move = event
                    return None
                self._pending_move = None
                self._last_move_t = event.t
                added.append(event)
            else:
                if self._pending_move is not None:
                    added.append(self._pending_move)
                    self._last_move_t = self._pending_move.t
                    self._pending_move = None
                added.append(event)
            self._events.extend(added)
            callback = self._on_event
        if callback is not None:
            for item in added:
                try:
                    callback(item)
                except Exception:
                    log.exception("Error en el callback de grabación")
        return None  # nunca bloquea

    def _trim_own_clicks(self, events: list[RawEvent]) -> list[RawEvent]:
        check = self._own_window_at
        if check is None:
            from .winput import window_is_own_process_at

            check = window_is_own_process_at
        i = len(events) - 1
        removed = False
        while i >= 0:
            event = events[i]
            if event.kind == "move" or event.consumed:
                i -= 1
                continue
            if event.kind in ("mouse_down", "mouse_up") and _safe_check(check, event.x, event.y):
                removed = True
                i -= 1
                continue
            break
        return events[: i + 1] if removed else events


def _safe_check(check: Callable[[int, int], bool], x: int, y: int) -> bool:
    try:
        return bool(check(x, y))
    except Exception:
        return False


def trim_ui_return(events: list[RawEvent], since: float, *, gesture_s: float = UI_GESTURE_S) -> list[RawEvent]:
    """Quita el final de una grabación desde que el usuario volvió a MacroTool para detenerla.

    ``since`` es el instante en que se activó la ventana o se abrió el menú de la bandeja. Ese
    momento llega un poco después del gesto que lo causó (clic en la barra de tareas o en el
    icono de la bandeja, Alt+Tab...), así que también se descarta ese gesto: yendo hacia atrás
    desde ``since``, las últimas pulsaciones (si terminaron menos de ``gesture_s`` antes) hasta
    encontrar el down de cada up. Una tecla mantenida desde antes (p. ej. W) no se toca.
    """
    cut = next((i for i, e in enumerate(events) if e.t >= since), len(events))
    start = cut
    needed: set[str] = set()  # entradas soltadas en el gesto cuyo down aún no se ha encontrado
    found = False
    for i in range(cut - 1, -1, -1):
        e = events[i]
        if e.kind not in _PRESS_KINDS or not e.token or e.injected or e.consumed:
            continue
        if not found:
            if since - e.t > gesture_s:
                break  # lo último es muy anterior: la ventana se activó por otro motivo
            found = True
        if e.kind in _UP_KINDS:
            needed.add(e.token)
        else:
            needed.discard(e.token)
        start = i
        if not needed:
            break  # gesto completo (o un down aún en curso, p. ej. el clic que activó la ventana)
    return events[:start]


# --- Conversión de eventos en pasos -----------------------------------------------------
@dataclass
class _Out:
    """Paso generado y el intervalo de tiempo de los eventos que consume."""

    step: Step
    first_t: float
    last_t: float
    # Para taps de un único botón del ratón: (botón, t_down, t_up, x, y) → detección de doble clic.
    click: Optional[tuple[str, float, float, int, int]] = None


def _is_mouse(token: str) -> bool:
    return token.startswith("mouse_")


def _near(x1: int, y1: int, x2: int, y2: int) -> bool:
    return (x1 - x2) ** 2 + (y1 - y2) ** 2 < CLICK_SLOP_PX**2


def _pair_by_scan(evs: list[RawEvent]) -> list[RawEvent]:
    """Da al up (y a las autorepeticiones) de cada tecla física el token de su down.

    Windows traduce el VK del teclado numérico según el estado del momento: Num 1 pulsada con
    Bloq Num puede soltarse como Fin si entretanto se pulsó Mayús.
    """
    by_scan: dict[int, tuple[str, float]] = {}  # scancode → (token del down, último down)
    result: list[RawEvent] = []
    for e in evs:
        if e.scan and e.kind == "key_down":
            previous = by_scan.get(e.scan)
            if previous is not None and previous[0] != e.token and e.t - previous[1] <= STALE_DOWN_S:
                e = replace(e, token=previous[0])
            by_scan[e.scan] = (e.token, e.t)
        elif e.scan and e.kind == "key_up":
            previous = by_scan.pop(e.scan, None)
            if previous is not None and previous[0] != e.token:
                e = replace(e, token=previous[0])
        result.append(e)
    return result


def clean_events(events: Iterable[RawEvent], *, held_at_end: Optional[Collection[str]] = None) -> list[RawEvent]:
    """Ordena y depura los eventos grabados.

    - Sin inyectados, autorepeticiones ni ups huérfanos (se pulsó antes de empezar a grabar).
    - Un down repetido de una entrada abierta tras más de ``STALE_DOWN_S`` (o de un botón del
      ratón, que no se autorepite) indica que se perdió el up: se descarta el down anterior.
    - Downs sin up: se descartan los finales (p. ej. el modificador del atajo de grabación). Si
      se indica ``held_at_end`` (entradas que seguían pulsadas físicamente al parar), las que
      tienen acciones después se conservan como «Mantener» (el motor las suelta al terminar).
    - Los eventos bloqueados por un atajo global (``consumed``) no generan pasos, y los
      modificadores pulsados sólo para ese atajo se descartan.
    """
    ordered = sorted((e for e in events if not e.injected), key=lambda e: e.t)
    markers = [e.t for e in ordered if e.consumed and e.kind in _DOWN_KINDS]
    evs = _pair_by_scan([e for e in ordered if not e.consumed])
    keep = [True] * len(evs)
    open_downs: dict[str, int] = {}  # token → índice de su down vigente
    last_down_t: dict[str, float] = {}
    pairs: list[tuple[int, int]] = []
    for i, e in enumerate(evs):
        if e.kind in _PRESS_KINDS:
            if not e.token:
                keep[i] = False
            elif e.kind in _DOWN_KINDS:
                j = open_downs.get(e.token)
                if j is not None:
                    lost_up = e.kind == "mouse_down" or e.t - last_down_t[e.token] > STALE_DOWN_S
                    last_down_t[e.token] = e.t
                    if not lost_up:
                        keep[i] = False  # autorepetición
                        continue
                    keep[j] = False  # se perdió el up de la pulsación anterior
                open_downs[e.token] = i
                last_down_t[e.token] = e.t
            elif e.token in open_downs:
                pairs.append((open_downs.pop(e.token), i))
            else:
                keep[i] = False  # up sin down previo (se pulsó antes de empezar a grabar)
        elif e.kind in _WHEEL_KINDS:
            keep[i] = e.delta != 0
        elif e.kind != "move":
            keep[i] = False
    open_indexes = set(open_downs.values())
    last_action = max((i for i, e in enumerate(evs)
                       if keep[i] and i not in open_indexes and e.kind != "move"), default=-1)
    held = set(held_at_end or ())
    for token, i in open_downs.items():
        if held_at_end is not None and token in held and i < last_action:
            continue  # se mantuvo pulsada mientras se hacían otras cosas (p. ej. W para avanzar)
        keep[i] = False  # down final sin up (p. ej. el modificador del atajo de grabación)
    if markers:
        for d, u in pairs:
            if not (keep[d] and keep[u] and evs[d].token in MODIFIERS):
                continue
            t0, t1 = evs[d].t, evs[u].t
            if not any(t0 <= m <= t1 for m in markers):
                continue
            if any(keep[k] and evs[k].kind in _PRESS_KINDS and evs[k].token not in MODIFIERS
                   for k in range(d + 1, u)):
                continue  # también acompañó a otras entradas grabadas: se conserva
            keep[d] = keep[u] = False  # modificador de un atajo global (p. ej. Alt de Alt+P)
    return [e for e, k in zip(evs, keep) if k]


def _split_units(evs: list[RawEvent]) -> list[tuple[str, list[RawEvent], Optional[tuple[int, int]]]]:
    """Divide en ráfagas (conjunto pulsado no vacío) y tramos sueltos (rueda/movimientos entre ráfagas).

    Cada ráfaga lleva la última posición conocida del cursor antes de empezar.
    """
    units: list[tuple[str, list[RawEvent], Optional[tuple[int, int]]]] = []
    pressed: set[str] = set()
    burst: list[RawEvent] | None = None
    burst_ref: Optional[tuple[int, int]] = None
    loose: list[RawEvent] = []
    last_pos: Optional[tuple[int, int]] = None
    for e in evs:
        if e.kind in _PRESS_KINDS:
            if burst is None:
                if loose:
                    units.append(("loose", loose, None))
                    loose = []
                burst, burst_ref = [], last_pos
            burst.append(e)
            if e.kind in _DOWN_KINDS:
                pressed.add(e.token)
            else:
                pressed.discard(e.token)
            if not pressed:
                units.append(("burst", burst, burst_ref))
                burst = None
        elif burst is not None:
            burst.append(e)  # rueda o movimiento con algo pulsado
        else:
            loose.append(e)
        if e.kind in _POSITIONAL_KINDS:
            last_pos = (e.x, e.y)
    if burst:
        units.append(("burst", burst, burst_ref))
    if loose:
        units.append(("loose", loose, None))
    return units


def _chord_tap(burst: list[RawEvent], ref: Optional[tuple[int, int]], positions: bool) -> Optional[_Out]:
    """Un único PressStep "tap" si todos los downs preceden al primer up (y no hay rueda ni arrastre)."""
    presses = [e for e in burst if e.kind in _PRESS_KINDS]
    first_up = next((i for i, e in enumerate(presses) if e.kind in _UP_KINDS), len(presses))
    downs, ups = presses[:first_up], presses[first_up:]
    if not downs or not ups or any(e.kind in _DOWN_KINDS for e in ups):
        return None
    extras = [e for e in burst if e.kind not in _PRESS_KINDS]
    mouse_downs = [e for e in downs if _is_mouse(e.token)]
    if positions:
        # Un arrastre (se suelta lejos de donde se pulsó) no es un clic: los pasos down/up
        # individuales conservan la posición en la que se soltó.
        pressed_at = {e.token: (e.x, e.y) for e in mouse_downs}
        if any(e.token in pressed_at and not _near(e.x, e.y, *pressed_at[e.token]) for e in ups):
            return None
    if extras:
        # Sólo se toleran pequeños temblores del ratón durante la pulsación.
        if any(e.kind != "move" for e in extras):
            return None
        if mouse_downs:
            cx, cy = mouse_downs[0].x, mouse_downs[0].y
        elif ref is not None:
            cx, cy = ref
        else:
            cx, cy = extras[0].x, extras[0].y
        if any(not _near(e.x, e.y, cx, cy) for e in extras):
            return None
    hold_ms = max(MIN_HOLD_MS, round((ups[0].t - downs[-1].t) * 1000))
    step = PressStep(inputs=[e.token for e in downs], action="tap", hold_ms=hold_ms)
    if positions and mouse_downs:
        step.x, step.y = mouse_downs[0].x, mouse_downs[0].y
    click = None
    if len(downs) == 1 and mouse_downs:
        d = downs[0]
        click = (d.token, d.t, ups[-1].t, d.x, d.y)
    return _Out(step, downs[0].t, ups[-1].t, click)


def _append_tap(outs: list[_Out], tap: _Out) -> None:
    """Añade un tap fusionando dos clics idénticos y seguidos en un doble clic (count=2)."""
    if tap.click is not None:
        button, down_t, _, x, y = tap.click
        j = len(outs) - 1
        # Los movimientos mínimos entre los dos clics no impiden el doble clic.
        while j >= 0 and isinstance(outs[j].step, MoveStep) and _near(outs[j].step.x, outs[j].step.y, x, y):
            j -= 1
        if j >= 0:
            prev = outs[j]
            if (
                prev.click is not None
                and prev.click[0] == button
                and down_t - prev.click[2] < DOUBLE_CLICK_S
                and _near(prev.click[3], prev.click[4], x, y)
            ):
                prev.step.count = 2  # type: ignore[attr-defined]
                prev.last_t = tap.last_t
                prev.click = None  # un tercer clic ya no se encadena
                del outs[j + 1 :]
                return
    outs.append(tap)


def _scroll_out(run: list[RawEvent], positions: bool) -> _Out:
    total = sum(e.delta for e in run)
    notches = int(abs(total) / 120 + 0.5) or 1  # redondeo; al menos una muesca
    step = ScrollStep(amount=notches if total > 0 else -notches, horizontal=run[0].kind == "hwheel")
    if positions:
        step.x, step.y = run[0].x, run[0].y
    return _Out(step, run[0].t, run[-1].t)


def _same_wheel_run(prev: RawEvent, e: RawEvent) -> bool:
    return prev.kind == e.kind and (prev.delta > 0) == (e.delta > 0) and e.t - prev.t < WHEEL_GAP_S


def _linear_steps(events: list[RawEvent], positions: bool) -> list[_Out]:
    """Un paso por evento (down/up individuales, movimientos) con la rueda agregada."""
    outs: list[_Out] = []
    run: list[RawEvent] = []
    for e in events:
        if e.kind in _WHEEL_KINDS:
            if run and not _same_wheel_run(run[-1], e):
                outs.append(_scroll_out(run, positions))
                run = []
            run.append(e)
            continue
        if run:
            outs.append(_scroll_out(run, positions))
            run = []
        if e.kind in _PRESS_KINDS:
            step = PressStep(inputs=[e.token], action="down" if e.kind in _DOWN_KINDS else "up")
            if positions and _is_mouse(e.token):
                step.x, step.y = e.x, e.y
            outs.append(_Out(step, e.t, e.t))
        elif e.kind == "move":
            outs.append(_Out(MoveStep(x=e.x, y=e.y), e.t, e.t))
    if run:
        outs.append(_scroll_out(run, positions))
    return outs


def events_to_steps(
    events: list[RawEvent],
    *,
    record_timing: bool = True,
    record_click_positions: bool = False,
    min_delay_ms: int = 0,
    held_at_end: Optional[Collection[str]] = None,
) -> list[Step]:
    """Convierte eventos grabados en pasos de macro (función pura).

    - Ráfaga (de "algo pulsado" a "nada pulsado") con todos los downs antes del
      primer up → un ``PressStep`` tap con las entradas en orden de pulsación y
      ``hold_ms`` = primer up − último down (mín. 10). Si no → down/up individuales.
    - Dos clics idénticos de un botón, < 400 ms y < 4 px → ``count=2``.
    - Rueda: muescas seguidas en la misma dirección (< 200 ms) → un ``ScrollStep``.
    - Movimientos → ``MoveStep`` absoluto.
    - ``record_timing``: ``delay_after_ms`` = ms hasta el primer evento del paso
      siguiente (≥ ``min_delay_ms``); el último paso y ``record_timing=False`` → None.
    - ``record_click_positions``: los pasos con botones del ratón llevan x, y (del
      primer down; en un "up" individual, la posición donde se soltó). Un arrastre sin
      movimientos grabados (se suelta lejos de donde se pulsó) queda como down/up.
    - ``held_at_end``: entradas que seguían pulsadas al parar (ver ``clean_events``).
    """
    evs = clean_events(events, held_at_end=held_at_end)
    outs: list[_Out] = []
    for kind, group, ref in _split_units(evs):
        if kind == "burst":
            tap = _chord_tap(group, ref, record_click_positions)
            if tap is not None:
                _append_tap(outs, tap)
                continue
        outs.extend(_linear_steps(group, record_click_positions))

    steps: list[Step] = []
    for i, out in enumerate(outs):
        if record_timing and i + 1 < len(outs):
            gap_ms = round((outs[i + 1].first_t - out.last_t) * 1000)
            out.step.delay_after_ms = max(int(min_delay_ms), gap_ms, 0)
        else:
            out.step.delay_after_ms = None
        steps.append(out.step)
    return steps
