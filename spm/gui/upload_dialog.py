"""Dialog odevzdání a deaktivace: přiřazení přejmenovaných souborů, výběr, komentář.

- Odevzdání: uživatel zaškrtne ve stromu složek, co nahrát (zaškrtnutím složky celý její
  obsah). Odevzdané soubory se uvolní – i zamčené soubory beze změny lze jen uvolnit.
  Projekt zůstává aktivovaný.
- Deaktivace: odevzdá se vše, uvolní se všechny zámky a aktivace skončí.

Záložka „Vše“ obsahuje všechny změny v jednom stromu, ostatní záložky jsou jen filtry;
zaškrtnutí se mezi záložkami přenáší.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..names import format_invalid
from ..notes import Draft
from ..scanner import to_abs
from ..sync import UploadPlan, auto_deactivation, new_key
from .check_tree import CheckTree, TreeEntry, in_scope

DELETED_LABEL = "— smazán —"
KEY_ROLE = Qt.ItemDataRole.UserRole

# záložky: (název, druhy změn, které obsahuje)
TABS = [
    ("Vše", {"modified", "renamed", "new", "deleted", "unchanged"}),
    ("Upravené", {"modified", "renamed"}),
    ("Nové", {"new"}),
    ("Smazané", {"deleted"}),
    ("Zamčené beze změny", {"unchanged"}),
]


class UploadDialog(QDialog):
    def __init__(self, plan: UploadPlan, end_session: bool, parent: QWidget | None = None,
                 local: Path | None = None, preselect: str | list[str] | None = None,
                 drafts: list[Draft] | None = None):
        """local: místní kopie projektu (pro ikony souborů).
        preselect: soubor nebo složka (zakončená '/'), jejíž změny budou předem zaškrtnuté.
        drafts: rozepsané poznámky – zaškrtnuté se zveřejní s odevzdáním."""
        super().__init__(parent)
        self.plan = plan
        self.local = local
        self.end_session = end_session
        self.selectable = not end_session
        self.setWindowTitle(("Deaktivace" if end_session else "Odevzdání") + f" – projekt {plan.name}")
        self.resize(920, 700)
        self.suggestions = {s.file_id: s for s in plan.suggestions}
        self.new_by_path = {lf.path: lf for lf in plan.new}
        self.combos: dict[str, QComboBox] = {}
        self.checked: set[str] = set()   # vybrané klíče (jen při odevzdání)
        self.invalid = set(plan.invalid_names)

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Nahrají se všechny vaše změny a uvolní se všechny vaše zámky. Aktivace projektu skončí."
            if end_session else
            "Zaškrtněte, co chcete odevzdat – zaškrtnutím složky vyberete celý její obsah. "
            "Odevzdané soubory se nahrají na server a uvolní – ostatní si je mohou stáhnout "
            "(Aktualizovat) nebo převzít k úpravě. Nezaškrtnuté zůstanou u vás rozpracované a zamčené."
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
            layout.addWidget(self._build_rename_table(), stretch=1)

        self.tabs = QTabWidget()
        self.trees: list[CheckTree] = []
        for title, _ in TABS:
            tree = CheckTree(["Soubor", "Změna"], checkable=self.selectable)
            tree.toggled.connect(lambda key, on, src=tree: self._on_toggled(key, on, src))
            self.trees.append(tree)
            self.tabs.addTab(tree, title)
        self.tree_all, self.tree_modified, self.tree_new, self.tree_deleted, self.tree_unchanged = self.trees
        layout.addWidget(self.tabs, stretch=2)

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

        # rozepsané poznámky (plovoucí okno Poznámky) – zaškrtnuté se zveřejní s odevzdáním
        self.drafts = list(drafts or [])
        self.draft_checks: list[QCheckBox] = []
        if self.drafts:
            box = QGroupBox("Poznámky pro další uživatele – zveřejnit s odevzdáním")
            box_lay = QVBoxLayout(box)
            for d in self.drafts:
                to = f" → pro {d.recipient_name or d.recipient}" if d.recipient else ""
                files = f"  ({len(d.targets)} souborů / složek)" if d.targets else ""
                check = QCheckBox(d.title + to + files)
                check.setChecked(bool(d.text.strip()))
                check.setEnabled(bool(d.text.strip()))
                check.setToolTip(d.text)
                box_lay.addWidget(check)
                self.draft_checks.append(check)
            note = QLabel("Nezaškrtnuté zůstanou rozepsané na příště"
                          + (" (i po deaktivaci – vrátí se při příští aktivaci)." if end_session else "."))
            note.setWordWrap(True)
            box_lay.addWidget(note)
            layout.addWidget(box)

        self.auto_info = QLabel()  # odevzdání posledního drženého souboru = deaktivace
        self.auto_info.setWordWrap(True)
        self.auto_info.setStyleSheet("color: #2f6fb5; font-weight: bold;")
        self.auto_info.hide()
        layout.addWidget(self.auto_info)

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

        if preselect and self.selectable:
            old_paths = {bf.file_id: bf.path for bf in plan.missing}
            self.checked = {e.key for _, e in self._entries()
                            if in_scope(e.path, preselect) or in_scope(old_paths.get(e.key, ""), preselect)}
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

        new_paths = sorted(self.new_by_path)
        for row, bf in enumerate(self._missing_sorted()):
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
        table.itemChanged.connect(self._on_table_changed)
        lay.addWidget(table)
        lay.addWidget(QLabel("Pokud soubor nebyl přejmenován, ale smazán, zvolte „— smazán —“."))
        return box

    def _missing_sorted(self):
        return sorted(self.plan.missing, key=lambda f: f.path)

    # --- výsledky ----------------------------------------------------------

    def assignments(self) -> dict[str, str | None]:
        return {fid: combo.currentData() for fid, combo in self.combos.items()}

    def selected_notes(self) -> list[Draft]:
        """Rozepsané poznámky zaškrtnuté ke zveřejnění."""
        return [d for d, check in zip(self.drafts, self.draft_checks) if check.isChecked()]

    def comment_text(self) -> str:
        return self.comment.toPlainText().strip()

    def selected_keys(self) -> set[str] | None:
        """Vybrané položky k odevzdání (None = vše, při deaktivaci)."""
        return None if self.end_session else set(self.checked)

    # --- položky -----------------------------------------------------------

    def _entries(self) -> list[tuple[str, TreeEntry]]:
        """Všechny položky k odevzdání jako (druh, položka stromu)."""
        assigned = self.assignments()
        used = {p for p in assigned.values() if p}

        def entry(path: str, key: str, note: str, exists: bool = True) -> TreeEntry:
            file_path = str(to_abs(self.local, path)) if self.local and exists else None
            return TreeEntry(path, key, [note], file_path=file_path)

        result: list[tuple[str, TreeEntry]] = []
        for bf, lf in self.plan.modified:
            result.append(("modified", entry(lf.path, bf.file_id, "upraven")))
        for bf in self.plan.missing:
            target = assigned[bf.file_id]
            if target:
                changed = self.new_by_path[target].sha256 != bf.sha256
                note = f"přejmenován z {bf.path}" + (" a upraven" if changed else "")
                result.append(("renamed", entry(target, bf.file_id, note)))
            else:
                result.append(("deleted", entry(bf.path, bf.file_id, "smazán", exists=False)))
        for lf in self.plan.new:
            if lf.path not in used:
                result.append(("new", entry(lf.path, new_key(lf.path), "nový")))
        for bf in self.plan.locked_unchanged:
            result.append(("unchanged", entry(bf.path, bf.file_id, "beze změny – jen uvolnit")))
        return result

    # --- chování -----------------------------------------------------------

    def _on_toggled(self, key: str, on: bool, source: CheckTree | None) -> None:
        (self.checked.add if on else self.checked.discard)(key)
        for tree in self.trees:
            if tree is not source:
                tree.set_key_checked(key, on)
        if self.plan.missing and self.selectable:
            self.rename_table.blockSignals(True)
            for row in range(self.rename_table.rowCount()):
                item = self.rename_table.item(row, 0)
                if item.data(KEY_ROLE) == key:
                    item.setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
            self.rename_table.blockSignals(False)
        self._summary()

    def _on_table_changed(self, item: QTableWidgetItem) -> None:
        key = item.data(KEY_ROLE)
        if key and self.selectable and item.column() == 0:
            self._on_toggled(key, item.checkState() == Qt.CheckState.Checked, None)

    def _check_all(self, on: bool) -> None:
        self.checked = {e.key for _, e in self._entries()} if on else set()
        self._refresh()

    def _refresh(self) -> None:
        assigned = self.assignments()
        for row, bf in enumerate(self._missing_sorted()):
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

        entries = self._entries()
        self.checked &= {e.key for _, e in entries}  # např. nový soubor se stal cílem přejmenování
        for tree, (_, kinds) in zip(self.trees, TABS):
            tree.set_entries([e for kind, e in entries if kind in kinds], self.checked)
        if self.plan.missing and self.selectable:
            self.rename_table.blockSignals(True)
            for row in range(self.rename_table.rowCount()):
                item = self.rename_table.item(row, 0)
                item.setCheckState(Qt.CheckState.Checked if item.data(KEY_ROLE) in self.checked
                                   else Qt.CheckState.Unchecked)
            self.rename_table.blockSignals(False)
        self._summary()

    def _summary(self) -> None:
        entries = self._entries()
        counts = {kind: sum(1 for k, _ in entries if k == kind)
                  for kind in ("modified", "renamed", "new", "deleted", "unchanged")}
        for i, (title, kinds) in enumerate(TABS):
            self.tabs.setTabText(i, f"{title} ({sum(counts[k] for k in kinds)})")
        text = (f"Upravené: <b>{counts['modified']}</b> &nbsp; Nové: <b>{counts['new']}</b> &nbsp; "
                f"Přejmenované: <b>{counts['renamed']}</b> &nbsp; Smazané: <b>{counts['deleted']}</b>")
        if self.selectable:
            text += f" &nbsp; | &nbsp; Vybráno k odevzdání: <b>{len(self.checked)}</b>"
        self.summary.setText(text)
        self._validate()

    def _selected_changes(self) -> tuple[bool, list[str]]:
        """Vybrané položky: (obsahují změny?, vybrané názvy s nepovolenými znaky)."""
        changes, paths = False, []
        for kind, e in self._entries():
            if kind == "unchanged" or not (self.end_session or e.key in self.checked):
                continue
            changes = True
            if kind != "deleted":
                paths.append(e.path)
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
        if self.selectable:
            ends, note = auto_deactivation(self.plan, self.assignments(), self.checked)
            self.auto_info.setText("Odevzdáváte poslední soubory, které držíte – projekt se poté "
                                   "deaktivuje." if ends else note)
            self.auto_info.setVisible(bool(ends or note))
