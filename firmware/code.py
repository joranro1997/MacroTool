"""Firmware CircuitPython: teclado + raton USB HID gobernado por serie.

Recibe comandos por el canal de datos USB (usb_cdc.data) y los reproduce como un
TECLADO y RATON USB reales (llegan sin el flag "inyectado" de SendInput):

    D <code>\\n   -> teclado.press(code)      (pulsar tecla)
    U <code>\\n   -> teclado.release(code)    (soltar tecla)
    MD <mask>\\n  -> mouse.press(mask)        (pulsar boton: 1 izq, 2 der, 4 central)
    MU <mask>\\n  -> mouse.release(mask)      (soltar boton)
    X\\n          -> release_all()            (soltar teclado y raton)
    P\\n          -> responde "PONG\\n"        (handshake que usa el backend)

<code> es un HID usage id (Keyboard/Keypad Page 0x07) en decimal. El raton solo hace
CLICS: no se envia movimiento del cursor.

Instalacion: copia este archivo y boot.py a la raiz de CIRCUITPY, y la carpeta
'adafruit_hid' a CIRCUITPY/lib. Reinicia la placa.
"""
import time

import usb_cdc
import usb_hid
from adafruit_hid.keyboard import Keyboard
from adafruit_hid.mouse import Mouse

serial = usb_cdc.data
kbd = Keyboard(usb_hid.devices)
mouse = Mouse(usb_hid.devices)


def handle(cmd):
    if not cmd:
        return
    op = cmd[0:1]
    if op == b"P":
        if serial is not None:
            serial.write(b"PONG\n")
    elif op == b"X":
        kbd.release_all()
        mouse.release_all()
    elif op == b"D":
        kbd.press(int(cmd[1:].strip()))  # ValueError si viene basura -> lo captura el bucle
    elif op == b"U":
        kbd.release(int(cmd[1:].strip()))
    elif op == b"M":  # MD/MU <mask>: clic de raton (1 izq, 2 der, 4 central)
        mask = int(cmd[2:].strip())
        if cmd[1:2] == b"D":
            mouse.press(mask)
        elif cmd[1:2] == b"U":
            mouse.release(mask)


buf = b""
while True:
    if serial is not None and serial.in_waiting:
        buf += serial.read(serial.in_waiting)
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            try:
                handle(line.strip())
            except Exception as exc:  # nunca morir por un comando malformado
                try:
                    serial.write(b"ERR " + str(exc).encode("utf-8") + b"\n")
                except Exception:
                    pass
    else:
        time.sleep(0.001)
