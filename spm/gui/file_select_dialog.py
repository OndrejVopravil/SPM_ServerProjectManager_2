"""Výběr souborů k zamknutí (při aktivaci a při převzetí k úpravě).

Strom složek a souborů se zaškrtávátky: zaškrtnutí složky vybere celý její obsah,
podsložku pak lze odškrtnout. Soubory, které drží jiný uživatel, vybrat nelze.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..scanner import to_abs
from ..sync import FileEntry
from .check_tree import CheckTree, TreeEntry

COLOR_TAKEN = QColor("#c0392b")
COLOR_MINE = QColor("#2e8b57")


class FileSelectDialog(QDialog):
    def __init__(self, title: str, intro: str, entries: list[FileEntry], ok_text: str,
                 parent: QWidget | None = None, allow_empty: bool = True, root: Path | None = None):
        """root: složka, kde soubory skutečně leží (pro jejich ikony)."""
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(760, 600)
        self.allow_empty = allow_empty

        self.tree = CheckTree(["Soubor", "Drží"])
        self.tree.set_entries([
            TreeEntry(e.path, e.path,
                      ["" if e.free else ("já" if e.mine else e.owner.label)],
                      enabled=e.free,
                      color=None if e.free else (COLOR_MINE if e.mine else COLOR_TAKEN),
                      file_path=str(to_abs(root, e.path)) if root else None)
            for e in entries
        ])
        self.tree.toggled.connect(self._update_count)

        select_all = QPushButton("Vybrat vše volné")
        select_all.clicked.connect(lambda: self._set_all(Qt.CheckState.Checked))
        select_none = QPushButton("Zrušit výběr")
        select_none.clicked.connect(lambda: self._set_all(Qt.CheckState.Unchecked))
        row = QHBoxLayout()
        row.addWidget(select_all)
        row.addWidget(select_none)
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

    def _set_all(self, state: Qt.CheckState) -> None:
        self.tree.set_all(state == Qt.CheckState.Checked)

    def selected_paths(self) -> list[str]:
        return self.tree.checked_keys()

    def _update_count(self, *_) -> None:
        n = len(self.selected_paths())
        self.count.setText(f"Vybráno k úpravě: <b>{n}</b> souborů")
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(self.allow_empty or n > 0)
