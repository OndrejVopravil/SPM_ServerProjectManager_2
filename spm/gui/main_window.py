from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path

from PySide6.QtCore import QByteArray, QEvent, QEventLoop, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QColor, QFont, QGuiApplication, QKeySequence, QPalette, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressDialog,
    QSplitter,
    QStackedWidget,
    QStyle,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..config import AppSettings, Settings, _same_dir
from ..db import ProjectDB, db_path
from ..names import format_invalid
from ..restore import RestoreCatalog, RestorePoint, RestoreResult
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
from .. import archive, recycle, storage
from ..folder_zones import ProtectedZones
from .folder_panel import FolderPanel
from .notes_panel import FILTER_MINE, FILTER_SELECTED, DetachedNotes, NotesPanel
from .tab_bar import TAB_COLORS, ColorTabBar, is_dark
from .. import notes as notes_mod
from .file_select_dialog import FileSelectDialog
from .file_history_dialog import FileHistoryDialog
from .project_panel import ProjectPanel
from .publish_dialog import PublishDialog
from .archive_dialog import ArchiveListDialog, ArchiveProjectDialog
from .storage_dialog import StorageDialog
from .timeline_dialog import TimelineDialog
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
TREE_HEADER_KEY = "tree.v1"  # šířky sloupců stromu souborů projektu
HEADER_KEY = "main.v3"  # změna sloupců -> neobnovovat šířky uložené pro starší rozvržení
LIST_FILTERS = ["Všechny projekty", "★ Oblíbené", "Naposledy otevřené"]
FILTER_ALL, FILTER_FAVORITES, FILTER_RECENT = range(3)
STAR = "★"
RECENT_COUNT = 15  # kolik naposledy otevřených projektů si pamatovat
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


class Pane:
    """Jeden panel okna (jako v Total Commanderu): vlastní záložka, seznam projektů, strom
    souborů a stav načítání. Okno pracuje vždy s „aktuálním“ panelem (MainWindow.pane) – mimo
    obsluhu událostí konkrétního panelu je to panel aktivní (zvýrazněný, na něj míří lišta)."""

    def __init__(self, index: int):
        self.index = index
        self.settings: Settings | None = None
        self.pm: ProjectManager | None = None
        self.shared: SharedConfig | None = None
        self.projects: list[ProjectStatus] = []
        self.name_issues: dict[str, list[str]] = {}  # projekt -> názvy s nepovolenými znaky
        self._name_check: NameCheckThread | None = None
        self._bg_refresh: ProjectListThread | None = None  # tiché obnovení seznamu na pozadí
        self._name_check_again = False
        self._rendering = False
        self._columns_sized = False
        self._last_refresh = 0.0  # time.monotonic() posledního obnovení seznamu
        self._notes_window: DetachedNotes | None = None  # odpojené poznámky panelu

    @property
    def key(self) -> str:
        """Přípona klíčů uloženého vzhledu (levý panel bez přípony – jako dřív)."""
        return "" if self.index == 0 else ".2"


