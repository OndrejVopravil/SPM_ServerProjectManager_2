from __future__ import annotations

import os
import unicodedata
from pathlib import Path

from PySide6.QtCore import QByteArray, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QColor, QKeySequence, QPalette, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QInputDialog,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSplitter,
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
    ProjectManager,
    ProjectStatus,
    RefreshResult,
    UploadPlan,
    UploadResult,
    validate_project_name,
)
from .file_select_dialog import FileSelectDialog
from .history_dialog import HistoryDialog
from .project_panel import ProjectPanel
from .settings_dialog import SettingsDialog
from .ui_state import restore_geometry, restore_header, save_geometry, save_header
from .upload_dialog import UploadDialog
from .worker import run_task

COLOR_MINE = QColor("#2e8b57")      # aktivováno mnou
COLOR_OTHER = QColor("#2f6fb5")     # pracují na něm jiní (lze se přidat)
COLOR_INVALID = QColor("#e67e22")   # nepovolené znaky v názvech – nelze aktivovat

COLUMNS = ["Projekt", "Stav", "Aktivováno", "Od", "Místní kopie"]
HEADER_KEY = "main.v2"  # změna sloupců -> neobnovovat šířky uložené pro starší rozvržení


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


def format_refresh(res: RefreshResult) -> str:
    lines = [f"Staženo: {len(res.downloaded)}   Přesunuto: {len(res.moved)}   Odstraněno: {len(res.removed)}"]
    for title, items in (("Staženo", res.downloaded), ("Přesunuto", res.moved), ("Odstraněno", res.removed)):
        if items:
            lines.append(f"\n{title}:\n  " + "\n  ".join(items[:15]) + ("\n  …" if len(items) > 15 else ""))
    if res.conflicts:
        lines.append("\nNepřevzato kvůli místním změnám:\n  " + "\n  ".join(res.conflicts[:15]))
    return "\n".join(lines)


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings):
        super().__init__()
        self.settings = settings
        self.pm = ProjectManager(settings)
        self.projects: list[ProjectStatus] = []
        self.name_issues: dict[str, list[str]] = {}  # projekt -> názvy s nepovolenými znaky
        self._name_check: NameCheckThread | None = None
        self._name_check_again = False
        self._rendering = False
        self._closing = False
        self.setWindowTitle(f"SPM – Server Project Manager {__version__}")
        self.resize(1000, 560)
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
        self._columns_sized = restore_header(settings, HEADER_KEY, self.table.horizontalHeader())
        self.table.itemSelectionChanged.connect(self._on_selection)
        # dvojklik otevírá jen složku projektu, ne celé místní úložiště
        self.table.doubleClicked.connect(lambda _: self.open_local(root_fallback=False))

        tb = QToolBar("Akce")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.addToolBar(tb)
        self.act_refresh = self._action(tb, "Obnovit", self.refresh, QKeySequence(Qt.Key.Key_F5),
                                        tip="Obnoví seznam projektů (F5)")
        tb.addSeparator()
        self.act_activate = self._action(tb, "Aktivovat…", self.activate,
                                         tip="Stáhne projekt a zamkne vybrané soubory k úpravě (F3)")
        self.act_submit = self._action(tb, "Odevzdat…", self.submit,
                                       tip="Nahraje vybrané změny na server a uvolní je – projekt zůstane aktivovaný")
        self.act_update = self._action(tb, "Aktualizovat", self.update_from_server,
                                       tip="Stáhne ze serveru soubory, které odevzdali ostatní")
        self.act_take = self._action(tb, "Převzít…", self.take,
                                     tip="Zamkne další volné soubory k úpravě")
        self.act_deactivate = self._action(tb, "Deaktivovat…", self.deactivate,
                                           tip="Odevzdá vše, uvolní všechny zámky a ukončí aktivaci (F3)")
        tb.addSeparator()
        self.act_publish = self._action(tb, "Publikovat…", self.publish)
        self.act_open = self._action(tb, "Otevřít místní složku", self.open_local)
        self.act_history = self._action(tb, "Historie", self.history)
        tb.addSeparator()
        self._action(tb, "Nastavení", self.open_settings)

        # F3 = aktivovat / deaktivovat podle stavu vybraného projektu
        toggle = QAction(self)
        toggle.setShortcut(QKeySequence(Qt.Key.Key_F3))
        toggle.triggered.connect(lambda: self.toggle_activation())
        self.addAction(toggle)

        # jen v kontextovém menu (pravé tlačítko)
        self.act_cancel = QAction("Zrušit aktivaci…", self)
        self.act_cancel.triggered.connect(lambda: self.cancel_activation())
        self.act_unlock = QAction("Odebrat zámky uživatele…", self)
        self.act_unlock.triggered.connect(lambda: self.force_unlock())
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
        # pod seznamem: stav vybraného aktivovaného projektu (kdo drží které soubory)
        self.panel = ProjectPanel(self.pm)
        self.panel.hide()
        self.panel.submit_requested.connect(self.submit)
        self.panel.take_requested.connect(self.take)
        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.splitter.addWidget(self.table)
        self.splitter.addWidget(self.panel)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 2)
        self.splitter.setChildrenCollapsible(False)
        state = settings.ui.get("main.splitter")
        if state:
            self.splitter.restoreState(QByteArray.fromBase64(state.encode("ascii")))
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(6, 6, 6, 0)
        layout.addWidget(self.search)
        layout.addWidget(self.splitter)
        self.setCentralWidget(central)
        focus = QAction(self)
        focus.setShortcut(QKeySequence.StandardKey.Find)
        focus.triggered.connect(lambda: (self.search.setFocus(), self.search.selectAll()))
        self.addAction(focus)
        clear = QShortcut(QKeySequence(Qt.Key.Key_Escape), self.search)
        clear.activated.connect(self.search.clear)

        self.timer = QTimer(self)
        self.timer.timeout.connect(lambda: self.refresh(silent=True, check_names=False))
        self._apply_refresh_timer()

        self._update_status_bar()
        QTimer.singleShot(0, self._startup)

    def _action(self, tb: QToolBar, text: str, slot, shortcut=None, tip: str = "") -> QAction:
        act = QAction(text, self)
        act.triggered.connect(lambda: slot())
        if shortcut:
            act.setShortcut(shortcut)
        if tip:
            act.setToolTip(tip)
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

    def _apply_refresh_timer(self) -> None:
        """Automatické obnovení podle nastavení (-1 = vypnuto)."""
        seconds = self.settings.auto_refresh_seconds
        if seconds > 0:
            self.timer.start(seconds * 1000)
        else:
            self.timer.stop()

    def refresh(self, silent: bool = False, check_names: bool = True) -> None:
        """Obnoví seznam projektů. Kontrola nepovolených znaků (prochází celé stromy projektů
        na serveru) běží jen při spuštění a při ručním obnovení, ne při automatickém."""
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
        if check_names:
            self._start_name_check()
        self.panel.show_project(self.selected())

    def _start_name_check(self) -> None:
        """Na pozadí zkontroluje názvy složek a souborů v projektech, které nemám aktivované."""
        if self._closing:
            return
        if self._name_check and self._name_check.isRunning():
            self._name_check_again = True
            return
        self._name_check_again = False
        projects = [p for p in self.projects if not p.mine]
        self._name_check = NameCheckThread(self.pm, projects)
        self._name_check.done.connect(self._on_name_check)
        self._name_check.start()

    def _on_name_check(self, issues: dict) -> None:
        if self._closing:
            return
        self.name_issues = issues
        current = self.selected().name if self.selected() else None
        self._render(current)
        if self._name_check_again:
            self._start_name_check()

    def issues_of(self, p: ProjectStatus | None) -> list[str]:
        return self.name_issues.get(p.name, []) if p and not p.mine else []

    def _render(self, current: str | None) -> None:
        # při překreslení celého seznamu jsou data čerstvá – výběr řádku nemá nic znovu načítat
        self._rendering = True
        try:
            self.table.setRowCount(len(self.projects))
            for r, p in enumerate(self.projects):
                self._render_row(r, p)
                if p.name == current:
                    self.table.selectRow(r)
        finally:
            self._rendering = False
        if not self._columns_sized and self.projects:
            self.table.resizeColumnsToContents()
            self.table.setColumnWidth(0, max(self.table.columnWidth(0), 250))
            self._columns_sized = True
        self._apply_filter()

    def _render_row(self, r: int, p: ProjectStatus) -> None:
        issues = self.issues_of(p)
        if issues:
            state = "Nepovolené znaky"
        elif p.local_only:
            state = "Jen místně"
        elif p.mine:
            state = "Aktivováno mnou"
        elif p.active:
            state = "Pracují jiní"
        else:
            state = "Volný"
        names = ["já" if not any(u is o for o in p.others) else u.user for u in p.users]
        since = min((u.since for u in p.users), default="").replace("T", " ")
        values = [p.name, state, ", ".join(names), since, "ano" if p.local_exists else ""]
        for c, v in enumerate(values):
            item = QTableWidgetItem(v)
            if issues:
                item.setForeground(COLOR_INVALID)
                item.setToolTip("Projekt obsahuje názvy s nepovolenými znaky – nelze ho "
                                + ("publikovat" if p.local_only else "aktivovat")
                                + ", dokud je neopravíte:\n\n" + format_invalid(issues))
            elif p.local_only:
                item.setForeground(self.palette().color(QPalette.ColorRole.PlaceholderText))
                item.setToolTip("Složka existuje jen v místním úložišti – lze ji publikovat na server.")
            elif p.active:
                item.setForeground(COLOR_MINE if p.mine else COLOR_OTHER)
                item.setToolTip("Aktivováno:\n" + "\n".join(
                    f"{u.label} od {u.since.replace('T', ' ')}" for u in p.users))
            self.table.setItem(r, c, item)

    def _refresh_selected_row(self) -> ProjectStatus | None:
        """Po klepnutí na projekt načte jeho aktuální stav (kdo ho má aktivovaný) – levné,
        takže řádek i panel odpovídají skutečnosti i bez automatického obnovení."""
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        r = rows[0].row()
        try:
            status = self.pm.project_status(self.projects[r].name)
        except OSError:
            status = None
        if status is not None:
            self.projects[r] = status
            self._render_row(r, status)
        return self.projects[r]

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
        p = self._refresh_selected_row() if not self._rendering else self.selected()
        if p:
            self.settings.ui["last_project"] = p.name
        self._update_actions()
        self.panel.show_project(p)

    def closeEvent(self, event) -> None:
        self.timer.stop()
        self._closing = True  # výsledky vláken na pozadí už nezobrazovat
        if self._name_check:
            self._name_check.wait(5000)
        self.panel.stop()
        self.settings.ui["main.splitter"] = bytes(self.splitter.saveState().toBase64()).decode("ascii")
        save_geometry(self.settings, "main", self)
        save_header(self.settings, HEADER_KEY, self.table.horizontalHeader())
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
        mine = bool(p and p.mine)
        self.act_activate.setEnabled(bool(p and not p.mine and not p.local_only and not issues))
        for act in (self.act_submit, self.act_update, self.act_take, self.act_deactivate, self.act_cancel):
            act.setEnabled(mine)
        self.act_publish.setEnabled(bool(p and p.local_only and not issues))
        self.act_invalid.setEnabled(issues)
        self.act_open.setEnabled(bool(self.settings.local_root))
        self.act_open.setToolTip(
            "Otevře místní složku vybraného projektu" if p and p.local_exists
            else "Otevře místní složku pro projekty")
        self.act_history.setEnabled(bool(p and not p.local_only))
        self.act_rename.setEnabled(bool(p and not p.active and not p.local_only))
        self.act_unlock.setEnabled(bool(p and p.others))

    def _context_menu(self, pos) -> None:
        row = self.table.rowAt(pos.y())
        if row < 0:
            return
        self.table.selectRow(row)
        menu = QMenu(self)
        actions = [self.act_activate, self.act_submit, self.act_update, self.act_take,
                   self.act_deactivate, self.act_publish, None, self.act_open, self.act_history,
                   None, self.act_rename, None, self.act_cancel, self.act_unlock]
        if self.act_invalid.isEnabled():
            actions[:0] = [self.act_invalid, None]
        for act in actions:
            if act is None:
                menu.addSeparator()
            else:
                menu.addAction(act)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    # --- akce ----------------------------------------------------------------

    def _entries(self, p: ProjectStatus):
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            return self.pm.file_entries(p.name)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Chyba", str(e))
            return None
        finally:
            QApplication.restoreOverrideCursor()

    def activate(self) -> None:
        p = self.selected()
        if not p:
            return
        entries = self._entries(p)
        if entries is None:
            return
        others = ", ".join(u.label for u in p.others)
        intro = ("Vyberte soubory, které budete upravovat – zamknou se pro vás. Můžete zaškrtnout "
                 "celou složku a pak odškrtnout podsložku. Ostatní soubory se stáhnou jen pro čtení. "
                 "Nové soubory můžete vytvářet kdekoliv.")
        if others:
            intro += f"\n\nNa projektu právě pracuje: {others}. Jejich soubory vybrat nelze."
        dlg = FileSelectDialog(f"Aktivovat projekt {p.name}", intro, entries, "Aktivovat", self,
                               root=self.pm.server_dir(p.name))
        accepted = self._exec_dialog(dlg, "select_files")
        paths = dlg.selected_paths()
        dlg.deleteLater()
        if not accepted:
            return

        def done(moved: Path | None) -> None:
            msg = (f"Projekt {p.name} byl aktivován a zkopírován do:\n{self.pm.local_dir(p.name)}\n\n"
                   f"Zamčeno k úpravě: {len(paths)} souborů, ostatní jsou jen pro čtení.")
            if moved:
                msg += f"\n\nPůvodní místní složka byla přesunuta do:\n{moved}"
            self.refresh()
            QMessageBox.information(self, "Aktivace", msg)

        run_task(self, f"Aktivace projektu {p.name}", self.pm.activate, p.name, paths,
                 on_done=done, on_error=lambda _: self.refresh())

    def take(self, preselect: str | None = None) -> None:
        """preselect: soubor / složka ('/' na konci) předem zaškrtnutá (z panelu stavu projektu)."""
        p = self.selected()
        if not p or not p.mine:
            return
        entries = self._entries(p)
        if entries is None:
            return
        dlg = FileSelectDialog(
            f"Převzít soubory k úpravě – {p.name}",
            "Vyberte volné soubory, které chcete upravovat. Zamknou se pro vás a stáhne se jejich "
            "aktuální verze ze serveru. Soubory, které držíte vy („já“) nebo jiní, vybrat nelze.",
            entries, "Převzít", self, allow_empty=False, root=self.pm.local_dir(p.name),
            preselect=preselect)
        accepted = self._exec_dialog(dlg, "select_files")
        paths = dlg.selected_paths()
        dlg.deleteLater()
        if not accepted:
            return

        def done(res: RefreshResult) -> None:
            self.refresh()
            msg = f"Převzato k úpravě: {len(paths)} souborů."
            if res.conflicts:
                msg += "\n\n" + "\n".join(res.conflicts)
            QMessageBox.information(self, "Převzít", msg)

        run_task(self, f"Převzetí souborů – {p.name}", self.pm.take, p.name, paths,
                 on_done=done, on_error=lambda _: self.refresh())

    def update_from_server(self) -> None:
        p = self.selected()
        if not p or not p.mine:
            return

        def done(res: RefreshResult) -> None:
            self.refresh()
            if res.total == 0 and not res.conflicts:
                QMessageBox.information(self, "Aktualizovat", "Místní kopie je aktuální.")
            else:
                QMessageBox.information(self, "Aktualizovat", format_refresh(res))

        run_task(self, f"Aktualizace ze serveru – {p.name}", self.pm.refresh, p.name,
                 on_done=done, on_error=lambda _: self.refresh())

    def toggle_activation(self) -> None:
        """F3: můj aktivovaný projekt deaktivuje, jinak (lze-li) aktivuje."""
        if self.act_deactivate.isEnabled():
            self.deactivate()
        elif self.act_activate.isEnabled():
            self.activate()

    def submit(self, preselect: str | None = None) -> None:
        """preselect: soubor / složka ('/' na konci) předem zaškrtnutá (z panelu stavu projektu)."""
        self._start_upload(end_session=False, preselect=preselect)

    def deactivate(self) -> None:
        self._start_upload(end_session=True)

    def _start_upload(self, end_session: bool, preselect: str | None = None) -> None:
        p = self.selected()
        if not p or not p.mine:
            return
        run_task(self, "Porovnávání souborů", self.pm.plan_upload, p.name,
                 on_done=lambda plan: self._confirm_upload(plan, end_session, preselect))

    def _confirm_upload(self, plan: UploadPlan, end_session: bool, preselect: str | None = None) -> None:
        dlg = UploadDialog(plan, end_session, self, local=self.pm.local_dir(plan.name), preselect=preselect)
        accepted = self._exec_dialog(dlg, "upload")
        assignments, comment, selected = dlg.assignments(), dlg.comment_text(), dlg.selected_keys()
        dlg.deleteLater()
        if not accepted:
            return
        title = "Deaktivace" if end_session else "Odevzdání"

        def done(res: UploadResult) -> None:
            self.refresh()
            msg = ((f"Projekt {plan.name} byl deaktivován." if end_session else "Změny byly odevzdány.")
                   + f"\n\nUpravené: {res.modified}\nNové: {res.added}\n"
                   f"Přejmenované: {res.renamed}\nSmazané: {res.deleted}\nUvolněno: {res.released}")
            if end_session and self.settings.delete_local_after_deactivate and not res.local_deleted:
                msg += "\n\nMístní kopii se nepodařilo celou smazat (některý soubor je otevřen)."
            QMessageBox.information(self, title, msg)

        run_task(self, f"{title} – nahrávání projektu {plan.name}", self.pm.upload, plan,
                 assignments, comment, selected, end_session, on_done=done,
                 on_error=lambda _: self.refresh())

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
        if not p or not p.mine:
            return
        answer = QMessageBox.question(
            self, "Zrušit aktivaci",
            f"Zrušit aktivaci projektu {p.name}?\n\nŽádné změny se nenahrají na server a všechny "
            "vaše zámky se uvolní. Místní kopie zůstane zachována, ale neodevzdané změny z ní už "
            "nepůjde nahrát.",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._safe(lambda: self.pm.cancel_activation(p.name))

    def force_unlock(self) -> None:
        p = self.selected()
        if not p or not p.others:
            return
        user = p.others[0]
        if len(p.others) > 1:
            labels = [u.label for u in p.others]
            label, ok = QInputDialog.getItem(self, "Odebrat zámky", "Uživatel:", labels, 0, False)
            if not ok:
                return
            user = p.others[labels.index(label)]
        answer = QMessageBox.warning(
            self, "Odebrat zámky",
            f"Projekt {p.name} má aktivovaný uživatel {user.label}.\n\n"
            "Po odebrání zámků nebude moci své neodevzdané změny nahrát na server. "
            "Použijte pouze, pokud uživatel není dostupný.\n\nOpravdu odebrat jeho zámky?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._safe(lambda: self.pm.force_unlock(p.name, user.user, user.pc))

    def _safe(self, fn) -> None:
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Chyba", str(e))
        self.refresh()

    def open_settings(self) -> None:
        if SettingsDialog(self.settings, self).exec():
            self._update_status_bar()
            self._apply_refresh_timer()
            self.refresh()
