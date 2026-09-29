from __future__ import annotations

import os
import unicodedata
from pathlib import Path

from PySide6.QtCore import QThread, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QColor, QKeySequence, QPalette, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QInputDialog,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..config import Settings
from ..db import ProjectDB
from ..names import format_invalid
from ..sync import (
    DeactivationPlan,
    DeactivationResult,
    ProjectManager,
    ProjectStatus,
    validate_project_name,
)
from .deactivate_dialog import DeactivateDialog
from .history_dialog import HistoryDialog
from .settings_dialog import SettingsDialog
from .ui_state import restore_geometry, restore_header, save_geometry, save_header
from .worker import run_task

REFRESH_MS = 30_000
COLOR_MINE = QColor("#2e8b57")      # aktivováno mnou
COLOR_OTHER = QColor("#c0392b")     # aktivováno jiným uživatelem
COLOR_INVALID = QColor("#e67e22")   # nepovolené znaky v názvech – nelze aktivovat

COLUMNS = ["Projekt", "Stav", "Aktivoval", "Počítač", "Od", "Místní kopie"]


class NameCheckThread(QThread):
    """Kontrola nepovolených znaků v názvech – prochází složky na serveru, proto mimo GUI vlákno."""

    done = Signal(dict)

    def __init__(self, pm: ProjectManager, projects: list[ProjectStatus]):
        super().__init__()
        self.pm, self.projects = pm, projects

    def run(self) -> None:
        issues = {}
        for p in self.projects:
            found = self.pm.invalid_names(p)
            if found:
                issues[p.name] = found
        self.done.emit(issues)


