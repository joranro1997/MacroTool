"""Lógica de disparadores (una vez / alternar / mientras se mantiene) sobre ``MacroPlayer``.

No depende de Qt. Los métodos públicos se llaman SIEMPRE desde el hilo principal; los
eventos del reproductor llegan desde su hilo de trabajo, actualizan el estado interno
(protegido con un lock) y se reenvían tal cual al receptor del usuario.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

from .engine import DEFAULT_STOP_REASON, TERMINAL_EVENTS, MacroPlayer, PlayerEvent
from .model import TRIGGER_MODES, Macro

log = logging.getLogger(__name__)

HOLD_RELEASE_REASON = "Disparador soltado"
SWITCH_REASON = "Sustituida por otra macro"


class MacroController:
    """Decide qué hacer con cada pulsación/suelta de un disparador de macro."""

    SWITCH_TIMEOUT_S = 1.0  # espera máxima a que termine la macro anterior al cambiar

    def __init__(self, player: MacroPlayer, get_macro: Callable[[str], Optional[Macro]],
                 on_event: Callable[[PlayerEvent], None], *,
                 failsafe: Callable[[], bool] = lambda: False) -> None:
        self._player = player
        self._get_macro = get_macro
        self._on_event = on_event
        self._failsafe = failsafe
        self._lock = threading.Lock()
        # Estado de la ejecución lanzada por este controlador. ``_token`` identifica la
        # ejecución: el evento terminal de una ejecución anterior no borra el de la nueva.
        self._token = 0
        self._running_id: Optional[str] = None
        self._running_mode: Optional[str] = None
        self._stop_requested = False

    # ------------------------------------------------------------------ estado
    @property
    def running_macro_id(self) -> Optional[str]:
        """Id de la macro en curso (hasta recibir su evento terminal) o None."""
        with self._lock:
            return self._running_id

    @property
    def is_running(self) -> bool:
        return self.running_macro_id is not None

    def _active_id(self) -> Optional[str]:
        """Como ``running_macro_id`` pero None si ya se pidió detenerla."""
        with self._lock:
            return None if self._stop_requested else self._running_id

    # ------------------------------------------------------------ disparadores
    def handle_trigger(self, macro_id: str, pressed: bool) -> None:
        """Pulsación (``pressed=True``) o suelta del disparador de ``macro_id``."""
        if not pressed:
            # Sólo importa en modo "hold". No se consulta la macro: aunque se haya
            # desactivado o borrado mientras se mantenía, al soltar hay que pararla.
            with self._lock:
                holding = (self._running_id == macro_id and self._running_mode == "hold"
                           and not self._stop_requested)
            if holding:
                self.stop(HOLD_RELEASE_REASON)
            return

        macro = self._get_macro(macro_id)
        if macro is None or not macro.enabled:
            return
        mode = macro.trigger_mode if macro.trigger_mode in TRIGGER_MODES else "once"
        if self._active_id() == macro_id:
            if mode == "toggle":
                self.stop()
            return  # once / hold: ya está en marcha, se ignora el re-disparo
        self._start(macro, mode)

    def run(self, macro_id: str) -> bool:
        """Ejecución manual (botón ▶ / atajo). Funciona aunque el disparador esté desactivado."""
        macro = self._get_macro(macro_id)
        if macro is None or self._active_id() == macro_id:
            return False
        return self._start(macro, "once")

    def toggle_run(self, macro_id: str) -> None:
        if self._active_id() == macro_id:
            self.stop()
        else:
            self.run(macro_id)

    def stop(self, reason: str = DEFAULT_STOP_REASON) -> None:
        with self._lock:
            if self._running_id is not None:
                self._stop_requested = True
        self._player.stop(reason)

    def toggle_pause(self) -> None:
        self._player.toggle_pause()

    # ----------------------------------------------------------------- interno
    def _start(self, macro: Macro, mode: str) -> bool:
        if self._player.is_running:
            # stop() es asíncrono: esperar a que el hilo anterior salga (es rápido
            # porque todas sus esperas son interrumpibles).
            self.stop(SWITCH_REASON)
            if not self._player.wait(self.SWITCH_TIMEOUT_S):
                log.warning("La macro anterior no se detuvo a tiempo; no se inicia %r", macro.name)
                return False

        with self._lock:
            self._token += 1
            token = self._token
            # Se fija ANTES de start(): el evento terminal puede llegar antes de que vuelva.
            self._running_id = macro.id
            self._running_mode = mode
            self._stop_requested = False

        def on_event(event: PlayerEvent) -> None:
            self._on_player_event(token, event)

        try:
            started = self._player.start(macro, on_event, failsafe=self._failsafe_enabled(),
                                         loop_until_stopped=(mode == "hold"))
        except Exception:  # noqa: BLE001
            log.exception("No se pudo iniciar la macro %r", macro.name)
            started = False
        if not started:
            self._clear(token)
        return started

    def _failsafe_enabled(self) -> bool:
        try:
            return bool(self._failsafe())
        except Exception:  # noqa: BLE001
            log.exception("Error al consultar la parada de emergencia")
            return False

    def _clear(self, token: int) -> None:
        with self._lock:
            if token == self._token:
                self._running_id = None
                self._running_mode = None
                self._stop_requested = False

    def _on_player_event(self, token: int, event: PlayerEvent) -> None:
        # Hilo de trabajo del reproductor.
        if event.kind in TERMINAL_EVENTS:
            self._clear(token)
        try:
            self._on_event(event)
        except Exception:  # noqa: BLE001
            log.exception("Error en el receptor de eventos del controlador")
