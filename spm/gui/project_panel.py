"""Panel „Stav projektu“ – strom souborů aktivovaného projektu a kdo který soubor drží.

Zobrazuje se pod seznamem projektů jen pro projekty, které má někdo aktivované.
Strom se načítá na pozadí (prochází složku na serveru a čte databázi projektu).
U mého aktivovaného projektu obsahuje i nové soubory z místní kopie, které ještě nejsou
odevzdané (šedě).
"""

from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QPointF, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QKeySequence, QPainter, QPalette, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QToolButton,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import recycle, zip_ops
from ..folder_ops import free_copy_name, free_name
from ..scanner import to_abs
from ..sync import (
    COPY_ADD,
    COPY_REPLACE,
    COPY_SKIP,
    COPY_TAKE,
    CopyItem,
    FileEntry,
    ProjectManager,
    ProjectStatus,
    as_copy,
    validate_file_name,
    validate_folder_name,
)
from .check_tree import KEY_ROLE, PATH_ROLE, CheckTree, TreeEntry, in_scope
from .project_tree import (
    CLIPBOARD_KEEP_S,
    CLIPBOARD_ROOT,
    MIME_CLIPBOARD,
    ProjectTree,
    clipboard_info,
    files_mime,
    new_staging_dir,
)
from .notes_window import fmt_time
from .search import wildcard_matcher

COLOR_MINE = QColor("#2e8b57")
COLOR_MIRROR = QColor("#8e44ad")  # spravuje zrcadlo z jiného serveru (jen pro čtení)
COLOR_MIRROR_STALE = QColor("#d35400")  # zrcadlená kopie je neaktuální (zdroj ji nezrcadlil)
COLOR_OTHER = QColor("#c0392b")
COLOR_FOUND = QColor("#fff176")  # podbarvení souborů nalezených hledáním
SEARCH_EXPAND_DELAY_MS = 1000    # prodleva před rozbalením složek s nalezenými soubory


def _tree_icon(expand: bool) -> QIcon:
    """Ikona „rozbalit vše“ (⊞) / „sbalit vše“ (⊟): čtvereček s plusem / mínusem."""
    pixmap = QPixmap(16, 16)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor("#7a7a7a"))
    pen.setWidthF(1.4)
    painter.setPen(pen)
    painter.drawRoundedRect(2, 2, 12, 12, 2, 2)
    painter.drawLine(QPointF(5, 8), QPointF(11, 8))
    if expand:
        painter.drawLine(QPointF(8, 5), QPointF(8, 11))
    painter.end()
    return QIcon(pixmap)
COLOR_NOTE = QColor("#b9770e")   # soubory / složky s otevřenou poznámkou


class EntriesThread(QThread):
    done = Signal(str, list, str)  # projekt, položky, chyba

    def __init__(self, pm: ProjectManager, name: str, mine: bool, local_only: bool = False):
        super().__init__()
        self.pm, self.name, self.mine, self.local_only = pm, name, mine, local_only
        self.open_notes: list = []  # otevřené (nevyřízené) poznámky projektu – značky ve stromu
        self.empty_folders: list[str] = []  # prázdné složky místní kopie (např. nově vytvořené)

    def run(self) -> None:
        try:
            if self.local_only or self.mine:
                try:
                    self.empty_folders = self.pm.local_empty_folders(self.name)
                except OSError:
                    self.empty_folders = []
            if self.local_only:  # složka jen v místním úložišti – jen její soubory
                self.done.emit(self.name, [FileEntry(p, None, False, local_new=True, in_local=True)
                                           for p in self.pm.local_files(self.name)], "")
                return
            entries = self.pm.file_entries(self.name)
            if self.mine:  # u mého projektu i nové a smazané soubory z místní kopie
                deleted = set(self.pm.local_deleted_files(self.name))
                for e in entries:
                    e.local_deleted = e.mine and e.path in deleted
                on_server = {e.path.lower() for e in entries}
                entries += [FileEntry(p, None, False, local_new=True)
                            for p in self.pm.local_new_files(self.name) if p.lower() not in on_server]
                # přejmenované (stejný obsah): místo „smazán“ + „nový“ jedna položka
                renames = self.pm.local_renames(self.name)
                if renames:
                    old_of = {new: old for old, new in renames.items()}
                    entries = [e for e in entries if not (e.local_deleted and e.path in renames)]
                    for e in entries:
                        if e.local_new and e.path in old_of:
                            e.renamed_from = old_of[e.path]
            local = self.pm.local_dir(self.name)
            if local.is_dir():  # co je v místní kopii, lze otevřít (cizí / volné jen pro čtení)
                for e in entries:
                    e.in_local = to_abs(local, e.path).is_file()
            try:
                self.open_notes = [n for n in self.pm.notes(self.name) if not n.resolved]
            except Exception:  # noqa: BLE001 – poznámky nejsou pro strom nezbytné
                self.open_notes = []
            self.done.emit(self.name, entries, "")
        except Exception as e:  # noqa: BLE001 – zobrazí se v panelu
            self.done.emit(self.name, [], str(e) or type(e).__name__)


