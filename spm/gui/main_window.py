from __future__ import annotations

import os
import time
from pathlib import Path

from PySide6.QtCore import QByteArray, QEvent, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QColor, QFont, QGuiApplication, QKeySequence, QPalette, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSplitter,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..config import AppSettings, Settings, _same_dir
from ..db import ProjectDB
from ..names import format_invalid
from ..restore import RestoreCatalog, RestoreResult
from ..shared_config import ERROR as SHARED_ERROR, SharedConfig
from ..sync import (
    ActivationResult,
    ProjectManager,
    ProjectStatus,
    RefreshResult,
    UploadPlan,
    PublishResult,
    UploadResult,
    has_script_changes,
    selection_files,
    validate_project_name,
)
from ..scripting import store as script_store
from ..scripting.model import ADDED, DEACTIVATE, PUBLISH, SUBMIT, RunReport, ScriptFile, TabInfo
from ..scripting.runtime import ScriptEnv, ScriptSession, clear_stale_marks
from .comment_dialog import CommentDialog
from .script_ui import GuiScriptUI
from .scripts_dialog import ScriptsDialog
from .notes_window import NotesWindow, QuickNoteDialog
from .tab_bar import TAB_COLORS, ColorTabBar, is_dark
from .. import notes as notes_mod
from .file_select_dialog import FileSelectDialog
from .history_dialog import HistoryDialog
from .project_panel import ProjectPanel
from .publish_dialog import PublishDialog
from .restore_dialog import RestoreDialog
from .search import normalize
from .settings_dialog import SettingsDialog
from .ui_state import restore_geometry, restore_header, save_geometry, save_header
from .upload_dialog import UploadDialog
from .worker import run_task

COLOR_MINE = QColor("#2e8b57")      # aktivováno mnou
COLOR_MINE_DARK = QColor("#6fdc95")  # totéž v tmavém režimu (záložky)
COLOR_OTHER = QColor("#2f6fb5")     # pracují na něm jiní (lze se přidat)
COLOR_INVALID = QColor("#e67e22")   # nepovolené znaky v názvech – nelze aktivovat

COLUMNS = ["Projekt", "Komentář", "Stav", "Aktivováno", "Od", "Místní kopie"]
COL_NAME, COL_COMMENT, COL_STATE, COL_USERS, COL_SINCE, COL_LOCAL = range(len(COLUMNS))
HEADER_KEY = "main.v3"  # změna sloupců -> neobnovovat šířky uložené pro starší rozvržení
COMMENT_COLUMN_MAX = 300  # výchozí šířka sloupce Komentář nejvýše (zbytek je v tooltipu)
LOCAL_NOT_DELETED = "\n\nMístní kopii se nepodařilo celou smazat (některý soubor je otevřen)."
FOCUS_REFRESH_MIN_GAP = 5.0  # s – při rychlém přepínání oken (Alt+Tab) neobnovovat pokaždé
# stavový text v řádku záložek: při souběhu více činností se ukáže ta nejdůležitější
BUSY_ORDER = ("task", "refresh", "names")
COLOR_STATUS_ERROR = QColor("#c0392b")


class ProjectListThread(QThread):
    """Seznam projektů na pozadí (tiché obnovení – po návratu do okna, automatické)."""

    done = Signal(object, str)  # projekty (nebo None), chyba

    def __init__(self, pm: ProjectManager):
        super().__init__()
        self.pm = pm

    def run(self) -> None:
        try:
            try:
                self.pm.register_user()  # adresář uživatelů (výběr adresáta poznámek)
            except OSError:
                pass
            self.done.emit(self.pm.list_projects(), "")
        except Exception as e:  # noqa: BLE001 – tiché obnovení chybu nehlásí
            self.done.emit(None, str(e) or type(e).__name__)


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


def format_refresh(res: RefreshResult) -> str:
    lines = [f"Staženo: {len(res.downloaded)}   Přesunuto: {len(res.moved)}   Odstraněno: {len(res.removed)}"]
    for title, items in (("Staženo", res.downloaded), ("Přesunuto", res.moved), ("Odstraněno", res.removed)):
        if items:
            lines.append(f"\n{title}:\n  " + "\n  ".join(items[:15]) + ("\n  …" if len(items) > 15 else ""))
    if res.conflicts:
        lines.append("\nNepřevzato kvůli místním změnám:\n  " + "\n  ".join(res.conflicts[:15]))
    if res.external:
        lines.append("\n" + format_external(res.external))
    return "\n".join(lines)


