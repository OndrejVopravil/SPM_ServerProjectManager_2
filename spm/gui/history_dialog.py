"""Historie projektu: odevzdání (kdo, kdy, komentář) a změny souborů v každém z nich.

Vyhledávání „obsahuje“ (bez ohledu na velikost písmen a diakritiku) prochází datum,
uživatele, počítač, akci, komentář i názvy souborů. Pokud text odpovídá jen názvům
souborů, zobrazí se u záznamu jen změny těchto souborů.
"""

from __future__ import annotations

from collections import defaultdict

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QHeaderView,
    QLabel,
    QLineEdit,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import db as dbm
from ..db import ProjectDB
from .search import contains, normalize


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


def _change_text(ch) -> str:
    return " ".join([dbm.CHANGE_LABELS.get(ch["change_type"], ch["change_type"]),
                     ch["old_path"] or "", ch["new_path"] or "", ch["backup_path"] or ""])


class HistoryDialog(QDialog):
    def __init__(self, db: ProjectDB, name: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle(f"Historie projektu {name}")
        self.resize(1000, 650)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Hledat v historii – uživatel, komentář, soubor…  (Ctrl+F)")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._apply_filter)
        QShortcut(QKeySequence.StandardKey.Find, self,
                  activated=lambda: (self.search.setFocus(), self.search.selectAll()))

        self.sessions = _table(["Datum", "Uživatel", "PC", "Akce", "Změn", "Komentář"])
        self.changes = _table(["Změna", "Původní cesta", "Nová cesta", "Záloha / koš"])
        self.changes.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.changes.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.show_imported = QCheckBox("Zobrazit i soubory načtené do evidence při aktivaci")
        self.show_imported.toggled.connect(self._apply_filter)
        self.found = QLabel()

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
        layout.addWidget(self.search)
        layout.addWidget(self.found)
        layout.addWidget(splitter)

        self._session_rows = db.commits()
        self._changes: dict[int, list] = defaultdict(list)  # všechny změny najednou – kvůli hledání
        for ch in db.all_changes():
            self._changes[ch["commit_id"]].append(ch)
        self._texts: list[str] = []
        self.sessions.setRowCount(len(self._session_rows))
        for r, s in enumerate(self._session_rows):
            comment = s["comment"] or ""
            tip = comment
            if s["kind"] == dbm.PROJECT_COMMENT:
                # komentář záznamu = nový text komentáře projektu (víceřádkový)
                tip = comment or "(komentář smazán)"
                comment = comment.replace("\n", " ↵ ") or "(komentář smazán)"
            values = [_fmt(s["created_at"]), s["user"], s["pc"],
                      dbm.COMMIT_LABELS.get(s["kind"], s["kind"]), str(s["n_changes"]), comment]
            self._texts.append(" ".join(values))
            for c, v in enumerate(values):
                item = QTableWidgetItem(v)
                item.setToolTip(tip if c == len(values) - 1 else v)
                self.sessions.setItem(r, c, item)
        self.sessions.resizeColumnsToContents()
        self.sessions.itemSelectionChanged.connect(self._load_changes)
        self._apply_filter()

    # --- vyhledávání ----------------------------------------------------------

    def _needle(self) -> str:
        return normalize(self.search.text().strip())

    def _visible_changes(self, commit_id: int) -> list:
        return [c for c in self._changes.get(commit_id, [])
                if self.show_imported.isChecked() or c["change_type"] != dbm.IMPORTED]

    def _matching_changes(self, row: int) -> list:
        """Změny záznamu, které se mají zobrazit při aktuálním hledání."""
        changes = self._visible_changes(self._session_rows[row]["commit_id"])
        needle = self._needle()
        if contains(self._texts[row], needle):
            return changes  # text odpovídá záznamu (uživatel, komentář…) -> všechny změny
        return [c for c in changes if contains(_change_text(c), needle)]

    def _apply_filter(self) -> None:
        needle = self._needle()
        shown = 0
        for r in range(len(self._session_rows)):
            hit = not needle or contains(self._texts[r], needle) or bool(self._matching_changes(r))
            self.sessions.setRowHidden(r, not hit)
            shown += hit
        self.found.setText(f"Nalezeno záznamů: {shown} z {len(self._session_rows)}" if needle else "")
        self.found.setVisible(bool(needle))

        rows = self.sessions.selectionModel().selectedRows()
        if not rows or self.sessions.isRowHidden(rows[0].row()):
            self.sessions.clearSelection()
            first = next((r for r in range(len(self._session_rows)) if not self.sessions.isRowHidden(r)), None)
            if first is not None:
                self.sessions.selectRow(first)
        self._load_changes()

    # --- změny ----------------------------------------------------------------

    def _load_changes(self) -> None:
        rows = self.sessions.selectionModel().selectedRows()
        self.changes.setRowCount(0)
        if not rows or self.sessions.isRowHidden(rows[0].row()):
            return
        items = self._matching_changes(rows[0].row())
        self.changes.setRowCount(len(items))
        for r, ch in enumerate(items):
            values = [dbm.CHANGE_LABELS.get(ch["change_type"], ch["change_type"]),
                      ch["old_path"] or "", ch["new_path"] or "", ch["backup_path"] or ""]
            for c, v in enumerate(values):
                self.changes.setItem(r, c, QTableWidgetItem(v))
        self.changes.resizeColumnToContents(0)
