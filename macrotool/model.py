"""Modelo de datos: pasos de una macro, la macro en sí y los ajustes globales.

Todo es serializable a JSON mediante ``to_dict`` / ``from_dict``. Este módulo no
depende de Qt ni de Win32; sólo importa ``keys`` de forma perezosa para generar
descripciones legibles.
"""
from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field, fields
from typing import Any, ClassVar, Optional

SCHEMA_VERSION = 1

# Registro TYPE -> clase de paso, rellenado por el decorador ``_register``.
STEP_TYPES: dict[str, type["Step"]] = {}

PRESS_ACTIONS = ("tap", "down", "up")
TRIGGER_MODES = ("once", "toggle", "hold")

# Rangos admitidos (los mismos que ofrece la interfaz). Al cargar datos externos (macros.json
# editado a mano, una importación, el portapapeles) los valores se recortan a estos límites para
# que el panel muestre exactamente lo que usará el motor (y Qt no reciba enteros desbordados).
MAX_DELAY_MS = 600_000  # retardo entre acciones / tras un paso, pulsación, duración, cuenta atrás
MAX_REPEAT_DELAY_MS = 3_600_000
MAX_JITTER_MS = 60_000
MAX_STAGGER_MS = 1_000
MAX_REPEATS = 1_000_000
MAX_COUNT = 10_000
MAX_CHAR_INTERVAL_MS = 10_000
MAX_WAIT_MS = 86_400_000
MAX_SCROLL_NOTCHES = 1_000
COORD_LIMIT = 100_000  # coordenadas y desplazamientos: de -COORD_LIMIT a +COORD_LIMIT


def _register(cls: type["Step"]) -> type["Step"]:
    STEP_TYPES[cls.TYPE] = cls
    return cls


_TRUE_STRINGS = frozenset({"1", "true", "yes", "y", "on", "si", "sí", "verdadero"})
_FALSE_STRINGS = frozenset({"0", "false", "no", "n", "off", "", "none", "null", "falso"})


def _to_int(value: Any) -> int:
    """Entero a partir de int, float o texto numérico ("150", "150.0")."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(round(value))  # ValueError/OverflowError con nan/inf
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text)
        except ValueError:
            return int(round(float(text)))
    raise TypeError(f"no es un número: {value!r}")


def _to_bool(value: Any) -> bool:
    """Booleano que entiende "false", "0", "no"... (``bool("false")`` sería True)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_STRINGS:
            return True
        if text in _FALSE_STRINGS:
            return False
    raise ValueError(f"no es un booleano: {value!r}")


def _to_str_list(value: Any) -> list[str]:
    """Lista de strings; un texto "ctrl+s" se trata como combinación."""
    if isinstance(value, str):
        items: Any = value.split("+")
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        raise TypeError(f"no es una lista: {value!r}")
    result: list[str] = []
    for item in items:
        if isinstance(item, bool) or not isinstance(item, (str, int)):
            continue  # se descartan null, objetos, listas anidadas...
        text = str(item).strip()
        if text:
            result.append(text)
    return result


def _coerce(value: Any, default: Any) -> Any:
    """Convierte ``value`` al tipo del valor por defecto cuando es razonable.

    Pensado para JSON "sucio": números como texto, booleanos como "false",
    ``null`` en campos obligatorios... Si no se puede convertir, se usa el
    valor por defecto. Los campos cuyo defecto es ``None`` son todos
    ``Optional[int]`` (retardo propio, coordenadas).
    """
    if value is None:
        return default
    try:
        if default is None:
            return _to_int(value)
        if isinstance(default, bool):
            return _to_bool(value)
        if isinstance(default, int):
            return _to_int(value)
        if isinstance(default, float):
            return float(value)
        if isinstance(default, str):
            if isinstance(value, (str, int, float, bool)):
                return str(value)
            return default
        if isinstance(default, list):
            return _to_str_list(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return value


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _clamp_opt(value: Optional[int], low: int, high: int) -> Optional[int]:
    return None if value is None else _clamp(value, low, high)


def _combo_text(tokens: list[str]) -> str:
    from . import keys  # import perezoso: keys usa ctypes

    return keys.format_combo(tokens)


def _pos_text(x: Optional[int], y: Optional[int]) -> str:
    return f" en ({x}, {y})" if x is not None and y is not None else ""


@dataclass
class Step:
    """Clase base de todos los pasos."""

    TYPE: ClassVar[str] = ""
    LABEL: ClassVar[str] = ""

    enabled: bool = True
    # Retardo tras el paso. None -> se usa ``Macro.default_delay_ms``
    # (salvo en WaitStep, donde None significa 0).
    delay_after_ms: Optional[int] = None
    comment: str = ""

    def describe(self) -> str:
        raise NotImplementedError

    def _sanitize(self) -> None:
        """Corrige valores fuera de rango tras cargar datos externos."""
        self.delay_after_ms = _clamp_opt(self.delay_after_ms, 0, MAX_DELAY_MS)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"type": self.TYPE}
        for f in fields(self):
            data[f.name] = copy.deepcopy(getattr(self, f.name))
        return data

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Step":
        if not isinstance(data, dict):
            raise ValueError(f"Paso no válido: {data!r}")
        step_type = data.get("type")
        cls = STEP_TYPES.get(step_type) if isinstance(step_type, str) else None
        if cls is None:
            raise ValueError(f"Tipo de paso desconocido: {step_type!r}")
        defaults = cls()
        kwargs: dict[str, Any] = {}
        for f in fields(cls):
            if f.name in data:
                kwargs[f.name] = _coerce(data[f.name], getattr(defaults, f.name))
        step = cls(**kwargs)
        step._sanitize()
        return step

    def clone(self) -> "Step":
        return Step.from_dict(self.to_dict())


