"""Lišta záložek s vlastní barvou každé záložky (světlá varianta ve světlém režimu, tmavá
v tmavém – text zůstane čitelný) a přesouváním záložek myší."""

from __future__ import annotations

from PySide6.QtCore import QEvent
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QStyle, QStyleOptionTab, QStylePainter, QTabBar, QWidget

# klíč -> (název, světlá varianta, tmavá varianta)
TAB_COLORS: dict[str, tuple[str, str, str]] = {
    "": ("Bez barvy", "", ""),
    "blue": ("Modrá", "#dbe9fb", "#1f3a5c"),
    "green": ("Zelená", "#dcf2dc", "#1f4a2a"),
    "orange": ("Oranžová", "#fde6cc", "#5a3a17"),
    "red": ("Červená", "#f9d9d9", "#5a2323"),
    "purple": ("Fialová", "#eadcf7", "#3e2a5a"),
    "teal": ("Tyrkysová", "#d6f3f1", "#1c4a47"),
    "yellow": ("Žlutá", "#faf3c8", "#4d4519"),
    "gray": ("Šedá", "#e6e6e6", "#3a3a3a"),
}


def is_dark(widget: QWidget) -> bool:
    """Tmavý režim – podle jasu pozadí okna."""
    return widget.palette().color(QPalette.ColorRole.Window).lightness() < 128


def tab_color(key: str, dark: bool) -> QColor | None:
    entry = TAB_COLORS.get(key or "")
    if not entry or not entry[1]:
        return None
    return QColor(entry[2] if dark else entry[1])


class ColorTabBar(QTabBar):
    """QTabBar, kde každá záložka může mít barvu pozadí (set_tab_color)."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._colors: dict[int, str] = {}  # index záložky -> klíč barvy
        self.tabMoved.connect(self._on_moved)

    def set_tab_color(self, index: int, key: str) -> None:
        self._colors[index] = key or ""
        self.update()

    def _on_moved(self, src: int, dst: int) -> None:
        """Přetažení záložky: barva se přesune s ní."""
        keys = [self._colors.get(i, "") for i in range(self.count())]
        keys.insert(dst, keys.pop(src))
        self._colors = dict(enumerate(keys))

    def tab_color_key(self, index: int) -> str:
        return self._colors.get(index, "")

    def tabInserted(self, index: int) -> None:  # noqa: N802 – Qt
        # posunout barvy za vloženou záložkou
        self._colors = {(i + 1 if i >= index else i): k for i, k in self._colors.items()}
        super().tabInserted(index)

    def tabRemoved(self, index: int) -> None:  # noqa: N802 – Qt
        self._colors = {(i - 1 if i > index else i): k for i, k in self._colors.items() if i != index}
        super().tabRemoved(index)

    def paintEvent(self, event) -> None:  # noqa: N802 – Qt
        if not any(self._colors.values()):
            super().paintEvent(event)
            return
        painter = QStylePainter(self)
        dark = is_dark(self)
        current = self.currentIndex()
        order = [i for i in range(self.count()) if i != current] + ([current] if current >= 0 else [])
        for i in order:  # vybraná záložka nakonec (překrývá sousední)
            opt = QStyleOptionTab()
            self.initStyleOption(opt, i)
            painter.drawControl(QStyle.ControlElement.CE_TabBarTabShape, opt)
            color = tab_color(self._colors.get(i, ""), dark)
            if color is not None:
                selected = i == current
                inner = opt.rect.adjusted(2, 2, -2, 0 if selected else -1)
                if not selected:  # nevybraná o něco méně sytě
                    color.setAlpha(190)
                painter.fillRect(inner, color)
            painter.drawControl(QStyle.ControlElement.CE_TabBarTabLabel, opt)

    def changeEvent(self, event) -> None:  # noqa: N802 – Qt
        if event.type() == QEvent.Type.PaletteChange:
            self.update()  # světlý / tmavý režim – jiná varianta barev
        super().changeEvent(event)
