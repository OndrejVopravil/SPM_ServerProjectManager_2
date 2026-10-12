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

from .. import shared_config as sc
from ..mirror import Mirror, MirrorResult, MirrorRule, resolve_target, rules_from_settings
from .worker import run_task


SERVER_PREFIX = "server:"  # položka cíle podle identity síťové složky (ne záložky)


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
        # sdílené pravidlo s cílem, který uživatel nemá jako záložku: cíl se nabídne a zachová
        if self.rule.target_server and not self.rule.target:
            self.target.addItem(f"{self.rule.target_name or self.rule.target_server} "
                                "(nemáte jako záložku)", SERVER_PREFIX + self.rule.target_server)
        for combo, current in ((self.source, self.rule.source), (self.target, self.rule.target)):
            for ws in workspaces:
                combo.addItem(ws["name"], ws["id"])
            index = combo.findData(current)
            combo.setCurrentIndex(index if index >= 0 else -1)
        if self.target.currentIndex() < 0 and self.rule.target_server and not self.rule.target:
            self.target.setCurrentIndex(0)
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
        target = self.target.currentData() or ""
        rule = MirrorRule(source=self.source.currentData() or "", target=target,
                          files=self.files.text().strip(), folders=self.folders.text().strip(),
                          exclude=self.exclude.text().strip(), projects=self.projects.text().strip(),
                          enabled=self.enabled.isChecked(), id=self.rule.id)
        if target.startswith(SERVER_PREFIX):  # cíl, který uživatel nemá jako záložku
            rule.target = ""
            rule.target_server, rule.target_name = self.rule.target_server, self.rule.target_name
        return rule

    def _accept(self) -> None:
        error = self.result_rule().validate()
        if error:
            QMessageBox.warning(self, "Pravidlo zrcadlení", error)
            return
        self.accept()


