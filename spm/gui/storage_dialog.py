"""Zálohy a koš: kolik místa zabírají v projektech síťové složky a jejich úklid
(náhled – co se smaže a kolik místa se uvolní – a potvrzení)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import storage
from ..storage import StorageInfo, fmt_bytes

COLUMNS = ["Projekt", "Zálohy", "Velikost záloh", "Koš", "Velikost koše", "Dočasné", "Celkem"]


class _Num(QTableWidgetItem):
    """Buňka řazená podle čísla (velikost v bajtech), zobrazená čitelně."""

    def __init__(self, text: str, value: int):
        super().__init__(text)
        self.value = value
        self.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

    def __lt__(self, other) -> bool:
        return self.value < getattr(other, "value", 0)


class StorageDialog(QDialog):
    def __init__(self, server_root: Path, infos: list[StorageInfo], user: str, pc: str,
                 backup_count: int, parent: QWidget | None = None, clean_problem: str | None = None,
                 leftovers: list[storage.Leftover] | None = None,
                 leftover_problems: list[str] | None = None):
        """clean_problem: proč uživatel nesmí uklízet (oprávnění) – úklid se pak zakáže
        s vysvětlením, přehled zůstane. leftovers: zbytky nedokončených operací v síťové složce
        (jen v přehledu celé síťové složky); leftover_problems: co se při jejich hledání
        nepodařilo přečíst (zobrazí se jako upozornění)."""
        super().__init__(parent)
        self.server_root, self.user, self.pc = server_root, user, pc
        self.clean_problem = clean_problem
        self.infos = {i.project: i for i in infos}
        single = len(infos) == 1
        self.setWindowTitle(f"Zálohy a koš – {infos[0].project}" if single
                            else f"Zálohy a koš – {server_root}")
        self.resize(900, 560)

        intro = QLabel("Zálohy (předchozí verze souborů), koš projektu (soubory smazané odevzdáním) "
                       "a dočasné soubory na serveru. Úklid je trvalý – nejdřív ukáže, co se smaže.")
        intro.setWordWrap(True)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.setSortingEnabled(True)
        self.total = QLabel()

        box = QGroupBox("Úklid vybraných projektů")
        blay = QVBoxLayout(box)
        row = QHBoxLayout()
        self.keep_check = QCheckBox("Ponechat jen posledních")
        self.keep_check.setChecked(True)
        self.keep = QSpinBox()
        self.keep.setRange(1, 99)
        self.keep.setValue(max(1, backup_count))
        row.addWidget(self.keep_check)
        row.addWidget(self.keep)
        row.addWidget(QLabel("záloh každého souboru"))
        row.addStretch()
        blay.addLayout(row)
        row = QHBoxLayout()
        self.trash_check = QCheckBox("Vysypat koš – soubory smazané před více než")
        self.trash_check.setChecked(True)
        self.trash_days = QSpinBox()
        self.trash_days.setRange(0, 3650)
        self.trash_days.setValue(30)
        row.addWidget(self.trash_check)
        row.addWidget(self.trash_days)
        row.addWidget(QLabel("dny"))
        row.addStretch()
        blay.addLayout(row)
        self.orphans_check = QCheckBox("Smazat neevidované zálohy (pozůstatky přerušených odevzdání) "
                                       "a dočasné soubory")
        self.orphans_check.setChecked(True)
        blay.addWidget(self.orphans_check)
        note = QLabel("Historie zůstává. Ke starším stavům projektu pak ale půjde obnovit méně "
                      "(časová osa v Historii to ukáže).")
        note.setWordWrap(True)
        blay.addWidget(note)

        self.btn_clean = QPushButton("Uklidit vybrané…")
        self.btn_clean.clicked.connect(lambda: self.clean_selected())
        if clean_problem:
            self.btn_clean.setToolTip(clean_problem)
            box.setEnabled(False)
            box.setToolTip(clean_problem)
        close = QPushButton("Zavřít")
        close.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addWidget(self.btn_clean)
        buttons.addStretch()
        buttons.addWidget(close)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.table, stretch=1)
        layout.addWidget(self.total)
        layout.addWidget(box)
        self.leftovers = list(leftovers or [])
        self.leftover_problems = list(leftover_problems or [])
        self.leftover_box = self._leftover_box()
        layout.addWidget(self.leftover_box)
        layout.addLayout(buttons)
        self._fill()
        self.table.selectAll() if single else self.table.selectRow(0)
        self.table.itemSelectionChanged.connect(self._update)
        self._update()

    # --- zbytky nedokončených operací ----------------------------------------------------

    def _leftover_box(self) -> QGroupBox:
        box = QGroupBox("Zbytky nedokončených operací v síťové složce")
        self.leftover_box = box
        lay = QVBoxLayout(box)
        info = QLabel("Skryté složky, které zůstaly po přerušené obnově z archivu, archivaci nebo "
                      "publikování (starší než 24 hodin – mladší mohou patřit operaci, která právě běží). "
                      "V seznamu projektů nejsou vidět, ale zabírají místo.")
        info.setWordWrap(True)
        lay.addWidget(info)
        # upozornění: některé složky (nebo celou síťovou složku) nešlo přečíst
        self.leftover_warning = QLabel()
        self.leftover_warning.setWordWrap(True)
        self.leftover_warning.setStyleSheet("color: #d35400;")
        lay.addWidget(self.leftover_warning)
        self.leftover_list = QListWidget()
        self.leftover_list.setMaximumHeight(110)
        lay.addWidget(self.leftover_list)
        row = QHBoxLayout()
        self.btn_leftovers = QPushButton("Smazat vybrané zbytky…")
        self.btn_leftovers.clicked.connect(lambda: self.remove_leftovers())
        if self.clean_problem:
            self.btn_leftovers.setToolTip(self.clean_problem)
        row.addWidget(self.btn_leftovers)
        row.addStretch()
        lay.addLayout(row)
        self._fill_leftovers()
        return box

    def _fill_leftovers(self) -> None:
        self.leftover_list.clear()
        for item in self.leftovers:
            row = QListWidgetItem(f"{item.path.name} – {item.kind}, {fmt_bytes(item.size)} "
                                  f"({item.files} souborů), naposledy {item.modified:%d.%m.%Y %H:%M}")
            row.setToolTip(str(item.path))
            row.setFlags(row.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            row.setCheckState(Qt.CheckState.Checked)
            self.leftover_list.addItem(row)
        self.btn_leftovers.setEnabled(bool(self.leftovers) and not self.clean_problem)
        problems = self.leftover_problems
        self.leftover_warning.setText(
            "⚠ Některé složky se nepodařilo přečíst (chybí právo, výpadek sítě) a nejsou v seznamu – "
            "jejich stáří a velikost nelze spolehlivě zjistit:\n" + "\n".join(f"• {p}" for p in problems[:8])
            + (f"\n… a další ({len(problems) - 8})" if len(problems) > 8 else "") if problems else "")
        self.leftover_warning.setVisible(bool(problems))
        self.leftover_list.setVisible(bool(self.leftovers))
        self.btn_leftovers.setVisible(bool(self.leftovers))
        # bez zbytků i bez problémů se sekce nezobrazí
        self.leftover_box.setVisible(bool(self.leftovers or problems))

    def checked_leftovers(self) -> list[storage.Leftover]:
        return [self.leftovers[i] for i in range(self.leftover_list.count())
                if self.leftover_list.item(i).checkState() == Qt.CheckState.Checked]

    def remove_leftovers(self, confirm: bool = True) -> int:
        """Smaže zaškrtnuté zbytky (trvale, jen pro oprávněné). Vrací počet smazaných složek."""
        if self.clean_problem:
            QMessageBox.information(self, "Zbytky operací", self.clean_problem)
            return 0
        items = self.checked_leftovers()
        if not items:
            return 0
        if confirm and QMessageBox.question(
                self, "Zbytky operací",
                f"Trvale smazat {len(items)} složek zbytků ({fmt_bytes(sum(i.size for i in items))})?\n\n"
                + "\n".join(i.path.name for i in items)) != QMessageBox.StandardButton.Yes:
            return 0
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            removed = storage.remove_leftovers(self.server_root, items)
        finally:
            QApplication.restoreOverrideCursor()
        if len(removed) < len(items):
            QMessageBox.warning(self, "Zbytky operací", "Některé složky se nepodařilo celé smazat "
                                "(soubor je otevřený nebo chybí právo zápisu).")
        self.leftover_problems = []
        self.leftovers = storage.leftovers(self.server_root, problems=self.leftover_problems)
        self._fill_leftovers()
        return len(removed)

    def _fill(self) -> None:
        self.table.setSortingEnabled(False)
        infos = sorted(self.infos.values(), key=lambda i: -i.total)
        self.table.setRowCount(len(infos))
        for r, i in enumerate(infos):
            name = QTableWidgetItem(i.project)
            name.setData(Qt.ItemDataRole.UserRole, i.project)
            self.table.setItem(r, 0, name)
            for c, (text, value) in enumerate((
                    (str(i.backup_files), i.backup_files), (fmt_bytes(i.backup_bytes), i.backup_bytes),
                    (str(i.trash_files), i.trash_files), (fmt_bytes(i.trash_bytes), i.trash_bytes),
                    (fmt_bytes(i.tmp_bytes), i.tmp_bytes), (fmt_bytes(i.total), i.total)), start=1):
                self.table.setItem(r, c, _Num(text, value))
        self.table.setSortingEnabled(True)
        self.table.sortItems(6, Qt.SortOrder.DescendingOrder)
        for c in range(1, len(COLUMNS)):
            self.table.resizeColumnToContents(c)
        total = sum(i.total for i in self.infos.values())
        self.total.setText(f"Celkem: <b>{fmt_bytes(total)}</b> v {len(self.infos)} projektech "
                           f"(zálohy {fmt_bytes(sum(i.backup_bytes for i in self.infos.values()))}, "
                           f"koš {fmt_bytes(sum(i.trash_bytes for i in self.infos.values()))})")

    def selected_projects(self) -> list[str]:
        rows = {i.row() for i in self.table.selectionModel().selectedRows()}
        return [self.table.item(r, 0).data(Qt.ItemDataRole.UserRole) for r in sorted(rows)]

    def _update(self) -> None:
        self.btn_clean.setEnabled(bool(self.selected_projects()) and not self.clean_problem)

    def _plans(self, projects: list[str]) -> list[storage.CleanupPlan]:
        keep = self.keep.value() if self.keep_check.isChecked() else None
        days = self.trash_days.value() if self.trash_check.isChecked() else None
        return [storage.plan_cleanup(self.server_root / name, keep, days, self.orphans_check.isChecked())
                for name in projects]

    def clean_selected(self, confirm: bool = True) -> int:
        """Náhled úklidu vybraných projektů, potvrzení a úklid. Vrací počet smazaných souborů."""
        if self.clean_problem:
            QMessageBox.information(self, "Úklid", self.clean_problem)
            return 0
        projects = self.selected_projects()
        if not projects:
            return 0
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            plans = [p for p in self._plans(projects) if p.remove]
        finally:
            QApplication.restoreOverrideCursor()
        if not plans:
            QMessageBox.information(self, "Úklid", "Podle zvolených pravidel není co uklidit.")
            return 0
        count = sum(len(p.remove) for p in plans)
        freed = sum(p.freed for p in plans)
        if confirm:
            box = QMessageBox(QMessageBox.Icon.Warning, "Úklid",
                              f"Trvale smazat {count} souborů a uvolnit {fmt_bytes(freed)} "
                              f"v {len(plans)} projektech?\n\nSmazané zálohy ani koš nepůjde vrátit.",
                              QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, self)
            box.setDetailedText("\n".join(f"{p.project}/{rel} – {reason}"
                                          for p in plans for rel, _size, reason in p.remove))
            if box.exec() != QMessageBox.StandardButton.Yes:
                return 0
        removed = 0
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            for plan in plans:
                try:
                    removed += len(storage.run_cleanup(self.server_root / plan.project, plan, self.user, self.pc).remove)
                except Exception as e:  # noqa: BLE001 – např. zaneprázdněná databáze
                    QApplication.restoreOverrideCursor()
                    QMessageBox.warning(self, "Úklid", f"Projekt {plan.project} se nepodařilo uklidit:\n{e}")
                    QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
                self.infos[plan.project] = storage.project_storage(self.server_root / plan.project)
        finally:
            QApplication.restoreOverrideCursor()
        self._fill()
        return removed
