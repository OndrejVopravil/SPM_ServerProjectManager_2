"""Panel „Stav projektu“ – strom souborů aktivovaného projektu a kdo který soubor drží.

Zobrazuje se pod seznamem projektů jen pro projekty, které má někdo aktivované.
Strom se načítá na pozadí (prochází složku na serveru a čte databázi projektu).
U mého aktivovaného projektu obsahuje i nové soubory z místní kopie, které ještě nejsou
odevzdané (šedě).
"""

from __future__ import annotations

import os
from collections import Counter
from typing import Callable

from PySide6.QtCore import QPointF, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QKeySequence, QPainter, QPalette, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
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

from ..scanner import to_abs
from ..sync import FileEntry, ProjectManager, ProjectStatus, validate_file_name
from .check_tree import KEY_ROLE, PATH_ROLE, CheckTree, TreeEntry, in_scope
from .search import wildcard_matcher

COLOR_MINE = QColor("#2e8b57")
COLOR_MIRROR = QColor("#8e44ad")  # spravuje zrcadlo z jiného serveru (jen pro čtení)
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

    def run(self) -> None:
        try:
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
    publish_requested = Signal(object)   # složka „Jen místně“

    def __init__(self, pm: ProjectManager, parent: QWidget | None = None):
        super().__init__(parent)
        self.pm = pm
        self.project: ProjectStatus | None = None
        self.entries: list[FileEntry] = []
        self.open_notes: list = []  # otevřené poznámky projektu (sloupec Poznámky)
        self._thread: EntriesThread | None = None
        self._again = False
        self._closed = False  # okno se zavírá – výsledky z pozadí už nezobrazovat
        # popis zdroje zrcadla (cesta zdrojového projektu -> např. název záložky)
        self.mirror_source_label: Callable[[FileEntry], str] = lambda entry: entry.mirror or ""
        # popis cíle zrcadla (db.MirrorTarget -> např. název záložky)
        self.mirror_target_label: Callable[[object], str] = lambda target: target.label
        # lze vybraný projekt aktivovat? (nastaví hlavní okno – např. nepovolené znaky)
        self.can_activate: Callable[[], bool] = lambda: False

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

        self.tree = CheckTree(["Soubor", "Drží", "Zrcadlo", "Poznámky"], checkable=False)
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
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.addLayout(head)
        layout.addWidget(self.summary)
        layout.addWidget(self.tree)

    def show_project(self, project: ProjectStatus | None) -> None:
        """Zobrazí strom vybraného projektu (jakéhokoliv – volného, aktivovaného i složky
        „Jen místně“); bez vybraného projektu se panel skryje."""
        if project is None:
            self.project = None
            self.hide()
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
        self.show()
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
            name, entries, error, thread.pm, thread.open_notes))
        self._thread = thread
        thread.start()

    def _on_loaded(self, name: str, entries: list, error: str, pm: ProjectManager | None = None,
                   open_notes: list | None = None) -> None:
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
        self._fill()

    def _fill(self) -> None:
        if self.project is None:
            return
        root = self.pm.server_dir(self.project.name)
        local = self.pm.local_dir(self.project.name)
        color_new = self.palette().color(QPalette.ColorRole.PlaceholderText)

        if self.project.local_only:
            self.tree.set_entries([TreeEntry(e.path, e.path, [""], file_path=str(to_abs(local, e.path)),
                                             tooltip="Dvojklikem otevřete")
                                   for e in self.entries])
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
                return TreeEntry(e.path, e.path, [f"přejmenován z {e.renamed_from} – neodevzdaný"],
                                 color=color_new, color_name=True, file_path=str(to_abs(local, e.path)),
                                 tooltip="Přejmenováno ve vaší místní kopii; na serveru se přejmenuje "
                                         "při odevzdání.\nDvojklikem otevřete.")
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
            return TreeEntry(e.path, e.path, [e.holder],
                             color=None if e.free else (COLOR_MINE if e.mine else
                                                        COLOR_MIRROR if e.mirror else COLOR_OTHER),
                             # zelená = drží ho já, červená = drží ho jiný, fialová = zrcadlo
                             color_name=True,
                             file_path=str(to_abs(root, e.path)), tooltip=tip)

        shown = [e for e in self.entries
                 if e.owner or e.local_new or not self.only_locked.isChecked()]
        self.tree.set_entries([tree_entry(e) for e in shown])
        counts = Counter("já" if e.mine else e.owner.label for e in self.entries if e.owner)
        free = sum(1 for e in self.entries if e.free)
        new = sum(1 for e in self.entries if e.local_new)
        mirrored = sum(1 for e in self.entries if e.mirror and not e.owner)
        parts = [f"{who}: <b>{n}</b>" for who, n in sorted(counts.items())]
        users = ", ".join(u.label for u in self.project.users) or "nikdo (projekt je volný)"
        self.summary.setText(
            f"Aktivováno: {users}<br>Zamčené soubory – " + (", ".join(parts) or "žádné")
            + f" &nbsp;|&nbsp; volné: <b>{free}</b>"
            + (f" &nbsp;|&nbsp; zrcadlo: <b>{mirrored}</b>" if mirrored else "")
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
            texts[path] = (f"📝 {len(notes)}", tip + "\n\n(celé poznámky: okno Poznámky)")
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
            if e.mirror:
                source = self.mirror_source_label(e)
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
            texts[e.path] = (", ".join(labels), "\n".join(tips))
            parts = e.path.split("/")[:-1]
            for i in range(1, len(parts) + 1):
                folders.setdefault("/".join(parts[:i]) + "/", Counter()).update(labels)
        for folder, counter in folders.items():
            texts[folder] = (", ".join(f"{label} ({n})" for label, n in sorted(counter.items())),
                             "Počet souborů ve složce: " + ", ".join(
                                 f"{label}: {n}" for label, n in sorted(counter.items())))
        self.tree.set_column_texts(2, texts, COLOR_MIRROR)
        self._mark_notes()
        self._apply_search()  # strom je nově naplněný – znovu podbarvit nalezené
        if not outgoing:
            return ""
        return " &nbsp;|&nbsp; zrcadleno: " + ", ".join(
            f"<b>{n}</b> → {name}" for name, n in sorted(outgoing.items()))

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
            "delete": (local_only or mine_project) and bool(self.deletable(scopes)),
            # přejmenovat: jen jeden soubor, a to můj (zamčený / nový) a v místní kopii
            "rename": (single_file and (local_only or mine_project)
                       and any((e.mine or e.local_new) and e.in_local and not e.local_deleted
                               for e in inside)),
        }

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
        self.reload()

    def deletable(self, scope: str | list[str]) -> list[str]:
        """Soubory v rozsahu, které smím smazat: zamčené mnou nebo moje nové (a ještě nesmazané);
        u složky „Jen místně“ všechny."""
        return [e.path for e in self.entries if in_scope(e.path, scope)
                and (e.mine or e.local_new) and not e.local_deleted]

    def _delete(self, scope: str | list[str]) -> None:
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
        self.reload()

    def _context_menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        if item is None or self.project is None:
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
        act_rename = menu.addAction("Přejmenovat…\tF2")
        act_rename.setEnabled(enabled["rename"])
        act_delete = menu.addAction("Smazat… (do Koše)" + many + "\tDel")
        act_delete.setEnabled(enabled["delete"])
        act_note = None
        if not self.project.local_only:
            menu.addSeparator()
            act_note = menu.addAction("Přidat poznámku…" + many)
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
