"""Výběr souborů k zamknutí (při aktivaci a při převzetí k úpravě).

Strom složek a souborů se zaškrtávátky: zaškrtnutí složky vybere celý její obsah,
podsložku pak lze odškrtnout. Soubory, které drží jiný uživatel, vybrat nelze.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
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
from .check_tree import CheckTree, TreeEntry, in_scope

COLOR_TAKEN = QColor("#c0392b")
COLOR_MINE = QColor("#2e8b57")
COLOR_MIRROR = QColor("#8e44ad")  # spravuje zrcadlo z jiného serveru (jen pro čtení)


class FileSelectDialog(QDialog):
    def __init__(self, title: str, intro: str, entries: list[FileEntry], ok_text: str,
                 parent: QWidget | None = None, allow_empty: bool = True, root: Path | None = None,
                 preselect: str | None = None, skip_option: bool | None = None,
                 mirror_label: Callable[[FileEntry], str] = lambda entry: entry.mirror or ""):
        """root: složka, kde soubory skutečně leží (pro jejich ikony).
        preselect: soubor nebo složka (zakončená '/'), jejíž volné soubory budou předem zaškrtnuté.
        skip_option: zobrazit volbu „Nestahovat neaktivované soubory“ s tímto výchozím stavem
                     (None = nezobrazovat; jen při aktivaci)."""
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(760, 600)
        self.allow_empty = allow_empty

        self.tree = CheckTree(["Soubor", "Drží", "Zrcadlo"])
        checked = {e.path for e in entries if e.free and in_scope(e.path, preselect)}
        self.tree.set_entries([
            TreeEntry(e.path, e.path,
                      [e.holder, f"← {mirror_label(e)}" if e.mirror else ""],
                      enabled=e.free,
                      color=None if e.free else (COLOR_MINE if e.mine else
                                                 COLOR_MIRROR if e.mirror else COLOR_TAKEN),
                      file_path=str(to_abs(root, e.path)) if root else None,
                      tooltip=(f"Soubor spravuje zrcadlo z {e.mirror} – zde ho nelze zamknout, "
                               "změny se dělají ve zdroji." if e.mirror else ""))
            for e in entries
        ], checked)
        # sloupec Zrcadlo jen u projektu, do kterého se zrcadlí (soubory ze zrcadla nelze vybrat)
        self.tree.setColumnHidden(2, not any(e.mirror for e in entries))
        self.tree.toggled.connect(self._update_count)

        select_all = QPushButton("Vybrat vše volné")
        select_all.clicked.connect(lambda: self._set_all(Qt.CheckState.Checked))
        select_none = QPushButton("Zrušit výběr")
        select_none.clicked.connect(lambda: self._set_all(Qt.CheckState.Unchecked))
        row = QHBoxLayout()
        row.addWidget(select_all)
        row.addWidget(select_none)
        row.addStretch()

        self.skip = QCheckBox("Nestahovat neaktivované soubory (stáhnou se jen zamčené soubory)")
        self.skip.setToolTip(
            "Rychlejší u velkých projektů. Místní kopie bude obsahovat jen soubory, které zamknete;\n"
            "Aktualizovat pak stahuje jen soubory, které už máte (další získáte Převzetím).\n"
            "Pozor: sestava odkazující na nestažené díly se nemusí otevřít celá.")
        self.skip.setChecked(bool(skip_option))
        self.skip.setVisible(skip_option is not None)
        self.skip.toggled.connect(self._update_count)

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
        layout.addWidget(self.skip)
        layout.addWidget(self.count)
        layout.addWidget(self.buttons)
        self._update_count()

    def _set_all(self, state: Qt.CheckState) -> None:
        self.tree.set_all(state == Qt.CheckState.Checked)

    def selected_paths(self) -> list[str]:
        return self.tree.checked_keys()

    def skip_unlocked(self) -> bool:
        """Zaškrtnuto „Nestahovat neaktivované soubory“?"""
        return not self.skip.isHidden() and self.skip.isChecked()

    def _update_count(self, *_) -> None:
        n = len(self.selected_paths())
        text = f"Vybráno k úpravě: <b>{n}</b> souborů"
        if not self.skip.isHidden():
            text += (f" – stáhne se jen těchto {n} souborů" if self.skip.isChecked()
                     else " – stáhne se celý projekt")
        self.count.setText(text)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(self.allow_empty or n > 0)
