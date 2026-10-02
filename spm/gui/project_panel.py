"""Panel „Stav projektu“ – strom souborů aktivovaného projektu a kdo který soubor drží.

Zobrazuje se pod seznamem projektů jen pro projekty, které má někdo aktivované.
Strom se načítá na pozadí (prochází složku na serveru a čte databázi projektu).
U mého aktivovaného projektu obsahuje i nové soubory z místní kopie, které ještě nejsou
odevzdané (šedě).
"""

from __future__ import annotations

import os
from collections import Counter

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..scanner import to_abs
from ..sync import FileEntry, ProjectManager, ProjectStatus
from .check_tree import KEY_ROLE, PATH_ROLE, CheckTree, TreeEntry, in_scope

COLOR_MINE = QColor("#2e8b57")
COLOR_OTHER = QColor("#c0392b")


class EntriesThread(QThread):
    done = Signal(str, list, str)  # projekt, položky, chyba

    def __init__(self, pm: ProjectManager, name: str, mine: bool):
        super().__init__()
        self.pm, self.name, self.mine = pm, name, mine

    def run(self) -> None:
        try:
            entries = self.pm.file_entries(self.name)
            if self.mine:  # u mého projektu i nové a smazané soubory z místní kopie
                deleted = set(self.pm.local_deleted_files(self.name))
                for e in entries:
                    e.local_deleted = e.mine and e.path in deleted
                on_server = {e.path.lower() for e in entries}
                entries += [FileEntry(p, None, False, local_new=True)
                            for p in self.pm.local_new_files(self.name) if p.lower() not in on_server]
            local = self.pm.local_dir(self.name)
            if local.is_dir():  # co je v místní kopii, lze otevřít (cizí / volné jen pro čtení)
                for e in entries:
                    e.in_local = to_abs(local, e.path).is_file()
            self.done.emit(self.name, entries, "")
        except Exception as e:  # noqa: BLE001 – zobrazí se v panelu
            self.done.emit(self.name, [], str(e) or type(e).__name__)