class MirrorDialog(QDialog):
    """main: hlavní okno (záložky, nastavení a ProjectManager pro každou záložku).

    Pravidla jsou místní (osobní nastavení), dokud je zdrojová záložka nemá uložená ve svém
    spm_config.json – pak platí pro všechny sdílená pravidla ze sítě (místní pravidla s tímto
    zdrojem se nepoužívají) a jejich změny se ukládají zpět na síť."""

    COLUMNS = ["Zapnuto", "Zdroj", "Cíl", "Pravidlo", "Uložení"]

    def __init__(self, main, parent: QWidget | None = None):
        super().__init__(parent)
        self.main = main
        self.setWindowTitle("Zrcadlení mezi záložkami")
        self.resize(940, 420)
        self.rules: list[MirrorRule] = []

        info = QLabel(
            "Vybrané soubory z projektů jedné záložky se po každém odevzdání (a publikování) "
            "zrcadlí do stejnojmenných projektů jiné záložky. Pravidla jsou místní, dokud je "
            "neuložíte na síť (<i>Uložit na síť…</i>) – pak platí pro všechny, kdo mají zdroj "
            "i cíl jako záložky, a mění se jen na síti.")
        info.setWordWrap(True)
        self.enabled_here = QCheckBox("Zrcadlit z tohoto počítače")
        self.enabled_here.setToolTip("Vypněte, pokud do cílové síťové složky nemáte právo zapisovat.")
        self.enabled_here.setChecked(main.settings.mirror_enabled)
        self.enabled_here.toggled.connect(self._toggle_here)

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
        self.btn_export = QPushButton("Uložit na síť…")
        self.btn_export.setToolTip("Uloží místní pravidla zdrojové záložky do jejího spm_config.json – "
                                   "budou platit pro všechny, kdo mají zdroj i cíl jako záložky")
        self.btn_export.clicked.connect(lambda: self.export_rules())
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
        side.addWidget(self.btn_export)
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
        layout.addWidget(self.enabled_here)
        layout.addWidget(self.no_tabs)
        layout.addLayout(row)
        layout.addWidget(close)
        self._load()

    # --- pravidla ----------------------------------------------------------------

    @property
    def app(self):
        return self.main.app_settings

    def _ws_name(self, ws_id: str) -> str:
        return next((w["name"] for w in self.app.workspaces if w["id"] == ws_id), "(odebraná záložka)")

    def _shared_of(self, ws_id: str) -> list[dict] | None:
        """Sdílená pravidla zdrojové záložky (None = nemá – platí místní)."""
        if not any(w["id"] == ws_id for w in self.app.workspaces):
            return None
        return self.app.view(ws_id).shared_mirror_rules

    def _local(self) -> list[MirrorRule]:
        return rules_from_settings(self.main.settings.mirror_rules)

    def _load(self, select_id: str | None = None) -> None:
        """Místní pravidla a sdílená pravidla všech záložek."""
        self.rules = self._local()
        for ws in self.app.workspaces:
            shared = self._shared_of(ws["id"])
            if shared is not None:
                self.rules += [MirrorRule.from_shared(d, ws["id"]) for d in shared]
        self._fill(select_id)

    def _target_text(self, rule: MirrorRule) -> str:
        target = resolve_target(self.app, rule)
        if target is not None:
            return self._ws_name(target)
        return f"{rule.target_name or rule.target_server or '?'} (nemáte jako záložku)"

    def _storage_text(self, rule: MirrorRule) -> str:
        if rule.shared:
            return f"síť – spm_config.json ({self._ws_name(rule.source)})"
        if self._shared_of(rule.source) is not None:
            return "místní – neplatí (zdroj má pravidla na síti)"
        return "místní"

    def _fill(self, select_id: str | None = None) -> None:
        self.table.blockSignals(True)
        try:
            self.table.setRowCount(len(self.rules))
            for r, rule in enumerate(self.rules):
                check = QTableWidgetItem()
                check.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled
                               | Qt.ItemFlag.ItemIsSelectable)
                check.setCheckState(Qt.CheckState.Checked if rule.enabled else Qt.CheckState.Unchecked)
                self.table.setItem(r, 0, check)
                for c, text in enumerate((self._ws_name(rule.source), self._target_text(rule),
                                          rule.describe(), self._storage_text(rule)), start=1):
                    item = QTableWidgetItem(text)
                    item.setToolTip(text)
                    self.table.setItem(r, c, item)
        finally:
            self.table.blockSignals(False)
        self.table.resizeColumnsToContents()
        for r, rule in enumerate(self.rules):
            if rule.id == select_id:
                self.table.selectRow(r)
        self._update_buttons()

    def _selected(self) -> MirrorRule | None:
        rows = self.table.selectionModel().selectedRows()
        return self.rules[rows[0].row()] if rows else None

    def _rule_usable(self, rule: MirrorRule) -> bool:
        ids = {w["id"] for w in self.app.workspaces}
        return rule.source in ids and resolve_target(self.app, rule) is not None

    def _update_buttons(self) -> None:
        enough = len(self.app.workspaces) >= 2
        self.no_tabs.setVisible(not enough)
        self.btn_add.setEnabled(enough)
        rule = self._selected()
        for b in (self.btn_edit, self.btn_remove, self.btn_preview):
            b.setEnabled(rule is not None)
        self.btn_export.setEnabled(rule is not None and not rule.shared
                                   and self._shared_of(rule.source) is None)
        self.btn_sync.setEnabled(rule is not None and rule.enabled and self._rule_usable(rule))

    def _toggle_here(self, on: bool) -> None:
        self.main.settings.mirror_enabled = on
        self._save_local(self._local())

    def _save_local(self, rules: list[MirrorRule]) -> None:
        self.main.settings.mirror_rules = [r.to_dict() for r in rules]
        try:
            self.main.settings.save()
        except OSError as e:
            QMessageBox.warning(self, "Zrcadlení", f"Nastavení nelze uložit:\n{e}")

    def _write_shared(self, source_ws: str, rules: list[MirrorRule], question: str) -> bool:
        """Uloží sdílená pravidla zdrojové záložky do jejího spm_config.json (po potvrzení).
        Cílové záložky dostanou identitu síťové složky (zapíše se do jejich spm_config.json)."""
        view = self.app.view(source_ws)
        answer = QMessageBox.question(
            self, "Zrcadlení – síťová pravidla",
            f"{question}\n\nUloží se do {sc.config_path(view.server_root)} a bude platit pro všechny, "
            "kdo mají zdroj i cíl jako záložky.")
        if answer != QMessageBox.StandardButton.Yes:
            return False
        try:
            sc.ensure_server_id(view.server_root, self._ws_name(source_ws))
            data = []
            for rule in rules:
                if rule.target in {w["id"] for w in self.app.workspaces}:
                    target_view = self.app.view(rule.target)
                    rule.target_server = sc.ensure_server_id(target_view.server_root,
                                                             self._ws_name(rule.target))
                    rule.target_name = self._ws_name(rule.target)
                data.append(rule.to_shared())
            sc.write(view.server_root, {sc.MIRROR_RULES: data})
        except sc.SharedConfigError as e:
            QMessageBox.warning(self, "Zrcadlení – síťová pravidla", str(e))
            return False
        self.main._sync_shared()  # převzít ze sítě (i identity složek)
        return True

    def _shared_rules(self, source_ws: str) -> list[MirrorRule]:
        return [MirrorRule.from_shared(d, source_ws) for d in self._shared_of(source_ws) or []]

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != 0:
            return
        rule = self.rules[item.row()]
        on = item.checkState() == Qt.CheckState.Checked
        if rule.shared:
            rules = self._shared_rules(rule.source)
            for r in rules:
                if r.id == rule.id:
                    r.enabled = on
            if not self._write_shared(rule.source, rules,
                                      ("Zapnout" if on else "Vypnout") + " sdílené pravidlo zrcadlení?"):
                self._load(rule.id)  # vrátit zaškrtnutí
                return
        else:
            local = self._local()
            for r in local:
                if r.id == rule.id:
                    r.enabled = on
            self._save_local(local)
        self._load(rule.id)

    def add_rule(self) -> None:
        current = self.main.settings.workspace_id
        dlg = RuleDialog(self.app.workspaces, MirrorRule(source=current, target=""), self)
        if not dlg.exec():
            return
        rule = dlg.result_rule()
        if self._shared_of(rule.source) is not None:  # zdroj má pravidla na síti -> přidat tam
            if not self._write_shared(rule.source, self._shared_rules(rule.source) + [rule],
                                      "Přidat sdílené pravidlo zrcadlení?"):
                return
        else:
            self._save_local(self._local() + [rule])
        self._load(rule.id)

    def edit_rule(self) -> None:
        rule = self._selected()
        if rule is None:
            return
        editable = MirrorRule.from_dict(rule.to_dict())
        editable.target = resolve_target(self.app, rule) or ""
        dlg = RuleDialog(self.app.workspaces, editable, self)
        if not dlg.exec():
            return
        new = dlg.result_rule()
        if rule.shared:
            rules = [new if r.id == rule.id else r for r in self._shared_rules(rule.source)]
            if not self._write_shared(rule.source, rules, "Uložit změnu sdíleného pravidla?"):
                return
        else:
            self._save_local([new if r.id == rule.id else r for r in self._local()])
        self._load(new.id)

    def remove_rule(self) -> None:
        rule = self._selected()
        if rule is None:
            return
        if rule.shared:
            rules = [r for r in self._shared_rules(rule.source) if r.id != rule.id]
            if not self._write_shared(rule.source, rules,
                                      f"Odebrat sdílené pravidlo {rule.describe()}?\n\nSoubory už "
                                      "zrcadlené v cíli zůstanou a zůstanou i zamčené zrcadlem."):
                return
        else:
            answer = QMessageBox.question(
                self, "Odebrat pravidlo",
                f"Odebrat pravidlo {rule.describe()}?\n\nSoubory už zrcadlené v cíli zůstanou "
                "a zůstanou i zamčené zrcadlem.")
            if answer != QMessageBox.StandardButton.Yes:
                return
            self._save_local([r for r in self._local() if r.id != rule.id])
        self._load()

    def export_rules(self) -> None:
        """Místní pravidla zdrojové záložky -> její spm_config.json (sdílená pro všechny)."""
        rule = self._selected()
        if rule is None or rule.shared:
            return
        source = rule.source
        moving = [r for r in self._local() if r.source == source]
        if not self._write_shared(source, [MirrorRule.from_dict(r.to_dict()) for r in moving],
                                  f"Uložit {len(moving)} místních pravidel se zdrojem "
                                  f"„{self._ws_name(source)}“ na síť?"):
            return
        self._save_local([r for r in self._local() if r.source != source])  # teď platí ze sítě
        self._load()

    # --- náhled a zrcadlení -------------------------------------------------------

    def _mirror(self, rule: MirrorRule) -> Mirror | None:
        if not self._rule_usable(rule):
            QMessageBox.warning(self, "Zrcadlení", "Zdroj nebo cíl pravidla nemáte jako záložku.")
            return None
        source, target = self.main.pm_for(rule.source), self.main.pm_for(resolve_target(self.app, rule))
        for pm in (source, target):
            if not pm.settings.is_complete():
                QMessageBox.warning(self, "Zrcadlení", "Záložka zdroje nebo cíle nemá nastavené složky.")
                return None
        enabled = MirrorRule.from_dict({**rule.to_dict(), "enabled": True})  # náhled i u vypnutého
        return Mirror(source, target, [enabled])

    def preview(self) -> None:
        rule = self._selected()
        mirror = self._mirror(rule) if rule is not None else None
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
        rule = self._selected()
        mirror = self._mirror(rule) if rule is not None else None
        if mirror is None:
            return
        target = resolve_target(self.app, rule)
        answer = QMessageBox.question(
            self, "Zrcadlit teď",
            f"Zrcadlit z „{self._ws_name(rule.source)}“ do „{self._ws_name(target)}“ "
            f"všechny projekty, které pravidlo zachytí?\n\n{rule.describe()}\n\n"
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
