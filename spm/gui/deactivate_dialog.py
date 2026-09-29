"""Dialog deaktivace: potvrzení přejmenování, přehled změn a povinný komentář."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QHeaderView,
    QLabel,
    QListWidget,
    QPlainTextEdit,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..names import format_invalid
from ..sync import DeactivationPlan

DELETED_LABEL = "— smazán —"


class DeactivateDialog(QDialog):
    def __init__(self, plan: DeactivationPlan, parent: QWidget | None = None):
        super().__init__(parent)
        self.plan = plan
        self.setWindowTitle(f"Deaktivace projektu {plan.name}")
        self.resize(900, 640)
        self.suggestions = {s.file_id: s for s in plan.suggestions}
        self.combos: dict[str, QComboBox] = {}

        layout = QVBoxLayout(self)
        self.summary = QLabel()
        layout.addWidget(self.summary)

        if plan.conflicts:
            box = QGroupBox("Pozor – soubory změněné přímo na serveru během aktivace")
            box.setStyleSheet("QGroupBox { color: #c0392b; font-weight: bold; }")
            lay = QVBoxLayout(box)
            lst = QListWidget()
            lst.addItems(plan.conflicts)
            lst.setMaximumHeight(90)
            lay.addWidget(lst)
            lay.addWidget(QLabel("Původní verze na serveru budou zazálohovány / přesunuty do koše projektu."))
            layout.addWidget(box)

        if plan.invalid_names:
            box = QGroupBox("Nepovolené znaky v názvech – projekt nelze deaktivovat")
            box.setStyleSheet("QGroupBox { color: #c0392b; font-weight: bold; }")
            lay = QVBoxLayout(box)
            lst = QListWidget()
            lst.addItems(format_invalid(plan.invalid_names, limit=len(plan.invalid_names)).splitlines())
            lst.setMaximumHeight(120)
            lay.addWidget(lst)
            lay.addWidget(QLabel("Nepovolená je česká diakritika, mezera a znaky ( ) & , . ; = ^ "
                                 "(tečka před příponou souboru je v pořádku).\nPřejmenujte je v "
                                 "místní složce, zavřete tento dialog a spusťte deaktivaci znovu."))
            layout.addWidget(box)

        if plan.missing:
            layout.addWidget(self._build_rename_table(), stretch=2)

        self.tabs = QTabWidget()
        self.list_modified = QListWidget()
        self.list_added = QListWidget()
        self.list_deleted = QListWidget()
        self.list_modified.addItems(sorted(lf.path for _, lf in plan.modified))
        self.tabs.addTab(self.list_modified, "")
        self.tabs.addTab(self.list_added, "")
        self.tabs.addTab(self.list_deleted, "")
        layout.addWidget(self.tabs, stretch=1)

        layout.addWidget(QLabel("<b>Komentář k úpravám</b> (povinný):"))
        self.comment = QPlainTextEdit()
        self.comment.setPlaceholderText("Popište, co jste v projektu změnili…")
        self.comment.setMaximumHeight(90)
        self.comment.textChanged.connect(self._validate)
        layout.addWidget(self.comment)

        self.error = QLabel()
        self.error.setStyleSheet("color: #c0392b;")
        layout.addWidget(self.error)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Deaktivovat a nahrát")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Zrušit")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self._refresh()

    def _build_rename_table(self) -> QGroupBox:
        box = QGroupBox("Chybějící soubory – zkontrolujte přiřazení přejmenovaných souborů")
        lay = QVBoxLayout(box)
        table = QTableWidget(len(self.plan.missing), 3)
        table.setHorizontalHeaderLabels(["Původní soubor", "Nový název / umístění", "Shoda"])
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        table.verticalHeader().setVisible(False)
        table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.rename_table = table

        new_paths = sorted(lf.path for lf in self.plan.new)
        for row, bf in enumerate(sorted(self.plan.missing, key=lambda f: f.path)):
            table.setItem(row, 0, QTableWidgetItem(bf.path))
            combo = QComboBox()
            combo.addItem(DELETED_LABEL, None)
            for p in new_paths:
                combo.addItem(p, p)
            sugg = self.suggestions.get(bf.file_id)
            if sugg:
                combo.setCurrentIndex(combo.findData(sugg.new_path))
            combo.currentIndexChanged.connect(self._refresh)
            self.combos[bf.file_id] = combo
            table.setCellWidget(row, 1, combo)
            table.setItem(row, 2, QTableWidgetItem())
        lay.addWidget(table)
        lay.addWidget(QLabel("Pokud soubor nebyl přejmenován, ale smazán, zvolte „— smazán —“."))
        return box

    def assignments(self) -> dict[str, str | None]:
        return {fid: combo.currentData() for fid, combo in self.combos.items()}

    def comment_text(self) -> str:
        return self.comment.toPlainText().strip()

    def _refresh(self) -> None:
        assigned = self.assignments()
        for row, fid in enumerate(self.combos):
            target = assigned[fid]
            sugg = self.suggestions.get(fid)
            item = self.rename_table.item(row, 2)
            if target is None:
                item.setText("smazán")
                item.setToolTip("")
            elif sugg and sugg.new_path == target:
                item.setText("stejný obsah" if sugg.same_content else f"{sugg.score:.0%}")
                item.setToolTip(sugg.reason)
            else:
                item.setText("ručně")
                item.setToolTip("Přiřazeno ručně")

        used = {p for p in assigned.values() if p}
        added = sorted(lf.path for lf in self.plan.new if lf.path not in used)
        deleted = sorted(f.path for f in self.plan.missing if not assigned[f.file_id])
        renamed = len(used)
        self.list_added.clear()
        self.list_added.addItems(added)
        self.list_deleted.clear()
        self.list_deleted.addItems(deleted)
        self.tabs.setTabText(0, f"Upravené ({len(self.plan.modified)})")
        self.tabs.setTabText(1, f"Nové ({len(added)})")
        self.tabs.setTabText(2, f"Smazané ({len(deleted)})")
        self.summary.setText(
            f"Upravené: <b>{len(self.plan.modified)}</b> &nbsp; Nové: <b>{len(added)}</b> &nbsp; "
            f"Přejmenované: <b>{renamed}</b> &nbsp; Smazané: <b>{len(deleted)}</b> &nbsp; "
            f"Beze změny: {len(self.plan.unchanged)}"
        )
        self._validate()

    def _validate(self) -> None:
        targets = [p for p in self.assignments().values() if p]
        error = ""
        if self.plan.invalid_names:
            error = "Projekt obsahuje názvy s nepovolenými znaky – nejdřív je opravte."
        elif len(targets) != len(set(targets)):
            error = "Jeden nový soubor je přiřazen k více původním souborům."
        elif not self.comment_text():
            error = "Bez komentáře nelze projekt deaktivovat."
        self.error.setText(error)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(not error)