class ProjectPanel(QWidget):
    # pravé tlačítko ve stromu: soubor nebo složka (zakončená '/'), která se má předvybrat
    submit_requested = Signal(str)
    take_requested = Signal(str)

    def __init__(self, pm: ProjectManager, parent: QWidget | None = None):
        super().__init__(parent)
        self.pm = pm
        self.project: ProjectStatus | None = None
        self.entries: list[FileEntry] = []
        self._thread: EntriesThread | None = None
        self._again = False
        self._closed = False  # okno se zavírá – výsledky z pozadí už nezobrazovat

        self.title = QLabel()
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.only_locked = QCheckBox("Jen zamčené soubory")
        self.only_locked.toggled.connect(self._fill)
        head = QHBoxLayout()
        head.addWidget(self.title)
        head.addStretch()
        head.addWidget(self.only_locked)

        self.tree = CheckTree(["Soubor", "Drží"], checkable=False)
        self.tree.itemDoubleClicked.connect(self._on_double_click)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.addLayout(head)
        layout.addWidget(self.summary)
        layout.addWidget(self.tree)

    def show_project(self, project: ProjectStatus | None) -> None:
        """Zobrazí stav projektu; pro projekt, který nikdo nemá aktivovaný, se panel skryje."""
        if project is None or not project.active or project.local_only:
            self.project = None
            self.hide()
            return
        same = self.project is not None and self.project.name == project.name
        self.project = project
        self.title.setText(f"<b>Stav projektu {project.name}</b>")
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
        self._thread = EntriesThread(self.pm, self.project.name, self.project.mine)
        self._thread.done.connect(self._on_loaded)
        self._thread.start()

    def _on_loaded(self, name: str, entries: list, error: str) -> None:
        if self._closed:
            return
        if self._again:
            self.reload()
        if self.project is None or name != self.project.name:
            return  # mezitím vybrán jiný projekt
        if error:
            self.summary.setText(f"Stav projektu nelze načíst: {error}")
            return
        self.entries = entries
        self._fill()

    def _fill(self) -> None:
        if self.project is None:
            return
        root = self.pm.server_dir(self.project.name)
        local = self.pm.local_dir(self.project.name)
        color_new = self.palette().color(QPalette.ColorRole.PlaceholderText)

        def tree_entry(e: FileEntry) -> TreeEntry:
            if e.local_deleted:  # šedě = můj soubor smazaný v místní kopii, ještě neodevzdaný
                return TreeEntry(e.path, e.path, ["smazán – neodevzdaný"], color=color_new,
                                 color_name=True, file_path=str(to_abs(root, e.path)),
                                 tooltip="Soubor je v Koši Windows. Na serveru se smaže až při "
                                         "odevzdání (do koše projektu).")
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
            return TreeEntry(e.path, e.path,
                             ["" if e.free else ("já" if e.mine else e.owner.label)],
                             color=None if e.free else (COLOR_MINE if e.mine else COLOR_OTHER),
                             color_name=True,  # zelená = drží ho já, červená = drží ho jiný
                             file_path=str(to_abs(root, e.path)), tooltip=tip)

        shown = [e for e in self.entries
                 if e.owner or e.local_new or not self.only_locked.isChecked()]
        self.tree.set_entries([tree_entry(e) for e in shown])
        counts = Counter("já" if e.mine else e.owner.label for e in self.entries if e.owner)
        free = sum(1 for e in self.entries if e.free)
        new = sum(1 for e in self.entries if e.local_new)
        parts = [f"{who}: <b>{n}</b>" for who, n in sorted(counts.items())]
        users = ", ".join(u.label for u in self.project.users)
        self.summary.setText(
            f"Aktivováno: {users}<br>Zamčené soubory – " + (", ".join(parts) or "žádné")
            + f" &nbsp;|&nbsp; volné: <b>{free}</b>"
            + (f" &nbsp;|&nbsp; nové neodevzdané: <b>{new}</b>" if new else "")
            + f" &nbsp;|&nbsp; na serveru celkem: {len(self.entries) - new}")

    def context_actions(self, scope: str) -> dict[str, bool]:
        """Které akce kontextového menu jsou pro soubor / složku dostupné."""
        mine_project = bool(self.project and self.project.mine)
        inside = [e for e in self.entries if in_scope(e.path, scope)]
        own = any(e.mine or e.local_new for e in inside)  # zamčené mnou nebo moje nové
        return {
            # otevřít jde jakýkoliv soubor, který je v místní kopii (cizí a volné jen pro čtení)
            "open": not scope.endswith("/") and any(e.in_local for e in inside),
            # u složky vždy – dialog odevzdání ukáže, co v ní skutečně je
            "submit": mine_project and (scope.endswith("/") or own),
            "take": mine_project and any(e.free for e in inside),
            "delete": mine_project and bool(self.deletable(scope)),
        }

    def deletable(self, scope: str) -> list[str]:
        """Soubory v rozsahu, které smím smazat: zamčené mnou nebo moje nové (a ještě nesmazané)."""
        return [e.path for e in self.entries if in_scope(e.path, scope)
                and (e.mine or e.local_new) and not e.local_deleted]

    def _delete(self, scope: str) -> None:
        paths = self.deletable(scope)
        others = [e.path for e in self.entries if in_scope(e.path, scope)
                  and not (e.mine or e.local_new) and not e.local_deleted]
        if not paths or self.project is None:
            return
        lines = "\n".join(paths[:15]) + ("\n…" if len(paths) > 15 else "")
        text = (f"Přesunout do Koše {len(paths)} souborů z vaší místní kopie?\n\n{lines}\n\n"
                "Na serveru se smažou až při odevzdání (a i tam se jen přesunou do koše projektu).")
        if others:
            text += (f"\n\n{len(others)} dalších souborů ve složce nemáte zamčených – ty zůstanou.")
        answer = QMessageBox.question(self, "Smazat", text)
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.pm.delete_local(self.project.name, paths)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Smazat", str(e))
        self.reload()

    def _context_menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        if item is None or self.project is None:
            return
        scope = item.data(0, PATH_ROLE)
        enabled = self.context_actions(scope)
        menu = QMenu(self)
        act_open = menu.addAction("Otevřít")
        act_open.setEnabled(enabled["open"])
        menu.addSeparator()
        act_submit = menu.addAction("Odevzdat…")
        act_submit.setEnabled(enabled["submit"])
        act_take = menu.addAction("Převzít…")
        act_take.setEnabled(enabled["take"])
        menu.addSeparator()
        act_delete = menu.addAction("Smazat… (do Koše)")
        act_delete.setEnabled(enabled["delete"])
        if not self.project.mine:
            menu.addSeparator()
            note = menu.addAction("Pro odevzdání a převzetí projekt nejdřív aktivujte")
            note.setEnabled(False)
        chosen = menu.exec(self.tree.viewport().mapToGlobal(pos))
        if chosen is act_open:
            self._on_double_click(item, 0)
        elif chosen is act_submit:
            self.submit_requested.emit(scope)
        elif chosen is act_take:
            self.take_requested.emit(scope)
        elif chosen is act_delete:
            self._delete(scope)

    def _on_double_click(self, item: QTreeWidgetItem, _column: int) -> None:
        """Dvojklik na soubor, který je v místní kopii, ho otevře výchozí aplikací Windows
        (jako dvojklik v Průzkumníku). Soubory, které nemám zamčené, jsou jen pro čtení."""
        path = item.data(0, KEY_ROLE)
        entry = next((e for e in self.entries if e.path == path), None)
        if self.project is None or entry is None or not entry.in_local:
            return
        local = to_abs(self.pm.local_dir(self.project.name), path)
        if not local.exists():
            QMessageBox.warning(self, "Otevřít soubor",
                                f"Soubor v místní kopii neexistuje (byl přejmenován nebo smazán?):\n{local}")
            return
        try:
            os.startfile(local)
        except OSError as e:
            QMessageBox.critical(self, "Otevřít soubor", f"Soubor nelze otevřít:\n{local}\n\n{e}")

    def stop(self) -> None:
        """Volá se při zavírání okna: počká na načítání a další výsledky zahodí."""
        self._closed = True
        self._again = False
        if self._thread:
            self._thread.wait(5000)
            try:
                self._thread.done.disconnect(self._on_loaded)
            except (RuntimeError, TypeError):
                pass
