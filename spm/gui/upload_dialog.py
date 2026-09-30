"""Dialog odevzdání a deaktivace: přiřazení přejmenovaných souborů, výběr, komentář.

- Odevzdání: uživatel zaškrtne, které změny nahrát. Odevzdané soubory se uvolní
  (i zamčené soubory beze změny lze jen uvolnit). Projekt zůstává aktivovaný.
- Deaktivace: odevzdá se vše, uvolní se všechny zámky a aktivace skončí.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..names import format_invalid
from ..sync import UploadPlan, new_key

DELETED_LABEL = "— smazán —"
KEY_ROLE = Qt.ItemDataRole.UserRole


class UploadDialog(QDialog):
    def __init__(self, plan: UploadPlan, end_session: bool, parent: QWidget | None = None):
        super().__init__(parent)
        self.plan = plan
        self.end_session = end_session
        self.selectable = not end_session
        self.setWindowTitle(("Deaktivace" if end_session else "Odevzdání") + f" – projekt {plan.name}")
        self.resize(920, 680)
        self.suggestions = {s.file_id: s for s in plan.suggestions}
        self.combos: dict[str, QComboBox] = {}
        self.checked: set[str] = set()   # vybrané klíče (jen při odevzdání)
        self.invalid = set(plan.invalid_names)

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Nahrají se všechny vaše změny a uvolní se všechny vaše zámky. Aktivace projektu skončí."
            if end_session else
            "Zaškrtněte, co chcete odevzdat. Odevzdané soubory se nahrají na server a uvolní – "
            "ostatní si je mohou stáhnout (Aktualizovat) nebo převzít k úpravě. "
            "Nezaškrtnuté zůstanou u vás rozpracované a zamčené."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.summary = QLabel()
        layout.addWidget(self.summary)

        if plan.conflicts:
            layout.addWidget(self._message_box(
                "Upozornění", plan.conflicts,
                "Změny v souborech, které nemáte zamčené, se nenahrají"
                + (" a po deaktivaci budou ztraceny." if end_session else ".")))
        if plan.invalid_names:
            layout.addWidget(self._message_box(
                "Nepovolené znaky v názvech",
                format_invalid(plan.invalid_names, limit=len(plan.invalid_names)).splitlines(),
                "Nepovolená je česká diakritika, mezera a znaky ( ) & , . ; = ^ (tečka před příponou "
                "souboru je v pořádku). Přejmenujte je v místní složce, zavřete tento dialog a akci "
                "spusťte znovu." + ("" if end_session else " Ostatní soubory odevzdat lze.")))

        if plan.missing:
            layout.addWidget(self._build_rename_table(), stretch=2)

        self.tabs = QTabWidget()
        self.list_modified = self._list()
        self.list_added = self._list()
        self.list_deleted = self._list()
        self.list_unchanged = self._list()
        self.tabs.addTab(self.list_modified, "")
        self.tabs.addTab(self.list_added, "")
        self.tabs.addTab(self.list_deleted, "")
        self.tabs.addTab(self.list_unchanged, "")
        layout.addWidget(self.tabs, stretch=1)

        if self.selectable:
            row = QHBoxLayout()
            all_btn = QPushButton("Vybrat vše")
            all_btn.clicked.connect(lambda: self._check_all(True))
            none_btn = QPushButton("Zrušit výběr")
            none_btn.clicked.connect(lambda: self._check_all(False))
            row.addWidget(all_btn)
            row.addWidget(none_btn)
            row.addStretch()
            layout.addLayout(row)

        layout.addWidget(QLabel("<b>Komentář k úpravám</b> (povinný, pokud se něco nahrává):"))
        self.comment = QPlainTextEdit()
        self.comment.setPlaceholderText("Popište, co jste v projektu změnili…")
        self.comment.setMaximumHeight(80)
        self.comment.textChanged.connect(self._validate)
        layout.addWidget(self.comment)

        self.error = QLabel()
        self.error.setStyleSheet("color: #c0392b;")
        layout.addWidget(self.error)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            "Deaktivovat a nahrát" if end_session else "Odevzdat vybrané")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Zrušit")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self._refresh()

    # --- stavba ----------------------------------------------------------

    @staticmethod
    def _message_box(title: str, lines: list[str], note: str) -> QGroupBox:
        box = QGroupBox(title)
        box.setStyleSheet("QGroupBox { color: #c0392b; font-weight: bold; }")
        lay = QVBoxLayout(box)
        lst = QListWidget()
        lst.addItems(lines)
        lst.setMaximumHeight(100)
        lay.addWidget(lst)
        label = QLabel(note)
        label.setWordWrap(True)
        lay.addWidget(label)
        return box

    def _list(self) -> QListWidget:
        lst = QListWidget()
        lst.itemChanged.connect(self._on_item_changed)
        return lst

    def _build_rename_table(self) -> QGroupBox:
        box = QGroupBox("Chybějící soubory – zkontrolujte přiřazení přejmenovaných souborů")
        lay = QVBoxLayout(box)
        cols = ["Odevzdat"] if self.selectable else []
        table = QTableWidget(len(self.plan.missing), len(cols) + 3)
        table.setHorizontalHeaderLabels(cols + ["Původní soubor", "Nový název / umístění", "Shoda"])
        off = len(cols)
        table.horizontalHeader().setSectionResizeMode(off, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(off + 1, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(off + 2, QHeaderView.ResizeMode.ResizeToContents)
        table.verticalHeader().setVisible(False)
        table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.rename_table, self.rename_off = table, off

        new_paths = sorted(lf.path for lf in self.plan.new)
        for row, bf in enumerate(sorted(self.plan.missing, key=lambda f: f.path)):
            if self.selectable:
                check = QTableWidgetItem()
                check.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
                check.setCheckState(Qt.CheckState.Unchecked)
                check.setData(KEY_ROLE, bf.file_id)
                table.setItem(row, 0, check)
            table.setItem(row, off, QTableWidgetItem(bf.path))
            combo = QComboBox()
            combo.addItem(DELETED_LABEL, None)
            for p in new_paths:
                combo.addItem(p, p)
            sugg = self.suggestions.get(bf.file_id)
            if sugg:
                combo.setCurrentIndex(combo.findData(sugg.new_path))
            combo.currentIndexChanged.connect(self._refresh)
            self.combos[bf.file_id] = combo
            table.setCellWidget(row, off + 1, combo)
            table.setItem(row, off + 2, QTableWidgetItem())
        table.itemChanged.connect(self._on_item_changed)
        lay.addWidget(table)
        lay.addWidget(QLabel("Pokud soubor nebyl přejmenován, ale smazán, zvolte „— smazán —“."))
        return box

    # --- výsledky ----------------------------------------------------------

    def assignments(self) -> dict[str, str | None]:
        return {fid: combo.currentData() for fid, combo in self.combos.items()}

    def comment_text(self) -> str:
        return self.comment.toPlainText().strip()

    def selected_keys(self) -> set[str] | None:
        """Vybrané položky k odevzdání (None = vše, při deaktivaci)."""
        return None if self.end_session else set(self.checked)

    # --- chování -----------------------------------------------------------

    def _fill(self, lst: QListWidget, items: list[tuple[str, str]]) -> None:
        lst.blockSignals(True)
        lst.clear()
        for label, key in items:
            item = QListWidgetItem(label)
            item.setData(KEY_ROLE, key)
            if self.selectable:
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Checked if key in self.checked else Qt.CheckState.Unchecked)
            lst.addItem(item)
        lst.blockSignals(False)

    def _on_item_changed(self, item) -> None:
        key = item.data(KEY_ROLE)
        if not key or not self.selectable:
            return
        if item.checkState() == Qt.CheckState.Checked:
            self.checked.add(key)
        else:
            self.checked.discard(key)
        self._summary()

    def _check_all(self, on: bool) -> None:
        keys = self._all_keys()
        self.checked = set(keys) if on else set()
        self._refresh()
        if self.plan.missing:
            self.rename_table.blockSignals(True)
            for row in range(self.rename_table.rowCount()):
                item = self.rename_table.item(row, 0)
                item.setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
            self.rename_table.blockSignals(False)
        self._summary()

    def _all_keys(self) -> list[str]:
        used = {p for p in self.assignments().values() if p}
        return ([bf.file_id for bf, _ in self.plan.modified]
                + [new_key(lf.path) for lf in self.plan.new if lf.path not in used]
                + [bf.file_id for bf in self.plan.missing]
                + [bf.file_id for bf in self.plan.locked_unchanged])

    def _refresh(self) -> None:
        assigned = self.assignments()
        for row, bf in enumerate(sorted(self.plan.missing, key=lambda f: f.path)):
            target = assigned[bf.file_id]
            sugg = self.suggestions.get(bf.file_id)
            item = self.rename_table.item(row, self.rename_off + 2)
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
        self._fill(self.list_modified, [(lf.path, bf.file_id) for bf, lf in sorted(
            self.plan.modified, key=lambda m: m[1].path)])
        self._fill(self.list_added, [(lf.path, new_key(lf.path)) for lf in sorted(
            self.plan.new, key=lambda f: f.path) if lf.path not in used])
        self._fill(self.list_deleted, [(f.path, f.file_id) for f in sorted(
            self.plan.missing, key=lambda f: f.path) if not assigned[f.file_id]])
        self._fill(self.list_unchanged, [(f.path, f.file_id) for f in sorted(
            self.plan.locked_unchanged, key=lambda f: f.path)])
        self._summary()

    def _summary(self) -> None:
        used = {p for p in self.assignments().values() if p}
        n_added = sum(1 for lf in self.plan.new if lf.path not in used)
        n_deleted = sum(1 for f in self.plan.missing if not self.assignments()[f.file_id])
        self.tabs.setTabText(0, f"Upravené ({len(self.plan.modified)})")
        self.tabs.setTabText(1, f"Nové ({n_added})")
        self.tabs.setTabText(2, f"Smazané ({n_deleted})")
        self.tabs.setTabText(3, f"Zamčené beze změny ({len(self.plan.locked_unchanged)})")
        text = (f"Upravené: <b>{len(self.plan.modified)}</b> &nbsp; Nové: <b>{n_added}</b> &nbsp; "
                f"Přejmenované: <b>{len(used)}</b> &nbsp; Smazané: <b>{n_deleted}</b>")
        if self.selectable:
            text += f" &nbsp; | &nbsp; Vybráno k odevzdání: <b>{len(self.checked)}</b>"
        self.summary.setText(text)
        self._validate()

    def _selected_changes(self) -> tuple[bool, list[str]]:
        """Vybrané položky: (obsahují změny?, vybrané názvy s nepovolenými znaky)."""
        assigned = self.assignments()
        sel = (lambda k: True) if self.end_session else (lambda k: k in self.checked)
        changes, paths = False, []
        for bf, lf in self.plan.modified:
            if sel(bf.file_id):
                changes = True
                paths.append(lf.path)
        for bf in self.plan.missing:
            if sel(bf.file_id):
                changes = True
                if assigned[bf.file_id]:
                    paths.append(assigned[bf.file_id])
        used = {p for p in assigned.values() if p}
        for lf in self.plan.new:
            if lf.path not in used and sel(new_key(lf.path)):
                changes = True
                paths.append(lf.path)
        return changes, [p for p in paths if p in self.invalid]

    def _validate(self) -> None:
        targets = [p for p in self.assignments().values() if p]
        changes, invalid = self._selected_changes()
        error = ""
        if invalid:
            error = "Vybrané soubory mají názvy s nepovolenými znaky – nejdřív je opravte."
        elif len(targets) != len(set(targets)):
            error = "Jeden nový soubor je přiřazen k více původním souborům."
        elif changes and not self.comment_text():
            error = "Bez komentáře nelze změny odevzdat."
        elif self.selectable and not self.checked:
            error = "Vyberte, co chcete odevzdat."
        self.error.setText(error)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(not error)
