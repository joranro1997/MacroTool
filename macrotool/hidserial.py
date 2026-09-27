"""Backend de entrada por HID real (teclado + clics de ratón, sin movimiento).

Envía las pulsaciones a una placa (Raspberry Pi Pico con CircuitPython) por puerto
serie; la placa las reproduce como un **teclado y ratón USB de verdad**, así que llegan
por la ruta de hardware genuina: SIN el flag "inyectado" que SendInput pone siempre. Es
la diferencia que enseña el inspector (``tools/input_inspector.py``).

Implementa el mismo Protocol que ``winput.WinInputBackend`` (``InputBackend``), así que
se enchufa sin tocar el motor::

    from macrotool.hidserial import HidSerialBackend
    player.set_backend(HidSerialBackend())     # autodetecta la placa

Soporta teclas y **clics** de ratón (izquierdo/derecho/central). NO hay movimiento del
ratón: ``move_to`` / ``move_rel`` / ``scroll`` no hacen nada (``cursor_pos`` /
``screen_size`` se consultan a Windows con normalidad). Los botones laterales x1/x2
necesitarían un descriptor extendido y lanzan ``ValueError``.

Protocolo serie (líneas ASCII terminadas en ``\\n``) hacia la placa:
    ``D <code>``   -> pulsar tecla  (code = HID usage id, en decimal)
    ``U <code>``   -> soltar tecla
    ``MD <mask>``  -> pulsar botón de ratón (mask = 1 izq, 2 der, 4 central)
    ``MU <mask>``  -> soltar botón de ratón
    ``X``          -> soltar todo (teclado y ratón)
    ``P``          -> ping; la placa responde ``PONG``
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Iterable, Optional, Sequence

from . import keys

log = logging.getLogger(__name__)

# VID USB habituales de placas con CircuitPython (Raspberry Pi 0x2E8A, Adafruit 0x239A).
_KNOWN_VIDS = (0x2E8A, 0x239A)


class HidSerialError(RuntimeError):
    """No se pudo abrir/encontrar la placa HID."""


# --- Mapa token -> HID usage id (Keyboard/Keypad Page 0x07) --------------------
def _build_hid_map() -> dict[str, int]:
    m: dict[str, int] = {}
    for i, c in enumerate("abcdefghijklmnopqrstuvwxyz"):
        m[c] = 0x04 + i
    for d in range(1, 10):
        m[str(d)] = 0x1E + (d - 1)
    m["0"] = 0x27
    for n in range(1, 13):
        m[f"f{n}"] = 0x3A + (n - 1)
    for n in range(13, 25):
        m[f"f{n}"] = 0x68 + (n - 13)
    for n in range(1, 10):
        m[f"num{n}"] = 0x59 + (n - 1)
    m["num0"] = 0x62
    m.update({
        "enter": 0x28, "esc": 0x29, "backspace": 0x2A, "tab": 0x2B, "space": 0x2C,
        "capslock": 0x39, "printscreen": 0x46, "scrolllock": 0x47, "pause": 0x48,
        "insert": 0x49, "home": 0x4A, "pageup": 0x4B, "delete": 0x4C, "end": 0x4D,
        "pagedown": 0x4E, "right": 0x4F, "left": 0x50, "down": 0x51, "up": 0x52,
        "numlock": 0x53, "apps": 0x65,
        "num_divide": 0x54, "num_multiply": 0x55, "num_subtract": 0x56,
        "num_add": 0x57, "num_enter": 0x58, "num_decimal": 0x63,
        # Modificadores: el genérico se manda como el izquierdo (igual que winput).
        "ctrl": 0xE0, "lctrl": 0xE0, "rctrl": 0xE4,
        "shift": 0xE1, "lshift": 0xE1, "rshift": 0xE5,
        "alt": 0xE2, "lalt": 0xE2, "ralt": 0xE6,
        "win": 0xE3, "lwin": 0xE3, "rwin": 0xE7,
    })
    return m


_HID: dict[str, int] = _build_hid_map()

# type_char: carácter ASCII -> (token, ¿shift?). Distribución US; para otros
# caracteres (ñ, tildes...) el resultado depende de la distribución del sistema, así
# que se avisan y se omiten. La app es de teclas, no de texto, así que es un extra.
_CHAR_MAP: dict[str, tuple[str, bool]] = {}
for _c in "abcdefghijklmnopqrstuvwxyz":
    _CHAR_MAP[_c] = (_c, False)
    _CHAR_MAP[_c.upper()] = (_c, True)
for _c in "1234567890":
    _CHAR_MAP[_c] = (_c, False)
for _plain, _shift in zip("1234567890", "!@#$%^&*()"):
    _CHAR_MAP[_shift] = (_plain, True)
_CHAR_MAP.update({
    " ": ("space", False), "\n": ("enter", False), "\t": ("tab", False),
})


def list_ports() -> list[str]:
    """Puertos serie candidatos (los de VID conocido primero). [] si no hay pyserial."""
    try:
        from serial.tools import list_ports as _lp
    except Exception:
        return []
    ports = list(_lp.comports())
    pref = [p.device for p in ports if getattr(p, "vid", None) in _KNOWN_VIDS]
    rest = [p.device for p in ports if p.device not in pref]
    return pref + rest


# Botones de ratón soportados -> máscara de adafruit_hid.Mouse (bit 1 izq, 2 der, 4 central).
_MOUSE_BUTTONS: dict[str, int] = {"mouse_left": 1, "mouse_right": 2, "mouse_middle": 4}


def _resolve(token: str) -> tuple[str, int, str]:
    """(kind, code, token_normalizado). ``kind`` = 'key' | 'mouse'. ValueError si no se soporta."""
    tok = keys.normalize(token)
    if tok in _MOUSE_BUTTONS:
        return ("mouse", _MOUSE_BUTTONS[tok], tok)
    code = _HID.get(tok)
    if code is not None:
        return ("key", code, tok)
    if tok in keys.MOUSE_TOKENS:  # mouse_x1 / mouse_x2: no en el descriptor estándar
        raise ValueError(f"El backend HID hace clic izquierdo/derecho/central; {token!r} "
                         "necesitaría un descriptor de ratón extendido")
    raise ValueError(f"Tecla o botón no soportado por el backend HID: {token!r}")


def _cmd(kind: str, code: int, down: bool) -> str:
    """Línea del protocolo serie para pulsar/soltar una tecla o un botón de ratón."""
    if kind == "mouse":
        return f"{'MD' if down else 'MU'} {code}"
    return f"{'D' if down else 'U'} {code}"


class HidSerialBackend:
    """Backend ``InputBackend`` que teclea a través de una placa HID por serie."""

    def __init__(self, port: Optional[str] = None, *, transport=None, baud: int = 115200,
                 timeout: float = 0.5, connect: bool = True, handshake: bool = True) -> None:
        self.baud = baud
        self.timeout = timeout
        self._lock = threading.Lock()
        self._pressed: dict[str, None] = {}  # tokens pulsados y no soltados (conjunto ordenado)
        self._port: Optional[str] = None
        if transport is not None:
            self._ser = transport               # inyectado (tests / transporte propio)
            self._port = port
        elif connect:
            self._ser = None
            self._connect(port, handshake)
        else:
            self._ser = None                    # se conecta luego con .connect()

    # ------------------------------------------------------------------ conexión
    def connect(self, port: Optional[str] = None, *, handshake: bool = True) -> None:
        self._connect(port, handshake)

    def _connect(self, port: Optional[str], handshake: bool) -> None:
        try:
            import serial  # noqa: F401  (pyserial)
        except Exception as exc:  # noqa: BLE001
            raise HidSerialError(
                "Falta 'pyserial'. Instálalo con:  pip install pyserial") from exc
        candidates = [port] if port else list_ports()
        if not candidates:
            raise HidSerialError("No se encontró ningún puerto serie. ¿Está la placa conectada?")
        last_err: Optional[Exception] = None
        for dev in candidates:
            ser = self._try_open(dev, handshake)
            if ser is not None:
                with self._lock:
                    self._ser = ser
                    self._port = dev
                log.info("Placa HID conectada en %s", dev)
                return
            last_err = getattr(self, "_last_err", None)
        raise HidSerialError(
            f"No respondió ninguna placa HID (probados: {', '.join(map(str, candidates))}). "
            f"¿Firmware cargado? Detalle: {last_err}")

    def _try_open(self, dev: str, handshake: bool):
        import serial
        self._last_err = None
        try:
            ser = serial.Serial(dev, self.baud, timeout=self.timeout, write_timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001
            self._last_err = exc
            return None
        if not handshake:
            return ser
        try:
            time.sleep(0.05)
            ser.reset_input_buffer()
            ser.write(b"P\n")
            ser.flush()
            end = time.monotonic() + max(0.4, self.timeout * 4)
            while time.monotonic() < end:
                line = ser.readline()
                if b"PONG" in line:
                    return ser
            self._last_err = RuntimeError("sin respuesta PONG")
        except Exception as exc:  # noqa: BLE001
            self._last_err = exc
        try:
            ser.close()
        except Exception:  # noqa: BLE001
            pass
        return None

    def close(self) -> None:
        with self._lock:
            ser, self._ser = self._ser, None
        if ser is not None:
            try:
                ser.close()
            except Exception:  # noqa: BLE001
                pass

    # --------------------------------------------------------------------- envío
    def _write_locked(self, line: str) -> None:
        if self._ser is None:
            raise HidSerialError("La placa HID no está conectada.")
        self._ser.write((line + "\n").encode("ascii"))
        flush = getattr(self._ser, "flush", None)
        if callable(flush):
            flush()

    # ------------------------------------------------------------- API InputBackend
    def press(self, tokens: Iterable[str]) -> None:
        resolved = [_resolve(t) for t in tokens]  # ValueError antes de enviar nada
        with self._lock:
            for kind, code, tok in resolved:
                self._write_locked(_cmd(kind, code, True))
                self._pressed.setdefault(tok, None)

    def release(self, tokens: Iterable[str]) -> None:
        with self._lock:
            for t in tokens:
                try:
                    kind, code, tok = _resolve(t)
                except ValueError:
                    continue  # nunca romper una suelta (se llama desde finally)
                self._write_locked(_cmd(kind, code, False))
                self._pressed.pop(tok, None)

    def release_all(self) -> bool:
        with self._lock:
            self._pressed.clear()
            try:
                self._write_locked("X")
                return True
            except Exception as exc:  # noqa: BLE001 - se llama desde finally
                log.warning("No se pudo soltar todo en la placa HID: %s", exc)
                return False

    def type_char(self, ch: str) -> None:
        entry = _CHAR_MAP.get(ch)
        if entry is None:
            log.warning("type_char: carácter %r no soportado por el backend HID (depende de "
                        "la distribución); se omite", ch)
            return
        token, shift = entry
        _kind, code, _tok = _resolve(token)  # siempre una tecla
        with self._lock:
            if shift:
                self._write_locked(f"D {_HID['lshift']}")
            self._write_locked(f"D {code}")
            self._write_locked(f"U {code}")
            if shift:
                self._write_locked(f"U {_HID['lshift']}")

    # --- Solo teclado: el ratón no aplica -------------------------------------
    def move_to(self, x: int, y: int) -> None:
        log.debug("HidSerialBackend no envía movimiento de ratón; move_to(%s, %s) ignorado", x, y)

    def move_rel(self, dx: int, dy: int) -> None:
        log.debug("HidSerialBackend no envía movimiento de ratón; move_rel(%s, %s) ignorado", dx, dy)

    def scroll(self, notches: int, horizontal: bool = False) -> None:
        log.debug("HidSerialBackend no envía movimiento de ratón; scroll(%s) ignorado", notches)

    def cursor_pos(self) -> tuple[int, int]:
        try:
            from . import winput
            return winput.get_cursor_pos()
        except Exception:  # noqa: BLE001
            return (0, 0)

    def screen_size(self) -> tuple[int, int]:
        try:
            from . import winput
            return winput.screen_size()
        except Exception:  # noqa: BLE001
            return (0, 0)

    # --- Compatibilidad con el manejo de "sueltas pendientes" del motor -------
    @property
    def pressed(self) -> list[str]:
        with self._lock:
            return list(self._pressed)

    @property
    def pending_release(self) -> list[str]:
        return []  # el envío por serie no tiene el problema de UIPI de SendInput

    def retry_pending_release(self) -> bool:
        return True
