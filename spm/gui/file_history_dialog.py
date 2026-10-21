"""Historie jednoho souboru: každá verze (kdo, kdy, komentář), zda je k dispozici, otevření
staré verze (jen pro čtení) a obnovení verze – do místní kopie (soubor mám aktivovaný), jinak
do zvolené složky."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import db as dbm
from ..folder_ops import free_name
from ..restore import FileVersion
from ..scanner import set_readonly
from ..sync import ProjectManager
from .project_tree import new_staging_dir
from .timeline_style import (
    CHANGE_STYLE,
    COLOR_FULL,
    COLOR_NONE,
    date_time,
    dot_icon,
    fmt_size,
    user_color,
)

COLUMNS = ["", "Datum", "Kdo", "Změna", "Velikost", "Komentář"]


def version_file_name(path: str, version: FileVersion) -> str:
    """Název kopie staré verze: deska_verze_20261018_0905.elt."""
    name = path.rpartition("/")[2]
    try:
        stamp = datetime.fromisoformat(version.created_at).strftime("%Y%m%d_%H%M")
    except ValueError:
        stamp = str(version.commit_id)
    stem, dot, ext = name.rpartition(".")
    return f"{stem}_verze_{stamp}.{ext}" if dot and stem else f"{name}_verze_{stamp}"


class FileHistoryDialog(QDialog):
    def __init__(self, pm: ProjectManager, project: str, path: str, local_target: Path | None,
                 parent: QWidget | None = None):
        """local_target: soubor v místní kopii, do kterého lze verzi obnovit (soubor mám
        aktivovaný); None = obnovit jen do zvolené složky."""
        super().__init__(parent)
        self.pm, self.project, self.path, self.local_target = pm, project, path, local_target
        self.setWindowTitle(f"Historie souboru {path}")
        self.resize(900, 460)
        self.versions = pm.file_versions(project, path)

        head = QLabel(f"<b>{path}</b> – projekt {project}<br>"
                      "<span style='color:#2e8b57'>●</span> verze je k dispozici (lze otevřít a obnovit) "
                      "&nbsp; <span style='color:#9e9e9e'>○</span> verze už není k dispozici")
        head.setTextFormat(Qt.TextFormat.RichText)
        self.table = QTableWidget(len(self.versions), len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        for r, v in enumerate(self.versions):
            mark, color = CHANGE_STYLE.get(v.change_type, ("", "#000000"))
            label = dbm.CHANGE_LABELS.get(v.change_type, v.change_type)
            values = ["", date_time(v.created_at), v.user, f"{mark} {label}".strip(), fmt_size(v.size),
                      v.comment.replace("\n", " ↵ ")]
            tip = ("Smazán – žádný obsah" if v.removed else
                   ("K dispozici: " + ", ".join("aktuální soubor na serveru" if b.current else b.path
                                                for b in v.sources)) if v.available
                   else f"Není k dispozici – {v.reason}")
            for c, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setToolTip(tip if c in (0, 3) else (v.comment if c == 5 else text))
                if c == 0 and not v.removed:
                    item.setIcon(dot_icon(COLOR_FULL if v.available else COLOR_NONE, hollow=not v.available))
                if c == 2:
                    item.setForeground(user_color(v.user))
                if c == 3 and color:
                    item.setForeground(QColor(color))
                if not v.available and c != 0:
                    item.setForeground(COLOR_NONE)
                self.table.setItem(r, c, item)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(0, 26)
        self.table.itemSelectionChanged.connect(self._update)
        self.table.doubleClicked.connect(lambda *_: self.open_version())

        self.info = QLabel()
        self.info.setWordWrap(True)
        self.btn_open = QPushButton("Otevřít tuto verzi")
        self.btn_open.setToolTip("Otevře kopii verze jen pro čtení (dočasný soubor)")
        self.btn_open.clicked.connect(lambda: self.open_version())
        self.btn_restore = QPushButton("Obnovit do místní kopie…" if local_target else "Uložit verzi do složky…")
        self.btn_restore.setToolTip(
            "Nahradí soubor v místní kopii touto verzí – na server se dostane s odevzdáním"
            if local_target else "Projekt / soubor nemáte aktivovaný – verze se uloží do zvolené složky")
        self.btn_restore.clicked.connect(lambda: self.restore_version())
        close = QPushButton("Zavřít")
        close.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addWidget(self.btn_open)
        buttons.addWidget(self.btn_restore)
        buttons.addStretch()
        buttons.addWidget(close)

        layout = QVBoxLayout(self)
        layout.addWidget(head)
        layout.addWidget(self.table)
        layout.addWidget(self.info)
        layout.addLayout(buttons)
        if self.versions:
            self.table.selectRow(0)
        self._update()

    def selected(self) -> FileVersion | None:
        rows = self.table.selectionModel().selectedRows()
        return self.versions[rows[0].row()] if rows else None

    def _update(self) -> None:
        v = self.selected()
        ok = bool(v and v.available and not v.removed)
        self.btn_open.setEnabled(ok)
        self.btn_restore.setEnabled(ok)
        if v is None:
            self.info.setText("")
        elif v.removed:
            self.info.setText("Soubor byl v tomto okamžiku smazán.")
        elif v.available:
            self.info.setText(f"Verze z {date_time(v.created_at)} ({v.user}) je k dispozici.")
        else:
            self.info.setText(f"<span style='color:#9e9e9e'>Verze z {date_time(v.created_at)} "
                              f"není k dispozici – {v.reason}.</span>")

    def _copy_to(self, v: FileVersion, dest: Path) -> bool:
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            ok = self.pm.restore_file_version(self.project, v, dest)
        finally:
            QApplication.restoreOverrideCursor()
        if not ok:
            QMessageBox.critical(self, "Historie souboru",
                                 "Verzi se nepodařilo načíst – záloha je nečitelná nebo poškozená "
                                 "(neodpovídá hash).")
        return ok

    def open_version(self) -> Path | None:
        v = self.selected()
        if v is None or not v.available or v.removed:
            return None
        dest = new_staging_dir() / version_file_name(self.path, v)
        if not self._copy_to(v, dest):
            return None
        set_readonly(dest, True)  # stará verze jen k nahlédnutí
        try:
            os.startfile(dest)  # noqa: S606 – výchozí aplikace Windows
        except OSError as e:
            QMessageBox.warning(self, "Historie souboru", f"Soubor nelze otevřít:\n{dest}\n\n{e}")
        return dest

    def restore_version(self) -> Path | None:
        v = self.selected()
        if v is None or not v.available or v.removed:
            return None
        when = date_time(v.created_at)
        if self.local_target is not None:
            if QMessageBox.question(
                    self, "Obnovit verzi",
                    f"Nahradit soubor v místní kopii verzí z {when} ({v.user})?\n\n{self.path}\n\n"
                    "Na server se dostane, až soubor odevzdáte. Aktuální verze na serveru zůstane "
                    "v historii (záloha se vytvoří při odevzdání).") != QMessageBox.StandardButton.Yes:
                return None
            return self.local_target if self._copy_to(v, self.local_target) else None
        folder = QFileDialog.getExistingDirectory(self, f"Uložit verzi z {when} do složky")
        if not folder:
            return None
        name = self.path.rpartition("/")[2]
        dest = Path(folder) / free_name(name, lambda n: (Path(folder) / n).exists())
        if self._copy_to(v, dest):
            QMessageBox.information(self, "Uložit verzi", f"Verze z {when} je uložená:\n{dest}")
            return dest
        return None
