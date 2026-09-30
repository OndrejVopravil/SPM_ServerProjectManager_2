"""Výběr souborů k zamknutí (při aktivaci a při převzetí k úpravě).

Strom složek a souborů se zaškrtávátky: zaškrtnutí složky vybere celý její obsah,
podsložku pak lze odškrtnout. Soubory, které drží jiný uživatel, vybrat nelze.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QTreeWidgetItemIterator,
    QVBoxLayout,
    QWidget,
)

from ..sync import FileEntry

PATH_ROLE = Qt.ItemDataRole.UserRole
COLOR_TAKEN = QColor("#c0392b")
COLOR_MINE = QColor("#2e8b57")


class FileSelectDialog(QDialog):
    def __init__(self, title: str, intro: str, entries: list[FileEntry], ok_text: str,
                 parent: QWidget | None = None, allow_empty: bool = True):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(760, 600)
        self.allow_empty = allow_empty

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Soubor", "Drží"])
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self._build(entries)
        self.tree.itemChanged.connect(self._update_count)

        select_all = QPushButton("Vybrat vše volné")
        select_all.clicked.connect(lambda: self._set_all(Qt.CheckState.Checked))
        select_none = QPushButton("Zrušit výběr")
        select_none.clicked.connect(lambda: self._set_all(Qt.CheckState.Unchecked))
        expand = QPushButton("Rozbalit vše")
        expand.clicked.connect(self.tree.expandAll)
        row = QHBoxLayout()
        for b in (select_all, select_none, expand):
            row.addWidget(b)
        row.addStretch()

        self.count = QLabel()
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText(ok_text)
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Zrušit")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        intro_label = QLabel(intro)
        intro_label.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.addWidget(intro_label)
        layout.addLayout(row)
        layout.addWidget(self.tree)
        layout.addWidget(self.count)
        layout.addWidget(self.buttons)
        self._update_count()

    def _build(self, entries: list[FileEntry]) -> None:
        folders: dict[str, QTreeWidgetItem] = {}

        def folder(path: str) -> QTreeWidgetItem | None:
            if not path:
                return None
            if path not in folders:
                parent_path, _, name = path.rpartition("/")
                parent = folder(parent_path)
                item = QTreeWidgetItem([name, ""])
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsAutoTristate)
                item.setCheckState(0, Qt.CheckState.Unchecked)
                (parent.addChild(item) if parent else self.tree.addTopLevelItem(item))
                folders[path] = item
            return folders[path]

        for e in sorted(entries, key=lambda e: e.path.lower()):
            parent_path, _, name = e.path.rpartition("/")
            parent = folder(parent_path)
            item = QTreeWidgetItem([name, e.owner.label if e.owner else ""])
            item.setData(0, PATH_ROLE, e.path)
            if e.free:
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(0, Qt.CheckState.Unchecked)
            else:
                # drží ho někdo jiný (nebo už já) – nelze vybrat
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled & ~Qt.ItemFlag.ItemIsUserCheckable)
                color = COLOR_MINE if e.mine else COLOR_TAKEN
                item.setForeground(1, color)
                item.setText(1, "já" if e.mine else e.owner.label)
            (parent.addChild(item) if parent else self.tree.addTopLevelItem(item))
        self.tree.sortItems(0, Qt.SortOrder.AscendingOrder)
        for i in range(self.tree.topLevelItemCount()):
            self.tree.topLevelItem(i).setExpanded(True)

    def _files(self):
        it = QTreeWidgetItemIterator(self.tree)
        while it.value():
            item = it.value()
            if item.data(0, PATH_ROLE):
                yield item
            it += 1

    def _set_all(self, state: Qt.CheckState) -> None:
        for item in self._files():
            if item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                item.setCheckState(0, state)

    def selected_paths(self) -> list[str]:
        return [item.data(0, PATH_ROLE) for item in self._files()
                if item.flags() & Qt.ItemFlag.ItemIsUserCheckable
                and item.checkState(0) == Qt.CheckState.Checked]

    def _update_count(self, *_) -> None:
        n = len(self.selected_paths())
        self.count.setText(f"Vybráno k úpravě: <b>{n}</b> souborů")
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(self.allow_empty or n > 0)
