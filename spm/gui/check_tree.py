"""Strom složek a souborů se zaškrtávátky (výběr při aktivaci, převzetí a odevzdání).

Zaškrtnutí složky vybere celý její obsah, podsložku pak lze odškrtnout (tristate).
Položky mají klíč; strom hlásí změnu výběru signálem toggled(klíč, zaškrtnuto).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QIcon, QImage, QPixmap
from PySide6.QtWidgets import QHeaderView, QTreeWidget, QTreeWidgetItem, QTreeWidgetItemIterator, QWidget

from . import icons

KEY_ROLE = Qt.ItemDataRole.UserRole
PATH_ROLE = Qt.ItemDataRole.UserRole + 1
SAVED_FOREGROUND_ROLE = Qt.ItemDataRole.UserRole + 2  # barvy textu před podbarvením hledáním


def in_scope(path: str, scope: str | list[str] | tuple[str, ...] | None) -> bool:
    """Patří soubor do předvýběru? scope = cesta souboru, nebo složky zakončená '/',
    případně seznam takových (víc označených položek ve stromu)."""
    if not scope:
        return False
    if not isinstance(scope, str):
        return any(in_scope(path, s) for s in scope)
    return path.startswith(scope) if scope.endswith("/") else path == scope


@dataclass
class TreeEntry:
    path: str                               # relativní cesta s '/'
    key: str                                # klíč pro výběr
    columns: list[str] = field(default_factory=list)  # text dalších sloupců
    enabled: bool = True                    # False = nelze vybrat (např. drží jiný uživatel)
    color: QColor | None = None             # barva textu dalších sloupců
    color_name: bool = False                # obarvit barvou i název souboru
    file_path: str | None = None            # skutečný soubor na disku – pro jeho vlastní ikonu
    tooltip: str = ""


class CheckTree(QTreeWidget):
    toggled = Signal(str, bool)

    def __init__(self, headers: list[str], checkable: bool = True, parent: QWidget | None = None):
        super().__init__(parent)
        self.checkable = checkable
        self.default_expanded = True  # složky, o kterých strom ještě neví, rozbalit?
        self.setHeaderLabels(headers)
        self.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for i in range(1, len(headers)):
            self.header().setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        self._items: dict[str, QTreeWidgetItem] = {}
        self._by_file: dict[str, QTreeWidgetItem] = {}  # soubor na disku -> položka (pro ikonu)
        self.itemChanged.connect(self._on_changed)
        self._shell_icons = icons.shell_icons_available()
        if self._shell_icons:
            icons.loader().loaded.connect(self._on_icon)

    # --- naplnění --------------------------------------------------------

    def set_entries(self, entries: list[TreeEntry], checked: set[str] = frozenset()) -> None:
        # sbalené / rozbalené složky zůstanou po novém naplnění tak, jak je uživatel nechal
        # (obnovení stromu stejného projektu); nové složky podle default_expanded
        previous = {item.data(0, PATH_ROLE): item.isExpanded() for item in self._folder_items()}
        self.blockSignals(True)
        self.clear()
        self._items.clear()
        self._by_file.clear()
        folders: dict[str, QTreeWidgetItem] = {}
        extra = self.columnCount() - 1
        dir_icon = icons.folder_icon()
        bold = QFont(self.font())
        bold.setBold(True)
        to_load: list[str] = []

        def folder(path: str) -> QTreeWidgetItem | None:
            if not path:
                return None
            if path not in folders:
                parent_path, _, name = path.rpartition("/")
                parent = folder(parent_path)
                item = QTreeWidgetItem([name] + [""] * extra)
                item.setData(0, PATH_ROLE, path + "/")
                if dir_icon.isNull():
                    item.setFont(0, bold)  # ikony nejsou k dispozici – složky aspoň tučně
                else:
                    item.setIcon(0, dir_icon)
                if self.checkable:
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsAutoTristate)
                    item.setCheckState(0, Qt.CheckState.Unchecked)
                (parent.addChild(item) if parent else self.addTopLevelItem(item))
                folders[path] = item
            return folders[path]

        for e in sorted(entries, key=lambda e: e.path.lower()):
            parent_path, _, name = e.path.rpartition("/")
            parent = folder(parent_path)
            item = QTreeWidgetItem([name] + (e.columns + [""] * extra)[:extra])
            item.setData(0, KEY_ROLE, e.key)
            item.setData(0, PATH_ROLE, e.path)
            if e.tooltip:
                item.setToolTip(0, e.tooltip)
            icon = None
            if e.file_path and self._shell_icons and icons.has_per_file_icon(name):
                icon = icons.loader().cached(e.file_path)
                if icon is None:
                    self._by_file[e.file_path] = item
                    to_load.append(e.file_path)
            item.setIcon(0, icon or icons.extension_icon(name))
            if e.color is not None:
                for c in range(0 if e.color_name else 1, extra + 1):
                    item.setForeground(c, e.color)
            if not e.enabled:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled & ~Qt.ItemFlag.ItemIsUserCheckable)
            elif self.checkable:
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(0, Qt.CheckState.Checked if e.key in checked else Qt.CheckState.Unchecked)
            (parent.addChild(item) if parent else self.addTopLevelItem(item))
            self._items[e.key] = item
        self.sortItems(0, Qt.SortOrder.AscendingOrder)
        for item in folders.values():
            item.setExpanded(previous.get(item.data(0, PATH_ROLE), self.default_expanded))
        self.blockSignals(False)
        if to_load:
            icons.loader().request(to_load)

    def set_column_texts(self, column: int, texts: dict[str, tuple[str, str]],
                         color: QColor | None = None) -> None:
        """Doplní text (a tooltip) sloupce podle cesty – soubor 'a/b.nc', složka 'a/'."""
        self.blockSignals(True)
        it = QTreeWidgetItemIterator(self)
        while it.value():
            item = it.value()
            text, tip = texts.get(item.data(0, PATH_ROLE), ("", ""))
            item.setText(column, text)
            item.setToolTip(column, tip)
            if color is not None and text:
                item.setForeground(column, color)
            it += 1
        self.blockSignals(False)

    def highlight(self, match, color: QColor) -> list[QTreeWidgetItem]:
        """Podbarví soubory, jejichž název odpovídá match(název) (None = zrušit podbarvení).
        Text podbarvených je černý (čitelný na světlém pozadí i v tmavém motivu); po zrušení
        se vrátí původní barva textu. Vrací podbarvené položky."""
        found = []
        empty = QBrush()
        black = QBrush(QColor("black"))
        columns = range(self.columnCount())
        self.blockSignals(True)
        for item in self.leaf_items():
            hit = match is not None and match(item.text(0))
            saved = item.data(0, SAVED_FOREGROUND_ROLE)
            if hit:
                if saved is None:  # původní barvy textu – jen poprvé
                    item.setData(0, SAVED_FOREGROUND_ROLE, [item.foreground(c) for c in columns])
                for c in columns:
                    item.setBackground(c, QBrush(color))
                    item.setForeground(c, black)
                found.append(item)
            else:
                for c in columns:
                    item.setBackground(c, empty)
                if saved is not None:
                    for c, brush in zip(columns, saved):
                        item.setForeground(c, brush)
                    item.setData(0, SAVED_FOREGROUND_ROLE, None)
        self.blockSignals(False)
        return found

    def _on_icon(self, path: str, image: QImage) -> None:
        """Doručena skutečná ikona souboru z vlákna na pozadí."""
        item = self._by_file.pop(path, None)
        if item is not None:
            self.blockSignals(True)
            item.setIcon(0, QIcon(QPixmap.fromImage(image)))
            self.blockSignals(False)

    # --- výběr -----------------------------------------------------------

    def _folder_items(self):
        it = QTreeWidgetItemIterator(self)
        while it.value():
            item = it.value()
            if not item.data(0, KEY_ROLE):
                yield item
            it += 1

    def leaf_items(self):
        it = QTreeWidgetItemIterator(self)
        while it.value():
            item = it.value()
            if item.data(0, KEY_ROLE):
                yield item
            it += 1

    def _selectable(self, item: QTreeWidgetItem) -> bool:
        return bool(item.flags() & Qt.ItemFlag.ItemIsUserCheckable)

    def checked_keys(self) -> list[str]:
        return [i.data(0, KEY_ROLE) for i in self.leaf_items()
                if self._selectable(i) and i.checkState(0) == Qt.CheckState.Checked]

    def set_all(self, on: bool) -> None:
        """Zaškrtne / odškrtne všechny vybratelné soubory (a nahlásí změny)."""
        state = Qt.CheckState.Checked if on else Qt.CheckState.Unchecked
        for item in self.leaf_items():
            if self._selectable(item):
                item.setCheckState(0, state)

    def set_key_checked(self, key: str, on: bool) -> None:
        """Nastaví stav položky bez hlášení (synchronizace s jiným stromem)."""
        item = self._items.get(key)
        if item is None or not self._selectable(item):
            return
        self.blockSignals(True)
        item.setCheckState(0, Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
        self.blockSignals(False)
        self.viewport().update()

    def _on_changed(self, item: QTreeWidgetItem, column: int) -> None:
        key = item.data(0, KEY_ROLE)
        if key and column == 0 and self._selectable(item):
            self.toggled.emit(key, item.checkState(0) == Qt.CheckState.Checked)
