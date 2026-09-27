"""Demo rápida del backend HID real: teclea una frase a través de la placa.

Requisitos: la Pico con el firmware de ``firmware/`` cargado y ``pip install pyserial``.

Uso:
    python tools/hid_demo.py                 # autodetecta la placa
    python tools/hid_demo.py --port COM7     # puerto concreto
    python tools/hid_demo.py --text "hola"   # frase a teclear

Al ejecutarlo hay 3 s de cuenta atrás: enfoca el Bloc de notas (o tu app) y verás el
texto aparecer, tecleado como hardware real. Ábrelo junto a tools/input_inspector.py
para comprobar que NO sale como "inyectado".
"""
from __future__ import annotations

import argparse
import sys
import time


def main() -> int:
    parser = argparse.ArgumentParser(description="Demo de tecleo por HID real (placa Pico).")
    parser.add_argument("--port", default=None, help="Puerto serie (p. ej. COM7). Por defecto: autodetecta.")
    parser.add_argument("--text", default="MacroTool via HID real OK", help="Texto a teclear.")
    parser.add_argument("--countdown", type=int, default=3, help="Segundos antes de teclear.")
    args = parser.parse_args()

    # Permite ejecutarlo tanto con 'python tools/hid_demo.py' como con '-m'.
    sys.path.insert(0, ".")
    try:
        from macrotool.hidserial import HidSerialBackend, HidSerialError
    except ImportError as exc:
        print(f"No se pudo importar el backend: {exc}")
        return 2

    try:
        backend = HidSerialBackend(port=args.port)
    except HidSerialError as exc:
        print(f"[ERROR] {exc}")
        print("Comprueba: placa enchufada, firmware cargado (firmware/), y 'pip install pyserial'.")
        return 1

    print(f"Conectada la placa en {backend._port}.")
    for i in range(args.countdown, 0, -1):
        print(f"  Tecleo en {i}...  (enfoca la ventana destino)")
        time.sleep(1)

    try:
        for ch in args.text:
            backend.type_char(ch)
            time.sleep(0.01)
        backend.press(["enter"])
        backend.release(["enter"])
    finally:
        backend.release_all()
        backend.close()

    print("Hecho. Revisa la ventana destino y el inspector de origen de input.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
