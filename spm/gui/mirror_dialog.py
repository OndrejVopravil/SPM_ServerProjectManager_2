"""Okno Zrcadlení: pravidla zrcadlení souborů mezi záložkami, náhled a ruční zrcadlení."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..mirror import Mirror, MirrorResult, MirrorRule, rules_from_settings
from .worker import run_task


def format_mirror_counts(res: MirrorResult) -> str:
    parts = [f"{label}: {len(items)}" for label, items in (
        ("nové", res.added), ("změněné", res.modified), ("přejmenované", res.renamed),
        ("smazané", res.deleted), ("převzaté", res.adopted), ("uvolněné", res.released)) if items]
    text = ", ".join(parts) or "beze změn"
    if res.created_project:
        text += " (projekt v cíli založen)"
    return text


def format_mirror_problems(res: MirrorResult, limit: int = 15) -> str:
    lines = [res.note] if res.note else []
    lines += res.conflicts[:limit]
    if len(res.conflicts) > limit:
        lines.append("…")
    if res.released:
        lines.append("Přestaly odpovídat pravidlu (v cíli ponechány, už je nespravuje zrcadlo): "
                     + ", ".join(res.released[:5]) + (" …" if len(res.released) > 5 else ""))
    return "\n".join(f"  {line}" for line in lines)


class RuleDialog(QDialog):
    def __init__(self, workspaces: list[dict], rule: MirrorRule | None = None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Pravidlo zrcadlení")
        self.setMinimumWidth(560)
        self.rule = rule or MirrorRule(source="", target="")

        self.source, self.target = QComboBox(), QComboBox()
        for combo, current in ((self.source, self.rule.source), (self.target, self.rule.target)):
            for ws in workspaces:
                combo.addItem(ws["name"], ws["id"])
            index = combo.findData(current)
            combo.setCurrentIndex(index if index >= 0 else -1)
        if self.target.currentIndex() < 0 and self.target.count() > 1:
            self.target.setCurrentIndex(1 if self.source.currentIndex() == 0 else 0)
        if self.source.currentIndex() < 0 and self.source.count():
            self.source.setCurrentIndex(0)

        self.files = QLineEdit(self.rule.files)
        self.files.setPlaceholderText("např. *.nc; *.mpf   (prázdné = všechny soubory)")
        self.folders = QLineEdit(self.rule.folders)
        self.folders.setPlaceholderText("např. nc_codes; *NC*   (prázdné = kdekoliv v projektu)")
        self.exclude = QLineEdit(self.rule.exclude)
        self.exclude.setPlaceholderText("např. stare; archiv*   (nepovinné)")
        self.projects = QLineEdit(self.rule.projects)
        self.projects.setPlaceholderText("např. 058*   (prázdné = všechny projekty)")
        self.enabled = QCheckBox("Pravidlo je zapnuté")
        self.enabled.setChecked(self.rule.enabled)

        hint = QLabel(
            "Soubor se zrcadlí, když odpovídá <b>všem</b> vyplněným podmínkám; v jedné podmínce "
            "stačí <b>kterýkoliv</b> vzor (oddělujte středníkem). Vzory: <i>*</i> = cokoliv, "
            "<i>?</i> = jeden znak, velikost písmen nerozhoduje. Složka se hledá kdekoliv v cestě "
            "souboru. Soubor skončí na stejné relativní cestě ve stejnojmenném projektu cíle "
            "(chybějící projekt se v cíli založí).<br><br>"
            "V cíli zrcadlené soubory nikdo nemůže převzít ani upravit – mění se jen ve zdroji.")
        hint.setWordWrap(True)

        form = QFormLayout()
        form.addRow("Zdroj (záložka):", self.source)
        form.addRow("Cíl (záložka):", self.target)
        form.addRow("Soubory:", self.files)
        form.addRow("Ve složkách:", self.folders)
        form.addRow("Vyjma složek:", self.exclude)
        form.addRow("Jen projekty:", self.projects)
        form.addRow(self.enabled)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Zrušit")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(hint)
        layout.addWidget(buttons)

    def result_rule(self) -> MirrorRule:
        return MirrorRule(source=self.source.currentData() or "", target=self.target.currentData() or "",
                          files=self.files.text().strip(), folders=self.folders.text().strip(),
                          exclude=self.exclude.text().strip(), projects=self.projects.text().strip(),
                          enabled=self.enabled.isChecked(), id=self.rule.id)

    def _accept(self) -> None:
        error = self.result_rule().validate()
        if error:
            QMessageBox.warning(self, "Pravidlo zrcadlení", error)
            return
        self.accept()


class MirrorDialog(QDialog):
    """main: hlavní okno (záložky, nastavení a ProjectManager pro každou záložku)."""

    COLUMNS = ["Zapnuto", "Zdroj", "Cíl", "Pravidlo"]

    def __init__(self, main, parent: QWidget | None = None):
        super().__init__(parent)
        self.main = main
        self.setWindowTitle("Zrcadlení mezi záložkami")
        self.resize(820, 380)
        self.rules = rules_from_settings(main.settings.mirror_rules)

        info = QLabel(
            "Vybrané soubory z projektů jedné záložky se po každém odevzdání (a publikování) "
            "zrcadlí do stejnojmenných projektů jiné záložky. Pravidla jsou vaše osobní nastavení.")
        info.setWordWrap(True)
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.doubleClicked.connect(lambda _: self.edit_rule())

        self.btn_add = QPushButton("Přidat…")
        self.btn_add.clicked.connect(lambda: self.add_rule())
        self.btn_edit = QPushButton("Upravit…")
        self.btn_edit.clicked.connect(lambda: self.edit_rule())
        self.btn_remove = QPushButton("Odebrat")
        self.btn_remove.clicked.connect(lambda: self.remove_rule())
        self.btn_preview = QPushButton("Náhled…")
        self.btn_preview.setToolTip("Ukáže, které soubory pravidlo zachytí (podle evidence zdroje)")
        self.btn_preview.clicked.connect(lambda: self.preview())
        self.btn_sync = QPushButton("Zrcadlit teď")
        self.btn_sync.setToolTip("Zrcadlí všechny zachycené projekty – i první nahrání dat")
        self.btn_sync.clicked.connect(lambda: self.sync_now())
        self.no_tabs = QLabel("Zrcadlení potřebuje alespoň dvě záložky – zdroj i cíl musí být "
                              "přidané jako záložky (tlačítko + nad seznamem projektů).")
        self.no_tabs.setWordWrap(True)
        self.no_tabs.setStyleSheet("color: #c0392b;")

        side = QVBoxLayout()
        for b in (self.btn_add, self.btn_edit, self.btn_remove):
            side.addWidget(b)
        side.addSpacing(12)
        side.addWidget(self.btn_preview)
        side.addWidget(self.btn_sync)
        side.addStretch()
        row = QHBoxLayout()
        row.addWidget(self.table)
        row.addLayout(side)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.button(QDialogButtonBox.StandardButton.Close).setText("Zavřít")
        close.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(info)
        layout.addWidget(self.no_tabs)
        layout.addLayout(row)
        layout.addWidget(close)
        self._fill()

    # --- seznam pravidel ------------------------------------------------------

    def _ws_name(self, ws_id: str) -> str:
        return next((w["name"] for w in self.main.app_settings.workspaces if w["id"] == ws_id),
                    "(odebraná záložka)")

    def _fill(self, select: int | None = None) -> None:
        self.table.blockSignals(True)
        try:
            self.table.setRowCount(len(self.rules))
            for r, rule in enumerate(self.rules):
                check = QTableWidgetItem()
                check.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled
                               | Qt.ItemFlag.ItemIsSelectable)
                check.setCheckState(Qt.CheckState.Checked if rule.enabled else Qt.CheckState.Unchecked)
                self.table.setItem(r, 0, check)
                for c, text in enumerate((self._ws_name(rule.source), self._ws_name(rule.target),
                                          rule.describe()), start=1):
                    item = QTableWidgetItem(text)
                    item.setToolTip(text)
                    self.table.setItem(r, c, item)
        finally:
            self.table.blockSignals(False)
        self.table.resizeColumnsToContents()
        if select is not None and 0 <= select < len(self.rules):
            self.table.selectRow(select)
        self._update_buttons()

    def _selected(self) -> int | None:
        rows = self.table.selectionModel().selectedRows()
        return rows[0].row() if rows else None

    def _update_buttons(self) -> None:
        enough = len(self.main.app_settings.workspaces) >= 2
        self.no_tabs.setVisible(not enough)
        self.btn_add.setEnabled(enough)
        i = self._selected()
        for b in (self.btn_edit, self.btn_remove, self.btn_preview):
            b.setEnabled(i is not None)
        self.btn_sync.setEnabled(i is not None and self.rules[i].enabled and self._rule_usable(self.rules[i]))

    def _rule_usable(self, rule: MirrorRule) -> bool:
        ids = {w["id"] for w in self.main.app_settings.workspaces}
        return rule.source in ids and rule.target in ids

    def _save(self) -> None:
        self.main.settings.mirror_rules = [r.to_dict() for r in self.rules]
        try:
            self.main.settings.save()
        except OSError as e:
            QMessageBox.warning(self, "Zrcadlení", f"Nastavení nelze uložit:\n{e}")

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() == 0:
            self.rules[item.row()].enabled = item.checkState() == Qt.CheckState.Checked
            self._save()
            self._update_buttons()

    def add_rule(self) -> None:
        current = self.main.settings.workspace_id
        dlg = RuleDialog(self.main.app_settings.workspaces, MirrorRule(source=current, target=""), self)
        if dlg.exec():
            self.rules.append(dlg.result_rule())
            self._save()
            self._fill(len(self.rules) - 1)

    def edit_rule(self) -> None:
        i = self._selected()
        if i is None:
            return
        dlg = RuleDialog(self.main.app_settings.workspaces, self.rules[i], self)
        if dlg.exec():
            self.rules[i] = dlg.result_rule()
            self._save()
            self._fill(i)

    def remove_rule(self) -> None:
        i = self._selected()
        if i is None:
            return
        answer = QMessageBox.question(
            self, "Odebrat pravidlo",
            f"Odebrat pravidlo {self.rules[i].describe()}?\n\nSoubory už zrcadlené v cíli zůstanou "
            "a zůstanou i zamčené zrcadlem.")
        if answer == QMessageBox.StandardButton.Yes:
            del self.rules[i]
            self._save()
            self._fill()

    # --- náhled a zrcadlení -------------------------------------------------------

    def _mirror(self, rule: MirrorRule) -> Mirror | None:
        if not self._rule_usable(rule):
            QMessageBox.warning(self, "Zrcadlení", "Zdrojová nebo cílová záložka pravidla už neexistuje.")
            return None
        source, target = self.main.pm_for(rule.source), self.main.pm_for(rule.target)
        for pm in (source, target):
            if not pm.settings.is_complete():
                QMessageBox.warning(self, "Zrcadlení", "Záložka zdroje nebo cíle nemá nastavené složky.")
                return None
        enabled = MirrorRule(**{**rule.to_dict(), "enabled": True})  # náhled i pro vypnuté pravidlo
        return Mirror(source, target, [enabled])

    def preview(self) -> None:
        i = self._selected()
        mirror = self._mirror(self.rules[i]) if i is not None else None
        if mirror is None:
            return

        def done(result: dict[str, list[str]]) -> None:
            with_files = {k: v for k, v in result.items() if v}
            no_evidence = sorted(k for k, v in result.items() if not v)
            total = sum(len(v) for v in with_files.values())
            text = f"Pravidlo zachytí {total} souborů v {len(with_files)} projektech."
            if no_evidence:
                text += (f"\n\n{len(no_evidence)} projektů ve zdroji ještě nemá evidenci (nebyly "
                         "nikdy aktivovány) – zrcadlí se až po první aktivaci:\n  "
                         + ", ".join(no_evidence[:20]) + (" …" if len(no_evidence) > 20 else ""))
            box = QMessageBox(QMessageBox.Icon.Information, "Náhled zrcadlení", text, parent=self)
            box.setDetailedText("\n\n".join(f"{name} ({len(paths)}):\n  " + "\n  ".join(paths)
                                            for name, paths in sorted(with_files.items())))
            box.exec()

        run_task(self, "Náhled zrcadlení", mirror.preview, on_done=done)

    def sync_now(self) -> None:
        i = self._selected()
        mirror = self._mirror(self.rules[i]) if i is not None else None
        if mirror is None:
            return
        answer = QMessageBox.question(
            self, "Zrcadlit teď",
            f"Zrcadlit z „{self._ws_name(self.rules[i].source)}“ do „{self._ws_name(self.rules[i].target)}“ "
            f"všechny projekty, které pravidlo zachytí?\n\n{self.rules[i].describe()}\n\n"
            "Chybějící projekty se v cíli založí, změněné soubory se v cíli zazálohují.")
        if answer != QMessageBox.StandardButton.Yes:
            return

        def done(results: list[MirrorResult]) -> None:
            changed = [r for r in results if r.changed or r.adopted or r.created_project]
            problems = [r for r in results if not r.ok]
            text = (f"Zrcadleno projektů: {len(results)}, se změnami: {len(changed)}"
                    + (f", s problémy: {len(problems)}" if problems else "") + ".")
            details = [f"{r.project}: {format_mirror_counts(r)}" for r in changed]
            details += [f"{r.project} – problémy:\n{format_mirror_problems(r)}" for r in problems]
            box = QMessageBox(QMessageBox.Icon.Warning if problems else QMessageBox.Icon.Information,
                              "Zrcadlit teď", text, parent=self)
            if details:
                box.setDetailedText("\n".join(details))
            box.exec()

        run_task(self, "Zrcadlení", mirror.sync_all, on_done=done)
