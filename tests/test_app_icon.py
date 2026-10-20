"""Ikona aplikace: všechny velikosti a soubor .ico pro SPM.exe."""
import os
import struct
import sys
from pathlib import Path


def test_icon_sizes_and_ico_file(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from spm.gui.app_icon import SIZES, app_icon, draw
    icon = app_icon()
    assert sorted(s.width() for s in icon.availableSizes()) == sorted(SIZES)
    img = draw(32)
    assert img.pixelColor(16, 16).alpha() == 255 and img.pixelColor(0, 0).alpha() == 0  # průhledné rohy
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    import make_icon
    out = tmp_path / "spm.ico"
    make_icon.write_ico(out)
    data = out.read_bytes()
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert (reserved, kind, count) == (0, 1, len(SIZES))
    assert data[6 + 16 * count:][:8] == b"\x89PNG\r\n\x1a\n"
