"""Panel „Stav projektu“ – strom souborů aktivovaného projektu a kdo který soubor drží.

Zobrazuje se pod seznamem projektů jen pro projekty, které má někdo aktivované.
Strom se načítá na pozadí (prochází složku na serveru a čte databázi projektu).
"""

from __future__ import annotations

from collections import Counter

from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ..scanner import to_abs
from ..sync import FileEntry, ProjectManager, ProjectStatus
from .check_tree import CheckTree, TreeEntry

COLOR_MINE = QColor("#2e8b57")
COLOR_OTHER = QColor("#c0392b")


class EntriesThread(QThread):
    done = Signal(str, list, str)  # projekt, položky, chyba

    def __init__(self, pm: ProjectManager, name: str):
        super().__init__()
        self.pm, self.name = pm, name

    def run(self) -> None:
        try:
            self.done.emit(self.name, self.pm.file_entries(self.name), "")
        except Exception as e:  # noqa: BLE001 – zobrazí se v panelu
            self.done.emit(self.name, [], str(e) or type(e).__name__)


class ProjectPanel(QWidget):
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
        self._thread = EntriesThread(self.pm, self.project.name)
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
        shown = [e for e in self.entries if e.owner or not self.only_locked.isChecked()]
        self.tree.set_entries([
            TreeEntry(e.path, e.path,
                      ["" if e.free else ("já" if e.mine else e.owner.label)],
                      color=None if e.free else (COLOR_MINE if e.mine else COLOR_OTHER),
                      file_path=str(to_abs(root, e.path)))
            for e in shown
        ])
        counts = Counter("já" if e.mine else e.owner.label for e in self.entries if e.owner)
        free = sum(1 for e in self.entries if e.free)
        parts = [f"{who}: <b>{n}</b>" for who, n in sorted(counts.items())]
        users = ", ".join(u.label for u in self.project.users)
        self.summary.setText(
            f"Aktivováno: {users}<br>Zamčené soubory – " + (", ".join(parts) or "žádné")
            + f" &nbsp;|&nbsp; volné: <b>{free}</b> &nbsp;|&nbsp; celkem: {len(self.entries)}")

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
