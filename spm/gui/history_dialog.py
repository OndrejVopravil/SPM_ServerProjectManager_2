"""Historie projektu: odevzdání (kdo, kdy, komentář) a změny souborů v každém z nich."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QHeaderView,
    QLabel,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import db as dbm
from ..db import ProjectDB


def _fmt(ts: str | None) -> str:
    return (ts or "").replace("T", " ")


def _table(headers: list[str]) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    t.verticalHeader().setVisible(False)
    t.horizontalHeader().setStretchLastSection(True)
    return t


class HistoryDialog(QDialog):
    def __init__(self, db: ProjectDB, name: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle(f"Historie projektu {name}")
        self.resize(1000, 650)

        self.sessions = _table(["Datum", "Uživatel", "PC", "Akce", "Změn", "Komentář"])
        self.changes = _table(["Změna", "Původní cesta", "Nová cesta", "Záloha / koš"])
        self.changes.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.changes.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.show_imported = QCheckBox("Zobrazit i soubory načtené do evidence při aktivaci")
        self.show_imported.toggled.connect(self._load_changes)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.sessions)
        bottom = QWidget()
        lay = QVBoxLayout(bottom)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(QLabel("<b>Změny ve vybraném záznamu</b>"))
        lay.addWidget(self.show_imported)
        lay.addWidget(self.changes)
        splitter.addWidget(bottom)

        layout = QVBoxLayout(self)
        layout.addWidget(splitter)

        self._session_rows = db.commits()
        self.sessions.setRowCount(len(self._session_rows))
        for r, s in enumerate(self._session_rows):
            values = [_fmt(s["created_at"]), s["user"], s["pc"],
                      dbm.COMMIT_LABELS.get(s["kind"], s["kind"]), str(s["n_changes"]),
                      s["comment"] or ""]
            for c, v in enumerate(values):
                item = QTableWidgetItem(v)
                item.setToolTip(v)
                self.sessions.setItem(r, c, item)
        self.sessions.resizeColumnsToContents()
        self.sessions.itemSelectionChanged.connect(self._load_changes)
        if self._session_rows:
            self.sessions.selectRow(0)

    def _load_changes(self) -> None:
        rows = self.sessions.selectionModel().selectedRows()
        self.changes.setRowCount(0)
        if not rows:
            return
        sid = self._session_rows[rows[0].row()]["commit_id"]
        items = [c for c in self.db.changes(sid)
                 if self.show_imported.isChecked() or c["change_type"] != dbm.IMPORTED]
        self.changes.setRowCount(len(items))
        for r, ch in enumerate(items):
            values = [dbm.CHANGE_LABELS.get(ch["change_type"], ch["change_type"]),
                      ch["old_path"] or "", ch["new_path"] or "", ch["backup_path"] or ""]
            for c, v in enumerate(values):
                self.changes.setItem(r, c, QTableWidgetItem(v))
        self.changes.resizeColumnToContents(0)
