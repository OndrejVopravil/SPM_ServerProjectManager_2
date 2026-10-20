"""Vyrobí soubor ikony pro SPM.exe (tools/spm.ico) z kresby v spm/gui/app_icon.py.

Spuštění: .venv\\Scripts\\python.exe tools\\make_icon.py  (spouští ho i build.bat)
ICO obsahuje všechny velikosti jako PNG (Windows Vista a novější).
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QGuiApplication

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from spm.gui.app_icon import SIZES, draw  # noqa: E402

OUT = Path(__file__).resolve().parent / "spm.ico"


def png_bytes(size: int) -> bytes:
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    draw(size).save(buffer, "PNG")
    return bytes(data)


def write_ico(path: Path) -> None:
    images = [(size, png_bytes(size)) for size in SIZES]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries, blobs = b"", b""
    for size, png in images:
        dim = 0 if size >= 256 else size  # 0 = 256 px
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset)
        blobs += png
        offset += len(png)
    path.write_bytes(header + entries + blobs)


if __name__ == "__main__":
    app = QGuiApplication.instance() or QGuiApplication(sys.argv)
    write_ico(OUT)
    print(f"Ikona: {OUT}")