# stav a ovládací prvky, které má každý panel svoje (MainWindow.<název> = aktuální panel)
PANE_ATTRS = ("settings", "pm", "shared", "projects", "name_issues", "_name_check", "_bg_refresh",
              "_name_check_again", "_rendering", "_columns_sized", "_last_refresh", "table",
              "search", "panel", "side", "splitter", "tabs", "btn_add_tab", "tabs_row",
              "projects_page", "folder_panel", "pages", "frame", "notes", "_notes_window",
              "list_filter")


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings | AppSettings):
        super().__init__()
        # celé nastavení se záložkami; samotné Settings (testy) = jedna záložka
        self.app_settings = settings if isinstance(settings, AppSettings) else AppSettings.from_settings(settings)
        # dva panely (druhý jen v režimu „Dva panely“); okno pracuje s aktuálním panelem
        self.panes = [Pane(0), Pane(1)]
        self.pane = self.active_pane = self.panes[0]
        self.dual = False
        # společná konfigurace na síti (spm_config.json) – každá záložka má svou
        self._shared: dict[str, SharedConfig] = {}
        for pane in self.panes:
            with self.on_pane(pane):
                ws_id = self.app_settings.active if pane.index == 0 else self._pane_workspace()
                self.settings = self.app_settings.view(ws_id)
                self.pm = ProjectManager(self.settings)
                self.shared = self._shared.setdefault(ws_id, SharedConfig())
        settings = self.settings
        self._closing = False
        self._finished_checks: list[NameCheckThread] = []  # dokončené kontroly názvů (viz _start_name_check)
        # návrat do aplikace z jiné (klepnutí, Alt+Tab, hlavní panel) -> obnovit seznam
        QGuiApplication.instance().applicationStateChanged.connect(self._on_app_state)
        self.setWindowTitle(f"SPM – Server Project Manager {__version__}")
        self.resize(1000, 560)
        restore_geometry(settings, "main", self)

        tb = QToolBar("Akce")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.addToolBar(tb)
        self.act_refresh = self._action(tb, "Obnovit", self.refresh_all, QKeySequence(Qt.Key.Key_F5),
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
                                      tip="Poznámky k projektu – záložka vedle stromu souborů")
        tb.addSeparator()
        self.act_scripts = self._action(tb, "Skripty", self.open_scripts,
                                        tip="Skripty spouštěné po odevzdání (zrcadlení, kopie, archivy…)")
        self.act_dual = QAction("Dva panely", self)
        self.act_dual.setCheckable(True)
        self.act_dual.setToolTip("Dva panely vedle sebe (jako v Total Commanderu) – v každém jiná "
                                 "záložka; soubory mezi nimi lze kopírovat a přetahovat")
        self.act_dual.toggled.connect(lambda on: self.set_dual(on))
        tb.addAction(self.act_dual)
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
        self.act_archive = QAction("Archivovat…", self)
        self.act_archive.setToolTip("Zabalí dokončený projekt do archivu (zip) a odstraní ho z pracovní složky")
        self.act_archive.triggered.connect(lambda: self.archive_selected())
        self.act_storage = QAction("Zálohy a koš…", self)
        self.act_storage.setToolTip("Kolik místa zabírají zálohy a koš projektu na serveru, úklid")
        self.act_storage.triggered.connect(lambda: self.show_storage())
        self.act_favorite = QAction("Přidat do oblíbených", self)
        self.act_favorite.setShortcut(QKeySequence("Ctrl+D"))
        self.act_favorite.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        self.act_favorite.triggered.connect(lambda: self.toggle_favorite())
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

        self.script_ui = GuiScriptUI(self)  # dialogy skriptů (ASK, INPUT, CHOOSE, FORM)
        # vpravo v řádku záložek: poznámky pro mě a stav aplikace (aktualizuji…, přenáším data…)
        self.btn_for_me = QToolButton()
        self.btn_for_me.setAutoRaise(True)
        self.btn_for_me.setToolTip("Otevřené poznámky pro vás – klepnutím vyberte projekt")
        self.btn_for_me.clicked.connect(lambda: self._for_me_menu())
        self.btn_for_me.hide()
        self.status_label = QLabel()
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.status_label.setContentsMargins(8, 0, 4, 0)
        self._busy: dict[str, str] = {}       # druh činnosti -> text (viz BUSY_ORDER)
        self._idle_status = ("", "", False)  # text, tooltip, chyba
        self.status_box = QWidget()
        status_lay = QHBoxLayout(self.status_box)
        status_lay.setContentsMargins(0, 0, 0, 0)
        status_lay.addWidget(self.btn_for_me)
        status_lay.addWidget(self.status_label)

        for pane in self.panes:
            with self.on_pane(pane):
                self._build_pane()
        self.panes_split = QSplitter(Qt.Orientation.Horizontal)
        self.panes_split.setChildrenCollapsible(False)
        for pane in self.panes:
            self.panes_split.addWidget(pane.frame)
        state = settings.ui.get("main.panes")
        if state:
            self.panes_split.restoreState(QByteArray.fromBase64(state.encode("ascii")))

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(4, 4, 4, 0)
        layout.addWidget(self.panes_split)
        self.setCentralWidget(central)
        for pane in self.panes:
            with self.on_pane(pane):
                self._show_page()
        focus = QAction(self)
        focus.setShortcut(QKeySequence.StandardKey.Find)
        focus.triggered.connect(lambda: (self.search.setFocus(), self.search.selectAll()))
        self.addAction(focus)
        # klepnutí do panelu (fokus) = aktivní panel
        QApplication.instance().focusChanged.connect(self._on_focus_changed)

        self.timer = QTimer(self)
        self.timer.timeout.connect(lambda: self._each_pane(
            lambda: self.refresh(silent=True, check_names=False, background=True, propagate=False)))
        self._apply_refresh_timer()

        self.act_dual.blockSignals(True)  # set_dual by hned načítal druhý panel
        self.act_dual.setChecked(settings.ui.get("main.dual") == "1")
        self.act_dual.blockSignals(False)
        self._apply_dual()
        self._activate_pane(self.panes[0])
        self._update_status_bar()
        QTimer.singleShot(0, self._startup)

    # --- panely ---------------------------------------------------------------------

    @contextmanager
    def on_pane(self, pane: Pane):
        """Okno dočasně pracuje s panelem pane (self.table, self.pm… patří jemu)."""
        previous, self.pane = self.pane, pane
        try:
            yield pane
        finally:
            self.pane = previous

    @contextmanager
    def _please_wait(self, text: str):
        """Krátká operace v hlavním vlákně – informační okénko „čekejte“, aby okno nevypadalo
        zamrzle (např. kontrola otevřených souborů, velikost projektu)."""
        dlg = QProgressDialog(text, None, 0, 0, self)
        dlg.setWindowTitle("SPM")
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.setMinimumWidth(380)
        dlg.show()
        self.set_busy("task", text)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
        try:
            yield
        finally:
            QApplication.restoreOverrideCursor()
            self.set_busy("task", None)
            dlg.close()
            dlg.deleteLater()

    def _in(self, pane: Pane, fn):
        """Obsluha události z panelu pane – proběhne v jeho kontextu."""
        def run(*args):
            with self.on_pane(pane):
                return fn(*args)
        return run

    def visible_panes(self) -> list[Pane]:
        return self.panes if self.dual else self.panes[:1]

    def _each_pane(self, fn) -> None:
        for pane in self.visible_panes():
            with self.on_pane(pane):
                fn()

    def _other_panes(self) -> list[Pane]:
        return [p for p in self.visible_panes() if p is not self.pane]

    def _same_workspace_panes(self) -> list[Pane]:
        """Ostatní zobrazené panely se stejnou záložkou jako aktuální panel."""
        return [p for p in self._other_panes()
                if p.settings.workspace_id == self.settings.workspace_id and not p.settings.is_folder]

    def _sync_other_panes(self) -> None:
        """Seznam aktuálního panelu se obnovil (po akci nebo ručně): druhý panel se stejnou
        záložkou převezme stejná data (bez sítě), s jinou záložkou se obnoví na pozadí – akce
        mohla změnit i jinou záložku (zrcadlení, kopie mezi projekty)."""
        source = self.pane
        for pane in self._other_panes():
            with self.on_pane(pane):
                if pane.settings.is_folder:
                    continue  # strom složky se obnovuje sám
                if pane.settings.workspace_id == source.settings.workspace_id:
                    current = self.selected().name if self.selected() else None
                    self.projects = list(source.projects)
                    self.name_issues = dict(source.name_issues)
                    self._render(current)
                    self._show_project(self.selected())
                else:
                    self.refresh(silent=True, check_names=False, background=True, propagate=False)

    def _sync_row(self, status: ProjectStatus) -> None:
        """Aktuální stav jednoho projektu (po klepnutí, poznámkách…) i ve druhém panelu."""
        for pane in self._same_workspace_panes():
            with self.on_pane(pane):
                names = [p.name for p in self.projects]
                if status.name not in names:
                    continue
                r = names.index(status.name)
                self.projects[r] = status
                self._render_row(r, status)
                if self.panel.project is not None and self.panel.project.name == status.name:
                    self.panel.reload()
                if self.notes.name == status.name:
                    self.notes.reload()

    def _on_panel_modified(self) -> None:
        """Soubory změněné ve stromu jednoho panelu: stromy ostatních panelů znovu načíst
        (stejný projekt, nebo zdroj přesunu z jiného projektu)."""
        for pane in self._other_panes():
            if pane.panel.project is not None and not pane.settings.is_folder:
                pane.panel.reload()

    def refresh_all(self) -> None:
        """F5: obnoví seznam ve všech zobrazených panelech."""
        self._each_pane(lambda: self.refresh(propagate=False))

    def _pane_workspace(self) -> str:
        """Záložka druhého panelu (uložená), jinak stejná jako v prvním."""
        ws_id = self.app_settings.base.ui.get("pane2.workspace", "")
        return ws_id if any(w["id"] == ws_id for w in self.app_settings.workspaces) else self.app_settings.active

    def _build_pane(self) -> None:
        """Ovládací prvky aktuálního panelu: záložky, hledání, seznam projektů, strom, složka."""
        pane = self.pane
        settings = self.settings
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.setWordWrap(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        # šířky sloupců: uložené od minula, jinak se jednou nastaví podle obsahu
        self._columns_sized = restore_header(settings, HEADER_KEY + pane.key, self.table.horizontalHeader())
        self.table.itemSelectionChanged.connect(self._in(pane, self._on_selection))
        # dvojklik: název = aktivovat / deaktivovat, komentář = upravit, jinak místní složka projektu
        self.table.doubleClicked.connect(self._in(pane, self._on_double_click))
        self.table.addAction(self.act_rename)
        self.table.addAction(self.act_favorite)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._in(pane, self._context_menu))

        self.search = QLineEdit()
        self.search.setPlaceholderText("Hledat projekt…  (Ctrl+F)")
        self.search.setClearButtonEnabled(True)
        self.search.setMaximumWidth(320)
        self.search.textChanged.connect(self._in(pane, lambda _text: self._apply_filter()))
        self.search.returnPressed.connect(self._in(pane, self._select_first_visible))
        clear = QShortcut(QKeySequence(Qt.Key.Key_Escape), self.search)
        # jen v poli hledání projektů – Esc v panelu stromu ruší hledání souborů
        clear.setContext(Qt.ShortcutContext.WidgetShortcut)
        clear.activated.connect(self.search.clear)

        # pod seznamem: stav vybraného projektu (kdo drží které soubory)
        self.panel = ProjectPanel(self.pm)
        self.panel.submit_requested.connect(self._in(pane, self.submit))
        self.panel.publish_requested.connect(self._in(pane, self.publish))
        self.panel.take_requested.connect(self._in(pane, self.take))
        self.panel.activate_requested.connect(self._in(pane, self.activate))
        self.panel.note_requested.connect(self._in(pane, self.add_note))
        self.panel.history_requested.connect(self._in(pane, self.file_history))
        self.panel.pm_for = self.pm_for
        self.panel.modified.connect(self._in(pane, self._on_panel_modified))
        self.panel.tree.itemSelectionChanged.connect(self._in(pane, self._on_tree_selection))
        self.panel.tree.itemClicked.connect(self._in(pane, self._on_tree_click))
        self.panel.can_activate = lambda: self.act_activate.isEnabled()
        self.panel.mirror_source_label = self.mirror_source_label
        self.panel.mirror_target_label = self.mirror_target_label
        self.panel.tree.default_expanded = settings.tree_expanded  # výchozí stav složek ve stromu
        self.splitter = QSplitter(Qt.Orientation.Vertical)
        # poznámky vybraného projektu tohoto panelu – záložka vedle stromu, lze odpojit
        self.notes = NotesPanel(self.pm)
        self.notes.seen_get = lambda name: int(self.settings.ui.get(self._notes_seen_key(name)) or 0)
        self.notes.seen_set = lambda name, seq: self.settings.ui.__setitem__(self._notes_seen_key(name), str(seq))
        self.notes.file_clicked.connect(self._in(pane, lambda path: self._show_in_tree(self.notes.name, path)))
        self.notes.changed.connect(self._in(pane, self._on_notes_changed))
        self.notes.counts_changed.connect(self._in(pane, self._update_notes_tab))
        self.notes.detach_toggled.connect(self._in(pane, self.toggle_notes_detached))
        self.side = QTabWidget()
        self.side.setDocumentMode(True)
        self.side.addTab(self.panel, "Soubory")
        self.side.addTab(self.notes, "Poznámky")
        self.side.hide()
        self.splitter.addWidget(self.table)
        self.splitter.addWidget(self.side)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 2)
        self.splitter.setChildrenCollapsible(False)
        state = settings.ui.get("main.splitter" + pane.key)
        if state:
            self.splitter.restoreState(QByteArray.fromBase64(state.encode("ascii")))
        self.splitter.splitterMoved.connect(self._in(pane, lambda *_: self._sync_splitters()))
        # šířky sloupců stromu souborů (uložené od minula)
        restore_header(settings, TREE_HEADER_KEY + pane.key, self.panel.tree.header())
        # záložky = pracovní prostory (každá má vlastní síťovou a místní složku)
        self.tabs = ColorTabBar()
        self.tabs.setMovable(True)  # pořadí záložek přetažením myší
        self.tabs.tabMoved.connect(self._in(pane, lambda *_: self._save_tab_order()))
        self.tabs.setExpanding(False)
        self.tabs.setDocumentMode(True)
        # bez posouvacích šipek – jinak si lišta i s jedinou záložkou rezervuje místo navíc
        self.tabs.setUsesScrollButtons(False)
        tab_font = QFont(self.tabs.font())
        if tab_font.pointSizeF() > 0:  # záložky o něco větší než ostatní text
            tab_font.setPointSizeF(tab_font.pointSizeF() * 1.2)
        self.tabs.setFont(tab_font)
        self.tabs.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tabs.customContextMenuRequested.connect(self._in(pane, self._tab_menu))
        self.tabs.tabBarClicked.connect(lambda *_: self._activate_pane(pane))
        self.btn_add_tab = QToolButton()
        self.btn_add_tab.setText("+")
        self.btn_add_tab.setToolTip("Přidat záložku (síťová složka projektů nebo obyčejná složka)")
        self.btn_add_tab.setAutoRaise(True)
        self.btn_add_tab.setFont(tab_font)
        self.btn_add_tab.clicked.connect(self._in(pane, lambda *_: self.add_workspace()))
        self._fill_tabs()
        self.tabs.currentChanged.connect(self._in(pane, self._on_tab_changed))
        self.tabs_row = QHBoxLayout()
        self.tabs_row.setSpacing(2)
        self.tabs_row.addWidget(self.tabs)
        self.tabs_row.addWidget(self.btn_add_tab)
        self.tabs_row.addStretch()

        # záložka projektů (hledání, seznam, strom a poznámky) / záložka „Složka“ (strom složky)
        self.projects_page = QWidget()
        page_lay = QVBoxLayout(self.projects_page)
        page_lay.setContentsMargins(0, 0, 0, 0)
        self.list_filter = QComboBox()
        for label in LIST_FILTERS:
            self.list_filter.addItem(label)
        self.list_filter.setToolTip("Zobrazit všechny projekty, jen oblíbené (★, Ctrl+D), nebo "
                                    "naposledy otevřené")
        self.list_filter.currentIndexChanged.connect(self._in(pane, lambda _i: self._apply_filter()))
        search_row = QHBoxLayout()
        search_row.addWidget(self.search)
        search_row.addWidget(self.list_filter)
        search_row.addStretch()
        page_lay.addLayout(search_row)
        page_lay.addWidget(self.splitter)
        self.folder_panel = FolderPanel()
        self.folder_panel.pm_for = self.pm_for
        self.folder_panel.modified.connect(self._in(pane, self._on_panel_modified))
        self.folder_panel.zones_get = self.protected_zones
        self.folder_panel.open_in_project.connect(self._in(pane, self.go_to_protected))
        self.pages = QStackedWidget()
        self.pages.addWidget(self.projects_page)
        self.pages.addWidget(self.folder_panel)
        self.frame = QFrame()
        frame_lay = QVBoxLayout(self.frame)
        frame_lay.setContentsMargins(2, 2, 2, 2)
        frame_lay.addLayout(self.tabs_row)
        frame_lay.addWidget(self.pages)

    def set_dual(self, on: bool) -> None:
        """Režim dvou panelů (zapnutí / vypnutí z lišty)."""
        if on == self.dual:
            return
        self.settings.ui["main.dual"] = "1" if on else ""
        self._apply_dual()
        if on:  # druhý panel se načte až při prvním zobrazení
            with self.on_pane(self.panes[1]):
                if self.settings.is_complete() and not self.projects:
                    self.refresh(check_names=False)
        elif self.active_pane is not self.panes[0]:
            self._activate_pane(self.panes[0])

    def _apply_dual(self) -> None:
        self.dual = self.act_dual.isChecked()
        self.panes[1].frame.setVisible(self.dual)
        # stav aplikace a „pro vás“ vpravo v řádku záložek posledního panelu
        self.visible_panes()[-1].tabs_row.addWidget(self.status_box)
        if self.dual:  # seznam projektů v obou panelech stejně vysoký
            with self.on_pane(self.panes[0]):
                self._sync_splitters()

    def _sync_splitters(self) -> None:
        """Dělič mezi seznamem projektů a stromem posunutý v jednom panelu – stejně i ve druhém."""
        sizes = self.splitter.sizes()
        if not sum(sizes):
            return  # ještě nerozvržené okno
        for pane in self.panes:
            if pane is not self.pane:
                pane.splitter.setSizes(sizes)

    def _on_focus_changed(self, _old, new) -> None:
        if new is None or self._closing:
            return
        for pane in self.visible_panes():
            if pane is not self.active_pane and pane.frame.isAncestorOf(new):
                self._activate_pane(pane)
                return

    def _activate_pane(self, pane: Pane) -> None:
        """Aktivní panel: na něj míří lišta a klávesové zkratky (poznámky má každý panel své)."""
        self.active_pane = self.pane = pane
        self._update_actions()
        self._update_for_me()
        self._update_status_bar()

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
        if self.dual:  # až po prvním rozvržení okna
            with self.on_pane(self.panes[0]):
                self._sync_splitters()
        if self.dual:  # druhý panel (záložka projektů – Složka se načítá sama)
            with self.on_pane(self.panes[1]):
                if self.settings.is_complete():
                    self.refresh()
        if self.settings.is_folder:
            pass
        elif not self.settings.is_complete():
            QMessageBox.information(self, "Vítejte", "Nejdříve nastavte síťovou a místní složku.")
            self.open_settings()
        else:
            self.refresh()
        for pane in self.visible_panes():  # poznámky byly naposledy v samostatném okně
            with self.on_pane(pane):
                if self.settings.ui.get("notes.detached" + pane.key) == "1":
                    self.toggle_notes_detached()

    def _update_status_bar(self) -> None:
        with self.on_pane(self.active_pane):  # stavový řádek patří aktivnímu panelu
            self._status_bar_text()

    def _status_bar_text(self) -> None:
        if self.settings.is_folder:
            self.statusBar().showMessage(f"Uživatel: {self.pm.user} @ {self.pm.pc}    |    "
                                         f"Složka: {self.settings.folder_root}")
            self._update_config_label()
            return
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
        self._fill_all_tabs()

    def _save_settings_of(self, view: Settings) -> None:
        try:
            view.save()
        except OSError as e:
            QMessageBox.warning(self, "Nastavení", f"Nastavení nelze uložit:\n{e}")

    def changeEvent(self, event) -> None:
        # světlý / tmavý režim: jiná varianta barev záložek a zelené barvy textu
        if event.type() == QEvent.Type.PaletteChange and hasattr(self, "tabs"):
            self._fill_all_tabs()
        super().changeEvent(event)

    def _fill_all_tabs(self) -> None:
        """Záložky se změnily (přidání, odebrání, název, barva, pořadí) – lišty obou panelů."""
        for pane in self.panes:
            if hasattr(pane, "tabs"):
                with self.on_pane(pane):
                    self._fill_tabs()

    def _fill_tabs(self) -> None:
        self.tabs.blockSignals(True)
        try:
            while self.tabs.count():
                self.tabs.removeTab(0)
            for ws in self.app_settings.workspaces:
                i = self.tabs.addTab("")
                self._style_tab(i, ws)
                self.tabs.setTabData(i, ws["id"])
                if ws.get("folder_root"):
                    self.tabs.setTabToolTip(i, f"Složka: {ws['folder_root']}")
                    self.tabs.setTabIcon(i, self.style().standardIcon(QStyle.StandardPixmap.SP_DirIcon))
                else:
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
        ws_id = self.settings.workspace_id
        ws = self.app_settings.workspace(ws_id)
        for pane in self.panes:  # záložka v liště obou panelů
            if not hasattr(pane, "tabs"):
                continue
            with self.on_pane(pane):
                for i in range(self.tabs.count()):
                    if self.tabs.tabData(i) == ws_id:
                        self._style_tab(i, ws)

    def _on_tab_changed(self, index: int) -> None:
        ws_id = self.tabs.tabData(index)
        if ws_id and ws_id != self.settings.workspace_id:
            self.switch_workspace(ws_id)

    def switch_workspace(self, ws_id: str) -> None:
        """Přepne na jinou záložku: jiné nastavení, jiný seznam projektů i strom."""
        if getattr(self, "_running_task", None):
            QMessageBox.information(self, "Záložky", "Počkejte na dokončení probíhající akce.")
            self._fill_all_tabs()
            return
        if self.pane.index == 0:
            self.app_settings.active = ws_id
        else:
            self.app_settings.base.ui["pane2.workspace"] = ws_id
        self.settings = self.app_settings.view(ws_id)
        self.pm = ProjectManager(self.settings)
        self.panel.pm = self.pm
        self.shared = self._shared.setdefault(ws_id, SharedConfig())
        self.projects, self.name_issues = [], {}
        self._name_check_again = False
        self._render(None)
        self._show_project(None)
        self._fill_all_tabs()
        self._update_status_bar()
        self._show_page()
        if self.settings.is_complete():
            self.refresh()
        else:
            self._update_config_label()

    def _show_page(self) -> None:
        """Záložka projektů, nebo záložka „Složka“ (obyčejná složka mimo spolupráci)."""
        if self.settings.is_folder:
            self.folder_panel.set_root(self.settings.folder_root, self.settings.workspace_id)
            self.pages.setCurrentWidget(self.folder_panel)
        else:
            self.pages.setCurrentWidget(self.projects_page)

    def add_workspace(self) -> None:
        kinds = ["Projekty (síťová složka pro spolupráci)", "Složka (obyčejná, např. Data)"]
        kind, ok = QInputDialog.getItem(self, "Přidat záložku", "Druh záložky:", kinds, 0, False)
        if not ok:
            return
        if kind == kinds[1]:
            self.add_folder_workspace()
            return
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
        self._fill_all_tabs()
        self.switch_workspace(ws_id)
        if not self.open_settings():  # bez nastavených složek záložku nezakládat
            self.app_settings.remove_workspace(ws_id)
            self.switch_workspace(previous)
            return
        self._save_settings()

    def choose_folder(self, ws_id: str, current: str = "") -> str | None:
        """Výběr složky pro záložku „Složka“ – s kontrolou, že se nekryje se spoluprací."""
        path = current
        while True:
            path = QFileDialog.getExistingDirectory(self, "Složka záložky", path)
            if not path:
                return None
            error = self.app_settings.folder_tab_conflict(ws_id, path)
            if not error:
                return str(Path(path))
            QMessageBox.warning(self, "Složka záložky", error)

    def add_folder_workspace(self) -> None:
        path = self.choose_folder("")
        if not path:
            return
        name, ok = QInputDialog.getText(self, "Přidat záložku", "Název záložky:",
                                        QLineEdit.EchoMode.Normal, Path(path).name)
        name = name.strip()
        if not ok or not name:
            return
        inside = self.app_settings.contained_collaboration(path)
        if inside:
            QMessageBox.information(
                self, "Přidat záložku",
                "Složka obsahuje složky pro spolupráci – v této záložce budou jen pro čtení "
                "(dvojklik na soubor přepne do jeho projektu):\n\n" + "\n".join(inside))
        ws_id = self.app_settings.add_workspace(name)
        self.app_settings.workspace(ws_id)["folder_root"] = path
        self.app_settings.view(ws_id).folder_root = path
        self._fill_all_tabs()
        self.switch_workspace(ws_id)
        self._save_settings()

    def protected_zones(self) -> ProtectedZones:
        """Složky spolupráce (chráněné v záložkách Složka) – podle aktuálních záložek."""
        key = tuple((w["id"], w.get("server_root"), w.get("local_root"), w.get("name"))
                    for w in self.app_settings.workspaces if not w.get("folder_root"))
        if getattr(self, "_zones_key", None) != key:
            self._zones_key = key
            self._zones = ProtectedZones.from_workspaces(self.app_settings.workspaces)
        return self._zones

    def go_to_protected(self, path: Path) -> None:
        """Soubor / složka z chráněné oblasti záložky Složka: přepnout panel do záložky projektů
        a projektu, kam patří, a soubor označit ve stromu."""
        zone = self.protected_zones().zone_of(Path(path), Path(self.settings.folder_root or path))
        if zone is None:
            return
        if not zone.ws_id:
            QMessageBox.information(self, "Chráněná složka",
                                    f"Soubor patří projektu SPM, který není v žádné vaší záložce:\n"
                                    f"{zone.root}\n\nPřidejte záložku s jeho síťovou složkou.")
            return
        project, rel = ProtectedZones.project_of(zone, Path(path))
        if self.settings.workspace_id != zone.ws_id:
            self.switch_workspace(zone.ws_id)
        if not project:
            return
        names = [p.name for p in self.projects]
        match = next((n for n in names if n.lower() == project.lower()), None)
        if match is None:
            QMessageBox.information(self, "Chráněná složka",
                                    f"Projekt {project} v seznamu záložky není.")
            return
        self.table.selectRow(names.index(match))
        self.side.setCurrentWidget(self.panel)
        if rel and not Path(path).is_dir():
            self.panel.select_when_loaded(rel)

    def change_folder(self, ws_id: str) -> None:
        view = self.app_settings.view(ws_id)
        path = self.choose_folder(ws_id, view.folder_root)
        if path:
            view.folder_root = path
            self._save_settings_of(view)
            self._fill_all_tabs()
            if ws_id == self.settings.workspace_id:
                self._show_page()
                self._update_status_bar()

    def _tab_menu(self, pos) -> None:
        index = self.tabs.tabAt(pos)
        if index < 0:
            return
        ws_id = self.tabs.tabData(index)
        menu = QMenu(self)
        act_rename = menu.addAction("Přejmenovat záložku…")
        act_settings = menu.addAction("Nastavení složek…")
        act_storage = act_archive = None
        if not self.app_settings.view(ws_id).is_folder:
            act_storage = menu.addAction("Zálohy a koš na serveru…")
            act_archive = menu.addAction("Archiv projektů…")
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
            if self.app_settings.view(ws_id).is_folder:
                self.change_folder(ws_id)
                return
            if ws_id != self.settings.workspace_id:
                self.switch_workspace(ws_id)
            self.open_settings()
        elif act_archive is not None and chosen is act_archive:
            if ws_id != self.settings.workspace_id:
                self.switch_workspace(ws_id)
            self.show_archive()
        elif act_storage is not None and chosen is act_storage:
            if ws_id != self.settings.workspace_id:
                self.switch_workspace(ws_id)
            self.show_storage(whole_server=True)
        elif chosen is act_remove:
            self.remove_workspace(ws_id)

    def rename_workspace(self, ws_id: str) -> None:
        ws = self.app_settings.workspace(ws_id)
        name, ok = QInputDialog.getText(self, "Přejmenovat záložku", "Nový název záložky:",
                                        QLineEdit.EchoMode.Normal, ws["name"])
        name = name.strip()
        if ok and name and name != ws["name"]:
            self.app_settings.rename_workspace(ws_id, name)
            self._fill_all_tabs()
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
            f"Odebrat záložku „{ws['name']}“?\n\n"
            + (f"Složka: {ws['folder_root']}\n\nSoubory ve složce se nemažou – odebere se jen záložka."
               if ws.get("folder_root") else
               f"Síť: {ws.get('server_root') or '—'}\nMístní: {ws.get('local_root') or '—'}\n\n"
               "Projekty na síti ani soubory v místní složce se nemažou – odebere se jen záložka."))
        if answer != QMessageBox.StandardButton.Yes:
            return
        current = self.settings.workspace_id
        self.app_settings.remove_workspace(ws_id)
        self._shared.pop(ws_id, None)
        for pane in self.panes:
            if pane.settings.workspace_id == ws_id:
                with self.on_pane(pane):
                    self.switch_workspace(self.app_settings.active)
        self._fill_all_tabs()
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
        self._each_pane(lambda: self.refresh(silent=True, check_names=False, background=True,
                                             propagate=False))

    def refresh(self, silent: bool = False, check_names: bool = True, background: bool = False,
                propagate: bool = True) -> None:
        """Obnoví seznam projektů. Kontrola nepovolených znaků (prochází celé stromy projektů
        na serveru) běží jen při spuštění a při ručním obnovení, ne při automatickém.
        background: seznam se načte ve vlákně a okno mezitím zůstane použitelné (tiché
        obnovení po návratu do okna a automatické obnovení).
        propagate: obnovit i druhý panel (po akci v tomto panelu – viz _sync_other_panes)."""
        if self.settings.is_folder:  # záložka „Složka“ – strom se obnovuje sám
            if not background:
                self._refreshed()
            return
        if not self.settings.is_complete() or getattr(self, "_running_task", None):
            return
        if background:
            self._refresh_in_background(check_names, propagate)
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
        if propagate:
            self._sync_other_panes()
        if check_names:
            self._start_name_check()
        self._show_project(self.selected())

    def _refresh_in_background(self, check_names: bool, propagate: bool = False) -> None:
        if self._bg_refresh is not None and self._bg_refresh.isRunning():
            return  # už se načítá
        self._last_refresh = time.monotonic()
        if self._sync_shared():  # společná konfigurace – jen pár malých souborů
            check_names = True
        thread = ProjectListThread(self.pm)
        thread.done.connect(self._in(self.pane, lambda projects, error, t=thread:
                            self._on_background_list(t, projects, check_names, error, propagate)))
        self._bg_refresh = thread
        self.set_busy("refresh", "Aktualizuji…")
        thread.start()

    def _on_background_list(self, thread: ProjectListThread, projects, check_names: bool,
                            error: str = "", propagate: bool = False) -> None:
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
        self._show_project(self.selected())
        if propagate:
            self._sync_other_panes()

    def _start_name_check(self) -> None:
        """Na pozadí zkontroluje názvy složek a souborů v projektech, které nemám aktivované."""
        if self._closing:
            return
        if self._name_check and self._name_check.isRunning():
            self._name_check_again = True
            return
        self._name_check_again = False
        projects = [p for p in self.projects if not p.mine]
        if self._name_check is not None:  # výsledek dokončené kontroly ještě nemusí být doručen
            self._finished_checks.append(self._name_check)
        self._name_check = NameCheckThread(self.pm, projects)
        self._name_check.done.connect(self._on_name_check_done)
        self.set_busy("names", "Kontroluji názvy souborů…")
        self._name_check.start()

    def _on_name_check_done(self, issues: dict) -> None:
        """Výsledek kontroly názvů – v kontextu panelu, kterému vlákno patří (spojení na metodu
        okna se doručí hned při dalším zpracování událostí)."""
        thread = self.sender()
        if thread in self._finished_checks:
            self._finished_checks.remove(thread)
        # i výsledek starší kontroly téže záložky (mezitím mohla začít další)
        pane = next((p for p in self.panes if p._name_check is thread), None) or next(
            (p for p in self.panes if thread is not None and p.pm is getattr(thread, "pm", None)), None)
        if pane is not None:
            with self.on_pane(pane):
                self._on_name_check(issues)

    def _on_name_check(self, issues: dict) -> None:
        if self._closing:
            return
        self.set_busy("names", None)
        if self._name_check is None or self._name_check.pm is self.pm:  # ne výsledek jiné záložky
            self.name_issues = issues
            current = self.selected().name if self.selected() else None
            self._render(current)
            for pane in self._same_workspace_panes():  # stejná záložka ve druhém panelu
                with self.on_pane(pane):
                    self.name_issues = dict(issues)
                    self._render(self.selected().name if self.selected() else None)
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
        self._update_for_me()

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
        shown_name = f"{STAR} {p.name}" if p.name in self.favorites() else p.name
        values = [shown_name, comment, state, ", ".join(names), since, "ano" if p.local_exists else ""]
        # barva stavu platí pro celý řádek (i komentář) – stejná jako u názvu projektu
        if issues:
            color = COLOR_INVALID
            tip = ("Projekt obsahuje názvy s nepovolenými znaky – nelze ho "
                   + ("publikovat" if p.local_only else "aktivovat")
                   + ", dokud je neopravíte:\n\n" + format_invalid(issues))
        elif p.local_only:
            color = self.palette().color(QPalette.ColorRole.PlaceholderText)
            tip = "Složka existuje jen v místním úložišti – lze ji publikovat na server."
        elif p.active:
            color = COLOR_MINE if p.mine else COLOR_OTHER
            tip = "Aktivováno:\n" + "\n".join(f"{u.label} od {u.since.replace('T', ' ')}" for u in p.users)
        else:
            color, tip = None, ""
        for c, v in enumerate(values):
            item = QTableWidgetItem(v)
            if color is not None:
                item.setForeground(color)
            if c == COL_COMMENT:  # celý text komentáře v bublině
                if p.comment:
                    item.setToolTip(f"{p.comment.text}\n\n{p.comment.describe_change()}")
                elif not p.local_only:
                    item.setToolTip("Bez komentáře – upravíte dvojklikem nebo v nabídce pravého tlačítka.")
            elif tip:
                item.setToolTip(tip)
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
            self._update_for_me()
            self._sync_row(status)
        return self.projects[r]

    def _update_for_me(self) -> None:
        """Počítadlo otevřených poznámek pro mě (všechny projekty záložky) v řádku záložek."""
        count = sum(p.notes_for_me for p in self.active_pane.projects)
        self.btn_for_me.setText(f"📝 {count} pro vás")
        self.btn_for_me.setVisible(count > 0)

    def _for_me_menu(self) -> None:
        """Nabídka projektů s poznámkami pro mě; výběr otevře jejich poznámky (filtr Pro mě)."""
        projects = [p for p in self.projects if p.notes_for_me]
        if not projects:
            return
        menu = QMenu(self)
        for p in projects:
            menu.addAction(f"{p.name}  ({p.notes_for_me})").setData(p.name)
        chosen = menu.exec(self.btn_for_me.mapToGlobal(self.btn_for_me.rect().bottomLeft()))
        if chosen is not None:
            self.show_notes_for_me(chosen.data())

    def show_notes_for_me(self, name: str) -> None:
        if self.open_notes(name) is not None:
            self.notes.set_filter(FILTER_MINE)

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
        mode = self.list_filter.currentIndex() if hasattr(self.pane, "list_filter") else 0
        only = self.favorites() if mode == FILTER_FAVORITES else (
            set(self.recent()) if mode == FILTER_RECENT else None)
        for r, p in enumerate(self.projects):
            text = p.name + "\n" + (p.comment.text if p.comment else "")
            hidden = (bool(needle) and needle not in normalize(text)) or (only is not None and p.name not in only)
            self.table.setRowHidden(r, hidden)
        rows = self.table.selectionModel().selectedRows()
        if rows and self.table.isRowHidden(rows[0].row()):
            self.table.clearSelection()
        self._update_actions()

    # --- oblíbené a naposledy otevřené ------------------------------------------------

    def favorites(self) -> set[str]:
        return {n for n in (self.settings.ui.get(self._ui_key("favorites")) or "").split("\n") if n}

    def recent(self) -> list[str]:
        """Naposledy otevřené projekty záložky (nejnovější první)."""
        return [n for n in (self.settings.ui.get(self._ui_key("recent")) or "").split("\n") if n]

    def _touch_recent(self, name: str) -> None:
        names = [name] + [n for n in self.recent() if n != name]
        self.settings.ui[self._ui_key("recent")] = "\n".join(names[:RECENT_COUNT])
        if self.list_filter.currentIndex() == FILTER_RECENT:
            self._apply_filter()

    def toggle_favorite(self, name: str | None = None) -> None:
        """Přidat projekt do oblíbených (★) / odebrat (Ctrl+D)."""
        if name is None:
            p = self.selected()
            if p is None:
                return
            name = p.name
        favs = self.favorites()
        favs.symmetric_difference_update({name})
        self.settings.ui[self._ui_key("favorites")] = "\n".join(sorted(favs, key=str.lower))
        for pane in self.panes:  # i druhý panel se stejnou záložkou
            if hasattr(pane, "table") and pane.settings.workspace_id == self.settings.workspace_id:
                with self.on_pane(pane):
                    names = [p.name for p in self.projects]
                    if name in names:
                        self._render_row(names.index(name), self.projects[names.index(name)])
                    self._apply_filter()
        self._update_actions()

    def _rename_in_lists(self, old: str, new: str) -> None:
        """Přejmenovaný projekt zůstane v oblíbených i naposledy otevřených."""
        for key in ("favorites", "recent"):
            names = (self.settings.ui.get(self._ui_key(key)) or "").split("\n")
            self.settings.ui[self._ui_key(key)] = "\n".join(new if n == old else n for n in names if n)

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
            if not self._rendering:
                self._touch_recent(p.name)
        self._update_actions()
        self._show_project(p)
        if p and p.notes_for_me and not self._rendering and self.pane is self.active_pane:
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
        for pane in self.panes:  # odpojené poznámky – zapamatovat, že byly odpojené
            if pane._notes_window is not None:
                save_geometry(self.settings, "notes_window" + pane.key, pane._notes_window)
                pane._notes_window.close()
        for pane in self.panes:
            if pane._name_check:
                pane._name_check.wait(5000)
            if pane._bg_refresh:
                pane._bg_refresh.wait(5000)
                try:
                    pane._bg_refresh.done.disconnect()  # výsledek po zavření okna už nezpracovat
                except (RuntimeError, TypeError):
                    pass
            pane.panel.stop()
            self.settings.ui["main.splitter" + pane.key] = bytes(pane.splitter.saveState().toBase64()).decode("ascii")
            save_header(self.settings, HEADER_KEY + pane.key, pane.table.horizontalHeader())
            save_header(self.settings, TREE_HEADER_KEY + pane.key, pane.panel.tree.header())
        self.settings.ui["main.panes"] = bytes(self.panes_split.saveState().toBase64()).decode("ascii")
        save_geometry(self.settings, "main", self)
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
        if self.pane is not self.active_pane:
            return  # lišta patří aktivnímu panelu
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
        self.act_favorite.setEnabled(p is not None)
        self.act_storage.setEnabled(bool(p and not p.local_only))
        self.act_archive.setEnabled(bool(p and not p.local_only and archive.archive_dir(self.settings)))
        self.act_favorite.setText("Odebrat z oblíbených\tCtrl+D" if p and p.name in self.favorites()
                                  else "Přidat do oblíbených\tCtrl+D")
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
                   self.act_restore, self.act_storage, self.act_archive, None, self.act_favorite,
                   self.act_comment,
                   self.act_rename, None,
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

    def _check_open_files(self, plan: UploadPlan, end_session: bool) -> str:
        """Soubory otevřené v jiné aplikaci (CAD, právě se ukládají) – upozornit před odevzdáním.
        Vrací "ok", "cancel", nebo "replan" (soubory se mezitím zavřely – mohly se uložit,
        porovnání se serverem je potřeba udělat znovu)."""
        title = "Deaktivace" if end_session else "Odevzdání"
        waited = False
        while True:
            with self._please_wait("Kontroluji, zda nejsou soubory otevřené v jiném programu…"):
                busy = self.pm.files_open_elsewhere(plan, end_session)
            if not busy:
                return "replan" if waited else "ok"
            lines = [f"{path} – otevřeno v programu {', '.join(apps)}" if apps else path
                     for path, apps in busy.items()]
            shown = "\n".join(lines[:15]) + ("\n…" if len(lines) > 15 else "")
            box = QMessageBox(
                QMessageBox.Icon.Warning, title,
                f"Tyto soubory má otevřené jiný program:\n\n"
                f"{shown}\n\nUložte je a zavřete, pak zvolte Zkusit znovu. Otevřený soubor se nemusí "
                "nahrát celý"
                + (" a místní kopie po deaktivaci nepůjde smazat." if end_session else "."), parent=self)
            retry = box.addButton("Zkusit znovu", QMessageBox.ButtonRole.AcceptRole)
            anyway = box.addButton("Přesto pokračovat", QMessageBox.ButtonRole.DestructiveRole)
            box.addButton("Zrušit", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is anyway:
                return "ok"
            if box.clickedButton() is not retry:
                return "cancel"
            waited = True

    def _confirm_upload(self, plan: UploadPlan, end_session: bool,
                        preselect: str | list[str] | None = None) -> None:
        check = self._check_open_files(plan, end_session)
        if check == "cancel":
            return
        if check == "replan":  # soubory zavřené (a možná uložené) – porovnat znovu
            run_task(self, "Porovnávání souborů", self.pm.plan_upload, plan.name,
                     status_text="Porovnávám soubory…",
                     on_done=lambda new: self._confirm_upload(new, end_session, preselect))
            return
        dlg = UploadDialog(plan, end_session, self, local=self.pm.local_dir(plan.name), preselect=preselect,
                           drafts=self.pm.drafts(plan.name), users=self.notes.users or self._known_users())
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
        self._rename_in_lists(p.name, new_name)
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
        for pane in [self.pane] + self.panes:
            if pane.settings.workspace_id == ws_id:
                return pane.pm
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
                for ws in self.app_settings.workspaces if not ws.get("folder_root")]

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
        """Obnovení ze zálohy – časová osa (u každého stavu je vidět, zda ho lze obnovit)."""
        self.history()

    def _restore_point(self, catalog: RestoreCatalog, point: RestorePoint) -> None:
        """Obnoví projekt ke stavu point do nové místní složky (na pozadí)."""
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

        run_task(self, f"Obnovení projektu {catalog.name}", self.pm.restore, catalog, point,
                 status_text="Obnovuji soubory…", on_done=done)

    # --- archivace ----------------------------------------------------------------------

    def archive_selected(self) -> None:
        """Archivovat vybraný projekt (zip do archivní složky, z pracovní složky pryč)."""
        p = self.selected()
        if not p or p.local_only:
            return
        problems = archive.archive_problems(self.pm, p.name)
        if problems:
            QMessageBox.information(self, "Archivovat", f"Projekt {p.name} nelze archivovat:\n\n• "
                                    + "\n• ".join(problems))
            return
        server = self.pm.server_dir(p.name)
        with self._please_wait(f"Zjišťuji velikost projektu {p.name}…"):
            info = storage.project_storage(server)
            project_bytes = sum(f.stat().st_size for f in server.rglob("*")
                                if f.is_file() and ".spm" not in f.relative_to(server).parts[:1])
        dlg = ArchiveProjectDialog(p.name, archive.archive_dir(self.settings), info, project_bytes,
                                   self.settings.archive_keep_backups, self)
        accepted = self._exec_dialog(dlg, "archive")
        comment = dlg.comment_text()
        dlg.deleteLater()
        if not accepted:
            return
        name = p.name

        def done(res) -> None:
            self.refresh()
            msg = (f"Projekt {name} je archivovaný:\n{res.zip_path}\n\n"
                   f"{res.files} souborů, zip {storage.fmt_bytes(res.size)}.")
            if res.leftover:
                msg += ("\n\nSložku projektu se v pracovní složce nepodařilo celou smazat (některý "
                        "soubor je otevřený) – smažte zbytek ručně.")
            QMessageBox.information(self, "Archivovat", msg)
            local = self.pm.local_dir(name)
            if local.exists() and QMessageBox.question(
                    self, "Archivovat",
                    f"Máte místní složku projektu:\n{local}\n\nProjekt už v pracovní složce není – "
                    "smazat ji (do Koše)?") == QMessageBox.StandardButton.Yes:
                try:
                    recycle.to_recycle_bin([local])
                except OSError as e:
                    QMessageBox.warning(self, "Archivovat", str(e))
                self.refresh()

        run_task(self, f"Archivace projektu {name}", archive.archive_project, self.pm, name, comment,
                 status_text="Archivuji…", on_done=done)

    def show_archive(self) -> None:
        """Archiv projektů záložky – seznam, náhled, obnova do pracovní složky."""
        folder = archive.archive_dir(self.settings)
        if folder is None:
            QMessageBox.information(self, "Archiv projektů",
                                    "Archivní složka není nastavená (Nastavení → Společné nastavení).")
            return
        if not folder.is_dir():
            QMessageBox.information(self, "Archiv projektů", f"Archivní složka zatím neexistuje "
                                    f"nebo není dostupná:\n{folder}")
            return

        def restore(entry) -> bool:
            name = entry.project
            try:
                same = archive.existing_identity(self.pm, entry.path)
            except Exception as e:  # noqa: BLE001 – poškozený zip apod.
                QMessageBox.critical(self, "Obnovit z archivu", str(e))
                return False
            if same:
                QMessageBox.information(self, "Obnovit z archivu", archive.identity_message(entry.project, same))
                return False
            if any(x.name.lower() == name.lower() for x in self.pm.server_root.iterdir()):
                name, ok = QInputDialog.getText(
                    self, "Obnovit z archivu",
                    f"V pracovní složce už projekt {entry.project} je.\nPod jakým názvem ho obnovit?",
                    QLineEdit.EchoMode.Normal, f"{entry.project}_obnova")
                if not ok or not name.strip():
                    return False
            elif QMessageBox.question(self, "Obnovit z archivu",
                                      f"Obnovit projekt {name} z archivu do pracovní složky?\n\n"
                                      f"{self.pm.server_root}\n\nZip v archivu zůstane.") \
                    != QMessageBox.StandardButton.Yes:
                return False
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                path = archive.restore_archive(self.pm, entry.path, name.strip())
            except Exception as e:  # noqa: BLE001
                QApplication.restoreOverrideCursor()
                QMessageBox.critical(self, "Obnovit z archivu", str(e))
                return False
            QApplication.restoreOverrideCursor()
            self.refresh()
            QMessageBox.information(self, "Obnovit z archivu", f"Projekt je obnovený:\n{path}")
            return True

        dlg = ArchiveListDialog(folder, restore, archive.permission_problem(self.settings, self.pm.user), self)
        self._exec_dialog(dlg, "archive_list")
        dlg.deleteLater()

    def show_storage(self, whole_server: bool = False) -> None:
        """Zálohy a koš – vybraného projektu, nebo všech projektů síťové složky záložky."""
        if self.settings.is_folder or not self.settings.is_complete():
            return

        def show(infos) -> None:
            if not infos:
                QMessageBox.information(self, "Zálohy a koš", "V síťové složce nejsou žádné projekty.")
                return
            dlg = StorageDialog(self.pm.server_root, infos, self.pm.user, self.pm.pc,
                                self.settings.backup_count, self,
                                clean_problem=archive.cleanup_problem(self.settings, self.pm.user))
            self._exec_dialog(dlg, "storage")
            dlg.deleteLater()

        if whole_server:
            run_task(self, "Zálohy a koš na serveru", lambda progress=None:
                     storage.server_storage(self.pm.server_root, progress),
                     status_text="Procházím zálohy…", on_done=show)
            return
        p = self.selected()
        if not p or p.local_only:
            return
        show([storage.project_storage(self.pm.server_dir(p.name))])

    def file_history(self, path: str) -> None:
        """Historie jednoho souboru (verze, otevření, obnovení)."""
        p = self.selected()
        if not p or p.local_only:
            return
        entry = next((e for e in self.panel.entries if e.path == path), None)
        # obnovit do místní kopie lze jen soubor, který mám aktivovaný (zamčený)
        local = (self.pm.local_dir(p.name).joinpath(*path.split("/"))
                 if p.mine and entry is not None and entry.mine else None)
        try:
            dlg = FileHistoryDialog(self.pm, p.name, path, local, self)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Historie souboru", str(e))
            return
        self._exec_dialog(dlg, "file_history")
        dlg.deleteLater()
        self.panel.reload()  # obnovená verze v místní kopii = změněný soubor

    def history(self) -> None:
        """Časová osa projektu: historie a obnova ze záloh v jednom okně."""
        p = self.selected()
        if not p or p.local_only:
            return
        server = self.pm.server_dir(p.name)
        if not db_path(server).exists():
            QMessageBox.information(self, "Historie", "Projekt zatím nemá žádnou historii.")
            return
        scopes = self.panel.selected_scopes() if self.panel.project is not None \
            and self.panel.project.name == p.name else []

        def show(catalog: RestoreCatalog | None) -> None:
            with ProjectDB.for_project(server) as db:
                dlg = TimelineDialog(db, p.name, catalog, self.pm.user, scopes, self,
                                     file_history=self.file_history)
                accepted = self._exec_dialog(dlg, "history")
                point = dlg.chosen_point
                dlg.deleteLater()
            if accepted and point is not None and catalog is not None:
                self._restore_point(catalog, point)

        # obnovitelné stavy (existence záloh) – na pozadí; bez nich se ukáže jen historie
        run_task(self, f"Historie projektu {p.name}", self.pm.restore_points, p.name,
                 status_text="Procházím historii…", on_done=show, on_error=lambda _e: show(None))

    # --- poznámky -----------------------------------------------------------------

    def _notes_seen_key(self, name: str) -> str:
        return f"notes_seen.{self.settings.workspace_id}.{name}"

    def _show_project(self, p: ProjectStatus | None) -> None:
        """Pod seznamem: strom souborů a poznámky vybraného projektu."""
        self.panel.show_project(p)
        self.side.setVisible(p is not None)
        self._show_notes_for(p)

    def _show_notes_for(self, p: ProjectStatus | None) -> None:
        """Poznámky projektu p (aktuální panel)."""
        self.notes.set_project(self.pm, p.name if p and not p.local_only else None)
        self.side.setTabEnabled(self.side.indexOf(self.notes), bool(p and not p.local_only))
        if self._notes_window is not None:
            self._notes_window.setWindowTitle(f"Poznámky – projekt {p.name}" if p else "Poznámky")

    def _on_tree_selection(self) -> None:
        self.notes.set_selection(self.panel.selected_scopes())

    def _update_notes_tab(self, open_count: int, for_me: int) -> None:
        text = "Poznámky" + (f" ({open_count}" + (f" · pro vás {for_me}" if for_me else "") + ")"
                             if open_count else "")
        index = self.side.indexOf(self.notes)
        if index >= 0:
            self.side.setTabText(index, text)

    def _on_notes_changed(self) -> None:
        self._refresh_selected_row()  # i řádek ve druhém panelu (_sync_row)
        self.panel.reload()  # sloupec Poznámky ve stromu

    def _on_tree_click(self, item, column: int) -> None:
        """Klepnutí na 📝 ve stromu: poznámky k tomuto souboru / složce."""
        if column == 3 and item.text(3):
            self.notes.set_filter(FILTER_SELECTED)
            self.open_notes()

    def open_notes(self, name: str | None = None) -> NotesPanel | None:
        """Zobrazí poznámky projektu (záložku vedle stromu, nebo odpojené okno)."""
        if name is not None and (self.selected() is None or self.selected().name != name):
            names = [p.name for p in self.projects]
            if name not in names:
                return None
            self.table.selectRow(names.index(name))
        p = self.selected()
        if not p or p.local_only:
            return None
        if self._notes_window is not None:
            self._notes_window.show()
            self._notes_window.raise_()
            self._notes_window.activateWindow()
        else:
            self.side.setCurrentWidget(self.notes)
        return self.notes

    def _open_notes_if_new(self, name: str, only_for_me: bool, after_activation: bool = False) -> None:
        """Ukáže poznámky, pokud jsou nové (od posledního zobrazení) – jen pro mě, nebo po
        aktivaci jakékoliv otevřené."""
        try:
            notes = [n for n in self.pm.notes(name) if not n.resolved]
        except Exception:  # noqa: BLE001
            return
        seen = int(self.settings.ui.get(self._notes_seen_key(name)) or 0)
        if only_for_me:  # pro mě / moje s odpovědí – nová, upravená nebo s novou odpovědí
            notes = [n for n in notes if n.concerns(self.pm.user) and n.has_news(seen, self.pm.user)]
        else:
            notes = [n for n in notes if n.has_news(seen, self.pm.user)]
        if notes:
            self.open_notes(name)

    def _reload_notes_window(self, name: str) -> None:
        """Poznámky projektu se změnily – obnovit je ve všech panelech, které ho ukazují."""
        ws_id = self.settings.workspace_id
        for pane in self.panes:
            if pane.settings.workspace_id == ws_id and pane.notes.name == name:
                pane.notes.reload()

    def toggle_notes_detached(self) -> None:
        """Poznámky ve vlastním okně, nebo zpět v záložce vedle stromu."""
        if self._notes_window is None:
            pane = self.pane
            self.side.removeTab(self.side.indexOf(self.notes))
            window = DetachedNotes(self)
            window.lay.addWidget(self.notes)
            window.closed.connect(self._in(pane, self._attach_notes))
            restore_geometry(self.settings, "notes_window" + pane.key, window)
            p = self.selected()
            window.setWindowTitle(f"Poznámky – projekt {p.name}" if p else "Poznámky")
            self._notes_window = window
            self.notes.set_detached(True)
            self.notes.show()
            window.show()
            self.settings.ui["notes.detached" + pane.key] = "1"
        else:
            self._notes_window.close()  # closed -> _attach_notes

    def _attach_notes(self) -> None:
        window, self._notes_window = self._notes_window, None
        if window is None:
            return
        save_geometry(self.settings, "notes_window" + self.pane.key, window)
        self.side.addTab(self.notes, "Poznámky")
        self.notes.set_detached(False)
        p = self.selected()
        self.side.setTabEnabled(self.side.indexOf(self.notes), bool(p and not p.local_only))
        self.notes.reload()  # popisek záložky
        if not self._closing:
            self.settings.ui["notes.detached" + self.pane.key] = ""
        window.deleteLater()

    def _show_in_tree(self, name: str | None, path: str) -> None:
        """Odkaz na soubor v poznámkách: vybrat projekt a soubor označit ve stromu."""
        if not name:
            return
        current = self.selected()
        if current is None or current.name != name:
            names = [p.name for p in self.projects]
            if name not in names:
                return
            self.table.selectRow(names.index(name))
        self.side.setCurrentWidget(self.panel)
        self.raise_()
        self.activateWindow()
        if not self.panel.select_path(path):
            QMessageBox.information(self, "Poznámky",
                                    f"Soubor ani složka „{path}“ ve stromu projektu není "
                                    "(byl smazán, nebo se strom ještě načítá).")

    def add_note(self, scopes: list[str]) -> None:
        """Nová poznámka k označeným souborům / složkám (nabídka stromu, Ctrl+N) – zaměří pole
        pro psaní v panelu poznámek; zveřejní se hned odesláním."""
        if self.open_notes() is not None:
            self.notes.compose(scopes)

    def _known_users(self) -> list:
        try:
            return self.pm.users()
        except OSError:
            return []

    def _settle_drafts_before_cancel(self, name: str) -> bool:
        """Zrušení aktivace: co s rozepsanými poznámkami (ze starších verzí)? False = nerušit."""
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
        if self.settings.is_folder:  # záložka „Složka“ má jen svou složku
            self.change_folder(self.settings.workspace_id)
            return True
        self._sync_shared()  # dialog má ukázat aktuální stav souboru na síti
        ws_id = self.settings.workspace_id
        ws = self.app_settings.workspace(ws_id)
        dlg = SettingsDialog(self.settings, self.shared.state, self, workspace_name=ws["name"],
                             folder_check=lambda server, local: self.app_settings.folder_conflict(
                                 ws_id, server, local))
        if dlg.exec():
            for pane in self.panes:
                pane.panel.tree.default_expanded = self.settings.tree_expanded
            self._fill_all_tabs()  # tooltip záložky se složkami
            self._update_status_bar()
            self._apply_refresh_timer()
            self.refresh()
            return True
        if dlg.exported:  # export na síť a pak Zrušit -> převzít, co je na síti
            self.refresh()
        return False


def _pane_property(name: str) -> property:
    return property(lambda self: getattr(self.pane, name),
                    lambda self, value: setattr(self.pane, name, value))


for _name in PANE_ATTRS:
    setattr(MainWindow, _name, _pane_property(_name))
