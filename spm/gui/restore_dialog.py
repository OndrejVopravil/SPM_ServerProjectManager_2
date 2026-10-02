"""Dialog obnovení projektu ze záloh: seznam stavů (odevzdání) a zda je lze obnovit kompletně."""

from __future__ import annotations

import html

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import db as dbm
from ..restore import RestoreCatalog, RestorePoint

COLOR_OK = QColor("#2e8b57")
COLOR_MISSING = QColor("#c0392b")


def _fmt(ts: str) -> str:
    return ts.replace("T", " ")


class RestoreDialog(QDialog):
    def __init__(self, catalog: RestoreCatalog, target_hint: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.catalog = catalog
        self.setWindowTitle(f"Obnovit projekt {catalog.name} ze zálohy")
        self.resize(950, 560)

        oldest = catalog.oldest_complete
        intro = QLabel(
            "Vyberte stav projektu, ke kterému ho chcete obnovit. Projekt se obnoví do nové místní "
            f"složky ({html.escape(target_hint)}); na serveru se nic nemění.<br><br>"
            + (f"Kompletně lze projekt obnovit až ke stavu z <b>{_fmt(oldest.created_at)}</b>"
               f" ({oldest.comment or dbm.COMMIT_LABELS.get(oldest.kind, oldest.kind)})."
               if oldest else "<b>Žádný stav nelze obnovit kompletně</b> – chybí zálohy souborů.")
            + "<br>Starší stavy nelze obnovit celé, protože některé zálohy už neexistují "
              "(ponechává se jen nastavený počet záloh na soubor).")
        intro.setTextFormat(Qt.TextFormat.RichText)
        intro.setWordWrap(True)

        self.table = QTableWidget(len(catalog.points), 5)
        self.table.setHorizontalHeaderLabels(["Datum", "Uživatel", "Akce", "Obnovení", "Komentář"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        for r, p in enumerate(catalog.points):
            status = (f"kompletní ({len(p.files)} souborů)" if p.complete
                      else f"chybí {len(p.missing)} z {len(p.files)} souborů")
            values = [_fmt(p.created_at), p.user, dbm.COMMIT_LABELS.get(p.kind, p.kind), status, p.comment]
            tip = "" if p.complete else ("Chybí zálohy těchto souborů:\n" + "\n".join(p.missing[:20])
                                         + ("\n…" if len(p.missing) > 20 else ""))
            for c, v in enumerate(values):
                item = QTableWidgetItem(v)
                if p.complete:
                    if c == 3:
                        item.setForeground(COLOR_OK)
                else:
                    # nekompletní stav nejde vybrat
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable & ~Qt.ItemFlag.ItemIsEnabled)
                    if c == 3:
                        item.setForeground(COLOR_MISSING)
                item.setToolTip(tip or v)
                self.table.setItem(r, c, item)
        self.table.resizeColumnsToContents()
        self.table.itemSelectionChanged.connect(self._update)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Obnovit")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Zrušit")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.table)
        layout.addWidget(self.buttons)
        if catalog.points and catalog.points[0].complete:
            self.table.selectRow(0)
        self._update()

    def selected_point(self) -> RestorePoint | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        p = self.catalog.points[rows[0].row()]
        return p if p.complete else None

    def _update(self) -> None:
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(self.selected_point() is not None)
