"""Okna archivace: potvrzení archivace projektu a seznam archivovaných projektů s obnovou."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..archive import ArchiveEntry, archive_files, list_archives
from ..storage import StorageInfo, fmt_bytes
from .search import contains, normalize
from .timeline_style import date_time


class ArchiveProjectDialog(QDialog):
    """Potvrzení archivace: co se zabalí, kolik záloh zůstane, kam, komentář."""

    def __init__(self, name: str, target_dir: Path, info: StorageInfo, project_bytes: int,
                 keep_backups: int, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(f"Archivovat projekt {name}")
        self.resize(560, 380)
        text = QLabel(
            f"Projekt <b>{name}</b> se zabalí do zipu v archivní složce<br><b>{target_dir}</b>"
            " a z pracovní síťové složky zmizí (obnovit ho lze v Archivu projektů).<br><br>"
            f"Soubory projektu: {fmt_bytes(project_bytes)}<br>"
            f"Zálohy: {info.backup_files} ({fmt_bytes(info.backup_bytes)}) – ponechá se "
            f"<b>{keep_backups}</b> posledních záloh každého souboru<br>"
            f"Koš projektu: {info.trash_files} ({fmt_bytes(info.trash_bytes)}) – vysype se<br>"
            "Historie, poznámky a komentář projektu zůstanou.<br><br>"
            "Projekt se z pracovní složky smaže až po ověření zipu.")
        text.setTextFormat(Qt.TextFormat.RichText)
        text.setWordWrap(True)
        self.comment = QPlainTextEdit()
        self.comment.setPlaceholderText("Komentář k archivaci (nepovinný) – např. „zakázka předána“")
        self.comment.setMaximumHeight(80)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Archivovat")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Zrušit")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(text)
        layout.addWidget(self.comment)
        layout.addWidget(buttons)

    def comment_text(self) -> str:
        return self.comment.toPlainText().strip()


COLUMNS = ["Projekt", "Archivováno", "Kdo", "Komentář archivace", "Komentář projektu", "Soubory",
           "Velikost", "Obnoveno"]


class ArchiveListDialog(QDialog):
    """Archivované projekty záložky: hledání, náhled obsahu, obnova do pracovní složky."""

    def __init__(self, folder: Path, restore: Callable[[ArchiveEntry], bool] | None,
                 restore_problem: str | None = None, parent: QWidget | None = None):
        """restore: obnoví projekt (vrací, zda se povedlo); restore_problem: proč obnova nejde
        (oprávnění) – tlačítko se pak zakáže s vysvětlením."""
        super().__init__(parent)
        self.folder, self.restore_cb = folder, restore
        self.setWindowTitle(f"Archiv projektů – {folder}")
        self.resize(1100, 620)
        self.entries: list[ArchiveEntry] = []

        self.search = QLineEdit()
        self.search.setPlaceholderText("Hledat projekt nebo komentář…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _: self._apply_filter())
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._show_files)
        self.files = QListWidget()
        files_box = QWidget()
        flay = QVBoxLayout(files_box)
        flay.setContentsMargins(6, 0, 0, 0)
        self.files_label = QLabel("<b>Obsah</b>")
        flay.addWidget(self.files_label)
        flay.addWidget(self.files)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self.table)
        split.addWidget(files_box)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)

        self.btn_restore = QPushButton("Obnovit do pracovní složky…")
        self.btn_restore.setToolTip(restore_problem or "Projekt se rozbalí zpět do síťové složky záložky; "
                                                       "zip v archivu zůstane")
        self.btn_restore.clicked.connect(lambda: self.restore_selected())
        self._can_restore = restore is not None and not restore_problem
        open_folder = QPushButton("Otevřít složku archivu")
        open_folder.clicked.connect(lambda: os.startfile(self.folder))  # noqa: S606
        close = QPushButton("Zavřít")
        close.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addWidget(self.btn_restore)
        buttons.addWidget(open_folder)
        buttons.addStretch()
        buttons.addWidget(close)
        self.info = QLabel()

        layout = QVBoxLayout(self)
        layout.addWidget(self.search)
        layout.addWidget(split, stretch=1)
        layout.addWidget(self.info)
        layout.addLayout(buttons)
        self.reload()

    def reload(self) -> None:
        self.entries = list_archives(self.folder)
        self.table.setRowCount(len(self.entries))
        for r, e in enumerate(self.entries):
            restored = (f"{date_time(e.restores[-1]['at'])} ({e.restores[-1].get('by', '')})"
                        if e.restores else "")
            values = [e.project, date_time(e.archived_at) if e.archived_at else "", e.archived_by,
                      e.comment.replace("\n", " ↵ "), e.project_comment.split("\n", 1)[0],
                      str(e.files) if e.files else "", fmt_bytes(e.size), restored]
            for c, v in enumerate(values):
                item = QTableWidgetItem(v)
                item.setToolTip({3: e.comment, 4: e.project_comment}.get(c, v) or v)
                if c == 7 and e.restores:
                    item.setToolTip("\n".join(f"{date_time(x['at'])} – {x.get('by', '')} jako {x.get('as', '')}"
                                              for x in e.restores))
                self.table.setItem(r, c, item)
        self.table.resizeColumnsToContents()
        self.info.setText(f"Archivovaných projektů: {len(self.entries)} – celkem "
                          f"{fmt_bytes(sum(e.size for e in self.entries))}")
        self._apply_filter()
        if self.entries and not self.table.selectionModel().selectedRows():
            first = next((r for r in range(self.table.rowCount()) if not self.table.isRowHidden(r)), None)
            if first is not None:
                self.table.selectRow(first)
        self._show_files()

    def _apply_filter(self) -> None:
        needle = normalize(self.search.text().strip())
        for r, e in enumerate(self.entries):
            text = " ".join([e.project, e.comment, e.project_comment, e.archived_by])
            self.table.setRowHidden(r, bool(needle) and not contains(text, needle))

    def selected(self) -> ArchiveEntry | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows or self.table.isRowHidden(rows[0].row()):
            return None
        return self.entries[rows[0].row()]

    def _show_files(self) -> None:
        e = self.selected()
        self.files.clear()
        self.btn_restore.setEnabled(e is not None and self._can_restore)
        if e is None:
            self.files_label.setText("<b>Obsah</b>")
            return
        try:
            names = archive_files(e.path)
        except Exception as ex:  # noqa: BLE001 – poškozený zip
            self.files_label.setText(f"<b>Obsah</b> – zip nelze přečíst: {ex}")
            return
        self.files_label.setText(f"<b>Obsah</b> – {len(names)} souborů")
        self.files.addItems(names)

    def restore_selected(self) -> bool:
        e = self.selected()
        if e is None or self.restore_cb is None:
            return False
        ok = self.restore_cb(e)
        if ok:
            self.reload()
        return ok
