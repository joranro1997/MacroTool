"""Firmware CircuitPython: teclado USB HID gobernado por serie.

Recibe comandos por el canal de datos USB (usb_cdc.data) y los reproduce como un
TECLADO USB real (llegan sin el flag "inyectado" de SendInput):

    D <code>\\n  -> teclado.press(code)      (pulsar)
    U <code>\\n  -> teclado.release(code)    (soltar)
    X\\n         -> teclado.release_all()    (soltar todo)
    P\\n         -> responde "PONG\\n"        (handshake que usa el backend)

<code> es un HID usage id (Keyboard/Keypad Page 0x07) en decimal; los mismos que envia
macrotool/hidserial.py.

Instalacion: copia este archivo y boot.py a la raiz de CIRCUITPY, y la carpeta
'adafruit_hid' a CIRCUITPY/lib. Reinicia la placa.
"""
import time

import usb_cdc
import usb_hid
from adafruit_hid.keyboard import Keyboard

serial = usb_cdc.data
kbd = Keyboard(usb_hid.devices)


def handle(cmd):
    if not cmd:
        return
    op = cmd[0:1]
    if op == b"P":
        if serial is not None:
            serial.write(b"PONG\n")
    elif op == b"X":
        kbd.release_all()
    elif op == b"D" or op == b"U":
        code = int(cmd[1:].strip())  # ValueError si viene basura -> lo captura el bucle
        if op == b"D":
            kbd.press(code)
        else:
            kbd.release(code)


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