@_register
@dataclass
class PressStep(Step):
    """Pulsa a la vez una o varias teclas y/o botones del ratón.

    ``inputs`` contiene nombres canónicos de ``keys`` ("w", "ctrl", "space"...)
    o de botones del ratón ("mouse_left", "mouse_right", "mouse_middle",
    "mouse_x1", "mouse_x2"). Todas se pulsan juntas, se mantienen ``hold_ms`` y
    se sueltan en orden inverso.
    """

    TYPE: ClassVar[str] = "press"
    LABEL: ClassVar[str] = "Pulsación"

    inputs: list[str] = field(default_factory=lambda: ["enter"])
    action: str = "tap"  # tap | down | up
    hold_ms: int = 50
    count: int = 1  # repeticiones del paso (2 = doble clic); separadas por hold_ms
    x: Optional[int] = None  # si x e y están definidos, se mueve el ratón antes
    y: Optional[int] = None

    def _sanitize(self) -> None:
        super()._sanitize()
        if self.action not in PRESS_ACTIONS:
            self.action = "tap"
        self.hold_ms = _clamp(self.hold_ms, 0, MAX_DELAY_MS)
        self.count = _clamp(self.count, 1, MAX_COUNT)
        self.x = _clamp_opt(self.x, -COORD_LIMIT, COORD_LIMIT)
        self.y = _clamp_opt(self.y, -COORD_LIMIT, COORD_LIMIT)

    def describe(self) -> str:
        combo = _combo_text(self.inputs) if self.inputs else "(vacío)"
        if self.action == "down":
            text = f"Mantener {combo}"
        elif self.action == "up":
            text = f"Soltar {combo}"
        else:
            text = combo
            if self.count > 1:
                text += f" ×{self.count}"
        return text + _pos_text(self.x, self.y)


@_register
@dataclass
class TextStep(Step):
    """Escribe un texto carácter a carácter (Unicode; los saltos de línea pulsan Intro)."""

    TYPE: ClassVar[str] = "text"
    LABEL: ClassVar[str] = "Texto"

    text: str = ""
    char_interval_ms: int = 20

    def _sanitize(self) -> None:
        super()._sanitize()
        self.char_interval_ms = _clamp(self.char_interval_ms, 0, MAX_CHAR_INTERVAL_MS)

    def describe(self) -> str:
        shown = self.text.replace("\r\n", "\n").replace("\n", " ⏎ ").replace("\t", " ⇥ ")
        if len(shown) > 40:
            shown = shown[:39] + "…"
        return f"Escribir «{shown}»"


@_register
@dataclass
class MoveStep(Step):
    """Mueve el ratón a una posición absoluta (píxeles físicos) o relativa."""

    TYPE: ClassVar[str] = "move"
    LABEL: ClassVar[str] = "Mover ratón"

    x: int = 0
    y: int = 0
    relative: bool = False
    duration_ms: int = 0  # 0 = instantáneo; >0 = movimiento suave

    def _sanitize(self) -> None:
        super()._sanitize()
        self.duration_ms = _clamp(self.duration_ms, 0, MAX_DELAY_MS)
        self.x = _clamp(self.x, -COORD_LIMIT, COORD_LIMIT)
        self.y = _clamp(self.y, -COORD_LIMIT, COORD_LIMIT)

    def describe(self) -> str:
        if self.relative:
            text = f"Mover ratón {self.x:+d}, {self.y:+d} (relativo)"
        else:
            text = f"Mover ratón a ({self.x}, {self.y})"
        if self.duration_ms > 0:
            text += f" en {self.duration_ms} ms"
        return text