def format_external(items: list[str]) -> str:
    """Upozornění na soubory změněné přímo na síti mimo aplikaci (zjištěno podle hashe)."""
    return ("POZOR – soubory na serveru byly změněny mimo aplikaci (jejich obsah neodpovídá "
            "poslednímu odevzdání):\n  " + "\n  ".join(items[:20])
            + ("\n  …" if len(items) > 20 else "")
            + "\n\nZměna je zapsaná v historii projektu jako „Změněn mimo aplikaci“.")


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings | AppSettings):
        super().__init__()
        # celé nastavení se záložkami; samotné Settings (testy) = jedna záložka
        self.app_settings = settings if isinstance(settings, AppSettings) else AppSettings.from_settings(settings)
        settings = self.settings = self.app_settings.view(self.app_settings.active)
        self.pm = ProjectManager(settings)
        # společná konfigurace na síti (spm_config.json) – každá záložka má svou
        self._shared: dict[str, SharedConfig] = {}
        self.shared = self._shared.setdefault(settings.workspace_id, SharedConfig())
        self.projects: list[ProjectStatus] = []
        self.name_issues: dict[str, list[str]] = {}  # projekt -> názvy s nepovolenými znaky
        self._name_check: NameCheckThread | None = None
        self._bg_refresh: ProjectListThread | None = None  # tiché obnovení seznamu na pozadí
        self._name_check_again = False
        self._rendering = False
        self._closing = False
        self._last_refresh = 0.0  # time.monotonic() posledního obnovení seznamu
        # návrat do aplikace z jiné (klepnutí, Alt+Tab, hlavní panel) -> obnovit seznam
        QGuiApplication.instance().applicationStateChanged.connect(self._on_app_state)
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
        # dvojklik: název = aktivovat / deaktivovat, komentář = upravit, jinak místní složka projektu
        self.table.doubleClicked.connect(self._on_double_click)

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
        self.act_notes = self._action(tb, "Poznámky", self.open_notes,
                                      tip="Předávací poznámky k projektu (plovoucí okno)")
        tb.addSeparator()
        self.act_scripts = self._action(tb, "Skripty", self.open_scripts,
                                        tip="Skripty spouštěné po odevzdání (zrcadlení, kopie, archivy…)")
        self._action(tb, "Nastavení", self.open_settings)

        # Popisky zkratek: v liště „Obnovit (F5)“, v kontextové nabídce zarovnaně vpravo.
        # F3 je jedna zkratka pro dvě akce (viz toggle_activation), proto se u aktivace
        # a deaktivace jen zobrazuje (text za tabulátorem), nenastavuje se jako shortcut.
        self.act_refresh.setIconText("Obnovit (F5)")
        self.act_activate.setText("Aktivovat…\tF3")
        self.act_activate.setIconText("Aktivovat (F3)")
        self.act_deactivate.setText("Deaktivovat…\tF3")
        self.act_deactivate.setIconText("Deaktivovat (F3)")

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
        self.act_comment = QAction("Upravit komentář…", self)
        self.act_comment.setToolTip("Komentář projektu vidí všichni; změny se zapisují do historie")
        self.act_comment.triggered.connect(lambda: self.edit_comment())
        self.act_invalid = QAction("Zobrazit nepovolené názvy…", self)
        self.act_invalid.triggered.connect(lambda: self.show_invalid_names())
        self.act_restore = QAction("Obnovit ze zálohy…", self)
        self.act_restore.setToolTip("Obnoví projekt k dřívějšímu stavu do nové místní složky")
        self.act_restore.triggered.connect(lambda: self.restore_project())
        self.act_rerun_scripts = QAction("Spustit skripty znovu…", self)
        self.act_rerun_scripts.setToolTip("Skript po odevzdání selhal – spustí skripty znovu se "
                                          "soubory tohoto odevzdání")
        self.act_rerun_scripts.triggered.connect(lambda: self.rerun_scripts())
        self.act_discard_scripts = QAction("Zahodit nedokončený skript…", self)
        self.act_discard_scripts.triggered.connect(lambda: self.discard_scripts())
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Hledat projekt…  (Ctrl+F)")
        self.search.setClearButtonEnabled(True)
        self.search.setMaximumWidth(320)
        self.search.textChanged.connect(self._apply_filter)
        self.search.returnPressed.connect(self._select_first_visible)
        # pod seznamem: stav vybraného aktivovaného projektu (kdo drží které soubory)
        self.script_ui = GuiScriptUI(self)  # dialogy skriptů (ASK, INPUT, CHOOSE, FORM)
        self.panel = ProjectPanel(self.pm)
        self.panel.hide()
        self.panel.submit_requested.connect(self.submit)
        self.panel.publish_requested.connect(self.publish)
        self.panel.take_requested.connect(self.take)
        self.panel.activate_requested.connect(self.activate)
        self.panel.note_requested.connect(self.add_note)
        self._notes_windows: dict[tuple[str, str], NotesWindow] = {}  # (záložka, projekt) -> okno
        self.panel.can_activate = lambda: self.act_activate.isEnabled()
        self.panel.mirror_source_label = self.mirror_source_label
        self.panel.mirror_target_label = self.mirror_target_label
        self.panel.tree.default_expanded = settings.tree_expanded  # výchozí stav složek ve stromu
        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.splitter.addWidget(self.table)
        self.splitter.addWidget(self.panel)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 2)
        self.splitter.setChildrenCollapsible(False)
        state = settings.ui.get("main.splitter")
        if state:
            self.splitter.restoreState(QByteArray.fromBase64(state.encode("ascii")))
        # záložky = pracovní prostory (každá má vlastní síťovou a místní složku)
        self.tabs = ColorTabBar()
        self.tabs.setMovable(True)  # pořadí záložek přetažením myší
        self.tabs.tabMoved.connect(lambda *_: self._save_tab_order())
        self.tabs.setExpanding(False)
        self.tabs.setDocumentMode(True)
        # bez posouvacích šipek – jinak si lišta i s jedinou záložkou rezervuje místo navíc
        self.tabs.setUsesScrollButtons(False)
        tab_font = QFont(self.tabs.font())
        if tab_font.pointSizeF() > 0:  # záložky o něco větší než ostatní text
            tab_font.setPointSizeF(tab_font.pointSizeF() * 1.2)
        self.tabs.setFont(tab_font)
        self.tabs.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tabs.customContextMenuRequested.connect(self._tab_menu)
        self.btn_add_tab = QToolButton()
        self.btn_add_tab.setText("+")
        self.btn_add_tab.setToolTip("Přidat záložku s další síťovou složkou projektů")
        self.btn_add_tab.setAutoRaise(True)
        self.btn_add_tab.setFont(tab_font)
        self.btn_add_tab.clicked.connect(lambda: self.add_workspace())
        self._fill_tabs()
        self.tabs.currentChanged.connect(self._on_tab_changed)
        tabs_row = QHBoxLayout()
        tabs_row.setSpacing(2)
        tabs_row.addWidget(self.tabs)
        tabs_row.addWidget(self.btn_add_tab)
        tabs_row.addStretch()
        # stav aplikace vpravo v řádku záložek (aktualizuji…, přenáším data…, aktualizováno)
        self.status_label = QLabel()
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.status_label.setContentsMargins(8, 0, 4, 0)
        self._busy: dict[str, str] = {}       # druh činnosti -> text (viz BUSY_ORDER)
        self._idle_status = ("", "", False)  # text, tooltip, chyba
        tabs_row.addWidget(self.status_label)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(6, 6, 6, 0)
        layout.addLayout(tabs_row)
        layout.addWidget(self.search)
        layout.addWidget(self.splitter)
        self.setCentralWidget(central)
        focus = QAction(self)
        focus.setShortcut(QKeySequence.StandardKey.Find)
        focus.triggered.connect(lambda: (self.search.setFocus(), self.search.selectAll()))
        self.addAction(focus)
        clear = QShortcut(QKeySequence(Qt.Key.Key_Escape), self.search)
        # jen v poli hledání projektů – Esc v panelu stromu ruší hledání souborů
        clear.setContext(Qt.ShortcutContext.WidgetShortcut)
        clear.activated.connect(self.search.clear)

        self.timer = QTimer(self)
        self.timer.timeout.connect(lambda: self.refresh(silent=True, check_names=False, background=True))
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
        if self._closing:  # okno zavřené dřív, než se stihlo spustit
            return
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
        self._update_config_label()

    # --- stavový text v řádku záložek ------------------------------------------

    def set_busy(self, kind: str, text: str | None) -> None:
        """Probíhající činnost (text) nebo její konec (None)."""
        if text:
            self._busy[kind] = text
        else:
            self._busy.pop(kind, None)
        self._show_status()

    def set_idle_status(self, text: str, tip: str = "", error: bool = False) -> None:
        """Stav, když nic neprobíhá (aktualizováno / síť nedostupná)."""
        self._idle_status = (text, tip, error)
        self._show_status()

    def task_status(self, text: str | None, done: int, total: int) -> None:
        """Průběh akce spuštěné přes run_task."""
        if text and total > 1:
            text = f"{text} {done}/{total}"
        self.set_busy("task", text)

    def _show_status(self) -> None:
        busy = next((self._busy[k] for k in BUSY_ORDER if k in self._busy), None)
        if busy:
            text, tip, error = busy, "", False
        else:
            text, tip, error = self._idle_status
        self.status_label.setText(text)
        self.status_label.setToolTip(tip)
        palette = self.status_label.palette()
        role = self.status_label.foregroundRole()
        if error:
            palette.setColor(role, COLOR_STATUS_ERROR)
        elif busy:
            palette.setColor(role, self.palette().color(QPalette.ColorRole.WindowText))
        else:
            palette.setColor(role, self.palette().color(QPalette.ColorRole.PlaceholderText))
        self.status_label.setPalette(palette)

    def _refreshed(self, error: str = "") -> None:
        """Výsledek načtení seznamu projektů do stavového textu."""
        if error:
            self.set_idle_status("Síť nedostupná", error, error=True)
        else:
            self.set_idle_status(f"Aktualizováno {time.strftime('%H:%M')}",
                                 f"Seznam projektů načten {time.strftime('%d.%m.%Y %H:%M:%S')}")

    def _update_config_label(self) -> None:
        if not hasattr(self, "config_label"):
            self.config_label = QLabel()
            self.statusBar().addPermanentWidget(self.config_label)
        state = self.shared.state
        self.config_label.setText(f"Nastavení: {state.describe()}")
        tip = str(state.path) if state.active else "Společná konfigurace na síti neexistuje."
        if state.error:
            tip += f"\n{state.error}"
        self.config_label.setToolTip(tip)
        self.config_label.setStyleSheet("color: #c0392b;" if state.status == SHARED_ERROR else "")

    def _sync_shared(self) -> bool:
        """Načte společnou konfiguraci ze sítě (jen při změně souboru) a převezme ji do nastavení.
        Kvůli skriptům (identita a název síťové složky) i u ostatních záložek.
        Vrací, zda se změnilo nastavení aktuální záložky."""
        changed = False
        for ws in self.app_settings.workspaces:
            view = self.app_settings.view(ws["id"])
            if not view.server_root:
                continue
            shared = self._shared.setdefault(ws["id"], SharedConfig())
            try:
                ws_changed = shared.sync(view)
            except Exception:  # noqa: BLE001 – nedostupná síť apod. nesmí zastavit obnovení
                ws_changed = False
            if view is self.settings:
                changed = ws_changed
        self._update_config_label()
        return changed

    # --- záložky (pracovní prostory) --------------------------------------------

    def _ui_key(self, key: str) -> str:
        """Klíč stavu rozhraní, který si pamatuje každá záložka zvlášť (např. poslední projekt)."""
        return f"{key}.{self.settings.workspace_id}"

    def _activated_count(self, ws_id: str) -> int:
        """Počet mých aktivovaných projektů v záložce (z jejího posledního obnovení)."""
        return int(self.settings.ui.get(f"activated.{ws_id}") or 0)

    def _tab_label(self, ws: dict) -> str:
        """Název záložky; s počtem mých aktivovaných projektů."""
        count = self._activated_count(ws["id"])
        return f"{ws['name']} ({count})" if count else ws["name"]

    def _style_tab(self, i: int, ws: dict) -> None:
        """Text záložky (zeleně, pokud v ní mám aktivovaný alespoň jeden projekt) a její barva."""
        self.tabs.setTabText(i, self._tab_label(ws))
        mine = COLOR_MINE_DARK if is_dark(self) else COLOR_MINE
        self.tabs.setTabTextColor(i, mine if self._activated_count(ws["id"]) else QColor())
        self.tabs.set_tab_color(i, self.app_settings.view(ws["id"]).tab_color)

    def _save_tab_order(self) -> None:
        """Záložka přetažena myší: pořadí záložek se uloží (bez přestavění lišty – drag běží)."""
        order = [self.tabs.tabData(i) for i in range(self.tabs.count())]
        by_id = {w["id"]: w for w in self.app_settings.workspaces}
        if set(order) == set(by_id):
            self.app_settings.workspaces = [by_id[i] for i in order]
            self._save_settings()

    def set_tab_color(self, ws_id: str, key: str) -> None:
        view = self.app_settings.view(ws_id)
        view.tab_color = key
        self._save_settings_of(view)
        self._fill_tabs()

    def _save_settings_of(self, view: Settings) -> None:
        try:
            view.save()
        except OSError as e:
            QMessageBox.warning(self, "Nastavení", f"Nastavení nelze uložit:\n{e}")

    def changeEvent(self, event) -> None:
        # světlý / tmavý režim: jiná varianta barev záložek a zelené barvy textu
        if event.type() == QEvent.Type.PaletteChange and hasattr(self, "tabs"):
            self._fill_tabs()
        super().changeEvent(event)

    def _fill_tabs(self) -> None:
        self.tabs.blockSignals(True)
        try:
            while self.tabs.count():
                self.tabs.removeTab(0)
            for ws in self.app_settings.workspaces:
                i = self.tabs.addTab("")
                self._style_tab(i, ws)
                self.tabs.setTabData(i, ws["id"])
                self.tabs.setTabToolTip(i, f"Síť: {ws.get('server_root') or '—'}\n"
                                           f"Místní: {ws.get('local_root') or '—'}")
                if ws["id"] == self.settings.workspace_id:
                    self.tabs.setCurrentIndex(i)
        finally:
            self.tabs.blockSignals(False)

    def _update_tab_label(self) -> None:
        """Po obnovení seznamu: počet mých aktivovaných projektů na aktuální záložce."""
        mine = sorted(p.name for p in self.projects if p.mine)
        self.settings.ui[self._ui_key("activated")] = str(len(mine))
        self.settings.ui[self._ui_key("activated_names")] = "\n".join(mine)  # pro upozornění při ukončení
        i = self.tabs.currentIndex()
        if i >= 0:
            self._style_tab(i, self.app_settings.workspace(self.settings.workspace_id))

    def _on_tab_changed(self, index: int) -> None:
        ws_id = self.tabs.tabData(index)
        if ws_id and ws_id != self.settings.workspace_id:
            self.switch_workspace(ws_id)

    def switch_workspace(self, ws_id: str) -> None:
        """Přepne na jinou záložku: jiné nastavení, jiný seznam projektů i strom."""
        if getattr(self, "_running_task", None):
            QMessageBox.information(self, "Záložky", "Počkejte na dokončení probíhající akce.")
            self._fill_tabs()
            return
        self.app_settings.active = ws_id
        self.settings = self.app_settings.view(ws_id)
        self.pm = ProjectManager(self.settings)
        self.panel.pm = self.pm
        self.shared = self._shared.setdefault(ws_id, SharedConfig())
        self.projects, self.name_issues = [], {}
        self._name_check_again = False
        self._render(None)
        self.panel.show_project(None)
        self._fill_tabs()
        self._update_status_bar()
        if self.settings.is_complete():
            self.refresh()
        else:
            self._update_config_label()

    def add_workspace(self) -> None:
        name, ok = QInputDialog.getText(self, "Přidat záložku",
                                        "Název záložky (např. podle síťového disku):")
        name = name.strip()
        if not ok or not name:
            return
        previous = self.settings.workspace_id
        previous_local = self.settings.local_root
        ws_id = self.app_settings.add_workspace(name)
        if previous_local:  # návrh místní složky vedle té dosavadní: C:\Projekty -> C:\<název>
            self.app_settings.view(ws_id).local_root = str(Path(previous_local).parent / name)
        self._fill_tabs()
        self.switch_workspace(ws_id)
        if not self.open_settings():  # bez nastavených složek záložku nezakládat
            self.app_settings.remove_workspace(ws_id)
            self.switch_workspace(previous)
            return
        self._save_settings()

    def _tab_menu(self, pos) -> None:
        index = self.tabs.tabAt(pos)
        if index < 0:
            return
        ws_id = self.tabs.tabData(index)
        menu = QMenu(self)
        act_rename = menu.addAction("Přejmenovat záložku…")
        act_settings = menu.addAction("Nastavení složek…")
        colors = menu.addMenu("Barva záložky")
        current_color = self.app_settings.view(ws_id).tab_color
        for key, (label, _light, _dark) in TAB_COLORS.items():
            act = colors.addAction(label)
            act.setCheckable(True)
            act.setChecked(key == current_color)
            act.triggered.connect(lambda _=False, k=key: self.set_tab_color(ws_id, k))
        menu.addSeparator()
        act_remove = menu.addAction("Odebrat záložku…")
        act_remove.setEnabled(len(self.app_settings.workspaces) > 1)
        menu.addSeparator()
        menu.addAction("Přidat záložku…").triggered.connect(lambda: self.add_workspace())
        chosen = menu.exec(self.tabs.mapToGlobal(pos))
        if chosen is act_rename:
            self.rename_workspace(ws_id)
        elif chosen is act_settings:
            if ws_id != self.settings.workspace_id:
                self.switch_workspace(ws_id)
            self.open_settings()
        elif chosen is act_remove:
            self.remove_workspace(ws_id)

    def rename_workspace(self, ws_id: str) -> None:
        ws = self.app_settings.workspace(ws_id)
        name, ok = QInputDialog.getText(self, "Přejmenovat záložku", "Nový název záložky:",
                                        QLineEdit.EchoMode.Normal, ws["name"])
        name = name.strip()
        if ok and name and name != ws["name"]:
            self.app_settings.rename_workspace(ws_id, name)
            self._fill_tabs()
            self._save_settings()

    def remove_workspace(self, ws_id: str) -> None:
        ws = self.app_settings.workspace(ws_id)
        if len(self.app_settings.workspaces) <= 1:
            return
        if int(self.settings.ui.get(f"activated.{ws_id}") or 0):
            QMessageBox.warning(self, "Odebrat záložku",
                                f"Na záložce „{ws['name']}“ máte aktivované projekty.\n"
                                "Nejdřív je deaktivujte.")
            return
        answer = QMessageBox.question(
            self, "Odebrat záložku",
            f"Odebrat záložku „{ws['name']}“?\n\nSíť: {ws.get('server_root') or '—'}\n"
            f"Místní: {ws.get('local_root') or '—'}\n\n"
            "Projekty na síti ani soubory v místní složce se nemažou – odebere se jen záložka.")
        if answer != QMessageBox.StandardButton.Yes:
            return
        current = self.settings.workspace_id
        self.app_settings.remove_workspace(ws_id)
        self._shared.pop(ws_id, None)
        if current == ws_id:
            self.switch_workspace(self.app_settings.active)
        else:
            self._fill_tabs()
        self._save_settings()

    def _save_settings(self) -> None:
        try:
            self.settings.save()
        except OSError as e:
            QMessageBox.warning(self, "Nastavení", f"Nastavení nelze uložit:\n{e}")

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

    def _on_app_state(self, state) -> None:
        """Aplikace se stala aktivní (přišla z jiné aplikace): lehké obnovení seznamu – bez
        kontroly nepovolených znaků (ta prochází celé stromy projektů na serveru)."""
        if (state != Qt.ApplicationState.ApplicationActive or self._closing
                or QApplication.activeModalWidget() is not None          # otevřený dialog
                or time.monotonic() - self._last_refresh < FOCUS_REFRESH_MIN_GAP):
            return
        self.refresh(silent=True, check_names=False, background=True)

    def refresh(self, silent: bool = False, check_names: bool = True, background: bool = False) -> None:
        """Obnoví seznam projektů. Kontrola nepovolených znaků (prochází celé stromy projektů
        na serveru) běží jen při spuštění a při ručním obnovení, ne při automatickém.
        background: seznam se načte ve vlákně a okno mezitím zůstane použitelné (tiché
        obnovení po návratu do okna a automatické obnovení)."""
        if not self.settings.is_complete() or getattr(self, "_running_task", None):
            return
        if background:
            self._refresh_in_background(check_names)
            return
        self._last_refresh = time.monotonic()
        current = self.selected().name if self.selected() else self.settings.ui.get(self._ui_key("last_project"))
        self.set_busy("refresh", "Aktualizuji…")
        self.status_label.repaint()  # načítá se v hlavním vlákně – vykreslit hned
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        error = ""
        try:
            if self._sync_shared() and not check_names:
                check_names = True  # změnila se kontrola znaků / ignorované soubory
            try:
                self.pm.register_user()  # adresář uživatelů (výběr adresáta poznámek)
            except OSError:
                pass
            self.projects = self.pm.list_projects()
        except Exception as e:  # noqa: BLE001
            self.projects = []
            error = str(e) or type(e).__name__
            if not silent:
                QMessageBox.critical(self, "Chyba", str(e))
        finally:
            QApplication.restoreOverrideCursor()
            self.set_busy("refresh", None)
        self._refreshed(error)

        self._render(current)
        self._update_tab_label()
        if check_names:
            self._start_name_check()
        self.panel.show_project(self.selected())

    def _refresh_in_background(self, check_names: bool) -> None:
        if self._bg_refresh is not None and self._bg_refresh.isRunning():
            return  # už se načítá
        self._last_refresh = time.monotonic()
        if self._sync_shared():  # společná konfigurace – jen pár malých souborů
            check_names = True
        thread = ProjectListThread(self.pm)
        thread.done.connect(lambda projects, error, t=thread:
                            self._on_background_list(t, projects, check_names, error))
        self._bg_refresh = thread
        self.set_busy("refresh", "Aktualizuji…")
        thread.start()

    def _on_background_list(self, thread: ProjectListThread, projects, check_names: bool,
                            error: str = "") -> None:
        if self._closing:
            return
        self.set_busy("refresh", None)
        if thread.pm is not self.pm or getattr(self, "_running_task", None):
            return  # jiná záložka / mezitím běží akce (ta seznam obnoví sama)
        if projects is None:
            # síť nedostupná – tiché obnovení nic nehlásí, seznam zůstane; jen stavový text
            self._refreshed(error or "Seznam projektů se nepodařilo načíst.")
            return
        self._refreshed()
        current = self.selected().name if self.selected() else self.settings.ui.get(self._ui_key("last_project"))
        self.projects = projects
        self._render(current)
        self._update_tab_label()
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
        self.set_busy("names", "Kontroluji názvy souborů…")
        self._name_check.start()

    def _on_name_check(self, issues: dict) -> None:
        if self._closing:
            return
        self.set_busy("names", None)
        if self._name_check is None or self._name_check.pm is self.pm:  # ne výsledek jiné záložky
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
            self.table.setColumnWidth(COL_NAME, max(self.table.columnWidth(COL_NAME), 250))
            # i pro texty, které se objeví až později („Aktivováno mnou“, datum a čas)
            for col, width in ((COL_STATE, 130), (COL_USERS, 110), (COL_SINCE, 140)):
                self.table.setColumnWidth(col, max(self.table.columnWidth(col), width))
            self.table.setColumnWidth(COL_COMMENT, min(max(self.table.columnWidth(COL_COMMENT), 150),
                                                       COMMENT_COLUMN_MAX))
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
        comment = p.comment.first_line + (" …" if "\n" in p.comment.text else "") if p.comment else ""
        values = [p.name, comment, state, ", ".join(names), since, "ano" if p.local_exists else ""]
        for c, v in enumerate(values):
            item = QTableWidgetItem(v)
            if c == COL_COMMENT:
                # komentář je popis projektu, ne stav – bez barvy stavu, celý text v tooltipu
                if p.comment:
                    item.setToolTip(f"{p.comment.text}\n\n{p.comment.describe_change()}")
                elif not p.local_only:
                    item.setToolTip("Bez komentáře – upravíte dvojklikem nebo v nabídce pravého tlačítka.")
            elif issues:
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
            if c == COL_NAME and p.script_pending:
                item.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxWarning))
                item.setToolTip((item.toolTip() + "\n\n" if item.toolTip() else "")
                                + "Skript po odevzdání selhal – spustíte ho znovu pravým tlačítkem "
                                  "(Spustit skripty znovu).")
            elif c == COL_NAME and p.notes_for_me:
                item.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxInformation))
                note = (f"Čeká na vás {p.notes_for_me} "
                        + ("poznámka" if p.notes_for_me == 1 else "poznámky") + " – tlačítko Poznámky.")
                item.setToolTip((item.toolTip() + "\n\n" if item.toolTip() else "") + note)
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
        """Skryje projekty, jejichž název ani komentář neobsahuje hledaný text (bez ohledu
        na velikost písmen a diakritiku)."""
        needle = normalize(self.search.text().strip())
        for r, p in enumerate(self.projects):
            text = p.name + "\n" + (p.comment.text if p.comment else "")
            self.table.setRowHidden(r, bool(needle) and needle not in normalize(text))
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
            self.settings.ui[self._ui_key("last_project")] = p.name
        self._update_actions()
        self.panel.show_project(p)
        if p and p.notes_for_me and not self._rendering:
            self._open_notes_if_new(p.name, only_for_me=True)

    def activated_projects(self) -> dict[str, list[str]]:
        """Název záložky -> mé aktivované projekty (aktuální záložka podle seznamu, ostatní
        podle jejich posledního obnovení – bez přístupu k síti)."""
        result = {}
        for ws in self.app_settings.workspaces:
            if ws["id"] == self.settings.workspace_id:
                names = sorted(p.name for p in self.projects if p.mine)
            else:
                names = [n for n in (self.settings.ui.get(f"activated_names.{ws['id']}") or "").split("\n") if n]
            if names:
                result[ws["name"]] = names
        return result

    def _confirm_exit(self) -> bool:
        """Upozornění na aktivované projekty při ukončení; False = uživatel chce zůstat."""
        if not self.settings.warn_active_on_exit:
            return True
        active = self.activated_projects()
        if not active:
            return True
        lines = "\n".join(f"{ws}: {', '.join(names)}" for ws, names in active.items())
        box = QMessageBox(QMessageBox.Icon.Warning, "Ukončit SPM",
                          f"Máte stále aktivované projekty:\n\n{lines}\n\n"
                          "Aktivace zůstane i po ukončení aplikace: soubory, které držíte, nemohou "
                          "ostatní upravovat, a neodevzdané změny jsou jen ve vaší místní kopii.\n\n"
                          "Opravdu ukončit?", parent=self)
        quit_button = box.addButton("Ukončit", QMessageBox.ButtonRole.AcceptRole)
        back = box.addButton("Zpět", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(back)
        dont_ask = QCheckBox("Příště neupozorňovat (lze znovu zapnout v Nastavení)")
        box.setCheckBox(dont_ask)
        box.exec()
        if box.clickedButton() is not quit_button:
            return False
        if dont_ask.isChecked():
            self.settings.warn_active_on_exit = False
        return True

    def closeEvent(self, event) -> None:
        # jen u zobrazeného okna (zavření skrytého okna – např. v testech – se neptá)
        if not self._closing and self.isVisible() and not self._confirm_exit():
            event.ignore()
            return
        self.timer.stop()
        self._closing = True  # výsledky vláken na pozadí už nezobrazovat
        for window in self._notes_windows.values():
            window.close()  # uloží rozepsané poznámky
        if self._name_check:
            self._name_check.wait(5000)
        if self._bg_refresh:
            self._bg_refresh.wait(5000)
            try:
                self._bg_refresh.done.disconnect()  # výsledek po zavření okna už nezpracovat
            except (RuntimeError, TypeError):
                pass
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
        # i se soubory s nepovolenými znaky – dialog je nedovolí vybrat, zbytek publikovat lze
        self.act_publish.setEnabled(bool(p and p.local_only))
        self.act_invalid.setEnabled(issues)
        self.act_open.setEnabled(bool(self.settings.local_root))
        self.act_open.setToolTip(
            "Otevře místní složku vybraného projektu" if p and p.local_exists
            else "Otevře místní složku pro projekty")
        self.act_history.setEnabled(bool(p and not p.local_only))
        self.act_notes.setEnabled(bool(p and not p.local_only))
        self.act_restore.setEnabled(bool(p and not p.local_only))
        self.act_rename.setEnabled(bool(p and not p.active))
        self.act_comment.setEnabled(bool(p and not p.local_only))
        self.act_unlock.setEnabled(bool(p and p.others))
        self.act_rerun_scripts.setEnabled(bool(p and p.script_pending))
        self.act_discard_scripts.setEnabled(bool(p and p.script_pending))

    def _context_menu(self, pos) -> None:
        row = self.table.rowAt(pos.y())
        menu = QMenu(self)
        act_create = QAction("Vytvořit projekt…", menu)
        act_create.triggered.connect(self.create_project)
        if row < 0:  # prázdné místo seznamu
            menu.addAction(act_create)
            menu.exec(self.table.viewport().mapToGlobal(pos))
            return
        self.table.selectRow(row)
        actions = [self.act_activate, self.act_submit, self.act_update, self.act_take,
                   self.act_deactivate, self.act_publish, None, self.act_open, self.act_history, self.act_notes,
                   self.act_restore, None, self.act_comment, self.act_rename, None,
                   self.act_cancel, self.act_unlock,
                   None, act_create, self.act_refresh]
        if self.act_invalid.isEnabled():
            actions[:0] = [self.act_invalid, None]
        if self.act_rerun_scripts.isEnabled():
            actions[:0] = [self.act_rerun_scripts, self.act_discard_scripts, None]
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

    def activate(self, preselect: str | list[str] | None = None) -> None:
        """preselect: soubory / složky ('/' na konci) předem zaškrtnuté (označené v panelu stromu)."""
        p = self.selected()
        if not p or not self.act_activate.isEnabled():
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
                               root=self.pm.server_dir(p.name),
                               skip_option=self.settings.skip_unlocked_download,
                               mirror_label=self.mirror_source_label, preselect=preselect)
        accepted = self._exec_dialog(dlg, "select_files")
        paths, only_locked = dlg.selected_paths(), dlg.skip_unlocked()
        dlg.deleteLater()
        if not accepted:
            return

        def done(res: ActivationResult) -> None:
            msg = (f"Projekt {p.name} byl aktivován a zkopírován do:\n{self.pm.local_dir(p.name)}\n\n"
                   + (f"Staženo a zamčeno k úpravě: {len(paths)} souborů. Ostatní soubory se "
                      "nestahovaly (získáte je Převzetím)." if only_locked else
                      f"Zamčeno k úpravě: {len(paths)} souborů, ostatní jsou jen pro čtení."))
            if res.moved_to:
                msg += f"\n\nPůvodní místní složka byla přesunuta do:\n{res.moved_to}"
            self.refresh()
            if res.external:
                QMessageBox.warning(self, "Aktivace", msg + "\n\n" + format_external(res.external))
            else:
                QMessageBox.information(self, "Aktivace", msg)
            self._open_notes_if_new(p.name, only_for_me=False, after_activation=True)

        run_task(self, f"Aktivace projektu {p.name}", self.pm.activate, p.name, paths,
                 only_locked=only_locked, status_text="Přenáším data…",
                 on_done=done, on_error=lambda _: self.refresh())

    def take(self, preselect: str | list[str] | None = None) -> None:
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
            preselect=preselect, mirror_label=self.mirror_source_label)
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
            if res.external:
                QMessageBox.warning(self, "Převzít", msg + "\n\n" + format_external(res.external))
            else:
                QMessageBox.information(self, "Převzít", msg)

        run_task(self, f"Převzetí souborů – {p.name}", self.pm.take, p.name, paths,
                 status_text="Přenáším data…", on_done=done, on_error=lambda _: self.refresh())

    def update_from_server(self) -> None:
        p = self.selected()
        if not p or not p.mine:
            return

        def done(res: RefreshResult) -> None:
            self.refresh()
            if res.total == 0 and not res.conflicts and not res.external:
                QMessageBox.information(self, "Aktualizovat", "Místní kopie je aktuální.")
            elif res.external:
                QMessageBox.warning(self, "Aktualizovat", format_refresh(res))
            else:
                QMessageBox.information(self, "Aktualizovat", format_refresh(res))

        run_task(self, f"Aktualizace ze serveru – {p.name}", self.pm.refresh, p.name,
                 status_text="Přenáším data…", on_done=done, on_error=lambda _: self.refresh())

    def toggle_activation(self) -> None:
        """F3: můj aktivovaný projekt deaktivuje, jinak (lze-li) aktivuje."""
        if self.act_deactivate.isEnabled():
            self.deactivate()
        elif self.act_activate.isEnabled():
            self.activate()

    def submit(self, preselect: str | list[str] | None = None) -> None:
        """preselect: soubor / složka ('/' na konci) předem zaškrtnutá (z panelu stavu projektu)."""
        self._start_upload(end_session=False, preselect=preselect)

    def deactivate(self) -> None:
        self._start_upload(end_session=True)

    def _start_upload(self, end_session: bool, preselect: str | list[str] | None = None) -> None:
        p = self.selected()
        if not p or not p.mine:
            return
        run_task(self, "Porovnávání souborů", self.pm.plan_upload, p.name,
                 status_text="Porovnávám soubory…",
                 on_done=lambda plan: self._confirm_upload(plan, end_session, preselect))

    def _confirm_upload(self, plan: UploadPlan, end_session: bool,
                        preselect: str | list[str] | None = None) -> None:
        window = self._notes_windows.get((self.settings.workspace_id, plan.name))
        if window is not None:
            window.flush()  # rozepsané v otevřeném okně se uloží (okno může zůstat otevřené)
        dlg = UploadDialog(plan, end_session, self, local=self.pm.local_dir(plan.name), preselect=preselect,
                           drafts=self.pm.drafts(plan.name))
        accepted = self._exec_dialog(dlg, "upload")
        assignments, comment, selected = dlg.assignments(), dlg.comment_text(), dlg.selected_keys()
        notes = dlg.selected_notes()
        dlg.deleteLater()
        if not accepted:
            return
        title = "Deaktivace" if end_session else "Odevzdání"
        files = selection_files(plan, assignments, selected)
        session = self._before_session(plan.name, DEACTIVATE if end_session else SUBMIT, files,
                                       comment, title) if has_script_changes(files) else None
        if session is False:
            return

        def work(progress=None):
            cur_plan, cur_selected, added = plan, selected, []
            if session is not None and session.has_before:
                if not session.run_before():
                    session.abort()
                    return None, []
                cur_plan, cur_selected, added = self.pm.adopt_script_outputs(
                    plan.name, plan, selected, progress, activated=session.interp.activated)
            res = self.pm.upload(cur_plan, assignments, comment, cur_selected, end_session,
                                 progress=progress, notes=notes, keep_local=bool(self.settings.scripts))
            return res, added

        def failed(_message: str) -> None:
            if session is not None:
                session.abort()
            self.refresh()

        def done(result) -> None:
            res, added = result
            if res is None:
                self._before_failed(title, session)
                return
            self.refresh()
            msg = ((f"Projekt {plan.name} byl deaktivován." if end_session else "Změny byly odevzdány.")
                   + f"\n\nUpravené: {res.modified}\nNové: {res.added}\n"
                   f"Přejmenované: {res.renamed}\nSmazané: {res.deleted}\nUvolněno: {res.released}")
            if res.auto_deactivated:
                msg += (f"\n\nOdevzdal jste poslední soubory, které jste držel – projekt {plan.name} "
                        "byl deaktivován.")
            elif res.note:
                msg += "\n\n" + res.note
            if res.ended and self.settings.delete_local_after_deactivate and not res.local_deleted \
                    and not res.cleanup_pending:
                msg += LOCAL_NOT_DELETED
            if res.notes_published:
                msg += f"\n\nZveřejněné poznámky: {res.notes_published}"
            if res.restored:
                msg += ("\n\nSoubory smazané na serveru mimo aplikaci byly obnoveny z vaší místní kopie:\n  "
                        + "\n  ".join(res.restored[:15]) + ("\n  …" if len(res.restored) > 15 else ""))
            if res.lost_versions:
                msg += ("\n\nPOZOR – poslední verze těchto souborů na serveru (před smazáním) se "
                        "nezachovala v zálohách, nahrála se rovnou vaše upravená verze:\n  "
                        + "\n  ".join(res.lost_versions[:15]))
            if added:
                msg += ("\n\nSkript (BEFORE) přidal k odevzdání:\n  " + "\n  ".join(added[:15])
                        + ("\n  …" if len(added) > 15 else ""))
            self._reload_notes_window(plan.name)
            if res.script_files is None:  # odevzdání bez změn – skripty neběží
                if session is not None:
                    session.abort()
                if res.cleanup_pending:
                    if not self.pm.finish_local(plan.name):
                        msg += LOCAL_NOT_DELETED
                    self.refresh()
                QMessageBox.information(self, title, msg)
                return
            self.run_commit_scripts(plan.name, DEACTIVATE if res.ended else SUBMIT, res.script_files,
                                    comment, res.commit_id, res.cleanup_pending, title, msg,
                                    session=session)

        run_task(self, f"{title} – nahrávání projektu {plan.name}", work,
                 status_text="Přenáším data…", on_done=done, on_error=failed)

    def _before_session(self, name: str, action: str, files: list[ScriptFile], comment: str,
                        title: str):
        """Skripty záložky pro odevzdání: None = žádné, False = chyba (odevzdání se nespustí)."""
        if not self.settings.scripts:
            return None
        scripts, load_error = self.load_tab_scripts()
        if load_error:
            QMessageBox.warning(self, title, load_error + "\n\nNic se neodevzdalo – opravte výběr "
                                "skriptů v okně Skripty.")
            return False
        session = ScriptSession(self.script_env(name, action, files, comment), scripts)
        if not session.ok:
            session.abort()
            QMessageBox.warning(self, title, "Skript obsahuje chybu – nic se neodevzdalo:\n\n"
                                + session.report.error)
            return False
        return session

    def _before_failed(self, title: str, session: ScriptSession) -> None:
        self.refresh()
        box = QMessageBox(QMessageBox.Icon.Warning, title,
                          "SKRIPT SELHAL PŘED ODEVZDÁNÍM (BEFORE):\n" + session.report.error
                          + "\n\nNic se neodevzdalo. Soubory, které skript stihl vytvořit, zůstaly "
                            "v místní kopii. Po opravě odevzdejte znovu.", parent=self)
        box.setDetailedText(session.report.text)
        box.exec()

    def rename_project(self) -> None:
        p = self.selected()
        if not p or p.active:
            return
        new_name = p.name
        hint = ("(Přejmenuje se složka v místním úložišti – projekt ještě nebyl odevzdán.)"
                if p.local_only else
                "(Přejmenuje se složka na síťovém úložišti, změna se zapíše do historie.)")
        while True:
            new_name, ok = QInputDialog.getText(
                self, "Přejmenovat projekt",
                f"Nový název projektu „{p.name}“:\n" + hint,
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
                if p.local_only:
                    self.pm.rename_local_project(p.name, new_name)
                else:
                    self.pm.rename_project(p.name, new_name)
            finally:
                QApplication.restoreOverrideCursor()
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Přejmenovat projekt", str(e))
            self.refresh()
            return
        self.settings.ui[self._ui_key("last_project")] = new_name
        self.table.clearSelection()
        self.refresh()
        if p.local_only:
            return
        msg = f"Projekt „{p.name}“ byl přejmenován na „{new_name}“."
        if p.local_exists:
            msg += (f"\n\nV místním úložišti zůstala složka se starým názvem „{p.name}“. "
                    "Nyní se zobrazuje jako „Jen místně“ – pokud ji nepotřebujete, smažte ji.")
        QMessageBox.information(self, "Přejmenovat projekt", msg)

    def create_project(self) -> None:
        """Nový projekt v místním úložišti (prázdná složka) – zobrazí se jako „Jen místně“
        a publikuje se na síť jako ručně vytvořená složka."""
        title = "Vytvořit projekt"
        name = ""
        while True:
            name, ok = QInputDialog.getText(
                self, title, "Název nového projektu:\n(Vytvoří se složka v místním úložišti "
                f"{self.pm.local_root}; na síť ji pak publikujete.)", QLineEdit.EchoMode.Normal, name)
            name = name.strip()
            if not ok or not name:
                return
            error = validate_project_name(name)
            if not error:
                break
            QMessageBox.warning(self, title, error)
        try:
            self.pm.create_local_project(name)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, title, str(e))
            return
        self.settings.ui[self._ui_key("last_project")] = name
        self.table.clearSelection()
        self.refresh(check_names=False)

    def publish(self, preselect: str | list[str] | None = None) -> None:
        """preselect: soubor / složka ('/' na konci) předvybraná (z panelu stavu projektu)."""
        p = self.selected()
        if not p or not p.local_only:
            return
        files = self.pm.local_files(p.name)
        if not files:
            QMessageBox.information(self, "Publikovat", f"Složka {p.name} neobsahuje žádné soubory.")
            return
        dlg = PublishDialog(p.name, self.pm.server_dir(p.name), self.pm.local_dir(p.name), files,
                            set(self.pm.invalid_file_paths(files)), self, preselect=preselect)
        accepted = self._exec_dialog(dlg, "publish")
        paths, comment = dlg.selected_paths(), dlg.comment_text()
        dlg.deleteLater()
        if not accepted:
            return
        session = self._before_session(p.name, PUBLISH, [ScriptFile(x, ADDED) for x in paths],
                                       comment, "Publikovat")
        if session is False:
            return

        def work(progress=None):
            selected, added = (None if len(paths) == len(files) else list(paths)), []
            if session is not None and session.has_before:
                if not session.run_before():
                    session.abort()
                    return None, []
                added = [x for x in self.pm.local_files(p.name) if x not in set(files)]
                if selected is not None:
                    selected += added
            return self.pm.publish(p.name, comment, selected, progress=progress), added

        def failed(_message: str) -> None:
            if session is not None:
                session.abort()
            self.refresh()

        def done(result) -> None:
            res, added = result
            if res is None:
                self._before_failed("Publikovat", session)
                return
            if res.leftover is not None:
                self.settings.ui[self._ui_key("last_project")] = p.name
            self.refresh()
            msg = f"Projekt {p.name} byl publikován na server ({res.published} souborů)."
            if res.leftover is not None and res.leftover != self.pm.local_dir(p.name):
                msg += f"\n\nNepublikované soubory zůstaly v místní složce:\n{res.leftover}"
            elif res.leftover is not None:
                msg += ("\n\nNepublikované soubory zůstaly v místní složce, ale tu se nepodařilo "
                        "přejmenovat (některý soubor je otevřen). Při aktivaci projektu se odloží.")
            elif not res.local_deleted:
                msg += ("\n\nMístní složku se nepodařilo celou smazat (některý soubor je otevřen). "
                        "Data na serveru jsou kompletní, zbytek místní složky smažte ručně.")
            if added:
                msg += "\n\nSkript (BEFORE) přidal k publikování:\n  " + "\n  ".join(added[:15])
            self.run_commit_scripts(p.name, PUBLISH, res.script_files, comment, res.commit_id,
                                    False, "Publikovat", msg, session=session)

        run_task(self, f"Publikování projektu {p.name}", work, status_text="Přenáším data…",
                 on_done=done, on_error=failed)

    # --- skripty po odevzdání ---------------------------------------------------------

    def pm_for(self, ws_id: str) -> ProjectManager:
        """ProjectManager jiné záložky (stejný uživatel a počítač)."""
        if ws_id == self.settings.workspace_id:
            return self.pm
        return ProjectManager(self.app_settings.view(ws_id), user=self.pm.user, pc=self.pm.pc)

    def _ws_name(self, ws_id: str) -> str:
        return next((w["name"] for w in self.app_settings.workspaces if w["id"] == ws_id), ws_id)

    def _tab_name_of(self, server_id: str, project_dir: str) -> str | None:
        """Název záložky se síťovou složkou projektu (podle identity složky nebo cesty)."""
        for ws in self.app_settings.workspaces:
            if server_id and self.app_settings.view(ws["id"]).server_id == server_id:
                return ws["name"]
        parent = str(Path(project_dir).parent)
        for ws in self.app_settings.workspaces:
            if ws.get("server_root") and _same_dir(ws["server_root"], parent):
                return ws["name"]
        return None

    def mirror_source_label(self, entry) -> str:
        """Odkud zrcadlo soubor přináší: název záložky (podle identity síťové složky nebo cesty),
        pokud ji uživatel má; jinak název zdrojové složky ze spm_config.json, nebo cesta."""
        return (self._tab_name_of(entry.mirror_server_id, entry.mirror)
                or entry.mirror_server_name or entry.mirror)

    def mirror_target_label(self, target) -> str:
        """Kam se soubor zrcadlí: název záložky (jinak název síťové složky nebo cesta);
        u jiného projektu než stejnojmenného i s jeho názvem."""
        tab = (self._tab_name_of(target.server_id, target.label) or target.server_name
               or str(Path(target.label).parent))
        project = Path(target.label).name
        p = self.panel.project
        return tab if p is not None and project.lower() == p.name.lower() else f"{tab}/{project}"

    def open_scripts(self) -> None:
        p = self.selected()
        dlg = ScriptsDialog(self, project=p.name if p and not p.local_only else None)
        self._exec_dialog(dlg, "scripts")
        dlg.deleteLater()

    def tab_infos(self) -> list[TabInfo]:
        return [TabInfo(ws["id"], ws["name"], self.app_settings.view(ws["id"]).server_name,
                        ws.get("server_root") or "", ws.get("local_root") or "")
                for ws in self.app_settings.workspaces]

    def script_env(self, name: str, action: str, files: list[ScriptFile], comment: str = "",
                   commit_id: int | None = None) -> ScriptEnv:
        tabs = self.tab_infos()
        tab = next(t for t in tabs if t.ws_id == self.settings.workspace_id)
        return ScriptEnv(self.pm, name, action, files, comment, commit_id, tab, tabs,
                         lambda t: self.pm_for(t.ws_id), nick=self.settings.nickname,
                         ui=self.script_ui, secrets=dict(self.settings.secrets))

    def load_tab_scripts(self) -> tuple[list[tuple[str, str]], str]:
        """Skripty záložky (název, text) v pořadí běhu; chyba = chybějící / nečitelný soubor."""
        scripts = []
        for name in self.settings.scripts:
            try:
                scripts.append((name, script_store.load_script(name)))
            except OSError as e:
                return scripts, f"Skript {name} nelze načíst ({script_store.script_path(name)}): {e}"
        return scripts, ""

    def run_commit_scripts(self, name: str, action: str, files: list[ScriptFile], comment: str,
                           commit_id: int | None, cleanup: bool, title: str, summary: str,
                           session: ScriptSession | None = None, saved_vars: dict | None = None) -> None:
        """Po odevzdání spustí skripty záložky (session = pokračování po BEFORE; jinak nový běh,
        u opakování s proměnnými z BEFORE). Když selžou, uloží se nedokončený běh (spustí se
        znovu z nabídky projektu) a místní kopie se neuklidí."""
        names = list(self.settings.scripts)
        if session is None and (not names or not files):
            # bez skriptů se nic nezrcadlí – označení změněných souborů už neplatí
            clear_stale_marks(self.script_env(name, action, files, comment, commit_id), RunReport(), set())
            if cleanup:
                if not self.pm.finish_local(name):
                    summary += LOCAL_NOT_DELETED
                self.refresh()
            QMessageBox.information(self, title, summary)
            return
        load_error = ""
        if session is None:
            scripts, load_error = self.load_tab_scripts()
            env = self.script_env(name, action, files, comment, commit_id)
            if not load_error:
                session = ScriptSession(env, scripts)
                session.import_vars(saved_vars or {})
        else:
            env = session.env
        pending = script_store.PendingRun(name, action, comment, commit_id, files, names, cleanup=cleanup)
        local_root = self.pm.local_root
        outcome = {"local_deleted": True}

        def work(progress=None) -> RunReport:
            if load_error:
                report = RunReport(ok=False, error=load_error)
                clear_stale_marks(env, report, set())
            else:
                session.interp.progress = progress
                session.run_after(files, commit_id, action)
                report = session.report
                pending.vars = session.export_vars()
            try:
                self.pm.record_script_run(name, commit_id, action, names, report.ok, report.text)
            except Exception as e:  # noqa: BLE001 – záznam nesmí zakrýt výsledek skriptu
                report.log.append(f"(Záznam do historie projektu se nepodařilo uložit: {e})")
            if report.ok:
                script_store.clear_pending(local_root, name)
                if cleanup:
                    outcome["local_deleted"] = self.pm.finish_local(name)
            else:
                pending.error = report.error
                script_store.save_pending(local_root, pending)
            return report

        def done(report: RunReport) -> None:
            self.refresh()
            text = (summary + "\n\n") if summary else ""
            if report.ok:
                text += "Skripty proběhly: " + ", ".join(names)
                if not outcome["local_deleted"]:
                    text += LOCAL_NOT_DELETED
                icon = QMessageBox.Icon.Information
            else:
                text += ("SKRIPT SELHAL:\n" + report.error + "\n\nData jsou odevzdaná na serveru"
                         + (", místní kopie zůstala" if cleanup else "")
                         + ". Po opravě spustíte skripty znovu pravým tlačítkem na projektu "
                           "(Spustit skripty znovu).")
                icon = QMessageBox.Icon.Warning
            box = QMessageBox(icon, title, text, parent=self)
            box.setDetailedText(report.text)
            box.exec()

        def failed(message: str) -> None:
            pending.error = message
            try:
                script_store.save_pending(local_root, pending)
            except OSError:
                pass
            self.refresh()

        run_task(self, f"Skripty – projekt {name}", work, status_text="Spouštím skripty…",
                 on_done=done, on_error=failed)

    def rerun_scripts(self) -> None:
        p = self.selected()
        if not p or not p.script_pending:
            return
        pending = script_store.load_pending(self.pm.local_root, p.name)
        if pending is None:
            QMessageBox.warning(self, "Spustit skripty znovu", "Záznam nedokončeného skriptu nelze přečíst.")
            return
        names = list(self.settings.scripts)
        if not names:
            QMessageBox.warning(self, "Spustit skripty znovu",
                                "Záložka nemá vybrané žádné skripty (okno Skripty).")
            return
        answer = QMessageBox.question(
            self, "Spustit skripty znovu",
            f"Odevzdání z {pending.created_at.replace('T', ' ')} ({len(pending.files)} souborů).\n"
            f"Chyba: {pending.error}\n\nSpustit znovu skripty: {', '.join(names)}?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        # projekt mezitím znovu aktivovaný – místní kopii nemazat
        cleanup = pending.cleanup and not p.mine
        self.run_commit_scripts(p.name, pending.action, pending.files, pending.comment,
                                pending.commit_id, cleanup, "Spustit skripty znovu", "",
                                saved_vars=pending.vars)

    def discard_scripts(self) -> None:
        p = self.selected()
        if not p or not p.script_pending:
            return
        answer = QMessageBox.question(
            self, "Zahodit nedokončený skript",
            f"Zahodit nedokončený skript projektu {p.name}?\n\nSkripty se pro toto odevzdání už "
            "nespustí. Místní kopie projektu (pokud zůstala) se nemaže.")
        if answer == QMessageBox.StandardButton.Yes:
            script_store.clear_pending(self.pm.local_root, p.name)
            self.refresh()

    def _on_double_click(self, index) -> None:
        """Dvojklik: na názvu aktivace / deaktivace (jako F3), na komentáři jeho úprava,
        jinde (a u projektu, který nelze aktivovat – např. Jen místně) otevření místní složky."""
        if index.column() == COL_COMMENT and self.act_comment.isEnabled():
            self.edit_comment()
        elif index.column() == COL_NAME and (self.act_activate.isEnabled()
                                             or self.act_deactivate.isEnabled()):
            self.toggle_activation()
        else:
            self.open_local(root_fallback=False)

    def edit_comment(self) -> None:
        """Úprava komentáře projektu – kdykoliv, i když projekt má aktivovaný někdo jiný."""
        p = self._refresh_selected_row()  # aktuální text (mohl ho mezitím změnit někdo jiný)
        if not p or p.local_only:
            return
        dlg = CommentDialog(p.name, p.comment, self)
        accepted = self._exec_dialog(dlg, "comment")
        text = dlg.text()
        dlg.deleteLater()
        if not accepted:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.pm.set_comment(p.name, text)
        except Exception as e:  # noqa: BLE001
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "Komentář projektu", str(e))
            return
        QApplication.restoreOverrideCursor()
        self._refresh_selected_row()
        self._apply_filter()

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

    def restore_project(self) -> None:
        """Obnovení ze zálohy: spočítat obnovitelné stavy, vybrat, obnovit do místní složky."""
        p = self.selected()
        if not p or p.local_only:
            return

        def chosen(catalog: RestoreCatalog) -> None:
            dlg = RestoreDialog(catalog, f"{p.name}_obnova_<datum stavu> v {self.settings.local_root}", self)
            accepted = self._exec_dialog(dlg, "restore")
            point = dlg.selected_point()
            dlg.deleteLater()
            if not accepted or point is None:
                return
            run_task(self, f"Obnovení projektu {p.name}", self.pm.restore, catalog, point,
                     status_text="Obnovuji soubory…",
                     on_done=done)

        def done(res: RestoreResult) -> None:
            self.refresh()  # jako F5 – nová složka …_obnova_… se objeví v seznamu
            if res.failed:
                box = QMessageBox(QMessageBox.Icon.Warning, "Obnovit ze zálohy",
                                  f"Obnoveno {res.restored} souborů do:\n{res.folder}\n\n"
                                  f"{len(res.failed)} souborů se obnovit nepodařilo (záloha chybí "
                                  "nebo neodpovídá historii):\n" + "\n".join(res.failed[:15]), parent=self)
                box.setDetailedText("\n".join(res.failed))
            else:
                box = QMessageBox(QMessageBox.Icon.Information, "Obnovit ze zálohy",
                                  f"Projekt byl obnoven ({res.restored} souborů, všechny ověřené) do:\n"
                                  f"{res.folder}", parent=self)
            open_btn = box.addButton("Otevřít složku", QMessageBox.ButtonRole.ActionRole)
            box.addButton(QMessageBox.StandardButton.Ok)
            box.exec()
            if box.clickedButton() is open_btn:
                os.startfile(res.folder)

        run_task(self, f"Výpočet obnovitelných stavů – {p.name}", self.pm.restore_points, p.name,
                 status_text="Procházím historii…",
                 on_done=chosen)

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

    # --- poznámky -----------------------------------------------------------------

    def _notes_seen_key(self, name: str) -> str:
        return f"notes_seen.{self.settings.workspace_id}.{name}"

    def open_notes(self, name: str | None = None) -> NotesWindow | None:
        """Plovoucí okno poznámek projektu (jedno na projekt; další volání ho jen vyzvedne)."""
        if name is None:
            p = self.selected()
            if not p or p.local_only:
                return None
            name = p.name
        key = (self.settings.workspace_id, name)
        window = self._notes_windows.get(key)
        if window is None:
            seen_key = self._notes_seen_key(name)
            window = NotesWindow(self.pm, name, self,
                                 seen_get=lambda: int(self.settings.ui.get(seen_key) or 0),
                                 seen_set=lambda note_id: self.settings.ui.__setitem__(seen_key, str(note_id)))
            window.file_clicked.connect(lambda path, n=name: self._show_in_tree(n, path))
            window.changed.connect(lambda: self._refresh_selected_row())
            self._notes_windows[key] = window
        else:
            window.reload()
        window.show()
        window.raise_()
        window.activateWindow()
        return window

    def _open_notes_if_new(self, name: str, only_for_me: bool, after_activation: bool = False) -> None:
        """Otevře poznámky, pokud jsou nové (od posledního zobrazení) – jen pro mě, nebo po
        aktivaci jakékoliv otevřené."""
        try:
            notes = [n for n in self.pm.notes(name) if not n.resolved]
        except Exception:  # noqa: BLE001
            return
        seen = int(self.settings.ui.get(self._notes_seen_key(name)) or 0)
        if only_for_me:  # pro mě / moje doplněná – nová, upravená nebo s novým doplněním
            notes = [n for n in notes if n.concerns(self.pm.user) and n.has_news(seen, self.pm.user)]
        if notes or (after_activation and self.pm.drafts(name)):
            self.open_notes(name)

    def _reload_notes_window(self, name: str) -> None:
        window = self._notes_windows.get((self.settings.workspace_id, name))
        if window is not None and window.isVisible():
            window.reload()

    def _show_in_tree(self, name: str, path: str) -> None:
        """Odkaz na soubor v okně poznámek: vybrat projekt a soubor označit ve stromu."""
        current = self.selected()
        if current is None or current.name != name:
            names = [p.name for p in self.projects]
            if name not in names:
                return
            self.table.selectRow(names.index(name))
        self.raise_()
        self.activateWindow()
        if not self.panel.select_path(path):
            QMessageBox.information(self, "Poznámky",
                                    f"Soubor ani složka „{path}“ ve stromu projektu není "
                                    "(byl smazán, nebo se strom ještě načítá).")

    def add_note(self, scopes: list[str]) -> None:
        """Přidat poznámku k označeným souborům / složkám (z nabídky stromu)."""
        p = self.selected()
        if not p or p.local_only:
            return
        if p.mine:  # aktivovaný projekt: rozepsaná poznámka – zveřejní se s odevzdáním
            window = self.open_notes(p.name)
            if window is not None:
                window.add_draft(scopes)
            return
        dlg = QuickNoteDialog(p.name, scopes, self._known_users(), self)
        if not self._exec_dialog(dlg, "quick_note"):
            return
        try:
            self.pm.add_notes(p.name, [dlg.draft()])
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Poznámky", str(e))
            return
        self._refresh_selected_row()
        self.panel.reload()
        self._reload_notes_window(p.name)

    def _known_users(self) -> list:
        try:
            return self.pm.users()
        except OSError:
            return []

    def _settle_drafts_before_cancel(self, name: str) -> bool:
        """Zrušení aktivace: co s rozepsanými poznámkami? False = nerušit."""
        window = self._notes_windows.get((self.settings.workspace_id, name))
        if window is not None:
            window.flush()
        drafts = [d for d in self.pm.drafts(name) if d.text.strip()]
        if not drafts:
            return True
        box = QMessageBox(QMessageBox.Icon.Question, "Zrušit aktivaci",
                          f"Máte {len(drafts)} rozepsaných poznámek. Co s nimi?", parent=self)
        publish = box.addButton("Zveřejnit", QMessageBox.ButtonRole.AcceptRole)
        keep = box.addButton("Ponechat na příště", QMessageBox.ButtonRole.ActionRole)
        discard = box.addButton("Zahodit", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("Nerušit aktivaci", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        chosen = box.clickedButton()
        try:
            if chosen is publish:
                self.pm.add_notes(name, drafts)
            elif chosen is keep:  # místní kopie se při příští aktivaci odloží – poznámky stranou
                notes_mod.park_drafts(self.pm.local_root, name, self.pm.local_dir(name))
            elif chosen is not discard:
                return False
            self.pm.save_drafts(name, [])
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Poznámky", str(e))
            return False
        return True

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
        if answer != QMessageBox.StandardButton.Yes:
            return
        if not self._settle_drafts_before_cancel(p.name):
            return
        self._safe(lambda: self.pm.cancel_activation(p.name))
        self._reload_notes_window(p.name)

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

    def open_settings(self) -> bool:
        """Nastavení aktuální záložky (a osobní nastavení). Vrací True, pokud se uložilo."""
        self._sync_shared()  # dialog má ukázat aktuální stav souboru na síti
        ws_id = self.settings.workspace_id
        ws = self.app_settings.workspace(ws_id)
        dlg = SettingsDialog(self.settings, self.shared.state, self, workspace_name=ws["name"],
                             folder_check=lambda server, local: self.app_settings.folder_conflict(
                                 ws_id, server, local))
        if dlg.exec():
            self.panel.tree.default_expanded = self.settings.tree_expanded
            self._fill_tabs()  # tooltip záložky se složkami
            self._update_status_bar()
            self._apply_refresh_timer()
            self.refresh()
            return True
        if dlg.exported:  # export na síť a pak Zrušit -> převzít, co je na síti
            self.refresh()
        return False
