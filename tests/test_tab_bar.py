"""Lišta záložek: aktivní záložka tučně, větším písmem a podtržená (i s barvou záložky)."""
import os

import pytest


@pytest.fixture
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("colors", [(), ("blue", "", "green")])
def test_active_tab_bold_larger_underlined(qapp, colors):
    from PySide6.QtGui import QPalette

    from spm.gui.tab_bar import UNDERLINE, ColorTabBar
    bar = ColorTabBar()
    bar.setDocumentMode(True)
    for name in ("Projekty", "Archiv 2025", "Nástroje"):
        bar.addTab(name)
    for i, c in enumerate(colors):
        bar.set_tab_color(i, c)
    font = bar.active_font()
    assert font.bold() and font.pointSizeF() > bar.font().pointSizeF()
    widths = [bar.tabSizeHint(i).width() for i in range(bar.count())]
    bar.setCurrentIndex(1)
    assert [bar.tabSizeHint(i).width() for i in range(bar.count())] == widths   # lišta se neposouvá
    bar.resize(bar.sizeHint())
    bar.show()
    qapp.processEvents()
    img = bar.grab().toImage()
    highlight = bar.palette().color(QPalette.ColorRole.Highlight)

    def underlined(index: int) -> bool:
        r = bar.tabRect(index)
        y = r.bottom() - UNDERLINE // 2
        return img.pixelColor(r.center().x(), y) == highlight
    assert underlined(1) and not underlined(0) and not underlined(2)
    bar.setCurrentIndex(2)
    qapp.processEvents()
    img = bar.grab().toImage()
    assert underlined(2) and not underlined(1)
    bar.close()
