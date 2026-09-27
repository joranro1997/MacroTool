"""Configuracion USB de la placa (se ejecuta ANTES que code.py, al arrancar).

- Expone SOLO un teclado HID (mas limpio y evidente para la demo).
- Habilita el canal de datos USB serie (usb_cdc.data) para recibir comandos.

Copia este archivo a la raiz de la unidad CIRCUITPY y REINICIA la placa (o pulsa el
boton reset) para que los cambios de USB surtan efecto.
"""
import usb_cdc
import usb_hid

# Solo teclado: la placa se presenta ante Windows como un teclado USB HID.
usb_hid.enable((usb_hid.Device.KEYBOARD,))

# console = REPL/errores ; data = canal por el que llegan los comandos D/U/X/P.
usb_cdc.enable(console=True, data=True)
