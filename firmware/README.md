# Firmware HID para Raspberry Pi Pico 2 (teclado + clics de ratón)

Convierte la Pico en un **teclado y ratón USB reales** que MacroTool gobierna por puerto
serie. Lo que envía la placa llega a Windows por la ruta de hardware genuina, **sin** el
flag "inyectado" que pone `SendInput` (compruébalo con `tools/input_inspector.py`).

El ratón solo hace **clics** (izquierdo, derecho, central); **no** se envía movimiento del
cursor.

## Pasos (una sola vez)

1. **Instala CircuitPython** en la Pico 2:
   - Mantén pulsado **BOOTSEL** mientras la enchufas por USB → aparece una unidad `RPI-RP2`.
   - Descarga el `.uf2` de CircuitPython 10.x para *Raspberry Pi Pico 2*:
     https://circuitpython.org/board/raspberry_pi_pico2/
   - Arrastra el `.uf2` a la unidad `RPI-RP2`. La placa se reinicia como unidad **`CIRCUITPY`**.

2. **Copia la librería HID**:
   - Descarga el *Adafruit CircuitPython Library Bundle* (versión 10.x):
     https://circuitpython.org/libraries
   - Copia la carpeta **`adafruit_hid`** dentro de **`CIRCUITPY/lib/`**.

3. **Copia el firmware**:
   - Copia **`boot.py`** y **`code.py`** (de esta carpeta) a la **raíz** de `CIRCUITPY`.
   - **Reinicia** la placa (desenchufar/enchufar, o botón reset). `boot.py` solo se aplica al arrancar.

## Comprobar que funciona

- En el PC: `pip install pyserial`
- Con la placa enchufada:  `python tools/hid_demo.py`
  - Cuenta atrás de 3 s → enfoca el Bloc de notas (o tu app) → verás aparecer el texto
    tecleado por HID real.
- Abre a la vez `python tools/input_inspector.py`: al teclear la placa, el **canal 1**
  saldrá *hardware (no inyectado)* y el **canal 2** con un `hDevice` real — a diferencia
  del botón "SendInput", que sale *INYECTADO*.

## Notas

- Hace **teclado + clics de ratón** (izq./der./central). Sin movimiento del cursor ni botones laterales x1/x2.
- El puerto serie de datos aparece como un `COM` nuevo en Windows; `hidserial.py` lo
  autodetecta con un *handshake* (`P` → `PONG`), así que no necesitas saber el número.