@_register
@dataclass
class ScrollStep(Step):
    """Gira la rueda del ratón. ``amount`` en muescas: positivo = arriba/derecha."""

    TYPE: ClassVar[str] = "scroll"
    LABEL: ClassVar[str] = "Rueda"

    amount: int = -3
    horizontal: bool = False
    x: Optional[int] = None
    y: Optional[int] = None

    def _sanitize(self) -> None:
        super()._sanitize()
        self.amount = _clamp(self.amount, -MAX_SCROLL_NOTCHES, MAX_SCROLL_NOTCHES)
        self.x = _clamp_opt(self.x, -COORD_LIMIT, COORD_LIMIT)
        self.y = _clamp_opt(self.y, -COORD_LIMIT, COORD_LIMIT)

    def describe(self) -> str:
        n = abs(self.amount)
        if self.horizontal:
            direction = "→ derecha" if self.amount > 0 else "← izquierda"
        else:
            direction = "↑ arriba" if self.amount > 0 else "↓ abajo"
        return f"Rueda {direction} ×{n}" + _pos_text(self.x, self.y)


@_register
@dataclass
class WaitStep(Step):
    """Pausa explícita dentro de la secuencia."""

    TYPE: ClassVar[str] = "wait"
    LABEL: ClassVar[str] = "Espera"

    ms: int = 500
    random_extra_ms: int = 0  # se suma un aleatorio en [0, random_extra_ms]

    def _sanitize(self) -> None:
        super()._sanitize()
        self.ms = _clamp(self.ms, 0, MAX_WAIT_MS)
        self.random_extra_ms = _clamp(self.random_extra_ms, 0, MAX_WAIT_MS)

    def describe(self) -> str:
        text = f"Esperar {self.ms} ms"
        if self.random_extra_ms > 0:
            text += f" (+0–{self.random_extra_ms} ms aleatorio)"
        return text


def new_id() -> str:
    return uuid.uuid4().hex


@dataclass
class Macro:
    id: str = field(default_factory=new_id)
    name: str = "Nueva macro"
    steps: list[Step] = field(default_factory=list)

    # Disparador
    enabled: bool = True  # si False, el disparador no hace nada
    trigger: str = ""  # combinación: "1", "ctrl+1", "mouse_x1"... ("" = sin disparador)
    trigger_mode: str = "once"  # once | toggle | hold
    block_trigger: bool = True  # no dejar pasar la tecla del disparador a la aplicación

    # Tiempos
    default_delay_ms: int = 100  # retardo entre acciones
    repeat_count: int = 1  # 0 = infinito
    repeat_delay_ms: int = 0  # pausa adicional entre repeticiones
    start_delay_ms: int = 0  # cuenta atrás antes de empezar

    # Humanización: pequeñas variaciones aleatorias para que parezca hecho a mano.
    # Con ``humanize=False`` todo es exacto (y se ignoran los tres valores siguientes).
    humanize: bool = True
    jitter_ms: int = 15  # variación ± del retardo tras cada paso y entre repeticiones
    hold_jitter_ms: int = 10  # variación ± de la duración de las pulsaciones
    chord_stagger_ms: int = 8  # desfase máximo entre entradas "a la vez" (0 = un único lote)

    def effective_delay_after(self, step: Step) -> int:
        """Retardo base tras ``step`` (sin jitter)."""
        if step.delay_after_ms is not None:
            return max(0, int(step.delay_after_ms))
        if isinstance(step, WaitStep):
            return 0
        return max(0, int(self.default_delay_ms))

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        for f in fields(self):
            if f.name == "steps":
                data["steps"] = [s.to_dict() for s in self.steps]
            else:
                data[f.name] = copy.deepcopy(getattr(self, f.name))
        return data

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Macro":
        if not isinstance(data, dict):
            raise ValueError(f"Macro no válida: {data!r}")
        defaults = Macro()
        kwargs: dict[str, Any] = {}
        for f in fields(Macro):
            if f.name not in data:
                continue
            if f.name == "steps":
                raw_steps = data.get("steps")
                steps = []
                for raw in raw_steps if isinstance(raw_steps, list) else []:
                    try:
                        steps.append(Step.from_dict(raw))
                    except (ValueError, TypeError, KeyError):
                        continue  # se ignoran pasos corruptos o desconocidos
                kwargs["steps"] = steps
            else:
                kwargs[f.name] = _coerce(data[f.name], getattr(defaults, f.name))
        macro = Macro(**kwargs)
        if not macro.id.strip():
            macro.id = new_id()
        if macro.trigger_mode not in TRIGGER_MODES:
            macro.trigger_mode = "once"
        # Los tiempos negativos no tienen sentido (repeat_count 0 = infinito) y los enormes
        # se recortan a lo que admite la interfaz.
        for name, high in _MACRO_LIMITS.items():
            setattr(macro, name, _clamp(getattr(macro, name), 0, high))
        return macro

    def clone(self, new_identity: bool = False) -> "Macro":
        macro = Macro.from_dict(self.to_dict())
        if new_identity:
            macro.id = new_id()
        return macro


