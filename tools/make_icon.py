"""Genera assets/icon.png (256 px) y assets/icon.ico (16–256 px) pintando el icono con QPainter.

Uso (desde la raíz del proyecto):
    python tools/make_icon.py

El dibujo está en ``macrotool.ui.theme.paint_app_icon`` (el mismo que usa la app si faltan los
archivos). El .ico se escribe a mano con ``struct``: entradas BMP (32 bits con alfa) hasta 128 px
y PNG para 256 px, el formato que Windows y PyInstaller aceptan sin problemas.
"""
from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # no hace falta ninguna ventana

from PySide6.QtCore import QBuffer, QIODevice  # noqa: E402
from PySide6.QtGui import QGuiApplication, QImage  # noqa: E402

from macrotool.ui.theme import render_app_icon  # noqa: E402

ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
PNG_SIZE = 256


def png_bytes(img: QImage) -> bytes:
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


def dib_bytes(img: QImage) -> bytes:
    """Imagen en formato DIB de icono: BITMAPINFOHEADER + BGRA de abajo arriba + máscara AND."""
    img = img.convertToFormat(QImage.Format_ARGB32)  # BGRA en memoria (little endian)
    w, h = img.width(), img.height()
    header = struct.pack("<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    rows = []
    for y in range(h - 1, -1, -1):
        line = img.constScanLine(y)
        rows.append(bytes(line)[: w * 4])
    mask_stride = ((w + 31) // 32) * 4
    and_mask = bytes(mask_stride * h)  # todo 0: la transparencia la da el canal alfa
    return header + b"".join(rows) + and_mask


def write_ico(path: Path, images: list[QImage]) -> None:
    entries: list[bytes] = []
    blobs: list[bytes] = []
    offset = 6 + 16 * len(images)
    for img in images:
        size = img.width()
        blob = png_bytes(img) if size >= 256 else dib_bytes(img)
        dim = 0 if size >= 256 else size  # 0 significa 256 en el formato ICO
        entries.append(struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(blob), offset))
        offset += len(blob)
        blobs.append(blob)
    path.write_bytes(struct.pack("<HHH", 0, 1, len(images)) + b"".join(entries) + b"".join(blobs))


def main() -> int:
    app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
    assets = ROOT / "assets"
    assets.mkdir(exist_ok=True)
    png_path = assets / "icon.png"
    ico_path = assets / "icon.ico"
    if not render_app_icon(PNG_SIZE).save(str(png_path), "PNG"):
        print(f"No se pudo guardar {png_path}", file=sys.stderr)
        return 1
    write_ico(ico_path, [render_app_icon(size) for size in ICO_SIZES])
    print(f"Generado {png_path.relative_to(ROOT)} y {ico_path.relative_to(ROOT)} "
          f"({', '.join(str(s) for s in ICO_SIZES)} px)")
    del app
    return 0


if __name__ == "__main__":
    sys.exit(main())