class ProjectPanel(QWidget):
    # pravé tlačítko ve stromu: soubor nebo složka (zakončená '/'), která se má předvybrat
    # předvýběr: seznam označených souborů a složek (složka zakončená '/')
    submit_requested = Signal(object)
    take_requested = Signal(object)
    activate_requested = Signal(object)  # projekt, který nemám aktivovaný
    note_requested = Signal(object)      # přidat poznámku k označeným souborům / složkám
    history_requested = Signal(str)      # historie jednoho souboru
    publish_requested = Signal(object)   # složka „Jen místně“
    modified = Signal()                  # změna souborů z tohoto panelu (kopie, přesun, mazání…)

    def __init__(self, pm: ProjectManager, parent: QWidget | None = None):
        super().__init__(parent)
        self.pm = pm
        # ProjectManager jiné záložky (vložení souborů vyjmutých v jiné záložce) – nastaví okno
        self.pm_for: Callable[[str], ProjectManager] = lambda ws_id: self.pm
        self.project: ProjectStatus | None = None
        self.entries: list[FileEntry] = []
        self.open_notes: list = []  # otevřené poznámky projektu (sloupec Poznámky)
        self._select_pending = ""   # soubor k označení po načtení stromu
        self.empty_folders: list[str] = []  # prázdné složky místní kopie
        self._thread: EntriesThread | None = None
        self._again = False
        self._closed = False  # okno se zavírá – výsledky z pozadí už nezobrazovat
        # popis zdroje zrcadla (cesta zdrojového projektu -> např. název záložky)
        self.mirror_source_label: Callable[[FileEntry], str] = lambda entry: entry.mirror or ""
        # popis cíle zrcadla (db.MirrorTarget -> např. název záložky)
        self.mirror_target_label: Callable[[object], str] = lambda target: target.label
        # lze vybraný projekt aktivovat? (nastaví hlavní okno – např. nepovolené znaky)
        self.can_activate: Callable[[], bool] = lambda: False
        # aktivace bez zámků a bez stahování (nová složka / vložení souborů do projektu, který
        # nemám aktivovaný); vrací, zda se povedla – nastaví hlavní okno
        self.quick_activate: Callable[[], bool] = lambda: False

        self.title = QLabel()
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.only_locked = QCheckBox("Jen zamčené soubory")
        self.only_locked.toggled.connect(self._fill)
        # hledání v názvech souborů („obsahuje“, * = cokoliv); nalezené se podbarví žlutě
        self.search = QLineEdit()
        self.search.setPlaceholderText("Hledat soubor…  (* = cokoliv, Esc zruší)")
        self.search.setClearButtonEnabled(True)
        self.search.setMaximumWidth(300)
        self.search.textChanged.connect(self._on_search_text)
        self.found = QLabel()
        # po dopsání hledaného textu (1 s bez změny) se rozbalí složky s nalezenými soubory
        self._expand_timer = QTimer(self)
        self._expand_timer.setSingleShot(True)
        self._expand_timer.setInterval(SEARCH_EXPAND_DELAY_MS)
        self._expand_timer.timeout.connect(self.expand_found)
        self.btn_expand = self._tool_button(_tree_icon(expand=True), "Rozbalit všechny složky",
                                            lambda: self.tree.expandAll())
        self.btn_collapse = self._tool_button(_tree_icon(expand=False), "Sbalit všechny složky",
                                              lambda: self.tree.collapseAll())
        clear = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        clear.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)  # jen v panelu (pole, strom)
        clear.activated.connect(self.search.clear)
        head = QHBoxLayout()
        head.addWidget(self.title)
        head.addWidget(self.btn_expand)
        head.addWidget(self.btn_collapse)
        head.addStretch()
        head.addWidget(self.found)
        head.addWidget(self.search)
        head.addWidget(self.only_locked)

        self.tree = ProjectTree(["Soubor", "Drží", "Zrcadlo", "Poznámky"])
        # přetahování myší: ven = kopie (i neaktivovaných souborů, bez „jen pro čtení“),
        # dovnitř = kopie do složky (nové soubory a mé aktivované – viz plan_copy_in)
        self.tree.drag_handler = self.drag_mime
        self.tree.drop_handler = self.drop_files
        # do neaktivovaného projektu taky – před vložením se nabídne aktivace bez zámků
        self.tree.can_drop = lambda: bool(self.project and (self.project.mine or self.project.local_only
                                                            or self._can_quick_activate()))
        self.tree.itemDoubleClicked.connect(self._on_double_click)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        rename = QAction(self.tree)  # F2 jen ve stromu (v seznamu projektů F2 = přejmenovat projekt)
        rename.setShortcut(QKeySequence(Qt.Key.Key_F2))
        rename.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        rename.triggered.connect(lambda: self.rename_selected())
        self.tree.addAction(rename)
        # víc souborů najednou: Ctrl = přidat / odebrat položku, Shift = rozsah
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        delete = QAction(self.tree)
        delete.setShortcut(QKeySequence(Qt.Key.Key_Delete))
        delete.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        delete.triggered.connect(lambda: self.delete_selected())
        self.tree.addAction(delete)
        note = QAction(self.tree)  # Ctrl+N – nová poznámka k označeným souborům
        note.setShortcut(QKeySequence("Ctrl+N"))
        note.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        note.triggered.connect(lambda: self.project and not self.project.local_only
                               and self.note_requested.emit(self.selected_scopes()))
        self.tree.addAction(note)
        # schránka: Ctrl+C / Ctrl+X / Ctrl+V (jako v Průzkumníku)
        for key, slot in ((QKeySequence.StandardKey.Copy, lambda: self.copy_selected()),
                          (QKeySequence.StandardKey.Cut, lambda: self.copy_selected(cut=True)),
                          (QKeySequence.StandardKey.Paste, lambda: self.paste_into(self.paste_target()))):
            act = QAction(self.tree)
            act.setShortcut(QKeySequence(key))
            act.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
            act.triggered.connect(slot)
            self.tree.addAction(act)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.addLayout(head)
        layout.addWidget(self.summary)
        layout.addWidget(self.tree)

    def _changed(self) -> None:
        """Po změně souborů: znovu načíst strom a ohlásit to (druhý panel se obnoví také)."""
        self.reload()
        self.modified.emit()

    def show_project(self, project: ProjectStatus | None) -> None:
        """Zobrazí strom vybraného projektu (jakéhokoliv – volného, aktivovaného i složky
        „Jen místně“). Viditelnost panelu řídí okno (záložky Soubory / Poznámky) – panel ji sám
        nemění, jinak by se strom ukázal přes záložku Poznámky."""
        if project is None:
            self.project = None
            return
        same = (self.project is not None and self.project.name == project.name
                and self.project.local_only == project.local_only
                and self._thread is not None and self._thread.pm is self.pm)  # stejná záložka
        self.project = project
        self.title.setText(f"<b>Místní složka {project.name}</b> (jen místně)" if project.local_only
                           else f"<b>Stav projektu {project.name}</b>")
        self.only_locked.setVisible(not project.local_only)
        self.tree.setColumnHidden(1, project.local_only)  # „Drží“ u místní složky nedává smysl
        if not same:
            self.entries = []
            self.tree.set_entries([])
            self.summary.setText("Načítám…")
        self.reload()

    def reload(self) -> None:
        if self.project is None or self._closed:
            return
        if self._thread and self._thread.isRunning():
            self._again = True
            return
        self._again = False
        thread = EntriesThread(self.pm, self.project.name, self.project.mine, self.project.local_only)
        thread.done.connect(lambda name, entries, error: self._on_loaded(
            name, entries, error, thread.pm, thread.open_notes, thread.empty_folders))
        self._thread = thread
        thread.start()

    def _on_loaded(self, name: str, entries: list, error: str, pm: ProjectManager | None = None,
                   open_notes: list | None = None, empty_folders: list[str] | None = None) -> None:
        if self._closed:
            return
        if self._again:
            self.reload()
        if self.project is None or name != self.project.name or (pm is not None and pm is not self.pm):
            return  # mezitím vybrán jiný projekt (nebo jiná záložka – projekt se stejným názvem)
        if error:
            self.summary.setText(f"Stav projektu nelze načíst: {error}")
            return
        self.entries = entries
        self.open_notes = open_notes or []
        self.empty_folders = empty_folders or []
        self._fill()
        # soubor vybraný odjinud (např. ze záložky Složka) – až po posledním načtení stromu
        if self._select_pending and not (self._thread is not None and self._thread.isRunning()):
            path, self._select_pending = self._select_pending, ""
            self.select_path(path)

    def select_when_loaded(self, path: str) -> None:
        """Označit soubor ve stromu – hned, nebo až se strom (znovu) načte."""
        if (self._thread is not None and self._thread.isRunning()) or not self.select_path(path):
            self._select_pending = path

    def _fill(self) -> None:
        if self.project is None:
            return
        root = self.pm.server_dir(self.project.name)
        local = self.pm.local_dir(self.project.name)
        color_new = self.palette().color(QPalette.ColorRole.PlaceholderText)

        empty = [TreeEntry(f + "/", "dir:" + f, ["prázdná složka"], color=color_new, folder=True,
                           tooltip="Prázdná složka ve vaší místní kopii – na server se dostane, až "
                                   "v ní při odevzdání bude aspoň jeden soubor.")
                 for f in self.empty_folders]
        if self.project.local_only:
            self.tree.set_entries([TreeEntry(e.path, e.path, [""], file_path=str(to_abs(local, e.path)),
                                             tooltip="Dvojklikem otevřete")
                                   for e in self.entries] + empty)
            self.summary.setText(
                f"Složka je jen v místním úložišti – <b>{len(self.entries)}</b> souborů. "
                "Pravým tlačítkem můžete soubor otevřít, publikovat celou složku nebo jen její část, "
                "nebo smazat do Koše." + self._mark_mirror())
            return

        def tree_entry(e: FileEntry) -> TreeEntry:
            if e.local_deleted:  # šedě = můj soubor smazaný v místní kopii, ještě neodevzdaný
                return TreeEntry(e.path, e.path, ["smazán – neodevzdaný"], color=color_new,
                                 color_name=True, file_path=str(to_abs(root, e.path)),
                                 tooltip="Soubor je v Koši Windows. Na serveru se smaže až při "
                                         "odevzdání (do koše projektu).")
            if e.renamed_from:  # šedě = můj soubor přejmenovaný v místní kopii, neodevzdaný
                # krátce (dlouhý text by zúžil sloupec s názvy); původní cesta je v bublině
                same_folder = e.renamed_from.rpartition("/")[0] == e.path.rpartition("/")[0]
                return TreeEntry(e.path, e.path, ["přejmenován" if same_folder else "cesta přejmenována"],
                                 color=color_new, color_name=True, file_path=str(to_abs(local, e.path)),
                                 tooltip=f"Původně: {e.renamed_from}\nPřejmenováno ve vaší místní kopii; "
                                         "na serveru se přejmenuje při odevzdání.\nDvojklikem otevřete.")
            if e.local_new:  # šedě = nový soubor jen v mé místní kopii
                return TreeEntry(e.path, e.path, ["nový – neodevzdaný"], color=color_new,
                                 color_name=True, file_path=str(to_abs(local, e.path)),
                                 tooltip="Nový soubor ve vaší místní kopii, zatím neodevzdaný.\n"
                                         "Dvojklikem otevřete.")
            if not e.in_local:
                tip = ""
            elif e.mine:
                tip = "Dvojklikem otevřete z místní kopie"
            else:
                tip = "Dvojklikem otevřete z místní kopie (jen pro čtení – soubor nemáte zamčený)"
            if e.mirror:
                tip = (f"Soubor spravuje zrcadlo z {e.mirror} – zde ho nelze převzít ani upravit, "
                       "změny se dělají ve zdroji." + (f"\n{tip}" if tip else ""))
                if e.mirror_stale:
                    tip = self.stale_text(e) + "\n" + tip
            return TreeEntry(e.path, e.path, [e.holder],
                             color=None if e.free else (COLOR_MINE if e.mine else
                                                        (COLOR_MIRROR_STALE if e.mirror_stale else
                                                         COLOR_MIRROR) if e.mirror else COLOR_OTHER),
                             # zelená = drží ho já, červená = drží ho jiný, fialová = zrcadlo,
                             # oranžová = neaktuální zrcadlo
                             color_name=True,
                             file_path=str(to_abs(root, e.path)), tooltip=tip)

        shown = [e for e in self.entries
                 if e.owner or e.local_new or not self.only_locked.isChecked()]
        self.tree.set_entries([tree_entry(e) for e in shown] + empty)
        counts = Counter("já" if e.mine else e.owner.label for e in self.entries if e.owner)
        free = sum(1 for e in self.entries if e.free)
        new = sum(1 for e in self.entries if e.local_new)
        mirrored = sum(1 for e in self.entries if e.mirror and not e.owner)
        stale = sum(1 for e in self.entries if e.mirror and e.mirror_stale)
        parts = [f"{who}: <b>{n}</b>" for who, n in sorted(counts.items())]
        users = ", ".join(u.label for u in self.project.users) or "nikdo (projekt je volný)"
        self.summary.setText(
            f"Aktivováno: {users}<br>Zamčené soubory – " + (", ".join(parts) or "žádné")
            + f" &nbsp;|&nbsp; volné: <b>{free}</b>"
            + (f" &nbsp;|&nbsp; zrcadlo: <b>{mirrored}</b>" if mirrored else "")
            + (f" (<span style='color:{COLOR_MIRROR_STALE.name()}'>neaktuální: <b>{stale}</b></span>)"
               if stale else "")
            + (f" &nbsp;|&nbsp; nové neodevzdané: <b>{new}</b>" if new else "")
            + f" &nbsp;|&nbsp; na serveru celkem: {len(self.entries) - new}"
            + self._mark_mirror())

    def _mark_notes(self) -> None:
        """Sloupec Poznámky: počet otevřených poznámek k souboru / složce (text v bublině)."""
        texts: dict[str, tuple[str, str]] = {}
        by_path: dict[str, list] = {}
        for note in self.open_notes:
            for path in note.targets:
                by_path.setdefault(path, []).append(note)
        for path, notes in by_path.items():
            tip = "\n\n".join(f"{n.author}: {n.text.strip()[:300]}" for n in notes)
            texts[path] = (f"📝 {len(notes)}", tip + "\n\n(klepnutím zobrazíte poznámky k tomuto souboru)")
        self.tree.set_column_texts(3, texts, COLOR_NOTE)

    def select_path(self, path: str) -> bool:
        """Označí soubor / složku ('/' na konci) ve stromu (z odkazu v okně poznámek)."""
        from PySide6.QtWidgets import QTreeWidgetItemIterator
        it = QTreeWidgetItemIterator(self.tree)
        while it.value():
            item = it.value()
            if item.data(0, PATH_ROLE) == path:
                parent = item.parent()
                while parent is not None:
                    parent.setExpanded(True)
                    parent = parent.parent()
                self.tree.clearSelection()
                self.tree.setCurrentItem(item)
                item.setSelected(True)
                self.tree.scrollToItem(item)
                return True
            it += 1
        return False

    def _tool_button(self, icon: QIcon, tip: str, slot) -> QToolButton:
        button = QToolButton()
        button.setIcon(icon)
        button.setToolTip(tip)
        button.setAutoRaise(True)
        button.clicked.connect(lambda: slot())
        return button

    def _on_search_text(self, _text: str) -> None:
        # podbarvit hned; rozbalit složky a posunout se k prvnímu nalezenému až po prodlevě
        # (posun stromu sbalené složky sám rozbaluje)
        self._apply_search(scroll=False)
        self._expand_timer.start()  # znovu od nuly při každém dalším znaku

    def expand_found(self) -> None:
        """Rozbalí složky, ve kterých jsou soubory odpovídající hledání (ostatní nechá být)."""
        match = wildcard_matcher(self.search.text())
        if match is None:
            return
        first = None
        for item in self.tree.leaf_items():
            if match(item.text(0)):
                parent = item.parent()
                while parent is not None:
                    parent.setExpanded(True)
                    parent = parent.parent()
                first = first or item
        if first is not None:
            self.tree.scrollToItem(first)

    def _apply_search(self, scroll: bool = False) -> None:
        """Podbarví žlutě soubory, jejichž název obsahuje hledaný text (s * jako „cokoliv“)."""
        match = wildcard_matcher(self.search.text())
        found = self.tree.highlight(match, COLOR_FOUND)
        self.found.setText(f"Nalezeno: {len(found)}" if match else "")
        if scroll and found:
            self.tree.scrollToItem(found[0])

    def _mark_mirror(self) -> str:
        """Sloupec Zrcadlo (zobrazený vždy):
        - „← zdroj“ u souborů, které sem zrcadlo přináší z jiného serveru (jen pro čtení),
          „← zdroj · neaktuální“, když se zdroj změnil, ale sem se nezrcadlil,
        - „→ cíl“ u souborů, které se odtud zrcadlí (příkazem MIRROR ve skriptu odevzdání),
        u složek s počtem takových souborů. Vrací doplněk souhrnu."""
        if self.project is None:
            return ""
        texts: dict[str, tuple[str, str]] = {}
        folders: dict[str, Counter] = {}
        outgoing: Counter = Counter()
        for e in self.entries:
            if e.local_deleted:
                continue
            labels, tips = [], []
            color = None
            if e.mirror:
                source = self.mirror_source_label(e)
                if e.mirror_stale:
                    labels.append(f"← {source} · neaktuální")
                    tips.append(self.stale_text(e))
                    color = COLOR_MIRROR_STALE
                else:
                    labels.append(f"← {source}")
                tips.append(f"Soubor spravuje zrcadlo z {e.mirror} – zde ho nelze převzít ani "
                            "upravit, změny se dělají ve zdroji.")
            for target in e.mirror_targets:
                name = self.mirror_target_label(target)
                labels.append(f"→ {name}")
                outgoing[name] += 1
                tips.append(f"Zrcadleno do {target.label} – {target.path}")
            if not labels:
                continue
            texts[e.path] = (", ".join(labels), "\n".join(tips), color)
            parts = e.path.split("/")[:-1]
            for i in range(1, len(parts) + 1):
                folders.setdefault("/".join(parts[:i]) + "/", Counter()).update(labels)
        for folder, counter in folders.items():
            texts[folder] = (", ".join(f"{label} ({n})" for label, n in sorted(counter.items())),
                             "Počet souborů ve složce: " + ", ".join(
                                 f"{label}: {n}" for label, n in sorted(counter.items())),
                             COLOR_MIRROR_STALE if any("neaktuální" in label for label in counter)
                             else None)
        self.tree.set_column_texts(2, texts, COLOR_MIRROR)
        self._mark_notes()
        self._apply_search()  # strom je nově naplněný – znovu podbarvit nalezené
        if not outgoing:
            return ""
        return " &nbsp;|&nbsp; zrcadleno: " + ", ".join(
            f"<b>{n}</b> → {name}" for name, n in sorted(outgoing.items()))

    @staticmethod
    def stale_text(e: FileEntry) -> str:
        """Popis neaktuální zrcadlené kopie (bublina)."""
        s = e.mirror_stale
        when = fmt_time(s.at)
        who = f" ({s.by})" if s.by else ""
        if s.reason == "deleted":
            what = f"Ve zdroji byl soubor {when}{who} smazán, zde ale zůstal – zrcadlo ho vynechalo."
        else:
            what = (f"Ve zdroji se soubor {when}{who} změnil, ale sem se nezrcadlil – tato kopie "
                    "odpovídá starší verzi.")
        return (what + "\nAktualizuje se při dalším zrcadlení; pravým tlačítkem ho můžete "
                "uvolnit ze zrcadla (bude to běžný soubor tohoto projektu).")

    def selected_scopes(self) -> list[str]:
        """Označené položky stromu (Ctrl / Shift = víc najednou): soubory a složky ('/' na konci)."""
        return [item.data(0, PATH_ROLE) for item in self.tree.selectedItems()]

    def context_actions(self, scope: str | list[str]) -> dict[str, bool]:
        """Které akce kontextového menu jsou pro soubor / složku (nebo víc označených) dostupné."""
        scopes = [scope] if isinstance(scope, str) else list(scope)
        local_only = bool(self.project and self.project.local_only)
        mine_project = bool(self.project and self.project.mine)
        inside = [e for e in self.entries if in_scope(e.path, scopes)]
        own = any(e.mine or e.local_new for e in inside)  # zamčené mnou nebo moje nové
        any_folder = any(s.endswith("/") for s in scopes)
        single_file = len(scopes) == 1 and not any_folder
        single_folder = len(scopes) == 1 and any_folder
        # jen prázdné složky (bez souborů) – lze přejmenovat i smazat
        only_empty = any_folder and not inside and all(
            s.endswith("/") and self.is_empty_folder(s) for s in scopes)
        return {
            # projekt, který nemám aktivovaný: aktivace s předvybranými soubory
            "activate": (not local_only and not mine_project and self.can_activate()
                         and any(e.free for e in inside)),
            # otevřít jde jakýkoliv soubor, který je v místní kopii (cizí a volné jen pro čtení)
            "open": not any_folder and any(e.in_local for e in inside),
            # u složky vždy – dialog odevzdání ukáže, co v ní skutečně je
            "submit": mine_project and (any_folder or own),
            "take": mine_project and any(e.free for e in inside),
            "publish": local_only and bool(inside),
            "delete": (local_only or mine_project) and (bool(self.deletable(scopes)) or only_empty),
            # přejmenovat: jeden soubor, a to můj (zamčený / nový) a v místní kopii; nebo jedna
            # složka, ve které (i v podsložkách) mám aktivované všechny soubory
            "rename": ((local_only or mine_project) and (
                (single_file and any((e.mine or e.local_new) and e.in_local and not e.local_deleted
                                     for e in inside))
                or (single_folder and (bool(inside) or only_empty) and (local_only or all(
                    e.mine or e.local_new for e in inside))))),
            # nová složka: v aktivovaném projektu, ve složce „Jen místně“, nebo v neaktivovaném
            # (nejdřív se aktivuje bez zámků a bez stahování)
            "new_folder": local_only or mine_project or self._can_quick_activate(),
            # neaktuální zrcadlené soubory lze uvolnit (stanou se běžnými soubory projektu)
            "release_mirror": not local_only and any(e.mirror and e.mirror_stale for e in inside),
            # zip vzniká v místní kopii – jako nová složka / soubor
            "zip": (local_only or mine_project) and any(not e.local_deleted for e in inside),
            # historie jednoho souboru, který už je na serveru (v evidenci)
            "history": not local_only and single_file and any(not e.local_new for e in inside),
            "unzip": ((local_only or mine_project) and single_file and zip_ops.is_zip(scopes[0])
                      and any(e.in_local for e in inside)),
        }

    def release_mirror(self, scopes: list[str]) -> None:
        """Uvolní neaktuální zrcadlené soubory (po potvrzení)."""
        if self.project is None:
            return
        paths = [e.path for e in self.entries if in_scope(e.path, scopes) and e.mirror and e.mirror_stale]
        if not paths:
            return
        title = "Uvolnit ze zrcadla"
        shown = "\n".join(paths[:15]) + (f"\n… a další ({len(paths) - 15})" if len(paths) > 15 else "")
        if QMessageBox.question(
                self, title,
                f"Uvolnit ze zrcadla neaktuální soubory ({len(paths)})?\n\n{shown}\n\n"
                "Zrcadlo je přestane spravovat – budou to běžné soubory tohoto projektu, které lze "
                "převzít a upravit. Kdyby je zdroj později znovu zrcadlil, přepíše je (pokud je "
                "v tu chvíli nikdo nedrží).") != QMessageBox.StandardButton.Yes:
            return
        try:
            self.pm.release_mirrored(self.project.name, paths)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, title, str(e))
        self._changed()

    def is_empty_folder(self, scope: str) -> bool:
        """Složka (i nadřazená) prázdných složek místní kopie – bez jediného souboru."""
        prefix = scope.rstrip("/").lower()
        return any(f.lower() == prefix or f.lower().startswith(prefix + "/") for f in self.empty_folders) \
            and not any(e.path.lower().startswith(prefix + "/") for e in self.entries)

    def _can_quick_activate(self) -> bool:
        """Projekt na serveru, který nemám aktivovaný a který jde aktivovat."""
        return bool(self.project and not self.project.mine and not self.project.local_only
                    and self.can_activate())

    def ensure_active(self, what: str) -> bool:
        """Před změnou v místní kopii (nová složka, vložení souborů): projekt, který nemám
        aktivovaný, se po potvrzení aktivuje bez zámků a bez stahování. Vrací, zda lze
        pokračovat."""
        if self.project is None:
            return False
        if self.project.mine or self.project.local_only:
            return True
        if not self._can_quick_activate():
            return False
        answer = QMessageBox.question(
            self, "Aktivovat projekt",
            f"Projekt {self.project.name} nemáte aktivovaný.\n\nAby šlo {what}, aktivuje se bez "
            "zamčení souborů a bez stahování (rychle). Ostatní uvidí, že projekt máte aktivovaný; "
            "nic jim tím nezamknete. Nové soubory pak odevzdáte jako obvykle.\n\nAktivovat?")
        if answer != QMessageBox.StandardButton.Yes:
            return False
        return self.quick_activate()

    def create_folder(self, parent: str) -> None:
        """Nová složka v místní kopii: parent = '' (kořen projektu) nebo 'a/b/'. V projektu,
        který nemám aktivovaný, se po zadání názvu nabídne aktivace bez zámků; složka se pak
        vytvoří i s celou cestou (nadřazené složky se nestahovaly)."""
        if self.project is None or not (self.project.mine or self.project.local_only
                                        or self._can_quick_activate()):
            return
        title = "Nová složka"
        where = f"ve složce {parent.rstrip('/')}" if parent else "v hlavní složce projektu"
        name = ""
        while True:
            name, ok = QInputDialog.getText(self, title, f"Název nové složky {where}:",
                                            QLineEdit.EchoMode.Normal, name)
            name = name.strip()
            if not ok or not name:
                return
            error = validate_folder_name(name, self.pm.settings.check_forbidden_chars)
            if not error:
                break
            QMessageBox.warning(self, title, error)
        if not self.ensure_active("vytvořit složku"):
            return
        try:
            self.pm.create_local_folder(self.project.name, parent, name)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, title, str(e))
            return
        if parent:  # nová složka má být vidět
            item = self.tree.find_folder(parent)
            if item is not None:
                item.setExpanded(True)
        self._changed()

    def rename_selected(self) -> None:
        """F2 – přejmenovat vybraný soubor (jen když je označený jediný)."""
        scopes = self.selected_scopes()
        if len(scopes) == 1:
            self._rename(scopes[0])

    def delete_selected(self) -> None:
        """Delete – smazat označené soubory a složky (do Koše)."""
        scopes = self.selected_scopes()
        if scopes and self.context_actions(scopes)["delete"]:
            self._delete(scopes)

    def _rename(self, path: str) -> None:
        if self.project is None or not self.context_actions(path)["rename"]:
            return
        if path.endswith("/"):
            self._rename_folder(path)
            return
        folder, _, old_name = path.rpartition("/")
        new_name = old_name
        while True:
            new_name, ok = QInputDialog.getText(
                self, "Přejmenovat soubor",
                f"Nový název souboru „{old_name}“"
                + (f"\n(ve složce {folder})" if folder else "") + ":"
                + ("" if self.project.local_only else
                   "\n\nPřejmenuje se ve vaší místní kopii, na serveru až při odevzdání."),
                QLineEdit.EchoMode.Normal, new_name)
            new_name = new_name.strip()
            if not ok or new_name == old_name:
                return
            error = validate_file_name(new_name, self.pm.settings.check_forbidden_chars)
            if not error:
                break
            QMessageBox.warning(self, "Přejmenovat soubor", error)
        try:
            self.pm.rename_local(self.project.name, path, new_name)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Přejmenovat soubor", str(e))
        self._changed()

    def _rename_folder(self, path: str) -> None:
        folder = path.rstrip("/")
        parent, _, old_name = folder.rpartition("/")
        title = "Přejmenovat složku"
        new_name = old_name
        while True:
            new_name, ok = QInputDialog.getText(
                self, title,
                f"Nový název složky „{old_name}“" + (f"\n(ve složce {parent})" if parent else "") + ":"
                + ("" if self.project.local_only else
                   "\n\nPřejmenuje se ve vaší místní kopii, na serveru až při odevzdání "
                   "(soubory si ponechají historii i zálohy)."),
                QLineEdit.EchoMode.Normal, new_name)
            new_name = new_name.strip()
            if not ok or new_name == old_name:
                return
            error = validate_folder_name(new_name, self.pm.settings.check_forbidden_chars)
            if not error:
                break
            QMessageBox.warning(self, title, error)
        try:
            self.pm.rename_local_folder(self.project.name, folder, new_name)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, title, str(e))
        self._changed()

    # --- přetahování myší ---------------------------------------------------------

    def drag_items(self, scopes: list[str]) -> list[tuple[str, str]]:
        """Soubory k vytažení: (cesta v projektu, cesta v cíli). Soubor se vytáhne pod svým
        názvem, složka i s obsahem (podsložkami); smazané neodevzdané soubory ne."""
        if self.project is None:
            return []
        present = [e for e in self.entries if not e.local_deleted]
        items: dict[str, str] = {}
        used: dict[str, str] = {}  # název v cíli (malými) -> zdroj, kvůli shodným názvům

        def top_name(name: str, source: str) -> str:
            stem, dot, ext = name.rpartition(".") if "." in name else (name, "", "")
            candidate, n = name, 2
            while used.get(candidate.lower(), source) != source:
                candidate = f"{stem} ({n}){dot}{ext}"
                n += 1
            used[candidate.lower()] = source
            return candidate
        for scope in scopes:
            if scope.endswith("/"):
                folder = scope.rstrip("/").rpartition("/")[2]
                top = top_name(folder, scope)
                for e in present:
                    if e.path.startswith(scope):
                        items.setdefault(e.path, f"{top}/{e.path[len(scope):]}")
            elif any(e.path == scope for e in present):
                items.setdefault(scope, top_name(scope.rpartition("/")[2], scope))
        return list(items.items())

    def drag_mime(self, scopes: list[str]):
        """Data pro tažení ven: přímo soubory místní kopie, pokud jsou to jen zapisovatelné
        soubory; jinak (složky, neaktivované = jen pro čtení, nestažené) jejich kopie
        v dočasné složce – v cíli tak nikdy nebudou jen pro čtení."""
        return self._export_mime(self.drag_items(scopes), "Přetažení", new_staging_dir)

    def _export_mime(self, items: list[tuple[str, str]], title: str, staging):
        if not items or self.project is None:
            return None
        local = self.pm.local_dir(self.project.name)
        direct = [to_abs(local, rel) for rel, out in items]
        if all("/" not in out for _, out in items) and \
                all(p.is_file() and os.access(p, os.W_OK) for p in direct):
            return files_mime(items, direct)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            tops = self.pm.export_copies(self.project.name, items, staging())
        except Exception as e:  # noqa: BLE001
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, title, f"Soubory se nepodařilo připravit:\n{e}")
            return None
        QApplication.restoreOverrideCursor()
        return files_mime(items, tops)

    # --- schránka (Kopírovat / Vyjmout / Vložit) ------------------------------------

    def can_cut(self, scopes: list[str]) -> bool:
        """Vyjmout (přesunout) lze jen moje soubory – zamčené mnou nebo nové – v místní kopii
        aktivovaného projektu; ve složce „Jen místně“ všechny."""
        if self.project is None or not (self.project.mine or self.project.local_only):
            return False
        inside = [e for e in self.entries if in_scope(e.path, scopes) and not e.local_deleted]
        return bool(inside) and all(e.in_local and (self.project.local_only or e.mine or e.local_new)
                                    for e in inside)

    def can_paste(self) -> bool:
        mime = QApplication.clipboard().mimeData()
        return bool(self.tree.can_drop() and mime is not None
                    and (mime.hasUrls() or mime.hasFormat(MIME_CLIPBOARD)))

    def paste_target(self) -> str:
        """Kam vložit: označená složka, složka označeného souboru, jinak kořen projektu."""
        item = self.tree.currentItem()
        if item is None or not item.isSelected():
            return ""
        path = item.data(0, PATH_ROLE)
        return path if path.endswith("/") else (path.rpartition("/")[0] + "/" if "/" in path else "")

    def copy_selected(self, scopes: list[str] | None = None, cut: bool = False) -> bool:
        """Kopírovat (Ctrl+C) / Vyjmout (Ctrl+X) označené soubory a složky do schránky.
        Ostatní programy (Průzkumník) dostanou vždy jen kopii – přesun soubor z projektu
        nikdy nesmaže mimo SPM; vyjmuté se přesunou až vložením v SPM."""
        scopes = self.selected_scopes() if scopes is None else scopes
        if self.project is None or not scopes:
            return False
        title = "Vyjmout" if cut else "Kopírovat"
        if cut and not self.can_cut(scopes):
            QMessageBox.information(self, title, "Vyjmout lze jen soubory, které máte aktivované "
                                                 "(zamčené) nebo nové, v místní kopii.")
            return False
        items = self.drag_items(scopes)
        mime = self._export_mime(items, title,
                                 lambda: new_staging_dir(CLIPBOARD_ROOT, CLIPBOARD_KEEP_S))
        if mime is None:
            return False
        info = {"ws": self.pm.settings.workspace_id, "project": self.project.name,
                "local_only": self.project.local_only, "items": items, "cut": cut}
        mime.setData(MIME_CLIPBOARD, json.dumps(info).encode("utf-8"))
        QApplication.clipboard().setMimeData(mime)
        return True

    def paste_into(self, target: str) -> None:
        """Vložit (Ctrl+V) do složky target: soubory zkopírované v SPM nebo v Průzkumníku;
        vyjmuté v SPM se přesunou (ze stejného projektu přímo, z jiného kopií a smazáním)."""
        if self.project is None:
            return
        if not self.tree.can_drop():
            QMessageBox.information(self, "Vložit", "Vložit lze jen do aktivovaného projektu "
                                                    "nebo do složky „Jen místně“.")
            return
        if not self.ensure_active("vložit soubory"):
            return
        mime = QApplication.clipboard().mimeData()
        info = clipboard_info(mime)
        if info and info["cut"]:
            self._paste_cut(info, target)
            return
        urls = [Path(u.toLocalFile()) for u in mime.urls() if u.isLocalFile()] if mime else []
        same = bool(info and info["ws"] == self.pm.settings.workspace_id
                    and info.get("project") == self.project.name)
        # ze stejného projektu se čte aktuální obsah souborů, odjinud kopie ze schránky
        if urls or same:
            self.drop_files(urls, info["items"] if same else None, target)

    def _paste_cut(self, info: dict, target: str) -> None:
        title = "Vložit"
        name = self.project.name
        if info.get("folder"):  # vyjmuté v záložce Složka: zkopírovat sem, originály do Koše
            self._paste_cut_folder([Path(x) for x in info["paths"]], target)
            return
        moves = [(rel, target + out) for rel, out in info["items"]]
        if info["ws"] == self.pm.settings.workspace_id and info["project"] == name:
            try:
                moved, skipped = self.pm.move_local(name, moves)
            except Exception as e:  # noqa: BLE001
                QMessageBox.critical(self, title, str(e))
                self._changed()
                return
            if skipped:
                box = QMessageBox(QMessageBox.Icon.Warning, title,
                                  f"Přesunuto: {len(moved)}, nepřesunuto: {len(skipped)}.", parent=self)
                box.setDetailedText("\n".join(f"{rel} – {reason}" for rel, reason in skipped))
                box.exec()
            QApplication.clipboard().clear()
            self._changed()
            return
        # z jiného projektu: zkopírovat sem a ve zdrojovém projektu smazat (do Koše)
        source_pm = self.pm_for(info["ws"])
        src_name = info["project"]
        sources = {target + out: rel for rel, out in info["items"]}
        try:
            plan = self.pm.plan_copy_in(name, [(source_pm.file_source(src_name, rel), dst)
                                               for dst, rel in sources.items()])
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, title, str(e))
            return
        copied = self.copy_into(plan, target)
        if not copied:
            return
        by_target = {dst.lower(): rel for dst, rel in sources.items()}
        done = [by_target[c.lower()] for c in copied if c.lower() in by_target]
        try:
            if info.get("local_only"):
                source_pm.delete_local_only(src_name, done)
            else:
                source_pm.delete_local(src_name, done)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, title, f"Soubory se zkopírovaly, ale v projektu {src_name} "
                                             f"se nepodařilo smazat originály:\n{e}")
        QApplication.clipboard().clear()

    @staticmethod
    def url_sources(urls: list[Path], target: str) -> list[tuple[Path, str]]:
        """Soubory a složky ze systému -> (soubor, cílová cesta v projektu)."""
        sources = []
        for path in urls:
            if path.is_dir():
                sources += [(f, f"{target}{path.name}/{f.relative_to(path).as_posix()}")
                            for f in sorted(path.rglob("*")) if f.is_file()]
            elif path.is_file():
                sources.append((path, target + path.name))
        return sources

    # --- ZIP -----------------------------------------------------------------------

    def _path_taken(self, rel: str) -> bool:
        """Je cesta v projektu obsazená (soubor nebo složka – na serveru i v místní kopii)?"""
        low = rel.lower().rstrip("/")
        paths = {e.path.lower() for e in self.entries} | {f.lower() for f in self.empty_folders}
        local = self.pm.local_dir(self.project.name)
        return (low in paths or any(x.startswith(low + "/") for x in paths)
                or to_abs(local, rel.rstrip("/")).exists())

    def zip_selected(self, scopes: list[str]) -> None:
        """Zabalí označené soubory a složky do zipu (název viz zip_ops) v jejich společné
        nadřazené složce; zip je nový soubor místní kopie (na server s odevzdáním)."""
        if self.project is None or not self.context_actions(scopes)["zip"]:
            return
        title = "Zabalit do ZIP"
        base = zip_ops.zip_base(scopes)
        name = free_name(zip_ops.zip_name(scopes, self.project.name) + zip_ops.ZIP_EXT,
                         lambda n: self._path_taken(base + n))
        members = [(self.pm.file_source(self.project.name, e.path), e.path[len(base):])
                   for e in self.entries if in_scope(e.path, scopes) and not e.local_deleted]
        stage = new_staging_dir() / name
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            zip_ops.make_zip(members, stage)
        except (OSError, zip_ops.ZipError) as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, title, str(e))
            return
        QApplication.restoreOverrideCursor()
        self.drop_files([stage], None, base)  # do místní kopie podle pravidel kopírování

    def unzip(self, path: str) -> None:
        """Rozbalí zip do nové složky vedle něj (se stejným názvem jako zip)."""
        if self.project is None or not self.context_actions(path)["unzip"]:
            return
        title = "Rozbalit ZIP"
        base = zip_ops._parent(path)
        folder = free_name(zip_ops.unzip_folder_name(path), lambda n: self._path_taken(base + n),
                           is_dir=True)
        stage = new_staging_dir() / folder
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            zip_ops.extract_zip(to_abs(self.pm.local_dir(self.project.name), path), stage)
        except (OSError, zip_ops.ZipError) as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, title, str(e))
            return
        QApplication.restoreOverrideCursor()
        if not any(stage.rglob("*")):
            QMessageBox.information(self, title, "Zip je prázdný.")
            return
        self.drop_files([stage], None, base)

    def _duplicate_folders(self, items: list[tuple[str, str]], target: str) -> list[tuple[str, str]]:
        """Složka vložená na své vlastní místo (Ctrl+C, Ctrl+V) = duplikát celé složky
        s _copy v názvu (Vykresy -> Vykresy_copy), ne _copy u každého souboru v ní."""
        local = self.pm.local_dir(self.project.name) if self.project else None
        paths = {e.path.lower() for e in self.entries} | {f.lower() for f in self.empty_folders}

        def taken(rel: str) -> bool:
            low = rel.lower()
            return (low in paths or any(p.startswith(low + "/") for p in paths)
                    or (local is not None and to_abs(local, rel).exists()))
        renamed: dict[str, str] = {}
        result = []
        for rel, out in items:
            top, sep, rest = out.partition("/")
            if sep:  # soubor ve vytahované složce
                source_top = rel[:len(rel) - len(out) + len(top)]
                if (target + top).lower() == source_top.lower():
                    if top not in renamed:
                        renamed[top] = free_copy_name(top, lambda n: taken(target + n), is_dir=True)
                    out = f"{renamed[top]}/{rest}"
            result.append((rel, out))
        return result

    def drop_files(self, urls: list[Path], internal: list[tuple[str, str]] | None, target: str) -> None:
        """Puštění do stromu: soubory a složky ze systému (urls), nebo položky z tohoto stromu
        (internal = (cesta v projektu, cesta v cíli)); target = cílová složka v projektu."""
        if self.project is None or not self.ensure_active("vložit soubory"):
            return
        name = self.project.name
        if internal is not None:
            internal = self._duplicate_folders(internal, target)
            sources = [(self.pm.file_source(name, rel), target + out) for rel, out in internal]
        else:
            sources = self.url_sources(urls, target)
        try:
            plan = self.pm.plan_copy_in(name, sources)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Kopírování", str(e))
            return
        self.copy_into(plan, target)

    def _paste_cut_folder(self, paths: list[Path], target: str) -> None:
        sources = self.url_sources(paths, target)
        try:
            plan = self.pm.plan_copy_in(self.project.name, sources)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Vložit", str(e))
            return
        copied = self.copy_into(plan, target)
        if not copied:
            return
        done = {c.lower() for c in copied}
        originals = [src for src, rel in sources if rel.strip("/").lower() in done]
        try:
            recycle.to_recycle_bin(originals)
            for folder in (p for p in paths if p.is_dir()):  # vyprázdněné složky
                if not any(f.is_file() for f in folder.rglob("*")):
                    recycle.to_recycle_bin([folder])
        except OSError as e:
            QMessageBox.warning(self, "Vložit", f"Soubory se zkopírovaly, ale originály se "
                                                f"nepodařilo přesunout do Koše:\n{e}")
        QApplication.clipboard().clear()

    def copy_into(self, plan: list[CopyItem], target: str) -> list[str] | None:
        """Potvrzení (když se něco nahrazuje, přebírá nebo přeskakuje) a kopírování do místní
        kopie. Přeskočené soubory (drží je jiný / spravuje zrcadlo) lze vložit jako kopie
        (soubor_copy.ext). Vrací zkopírované cesty (None = zrušeno nebo chyba)."""
        adds = [i for i in plan if i.action == COPY_ADD]
        replaces = [i for i in plan if i.action == COPY_REPLACE]
        takes = [i for i in plan if i.action == COPY_TAKE]
        skips = [i for i in plan if i.action == COPY_SKIP]
        copyable = [i for i in skips if i.copy_rel]
        where = f"do složky {target.rstrip('/')}" if target else "do kořene projektu"
        details = "\n".join(
            [f"+ {i.rel}" for i in adds] + [f"* {i.rel} (nahradí se)" for i in replaces]
            + [f"* {i.rel} (převezme se a nahradí)" for i in takes]
            + [f"- {i.rel} – {i.reason}" + (f" (lze vložit jako {i.copy_rel})" if i.copy_rel else "")
               for i in skips])
        Btn = QMessageBox.StandardButton
        if not adds and not replaces and not takes and not copyable:
            box = QMessageBox(QMessageBox.Icon.Information, "Kopírování",
                              f"Nic se nezkopírovalo {where} – žádný soubor nelze přidat ani nahradit.",
                              parent=self)
            box.setDetailedText(details)
            box.exec()
            return None
        if replaces or takes or skips:
            if adds or replaces or takes:
                text = (f"Kopírování {where}:\n\nnové soubory: {len(adds)}\n"
                        f"nahradí se (máte je aktivované): {len(replaces)}\n"
                        + (f"převezmou se a nahradí (jsou volné): {len(takes)}\n" if takes else "")
                        + f"přeskočí se: {len(skips)}"
                        + (f" (z toho {len(copyable)} drží jiný uživatel nebo zrcadlo – lze je vložit "
                           "jako kopie)" if copyable else "")
                        + ("\n\nNahrazené soubory se na serveru změní až při odevzdání."
                           if replaces or takes else "")
                        + "\n\nPokračovat?")
                buttons = Btn.Yes | Btn.No | (Btn.YesToAll if copyable else Btn.NoButton)
            else:  # nic nejde přidat ani nahradit – jen vložit jako kopie
                text = (f"Soubory {where} nelze nahradit – drží je jiný uživatel nebo je spravuje "
                        f"zrcadlo ({len(copyable)}).\n\nVložit je jako kopie (s _copy v názvu)?")
                buttons = Btn.YesToAll | Btn.No
            box = QMessageBox(QMessageBox.Icon.Question, "Kopírování", text, buttons, self)
            box.button(Btn.No).setText("Zrušit")
            if box.button(Btn.Yes) is not None:
                box.button(Btn.Yes).setText("Pokračovat" if copyable else "Ano")
            if box.button(Btn.YesToAll) is not None:
                box.button(Btn.YesToAll).setText(
                    "Vložit jako kopie" if not (adds or replaces or takes)
                    else "Pokračovat a přeskočené vložit jako kopie")
            box.setDetailedText(details)
            answer = box.exec()
            if answer == Btn.YesToAll:
                plan = [as_copy(i) if i.action == COPY_SKIP and i.copy_rel else i for i in plan]
            elif answer != Btn.Yes:
                return None
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        copied = None
        try:
            copied = self.pm.copy_in(self.project.name, plan)
        except Exception as e:  # noqa: BLE001
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "Kopírování", str(e))
        else:
            QApplication.restoreOverrideCursor()
        self._changed()
        return copied

    def deletable(self, scope: str | list[str]) -> list[str]:
        """Soubory v rozsahu, které smím smazat: zamčené mnou nebo moje nové (a ještě nesmazané);
        u složky „Jen místně“ všechny."""
        return [e.path for e in self.entries if in_scope(e.path, scope)
                and (e.mine or e.local_new) and not e.local_deleted]

    def _delete(self, scope: str | list[str]) -> None:
        scopes = [scope] if isinstance(scope, str) else list(scope)
        if self.project is not None and not self.deletable(scopes) and scopes and \
                all(s.endswith("/") and self.is_empty_folder(s) for s in scopes):
            names = ", ".join(s.rstrip("/") for s in scopes)
            if QMessageBox.question(self, "Smazat", f"Smazat prázdnou složku {names}?") \
                    != QMessageBox.StandardButton.Yes:
                return
            try:
                for s in scopes:
                    self.pm.remove_empty_local_folder(self.project.name, s)
            except Exception as e:  # noqa: BLE001
                QMessageBox.critical(self, "Smazat", str(e))
            self._changed()
            return
        paths = self.deletable(scope)
        others = [e.path for e in self.entries if in_scope(e.path, scope)
                  and not (e.mine or e.local_new) and not e.local_deleted]
        if not paths or self.project is None:
            return
        lines = "\n".join(paths[:15]) + ("\n…" if len(paths) > 15 else "")
        if self.project.local_only:
            text = f"Přesunout do Koše {len(paths)} souborů z místní složky?\n\n{lines}"
        else:
            text = (f"Přesunout do Koše {len(paths)} souborů z vaší místní kopie?\n\n{lines}\n\n"
                    "Na serveru se smažou až při odevzdání (a i tam se jen přesunou do koše projektu).")
        if others:
            text += (f"\n\n{len(others)} dalších označených souborů nemáte zamčených – ty zůstanou.")
        answer = QMessageBox.question(self, "Smazat", text)
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            if self.project.local_only:
                self.pm.delete_local_only(self.project.name, paths)
            else:
                self.pm.delete_local(self.project.name, paths)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Smazat", str(e))
        self._changed()

    def _context_menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        if self.project is None:
            return
        if item is None:  # prázdné místo stromu: složka v hlavní složce projektu
            menu = QMenu(self)
            act = menu.addAction("Nová složka…")
            act.setEnabled(self.context_actions([])["new_folder"])
            act_paste = menu.addAction("Vložit\tCtrl+V")
            act_paste.setEnabled(self.can_paste())
            chosen = menu.exec(self.tree.viewport().mapToGlobal(pos))
            if chosen is act:
                self.create_folder("")
            elif chosen is act_paste:
                self.paste_into("")
            return
        # akce platí pro všechny označené položky (Ctrl / Shift); klepnutí mimo výběr = jen tato
        scopes = self.selected_scopes() if item.isSelected() else [item.data(0, PATH_ROLE)]
        enabled = self.context_actions(scopes)
        many = f" ({len(scopes)} označených)" if len(scopes) > 1 else ""
        menu = QMenu(self)
        act_open = menu.addAction("Otevřít" + many)
        act_open.setEnabled(enabled["open"])
        menu.addSeparator()
        act_activate = act_submit = act_take = act_publish = None
        if self.project.local_only:
            act_publish = menu.addAction("Publikovat…" + many)
            act_publish.setEnabled(enabled["publish"])
        elif not self.project.mine:
            act_activate = menu.addAction("Aktivovat…" + many)
            act_activate.setEnabled(enabled["activate"])
        else:
            act_submit = menu.addAction("Odevzdat…" + many)
            act_submit.setEnabled(enabled["submit"])
            act_take = menu.addAction("Převzít…" + many)
            act_take.setEnabled(enabled["take"])
        menu.addSeparator()
        act_copy = menu.addAction("Kopírovat" + many + "\tCtrl+C")
        act_cut = menu.addAction("Vyjmout" + many + "\tCtrl+X")
        act_cut.setEnabled(self.can_cut(scopes))
        act_paste = menu.addAction("Vložit\tCtrl+V")
        act_paste.setEnabled(self.can_paste())
        menu.addSeparator()
        act_new_folder = menu.addAction("Nová složka…")
        act_new_folder.setEnabled(enabled["new_folder"])
        act_rename = menu.addAction("Přejmenovat…\tF2")
        act_rename.setEnabled(enabled["rename"])
        act_delete = menu.addAction("Smazat… (do Koše)" + many + "\tDel")
        act_delete.setEnabled(enabled["delete"])
        menu.addSeparator()
        act_zip = menu.addAction("Zabalit do ZIP" + many)
        act_zip.setEnabled(enabled["zip"])
        act_unzip = None
        if zip_ops.is_zip(scopes[0]) and len(scopes) == 1:
            act_unzip = menu.addAction("Rozbalit ZIP")
            act_unzip.setEnabled(enabled["unzip"])
        act_note = act_release = None
        if enabled["release_mirror"]:
            menu.addSeparator()
            act_release = menu.addAction("Uvolnit ze zrcadla…" + many)
        act_history = None
        if not self.project.local_only:
            menu.addSeparator()
            act_note = menu.addAction("Přidat poznámku…" + many + "\tCtrl+N")
            act_history = menu.addAction("Historie souboru…")
            act_history.setEnabled(enabled["history"])
        if not self.project.mine and not self.project.local_only:
            menu.addSeparator()
            note = menu.addAction("Odevzdat a převzít lze až u aktivovaného projektu")
            note.setEnabled(False)
        chosen = menu.exec(self.tree.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen is act_open:
            self._open([s for s in scopes if not s.endswith("/")])
        elif chosen is act_activate:
            self.activate_requested.emit(scopes)
        elif chosen is act_publish:
            self.publish_requested.emit(scopes)
        elif chosen is act_new_folder:
            clicked = item.data(0, PATH_ROLE)  # složka -> podsložka, soubor -> vedle něj
            self.create_folder(clicked if clicked.endswith("/") else
                               (clicked.rpartition("/")[0] + "/" if "/" in clicked else ""))
        elif chosen is act_copy:
            self.copy_selected(scopes)
        elif chosen is act_cut:
            self.copy_selected(scopes, cut=True)
        elif chosen is act_paste:
            clicked = item.data(0, PATH_ROLE)  # do složky, na soubor = vedle něj
            self.paste_into(clicked if clicked.endswith("/") else
                            (clicked.rpartition("/")[0] + "/" if "/" in clicked else ""))
        elif chosen is act_rename:
            self._rename(scopes[0])
        elif chosen is act_submit:
            self.submit_requested.emit(scopes)
        elif chosen is act_take:
            self.take_requested.emit(scopes)
        elif chosen is act_delete:
            self._delete(scopes)
        elif chosen is act_note:
            self.note_requested.emit(scopes)
        elif chosen is act_history:
            self.history_requested.emit(scopes[0])
        elif chosen is act_release:
            self.release_mirror(scopes)
        elif chosen is act_zip:
            self.zip_selected(scopes)
        elif chosen is act_unzip:
            self.unzip(scopes[0])

    OPEN_CONFIRM_OVER = 10  # při otevírání víc souborů najednou se nejdřív zeptat

    def _open(self, paths: list[str]) -> None:
        """Otevře soubory z místní kopie (ty, které v ní jsou)."""
        entries = [e for e in self.entries if e.path in paths and e.in_local]
        if len(entries) > self.OPEN_CONFIRM_OVER:
            answer = QMessageBox.question(self, "Otevřít soubory",
                                          f"Otevřít najednou {len(entries)} souborů?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        for e in entries:
            if not self._open_file(e.path):
                break

    def _on_double_click(self, item: QTreeWidgetItem, _column: int) -> None:
        """Dvojklik na soubor, který je v místní kopii, ho otevře výchozí aplikací Windows
        (jako dvojklik v Průzkumníku). Soubory, které nemám zamčené, jsou jen pro čtení."""
        path = item.data(0, KEY_ROLE)
        if path:
            self._open_file(path)

    def _open_file(self, path: str) -> bool:
        """Otevře jeden soubor z místní kopie; False = nepodařilo se (chyba už ohlášena)."""
        entry = next((e for e in self.entries if e.path == path), None)
        if self.project is None or entry is None or not entry.in_local:
            return True
        local = to_abs(self.pm.local_dir(self.project.name), path)
        if not local.exists():
            QMessageBox.warning(self, "Otevřít soubor",
                                f"Soubor v místní kopii neexistuje (byl přejmenován nebo smazán?):\n{local}")
            return False
        try:
            os.startfile(local)
        except OSError as e:
            QMessageBox.critical(self, "Otevřít soubor", f"Soubor nelze otevřít:\n{local}\n\n{e}")
            return False
        return True

    def stop(self) -> None:
        """Volá se při zavírání okna: počká na načítání a další výsledky zahodí."""
        self._closed = True
        self._again = False
        if self._thread:
            self._thread.wait(5000)
            try:
                self._thread.done.disconnect()  # všechna připojení (výsledek jde přes lambda)
            except (RuntimeError, TypeError):
                pass