_MACRO_LIMITS = {
    "default_delay_ms": MAX_DELAY_MS,
    "repeat_count": MAX_REPEATS,
    "repeat_delay_ms": MAX_REPEAT_DELAY_MS,
    "start_delay_ms": MAX_DELAY_MS,
    "jitter_ms": MAX_JITTER_MS,
    "hold_jitter_ms": MAX_JITTER_MS,
    "chord_stagger_ms": MAX_STAGGER_MS,
}


@dataclass
class Settings:
    # Atajos globales de control ("" = desactivado)
    hotkey_run_selected: str = "f6"  # ejecutar / detener la macro seleccionada
    hotkey_stop: str = "f7"  # detener cualquier macro en curso
    hotkey_record: str = "f8"  # iniciar / detener grabación
    hotkey_enable: str = "f9"  # activar / desactivar todos los disparadores
    hotkey_pause: str = ""  # pausar / reanudar

    triggers_enabled: bool = True
    ignore_triggers_when_focused: bool = True  # no disparar si MacroTool está en primer plano
    use_scancodes: bool = True  # compatibilidad con juegos (DirectInput)
    use_hid_backend: bool = False  # enviar por placa HID (teclado real) en vez de SendInput
    minimize_on_run: bool = False
    minimize_to_tray: bool = True  # cerrar la ventana la oculta en la bandeja
    always_on_top: bool = False
    failsafe_corner: bool = False  # esquina de la pantalla principal = parada de emergencia
    always_admin: bool = False  # relanzar siempre como administrador al abrir (elevación automática)

    record_mouse_moves: bool = False
    record_click_positions: bool = False
    record_timing: bool = True

    last_macro_id: str = ""
    window_geometry: str = ""  # base64 de QWidget.saveGeometry()

    def to_dict(self) -> dict[str, Any]:
        return {f.name: copy.deepcopy(getattr(self, f.name)) for f in fields(self)}

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Settings":
        if not isinstance(data, dict):
            raise ValueError(f"Ajustes no válidos: {data!r}")
        defaults = Settings()
        kwargs = {
            f.name: _coerce(data[f.name], getattr(defaults, f.name))
            for f in fields(Settings)
            if f.name in data
        }
        return Settings(**kwargs)


def example_macros() -> list[Macro]:
    """Macros precargadas en el primer arranque: los tres combos del usuario (teclas 1, 2 y 3)."""

    def combo(number: int, steps: list[Step]) -> Macro:
        return Macro(
            name=f"Combo {number}",
            enabled=True,
            trigger=str(number),
            trigger_mode="once",
            block_trigger=True,
            default_delay_ms=150,
            steps=steps,
        )

    return [
        combo(1, [
            PressStep(inputs=["w", "mouse_right"]),
            PressStep(inputs=["s", "q"]),
            PressStep(inputs=["s", "mouse_left"]),
            PressStep(inputs=["space"]),
            PressStep(inputs=["mouse_left", "mouse_right"]),
        ]),
        combo(2, [
            PressStep(inputs=["mouse_right"]),
            PressStep(inputs=["f"]),
            PressStep(inputs=["s", "c"]),
            PressStep(inputs=["shift", "mouse_right"]),
            PressStep(inputs=["mouse_left"], hold_ms=1000, comment="HOLD LMB: mantener pulsado"),
            PressStep(inputs=["s", "f"]),
        ]),
        combo(3, [
            PressStep(inputs=["mouse_left", "mouse_right"]),
            PressStep(inputs=["shift", "mouse_right"]),
            PressStep(inputs=["s", "mouse_left"]),
            PressStep(inputs=["f"]),
            PressStep(inputs=["s", "mouse_right"]),
            PressStep(inputs=["shift", "q"]),
        ]),
    ]


def example_macro() -> Macro:
    """Macro de ejemplo que se crea en el primer arranque (desactivada)."""
    return Macro(
        name="Ejemplo: combo con la tecla 1",
        enabled=False,
        trigger="1",
        trigger_mode="once",
        block_trigger=True,
        default_delay_ms=150,
        steps=[
            PressStep(inputs=["w", "mouse_right"]),
            PressStep(inputs=["s", "q"]),
            PressStep(inputs=["s", "mouse_left"]),
            PressStep(inputs=["space"]),
            PressStep(inputs=["mouse_left", "mouse_right"]),
        ],
    )
