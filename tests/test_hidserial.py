"""Tests en seco del backend HID por serie (sin placa ni pyserial).

Se inyecta un ``FakeSerial`` como transporte, así que se comprueba el mapeo de teclas y
el protocolo serie sin hardware.
"""
from __future__ import annotations

import pytest

from macrotool.hidserial import HidSerialBackend
from macrotool.winput import InputBackend


class FakeSerial:
    """Transporte de mentira: guarda lo que se escribe."""

    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, data) -> int:
        self.writes.append(bytes(data))
        return len(data)

    def flush(self) -> None:
        pass

    def readline(self) -> bytes:
        return b"PONG\n"


def _backend() -> tuple[HidSerialBackend, FakeSerial]:
    fake = FakeSerial()
    return HidSerialBackend(transport=fake), fake


def _lines(fake: FakeSerial) -> list[str]:
    return b"".join(fake.writes).decode("ascii").splitlines()


def test_cumple_el_protocol_inputbackend():
    backend, _ = _backend()
    assert isinstance(backend, InputBackend)


def test_press_mapea_ctrl_c():
    backend, fake = _backend()
    backend.press(["ctrl", "c"])
    assert _lines(fake) == ["D 224", "D 6"]  # ctrl=0xE0=224, c=0x04+2=6


def test_release_manda_up_y_deja_de_seguir():
    backend, fake = _backend()
    backend.press(["ctrl", "c"])
    assert backend.pressed == ["ctrl", "c"]
    fake.writes.clear()
    backend.release(["c", "ctrl"])
    assert _lines(fake) == ["U 6", "U 224"]
    assert backend.pressed == []


@pytest.mark.parametrize("token,code", [
    ("a", 4), ("z", 29), ("0", 39), ("1", 30), ("enter", 40), ("space", 44),
    ("esc", 41), ("f1", 58), ("f5", 62), ("f12", 69), ("f13", 104), ("up", 82),
    ("num0", 98), ("num5", 93), ("num_enter", 88), ("shift", 225), ("win", 227),
    ("ralt", 230),
])
def test_mapa_de_usages(token, code):
    backend, fake = _backend()
    backend.press([token])
    assert _lines(fake) == [f"D {code}"]


def test_release_all_manda_x():
    backend, fake = _backend()
    backend.press(["a"])
    fake.writes.clear()
    assert backend.release_all() is True
    assert _lines(fake) == ["X"]
    assert backend.pressed == []


def test_boton_de_raton_lanza_valueerror():
    backend, fake = _backend()
    with pytest.raises(ValueError):
        backend.press(["mouse_left"])
    assert fake.writes == []  # nada se envió


def test_tecla_no_soportada_lanza_valueerror():
    backend, fake = _backend()
    with pytest.raises(ValueError):
        backend.press(["volume_up"])  # multimedia no está en la página de teclado
    assert fake.writes == []


def test_type_char_mayuscula_usa_shift():
    backend, fake = _backend()
    backend.type_char("A")
    assert _lines(fake) == ["D 225", "D 4", "U 4", "U 225"]  # lshift, a down, a up, lshift up


def test_type_char_no_ascii_se_omite():
    backend, fake = _backend()
    backend.type_char("ñ")
    assert fake.writes == []  # depende de la distribución: se avisa y se omite


def test_press_no_envia_nada_si_una_tecla_es_invalida():
    backend, fake = _backend()
    with pytest.raises(ValueError):
        backend.press(["a", "mouse_left"])  # valida TODAS antes de enviar
    assert fake.writes == []