def normalize(text: str) -> str:
    """Pro vyhledávání: malá písmena a bez diakritiky („Ústí“ -> „usti“)."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings):
        super().__init__()
        self.settings = settings
        self.pm = ProjectManager(settings)
        self.projects: list[ProjectStatus] = []
        self.name_issues: dict[str, list[str]] = {}  # projekt -> názvy s nepovolenými znaky
        self._name_check: NameCheckThread | None = None
        self._name_check_again = False
        self.setWindowTitle(f"SPM – Server Project Manager {__version__}")
        self.resize(950, 560)
        restore_geometry(settings, "main", self)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.setWordWrap(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        # šířky sloupců: uložené od minula, jinak se jednou nastaví podle obsahu
        self._columns_sized = restore_header(settings, "main", self.table.horizontalHeader())
        self.table.itemSelectionChanged.connect(self._on_selection)
        # dvojklik otevírá jen složku projektu, ne celé místní úložiště
        self.table.doubleClicked.connect(lambda _: self.open_local(root_fallback=False))

        tb = QToolBar("Akce")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.addToolBar(tb)
        self.act_refresh = self._action(tb, "Obnovit", self.refresh, QKeySequence.StandardKey.Refresh)
        tb.addSeparator()
        self.act_activate = self._action(tb, "Aktivovat", self.activate)
        self.act_deactivate = self._action(tb, "Deaktivovat…", self.deactivate)
        self.act_publish = self._action(tb, "Publikovat…", self.publish)
        self.act_open = self._action(tb, "Otevřít místní složku", self.open_local)
        self.act_history = self._action(tb, "Historie", self.history)
        tb.addSeparator()
        self.act_cancel = self._action(tb, "Zrušit aktivaci", self.cancel_activation)
        self.act_unlock = self._action(tb, "Odebrat zámek…", self.force_unlock)
        tb.addSeparator()
        self._action(tb, "Nastavení", self.open_settings)

        # jen v kontextovém menu (pravé tlačítko) a pod klávesou F2
        self.act_rename = QAction("Přejmenovat…", self)
        self.act_rename.setShortcut(QKeySequence(Qt.Key.Key_F2))
        self.act_rename.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        self.act_rename.triggered.connect(lambda: self.rename_project())
        self.table.addAction(self.act_rename)
        self.act_invalid = QAction("Zobrazit nepovolené názvy…", self)
        self.act_invalid.triggered.connect(lambda: self.show_invalid_names())
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Hledat projekt…  (Ctrl+F)")
        self.search.setClearButtonEnabled(True)
        self.search.setMaximumWidth(320)
        self.search.textChanged.connect(self._apply_filter)
        self.search.returnPressed.connect(self._select_first_visible)
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(6, 6, 6, 0)
        layout.addWidget(self.search)
        layout.addWidget(self.table)
        self.setCentralWidget(central)
        focus = QAction(self)
        focus.setShortcut(QKeySequence.StandardKey.Find)
        focus.triggered.connect(lambda: (self.search.setFocus(), self.search.selectAll()))
        self.addAction(focus)
        clear = QShortcut(QKeySequence(Qt.Key.Key_Escape), self.search)
        clear.activated.connect(self.search.clear)

        self.timer = QTimer(self)
        self.timer.timeout.connect(lambda: self.refresh(silent=True))
        self.timer.start(REFRESH_MS)

        self._update_status_bar()
        QTimer.singleShot(0, self._startup)

    def _action(self, tb: QToolBar, text: str, slot, shortcut=None) -> QAction:
        act = QAction(text, self)
        act.triggered.connect(lambda: slot())
        if shortcut:
            act.setShortcut(shortcut)
        tb.addAction(act)
        return act

    def _startup(self) -> None:
        if not self.settings.is_complete():
            QMessageBox.information(self, "Vítejte", "Nejdříve nastavte síťovou a místní složku.")
            self.open_settings()
        else:
            self.refresh()

    def _update_status_bar(self) -> None:
        self.statusBar().showMessage(
            f"Uživatel: {self.pm.user} @ {self.pm.pc}    |    Server: {self.settings.server_root or '—'}"
            f"    |    Místní: {self.settings.local_root or '—'}"
        )

    # --- seznam projektů -----------------------------------------------------

    def selected(self) -> ProjectStatus | None:
        rows = self.table.selectionModel().selectedRows()
        return self.projects[rows[0].row()] if rows else None

    def refresh(self, silent: bool = False) -> None:
        if not self.settings.is_complete() or getattr(self, "_running_task", None):
            return
        current = self.selected().name if self.selected() else self.settings.ui.get("last_project")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.projects = self.pm.list_projects()
        except Exception as e:  # noqa: BLE001
            self.projects = []
            if not silent:
                QMessageBox.critical(self, "Chyba", str(e))
        finally:
            QApplication.restoreOverrideCursor()

        self._render(current)
        self._start_name_check()

    def _start_name_check(self) -> None:
        """Na pozadí zkontroluje názvy složek a souborů v neaktivovaných projektech."""
        if self._name_check and self._name_check.isRunning():
            self._name_check_again = True
            return
        self._name_check_again = False
        projects = [p for p in self.projects if not p.active]
        self._name_check = NameCheckThread(self.pm, projects)
        self._name_check.done.connect(self._on_name_check)
        self._name_check.start()

    def _on_name_check(self, issues: dict) -> None:
        self.name_issues = issues
        current = self.selected().name if self.selected() else None
        self._render(current)
        if self._name_check_again:
            self._start_name_check()

    def issues_of(self, p: ProjectStatus | None) -> list[str]:
        return self.name_issues.get(p.name, []) if p and not p.active else []

    def _render(self, current: str | None) -> None:
        color_local = self.palette().color(QPalette.ColorRole.PlaceholderText)
        self.table.setRowCount(len(self.projects))
        for r, p in enumerate(self.projects):
            issues = self.issues_of(p)
            if issues:
                state = "Nepovolené znaky"
            elif p.local_only:
                state = "Jen místně"
            elif p.mine:
                state = "Aktivováno mnou"
            elif p.active:
                state = "Aktivováno"
            else:
                state = "Volný"
            lock = p.lock
            values = [p.name, state, lock.user if lock else "", lock.pc if lock else "",
                      lock.since.replace("T", " ") if lock else "", "ano" if p.local_exists else ""]
            for c, v in enumerate(values):
                item = QTableWidgetItem(v)
                if issues:
                    item.setForeground(COLOR_INVALID)
                    item.setToolTip("Projekt obsahuje názvy s nepovolenými znaky – nelze ho "
                                    + ("publikovat" if p.local_only else "aktivovat")
                                    + ", dokud je neopravíte:\n\n" + format_invalid(issues))
                elif p.local_only:
                    item.setForeground(color_local)
                    item.setToolTip("Složka existuje jen v místním úložišti – lze ji publikovat na server.")
                elif p.active:
                    item.setForeground(COLOR_MINE if p.mine else COLOR_OTHER)
                self.table.setItem(r, c, item)
            if p.name == current:
                self.table.selectRow(r)
        if not self._columns_sized and self.projects:
            self.table.resizeColumnsToContents()
            self.table.setColumnWidth(0, max(self.table.columnWidth(0), 250))
            self._columns_sized = True
        self._apply_filter()

    def show_invalid_names(self) -> None:
        p = self.selected()
        issues = self.issues_of(p)
        if not issues:
            return
        where = self.pm.local_dir(p.name) if p.local_only else self.pm.server_dir(p.name)
        box = QMessageBox(QMessageBox.Icon.Warning, "Nepovolené znaky",
                          f"Projekt {p.name} obsahuje {len(issues)} názvů s nepovolenými znaky "
                          "(česká diakritika, mezera, ( ) & , . ; = ^).\n\n"
                          f"Přejmenujte je přímo ve složce:\n{where}\n\n"
                          + format_invalid(issues, limit=30), parent=self)
        box.setDetailedText("\n".join(issues))
        box.exec()

    def _apply_filter(self) -> None:
        """Skryje projekty, jejichž název neobsahuje hledaný text (bez ohledu na velikost
        písmen a diakritiku)."""
        needle = normalize(self.search.text().strip())
        for r, p in enumerate(self.projects):
            self.table.setRowHidden(r, bool(needle) and needle not in normalize(p.name))
        rows = self.table.selectionModel().selectedRows()
        if rows and self.table.isRowHidden(rows[0].row()):
            self.table.clearSelection()
        self._update_actions()

    def _select_first_visible(self) -> None:
        for r in range(self.table.rowCount()):
            if not self.table.isRowHidden(r):
                self.table.selectRow(r)
                self.table.setFocus()
                return

    def _on_selection(self) -> None:
        p = self.selected()
        if p:
            self.settings.ui["last_project"] = p.name
        self._update_actions()

    def closeEvent(self, event) -> None:
        self.timer.stop()
        if self._name_check:
            self._name_check.wait(5000)
        save_geometry(self.settings, "main", self)
        save_header(self.settings, "main", self.table.horizontalHeader())
        try:
            self.settings.save()
        except OSError:
            pass  # neuložený vzhled okna není důvod bránit zavření aplikace
        super().closeEvent(event)

    def _exec_dialog(self, dlg, key: str) -> bool:
        """Otevře dialog s velikostí a polohou z minula a po zavření je uloží."""
        restore_geometry(self.settings, key, dlg)
        accepted = bool(dlg.exec())
        save_geometry(self.settings, key, dlg)
        return accepted

    def _update_actions(self) -> None:
        p = self.selected()
        issues = bool(self.issues_of(p))
        self.act_activate.setEnabled(bool(p and not p.active and not p.local_only and not issues))
        self.act_deactivate.setEnabled(bool(p and p.mine))
        self.act_publish.setEnabled(bool(p and p.local_only and not issues))
        self.act_invalid.setEnabled(issues)
        self.act_cancel.setEnabled(bool(p and p.mine))
        self.act_open.setEnabled(bool(self.settings.local_root))
        self.act_open.setToolTip(
            "Otevře místní složku vybraného projektu" if p and p.local_exists
            else "Otevře místní složku pro projekty")
        self.act_history.setEnabled(bool(p and not p.local_only))
        self.act_rename.setEnabled(bool(p and not p.active and not p.local_only))
        self.act_unlock.setEnabled(bool(p and p.active and not p.mine))

    def _context_menu(self, pos) -> None:
        row = self.table.rowAt(pos.y())
        if row < 0:
            return
        self.table.selectRow(row)
        menu = QMenu(self)
        actions = [self.act_activate, self.act_deactivate, self.act_publish, None,
                   self.act_open, self.act_history, None, self.act_rename, None,
                   self.act_cancel, self.act_unlock]
        if self.act_invalid.isEnabled():
            actions[:0] = [self.act_invalid, None]
        for act in actions:
            if act is None:
                menu.addSeparator()
            else:
                menu.addAction(act)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    # --- akce ----------------------------------------------------------------

    def rename_project(self) -> None:
        p = self.selected()
        if not p or p.active or p.local_only:
            return
        new_name = p.name
        while True:
            new_name, ok = QInputDialog.getText(
                self, "Přejmenovat projekt",
                f"Nový název projektu „{p.name}“:\n"
                "(Přejmenuje se složka na síťovém úložišti, změna se zapíše do historie.)",
                QLineEdit.EchoMode.Normal, new_name,
            )
            new_name = new_name.strip()
            if not ok or new_name == p.name:
                return
            error = validate_project_name(new_name)
            if not error:
                break
            QMessageBox.warning(self, "Přejmenovat projekt", error)

        try:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                self.pm.rename_project(p.name, new_name)
            finally:
                QApplication.restoreOverrideCursor()
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Přejmenovat projekt", str(e))
            self.refresh()
            return
        self.settings.ui["last_project"] = new_name
        self.table.clearSelection()
        self.refresh()
        msg = f"Projekt „{p.name}“ byl přejmenován na „{new_name}“."
        if p.local_exists:
            msg += (f"\n\nV místním úložišti zůstala složka se starým názvem „{p.name}“. "
                    "Nyní se zobrazuje jako „Jen místně“ – pokud ji nepotřebujete, smažte ji.")
        QMessageBox.information(self, "Přejmenovat projekt", msg)

    def activate(self) -> None:
        p = self.selected()
        if not p:
            return

        def done(moved: Path | None) -> None:
            msg = f"Projekt {p.name} byl aktivován a zkopírován do:\n{self.pm.local_dir(p.name)}"
            if moved:
                msg += f"\n\nPůvodní místní složka byla přesunuta do:\n{moved}"
            self.refresh()
            QMessageBox.information(self, "Aktivace", msg)

        run_task(self, f"Aktivace projektu {p.name}", self.pm.activate, p.name,
                 on_done=done, on_error=lambda _: self.refresh())

    def deactivate(self) -> None:
        p = self.selected()
        if not p:
            return
        run_task(self, "Porovnávání souborů", self.pm.plan_deactivation, p.name,
                 on_done=self._confirm_deactivation)

    def _confirm_deactivation(self, plan: DeactivationPlan) -> None:
        dlg = DeactivateDialog(plan, self)
        accepted = self._exec_dialog(dlg, "deactivate")
        assignments, comment = dlg.assignments(), dlg.comment_text()
        dlg.deleteLater()
        if not accepted:
            return

        def done(res: DeactivationResult) -> None:
            self.refresh()
            msg = (f"Projekt {plan.name} byl deaktivován.\n\n"
                   f"Upravené: {res.modified}\nNové: {res.added}\n"
                   f"Přejmenované: {res.renamed}\nSmazané: {res.deleted}")
            if self.settings.delete_local_after_deactivate and not res.local_deleted:
                msg += "\n\nMístní kopii se nepodařilo celou smazat (některý soubor je otevřen)."
            QMessageBox.information(self, "Deaktivace", msg)

        run_task(self, f"Nahrávání projektu {plan.name}", self.pm.deactivate, plan,
                 assignments, comment, on_done=done,
                 on_error=lambda _: self.refresh())

    def publish(self) -> None:
        p = self.selected()
        if not p or not p.local_only:
            return
        comment = ""
        while True:
            comment, ok = QInputDialog.getMultiLineText(
                self, f"Publikovat projekt {p.name}",
                f"Projekt se přesune na server:\n{self.pm.server_dir(p.name)}\n\n"
                "Po úspěšném nahrání a ověření se místní složka smaže. "
                "Chcete-li na projektu dál pracovat, aktivujte ho.\n\n"
                "Komentář (povinný):",
                comment,
            )
            if not ok:
                return
            if comment.strip():
                break
            QMessageBox.warning(self, "Publikovat", "Bez komentáře nelze projekt publikovat.")

        def done(local_deleted: bool) -> None:
            self.refresh()
            msg = f"Projekt {p.name} byl publikován na server."
            if not local_deleted:
                msg += ("\n\nMístní složku se nepodařilo celou smazat (některý soubor je otevřen). "
                        "Data na serveru jsou kompletní, zbytek místní složky smažte ručně.")
            QMessageBox.information(self, "Publikovat", msg)

        run_task(self, f"Publikování projektu {p.name}", self.pm.publish, p.name, comment,
                 on_done=done, on_error=lambda _: self.refresh())

    def open_local(self, root_fallback: bool = True) -> None:
        """Otevře místní složku vybraného projektu. Pokud vybraný projekt místní složku
        nemá (nebo není vybrán žádný), otevře místní složku pro projekty."""
        p = self.selected()
        if p and p.local_exists:
            os.startfile(self.pm.local_dir(p.name))
        elif root_fallback and self.settings.local_root:
            try:
                self.pm.local_root.mkdir(parents=True, exist_ok=True)
                os.startfile(self.pm.local_root)
            except OSError as e:
                QMessageBox.critical(self, "Otevřít místní složku", str(e))

    def history(self) -> None:
        p = self.selected()
        if not p:
            return
        try:
            db = ProjectDB.for_project(self.pm.server_dir(p.name))
        except FileNotFoundError:
            QMessageBox.information(self, "Historie", "Projekt zatím nemá žádnou historii.")
            return
        with db:
            dlg = HistoryDialog(db, p.name, self)
            self._exec_dialog(dlg, "history")
            dlg.deleteLater()

    def cancel_activation(self) -> None:
        p = self.selected()
        if not p:
            return
        answer = QMessageBox.question(
            self, "Zrušit aktivaci",
            f"Zrušit aktivaci projektu {p.name}?\n\nŽádné změny se nenahrají na server. "
            "Místní kopie zůstane zachována, ale už ji nepůjde nahrát zpět.",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._safe(lambda: self.pm.cancel_activation(p.name))

    def force_unlock(self) -> None:
        p = self.selected()
        if not p or not p.lock:
            return
        answer = QMessageBox.warning(
            self, "Odebrat zámek",
            f"Projekt {p.name} má aktivovaný uživatel {p.lock.user} ({p.lock.pc}).\n\n"
            "Po odebrání zámku nebude moci své změny nahrát zpět na server. "
            "Použijte pouze, pokud uživatel není dostupný.\n\nOpravdu odebrat zámek?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._safe(lambda: self.pm.force_unlock(p.name))

    def _safe(self, fn) -> None:
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Chyba", str(e))
        self.refresh()

    def open_settings(self) -> None:
        if SettingsDialog(self.settings, self).exec():
            self._update_status_bar()
            self.refresh()
