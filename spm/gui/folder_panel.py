"""Záložka „Složka“: obyčejná složka mimo spolupráci (např. Data) jako strom souborů.

Bez aktivace, zámků a historie – soubory se kopírují, přesouvají, přejmenovávají a mažou (do
Koše) volně jako v Průzkumníku. Schránka a přetahování fungují i s projekty: kopie z projektu
sem, vyjmuté v projektu (moje soubory) se sem přesunou, a naopak.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QDir, QMimeData, QModelIndex, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileSystemModel,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from .. import folder_ops, recycle
from ..sync import ProjectManager
from .project_tree import MIME_CLIPBOARD, clipboard_info


def _same_or_inside(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


HEADERS = ("Název", "Velikost", "Typ", "Změněno")


class FolderModel(QFileSystemModel):
    """Model souborů s českými názvy sloupců."""

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if (orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole
                and 0 <= section < len(HEADERS)):
            return HEADERS[section]
        return super().headerData(section, orientation, role)


class FolderTree(QTreeView):
    """Strom složky s přetahováním: uvnitř stromu přesun, odjinud kopie."""

    def __init__(self, panel: "FolderPanel"):
        super().__init__()
        self.panel = panel
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)

    def dragEnterEvent(self, event) -> None:  # noqa: N802 – Qt
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.setDropAction(Qt.DropAction.MoveAction if event.source() is self
                                else Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:  # noqa: N802
        mime = event.mimeData()
        if not mime.hasUrls():
            event.ignore()
            return
        target = self.panel.folder_at(self.indexAt(event.position().toPoint()))
        urls = [Path(u.toLocalFile()) for u in mime.urls() if u.isLocalFile()]
        move = event.source() is self
        event.setDropAction(Qt.DropAction.MoveAction if move else Qt.DropAction.CopyAction)
        event.accept()
        # až po skončení tažení (dotaz nesmí blokovat Průzkumník, ze kterého se táhne)
        QTimer.singleShot(0, lambda: self.panel.put(urls, target, move=move))


class FolderPanel(QWidget):
    modified = Signal()  # soubory změněné odsud (i v projektu – vyjmuté a vložené sem)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.root: Path | None = None
        self.ws_id = ""
        # ProjectManager záložky (vložení souborů vyjmutých v projektu) – nastaví okno
        self.pm_for: Callable[[str], ProjectManager] | None = None
        self.title = QLabel()
        btn_new = QPushButton("Nová složka")
        btn_new.clicked.connect(lambda: self.create_folder(self.current_folder()))
        btn_explorer = QPushButton("Otevřít v Průzkumníku")
        btn_explorer.clicked.connect(lambda: self.root and os.startfile(self.root))  # noqa: S606
        head = QHBoxLayout()
        head.addWidget(self.title)
        head.addStretch()
        head.addWidget(btn_new)
        head.addWidget(btn_explorer)

        self.model = FolderModel(self)
        self.model.setReadOnly(False)  # přejmenování přímo ve stromu (F2)
        self.model.setFilter(QDir.Filter.AllEntries | QDir.Filter.NoDotAndDotDot | QDir.Filter.Hidden)
        self.tree = FolderTree(self)
        self.tree.setModel(self.model)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.EditKeyPressed)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.tree.setColumnWidth(0, 360)
        self.tree.doubleClicked.connect(self._open_index)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        for key, slot in ((QKeySequence.StandardKey.Copy, lambda: self.copy_selected()),
                          (QKeySequence.StandardKey.Cut, lambda: self.copy_selected(cut=True)),
                          (QKeySequence.StandardKey.Paste, lambda: self.paste(self.current_folder())),
                          (QKeySequence(Qt.Key.Key_Delete), lambda: self.delete_selected()),
                          (QKeySequence("Ctrl+Shift+N"), lambda: self.create_folder(self.current_folder()))):
            act = QAction(self.tree)
            act.setShortcut(QKeySequence(key))
            act.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
            act.triggered.connect(slot)
            self.tree.addAction(act)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.addLayout(head)
        layout.addWidget(self.tree)

    # --- složka ------------------------------------------------------------------

    def set_root(self, path: str, ws_id: str) -> None:
        self.root = Path(path)
        self.ws_id = ws_id
        index = self.model.setRootPath(str(self.root))
        self.tree.setRootIndex(index)
        exists = self.root.is_dir()
        self.title.setText(f"<b>Složka</b> {self.root}" + ("" if exists else
                           "  <span style='color:#c0392b'>(složka neexistuje)</span>"))

    def path_of(self, index: QModelIndex) -> Path:
        return Path(self.model.filePath(index))

    def selected_paths(self) -> list[Path]:
        rows = self.tree.selectionModel().selectedRows(0) if self.tree.selectionModel() else []
        return [self.path_of(i) for i in rows]

    def folder_at(self, index: QModelIndex) -> Path:
        """Cílová složka: složka pod myší, u souboru jeho složka, jinak kořen."""
        if not index.isValid() or self.root is None:
            return self.root
        path = self.path_of(index)
        return path if path.is_dir() else path.parent

    def current_folder(self) -> Path:
        index = self.tree.currentIndex()
        if not index.isValid() or not self.tree.selectionModel().isSelected(index):
            return self.root
        return self.folder_at(index)

    def _inside(self, path: Path) -> bool:
        return self.root is not None and _same_or_inside(path, self.root)

    # --- otevření, nová složka, smazání -------------------------------------------------

    def _open_index(self, index: QModelIndex) -> None:
        path = self.path_of(index)
        if path.is_file():
            try:
                os.startfile(path)  # noqa: S606 – výchozí aplikace Windows
            except OSError as e:
                QMessageBox.warning(self, "Otevřít soubor", str(e))

    def create_folder(self, parent: Path | None) -> None:
        if parent is None or not self._inside(parent):
            return
        name, ok = QInputDialog.getText(self, "Nová složka", f"Název nové složky v {parent}:",
                                        QLineEdit.EchoMode.Normal, "")
        name = name.strip()
        if not ok or not name:
            return
        try:
            (parent / name).mkdir()
        except OSError as e:
            QMessageBox.critical(self, "Nová složka", str(e))

    def delete_selected(self) -> None:
        paths = [p for p in self.selected_paths() if self._inside(p) and p != self.root]
        if not paths:
            return
        shown = "\n".join(p.name for p in paths[:15]) + ("\n…" if len(paths) > 15 else "")
        if QMessageBox.question(self, "Smazat", f"Přesunout do Koše ({len(paths)})?\n\n{shown}") \
                != QMessageBox.StandardButton.Yes:
            return
        try:
            recycle.to_recycle_bin(paths)
        except OSError as e:
            QMessageBox.critical(self, "Smazat", str(e))

    def rename_selected(self) -> None:
        index = self.tree.currentIndex()
        if index.isValid():
            self.tree.edit(index.siblingAtColumn(0))

    # --- schránka --------------------------------------------------------------------

    def copy_selected(self, paths: list[Path] | None = None, cut: bool = False) -> bool:
        paths = self.selected_paths() if paths is None else paths
        if not paths:
            return False
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
        info = {"ws": self.ws_id, "folder": True, "cut": cut, "paths": [str(p) for p in paths]}
        mime.setData(MIME_CLIPBOARD, json.dumps(info).encode("utf-8"))
        QApplication.clipboard().setMimeData(mime)
        return True

    def can_paste(self) -> bool:
        mime = QApplication.clipboard().mimeData()
        return bool(self.root and mime is not None and mime.hasUrls())

    def paste(self, target: Path | None) -> None:
        mime = QApplication.clipboard().mimeData()
        if target is None or mime is None or not mime.hasUrls():
            return
        urls = [Path(u.toLocalFile()) for u in mime.urls() if u.isLocalFile()]
        info = clipboard_info(mime)
        if info and info.get("cut") and info.get("folder"):        # vyjmuté v záložce Složka
            self.put([Path(p) for p in info["paths"]], target, move=True)
            QApplication.clipboard().clear()
        elif info and info.get("cut") and not info.get("folder"):  # vyjmuté v projektu
            done = self.put(urls, target)
            if done and self._remove_from_project(info, done, target):
                QApplication.clipboard().clear()
                self.modified.emit()  # projekt se změnil – jeho strom v druhém panelu
        else:
            self.put(urls, target)

    def put(self, sources: list[Path], target: Path | None, move: bool = False) -> list[Path] | None:
        """Kopie (nebo přesun) položek do složky target; při shodě názvů se zeptá.
        Vrací zkopírované / přesunuté položky v cíli (None = zrušeno nebo chyba)."""
        if target is None or not self._inside(target) or not sources:
            return None
        title = "Přesunout" if move else "Kopírovat"
        pairs = folder_ops.targets(sources, target)
        overwrite = False
        clashes = folder_ops.conflicts(pairs)
        if clashes:
            box = QMessageBox(QMessageBox.Icon.Question, title,
                              f"V cíli už existuje {len(clashes)} položek se stejným názvem:\n\n"
                              + "\n".join(p.name for p in clashes[:15])
                              + ("\n…" if len(clashes) > 15 else "")
                              + "\n\nPřepsat je? (Ne = přeskočit existující soubory)", parent=self)
            yes = box.addButton("Přepsat", QMessageBox.ButtonRole.YesRole)
            no = box.addButton("Přeskočit", QMessageBox.ButtonRole.NoRole)
            box.addButton("Zrušit", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() not in (yes, no):
                return None
            overwrite = box.clickedButton() is yes
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            done, skipped = (folder_ops.move_items if move else folder_ops.copy_items)(pairs, overwrite)
        except OSError as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, title, str(e))
            return None
        QApplication.restoreOverrideCursor()
        if skipped and not (clashes and not overwrite):
            box = QMessageBox(QMessageBox.Icon.Warning, title, f"Přeskočeno: {len(skipped)}", parent=self)
            box.setDetailedText("\n".join(skipped))
            box.exec()
        return done

    def _remove_from_project(self, info: dict, copied: list[Path], target: Path) -> bool:
        """Po vložení vyjmutých souborů projektu: originály v projektu do Koše – jen ty, které
        se skutečně zkopírovaly (přeskočené zůstávají v projektu)."""
        if self.pm_for is None:
            return False
        copied_keys = {os.path.normcase(str(p)) for p in copied}
        done = [rel for rel, out in info["items"]
                if os.path.normcase(str(target / out)) in copied_keys]
        if not done:
            return False
        pm = self.pm_for(info["ws"])
        try:
            if info.get("local_only"):
                pm.delete_local_only(info["project"], done)
            else:
                pm.delete_local(info["project"], done)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Vložit", f"Soubory se zkopírovaly, ale v projektu "
                                                f"{info['project']} se nepodařilo smazat originály:\n{e}")
            return False
        return True

    # --- nabídka ---------------------------------------------------------------------

    def _context_menu(self, pos) -> None:
        index = self.tree.indexAt(pos)
        if index.isValid() and not self.tree.selectionModel().isSelected(index):
            self.tree.selectionModel().select(
                index, self.tree.selectionModel().SelectionFlag.ClearAndSelect
                | self.tree.selectionModel().SelectionFlag.Rows)
            self.tree.setCurrentIndex(index)
        paths = self.selected_paths() if index.isValid() else []
        target = self.folder_at(index)
        many = f" ({len(paths)} označených)" if len(paths) > 1 else ""
        menu = QMenu(self)
        act_open = menu.addAction("Otevřít")
        act_open.setEnabled(len(paths) == 1 and paths[0].is_file())
        menu.addSeparator()
        act_copy = menu.addAction("Kopírovat" + many + "\tCtrl+C")
        act_cut = menu.addAction("Vyjmout" + many + "\tCtrl+X")
        act_paste = menu.addAction("Vložit\tCtrl+V")
        for act in (act_copy, act_cut):
            act.setEnabled(bool(paths))
        act_paste.setEnabled(self.can_paste())
        menu.addSeparator()
        act_new = menu.addAction("Nová složka…\tCtrl+Shift+N")
        act_rename = menu.addAction("Přejmenovat\tF2")
        act_rename.setEnabled(len(paths) == 1)
        act_delete = menu.addAction("Smazat (do Koše)" + many + "\tDel")
        act_delete.setEnabled(bool(paths))
        chosen = menu.exec(self.tree.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen is act_open:
            self._open_index(index)
        elif chosen is act_copy:
            self.copy_selected(paths)
        elif chosen is act_cut:
            self.copy_selected(paths, cut=True)
        elif chosen is act_paste:
            self.paste(target)
        elif chosen is act_new:
            self.create_folder(target)
        elif chosen is act_rename:
            self.rename_selected()
        elif chosen is act_delete:
            self.delete_selected()
