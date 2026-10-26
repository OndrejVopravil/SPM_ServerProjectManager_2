"""Vyrobí soubor ikony pro SPM.exe (tools/spm.ico) z kresby v spm/gui/app_icon.py.

Spuštění: .venv\\Scripts\\python.exe tools\\make_icon.py  (spouští ho i build.bat)
ICO obsahuje všechny velikosti jako PNG (Windows Vista a novější).

S parametrem --web vyrobí ikony pro web statistiky (web/favicon.ico a PNG pro prohlížeče,
telefony a nadpis stránky): .venv\\Scripts\\python.exe tools\\make_icon.py --web
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
WEB = Path(__file__).resolve().parent.parent / "web"
# web: (soubor, velikost) – favicona v PNG, ikona pro telefony (apple-touch-icon), nadpis stránky
WEB_PNGS = (("icon-32.png", 32), ("icon-96.png", 96), ("apple-touch-icon.png", 180), ("icon-192.png", 192))


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


def write_web_icons(folder: Path) -> list[Path]:
    """Ikony pro web: favicon.ico (16–256 px) a PNG; vrací vytvořené soubory."""
    written = [folder / "favicon.ico"]
    write_ico(written[0])
    for name, size in WEB_PNGS:
        (folder / name).write_bytes(png_bytes(size))
        written.append(folder / name)
    return written


if __name__ == "__main__":
    app = QGuiApplication.instance() or QGuiApplication(sys.argv)
    if "--web" in sys.argv:
        for path in write_web_icons(WEB):
            print(f"Ikona webu: {path}")
    else:
        write_ico(OUT)
        print(f"Ikona: {OUT}")
