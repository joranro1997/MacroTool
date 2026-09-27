"""Configuracion USB de la placa (se ejecuta ANTES que code.py, al arrancar).

- Expone SOLO un teclado HID (mas limpio y evidente para la demo).
- Habilita el canal de datos USB serie (usb_cdc.data) para recibir comandos.

Copia este archivo a la raiz de la unidad CIRCUITPY y REINICIA la placa (o pulsa el
boton reset) para que los cambios de USB surtan efecto.
"""
import usb_cdc
import usb_hid

# Teclado + raton: la placa se presenta ante Windows como un teclado y un raton USB HID.
# El raton solo se usa para CLICS; no se envia movimiento del cursor.
usb_hid.enable((usb_hid.Device.KEYBOARD, usb_hid.Device.MOUSE))

# console = REPL/errores ; data = canal por el que llegan los comandos D/U/X/P.
usb_cdc.enable(console=True, data=True)
