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
from PySide6.QtGui import QAction, QColor, QKeySequence
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

from .. import folder_ops, recycle, zip_ops
from ..sync import ProjectManager
from ..folder_zones import ProtectedZones, Zone
from . import name_dialog
from .project_tree import MIME_CLIPBOARD, clipboard_info


def _same_or_inside(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


HEADERS = ("Název", "Velikost", "Typ", "Změněno")


LOCK = "🔒"
COLOR_PROTECTED = QColor("#8a8a8a")


class FolderModel(QFileSystemModel):
    """Model souborů s českými názvy sloupců; chráněné oblasti (složky spolupráce) šedě,
    kořen oblasti se zámkem, bez přejmenování."""

    def __init__(self, panel: "FolderPanel"):
        super().__init__(panel)
        self.panel = panel

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if (orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole
                and 0 <= section < len(HEADERS)):
            return HEADERS[section]
        return super().headerData(section, orientation, role)

    def _zone(self, index: QModelIndex):
        zones = self.panel.zones()
        return zones.zone_of(Path(self.filePath(index)), self.panel.root) if zones else None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):  # noqa: D401 – Qt
        if role in (Qt.ItemDataRole.ForegroundRole, Qt.ItemDataRole.ToolTipRole) or (
                role == Qt.ItemDataRole.DisplayRole and index.column() == 0):
            zone = self._zone(index)
            if zone is not None:
                if role == Qt.ItemDataRole.ForegroundRole:
                    return COLOR_PROTECTED
                if role == Qt.ItemDataRole.ToolTipRole:
                    return (f"Chráněno – {zone.label}.\nZměny jen v záložce projektů; dvojklik na "
                            "soubor přepne do jeho projektu.")
                zones = self.panel.zones()
                if zones._k(self.filePath(index)) == zones._k(zone.root):  # kořen oblasti
                    return f"{super().data(index, role)}  {LOCK}"
        return super().data(index, role)

    def flags(self, index):
        flags = super().flags(index)
        if index.isValid() and self._zone(index) is not None:
            flags &= ~Qt.ItemFlag.ItemIsEditable
        return flags

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):  # noqa: N802
        """Přejmenování přímo ve stromu – ne u chráněných a u složek, které chráněné obsahují."""
        zone = self.panel.modify_blocked(Path(self.filePath(index)))
        if zone is not None:
            self.panel.refuse(zone, "Přejmenovat nelze", Path(self.filePath(index)))
            return False
        return super().setData(index, value, role)


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
        # uvnitř stromu přesun – chráněné položky (a složky, které je obsahují) jen kopie
        move = event.source() is self and not any(self.panel.modify_blocked(u) for u in urls)
        event.setDropAction(Qt.DropAction.MoveAction if move else Qt.DropAction.CopyAction)
        event.accept()
        # až po skončení tažení (dotaz nesmí blokovat Průzkumník, ze kterého se táhne)
        QTimer.singleShot(0, lambda: self.panel.put(urls, target, move=move))


class FolderPanel(QWidget):
    modified = Signal()  # soubory změněné odsud (i v projektu – vyjmuté a vložené sem)
    # soubor / složka v chráněné oblasti – přepnout do záložky projektu, kam patří
    open_in_project = Signal(object)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.root: Path | None = None
        self.ws_id = ""
        # chráněné oblasti (složky spolupráce) – nastaví okno
        self.zones_get: Callable[[], ProtectedZones | None] = lambda: None
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

    # --- chráněné oblasti ------------------------------------------------------------

    def zones(self) -> ProtectedZones | None:
        return self.zones_get()

    def write_blocked(self, folder: Path) -> Zone | None:
        zones = self.zones()
        return zones.write_blocked(folder, self.root) if zones else None

    def modify_blocked(self, path: Path) -> Zone | None:
        zones = self.zones()
        return zones.modify_blocked(path, self.root) if zones else None

    def refuse(self, zone: Zone, what: str, path: Path | None = None) -> None:
        """Vysvětlení, proč to nejde; u složky záložky nabídne přechod do ní."""
        box = QMessageBox(QMessageBox.Icon.Information, "Chráněná složka",
                          f"{what} – {zone.root} je chráněná ({zone.label}).\n\n"
                          "Soubory projektů se mění jen v záložce projektů (aktivace, odevzdání), "
                          "aby se nic nezměnilo mimo evidenci.", parent=self)
        go = box.addButton(f"Přejít do záložky {zone.ws_name}", QMessageBox.ButtonRole.ActionRole) \
            if zone.ws_id else None
        box.addButton(QMessageBox.StandardButton.Ok)
        box.exec()
        if go is not None and box.clickedButton() is go:
            self.open_in_project.emit(path or zone.root)

    # --- otevření, nová složka, smazání -------------------------------------------------

    def _open_index(self, index: QModelIndex) -> None:
        path = self.path_of(index)
        zones = self.zones()
        if path.is_file() and zones and zones.zone_of(path, self.root) is not None:
            self.open_in_project.emit(path)  # neotevírat – přepnout do projektu
            return
        if path.is_file():
            try:
                os.startfile(path)  # noqa: S606 – výchozí aplikace Windows
            except OSError as e:
                QMessageBox.warning(self, "Otevřít soubor", str(e))

    def create_folder(self, parent: Path | None) -> None:
        if parent is None or not self._inside(parent):
            return
        zone = self.write_blocked(parent)
        if zone is not None:
            self.refuse(zone, "Novou složku sem nelze vytvořit", parent)
            return
        try:  # názvy sousedních složek – nabízejí se při psaní
            siblings = [p.name for p in parent.iterdir() if p.is_dir()]
        except OSError:
            siblings = []
        name, ok = name_dialog.ask_text(self, "Nová složka", f"Název nové složky v {parent}:", "", siblings)
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
        blocked = next(((p, z) for p in paths if (z := self.modify_blocked(p)) is not None), None)
        if blocked is not None:
            self.refuse(blocked[1], f"Smazat {blocked[0].name} nelze", blocked[0])
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
        if not index.isValid():
            return
        zone = self.modify_blocked(self.path_of(index))
        if zone is not None:
            self.refuse(zone, "Přejmenovat nelze", self.path_of(index))
            return
        self.tree.edit(index.siblingAtColumn(0))

    # --- schránka --------------------------------------------------------------------

    def copy_selected(self, paths: list[Path] | None = None, cut: bool = False) -> bool:
        paths = self.selected_paths() if paths is None else paths
        if not paths:
            return False
        if cut:  # vyjmout = přesunout – ne chráněné (ani složky, které je obsahují)
            blocked = next(((p, z) for p in paths if (z := self.modify_blocked(p)) is not None), None)
            if blocked is not None:
                self.refuse(blocked[1], f"Vyjmout {blocked[0].name} nelze (kopírovat ano)", blocked[0])
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
        zone = self.write_blocked(target)
        if zone is not None:
            self.refuse(zone, "Sem nelze nic vložit", target)
            return None
        if move:
            blocked = next(((p, z) for p in sources if (z := self.modify_blocked(p)) is not None), None)
            if blocked is not None:
                self.refuse(blocked[1], f"Přesunout {blocked[0].name} nelze", blocked[0])
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
                              + "\n\nPřepsat je, přeskočit, nebo ponechat obě (vložené dostanou "
                                "_copy v názvu)?", parent=self)
            yes = box.addButton("Přepsat", QMessageBox.ButtonRole.YesRole)
            both = box.addButton("Ponechat obě", QMessageBox.ButtonRole.ActionRole)
            no = box.addButton("Přeskočit", QMessageBox.ButtonRole.NoRole)
            box.addButton("Zrušit", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() not in (yes, both, no):
                return None
            overwrite = box.clickedButton() is yes
            if box.clickedButton() is both:
                pairs = folder_ops.keep_both(pairs)
                clashes = []  # nic se nepřeskočí (hlášení přeskočených jen při chybě)
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

    # --- ZIP -------------------------------------------------------------------------

    def _rel(self, path: Path) -> str:
        rel = path.relative_to(self.root).as_posix()
        return rel + "/" if path.is_dir() else rel

    def zip_paths(self, paths: list[Path]) -> Path | None:
        """Zabalí položky do zipu ve společné nadřazené složce (název viz zip_ops)."""
        paths = [p for p in paths if self._inside(p) and p != self.root]
        if not paths:
            return None
        scopes = [self._rel(p) for p in paths]
        base = self.root / zip_ops.zip_base(scopes)
        zone = self.write_blocked(base)
        if zone is not None:
            self.refuse(zone, "Zip sem nelze uložit", base)
            return None
        name = zip_ops.zip_name(scopes, self.root.name) + zip_ops.ZIP_EXT
        dest = base / folder_ops.free_name(name, lambda n: (base / n).exists())
        members = []
        for p in paths:
            files = sorted(f for f in p.rglob("*") if f.is_file()) if p.is_dir() else [p]
            members += [(f, f.relative_to(base).as_posix()) for f in files if f != dest]
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            zip_ops.make_zip(members, dest)
        except (OSError, zip_ops.ZipError) as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "Zabalit do ZIP", str(e))
            return None
        QApplication.restoreOverrideCursor()
        return dest

    def unzip(self, path: Path) -> Path | None:
        """Rozbalí zip do nové složky vedle něj (se stejným názvem jako zip)."""
        if not self._inside(path):
            return None
        zone = self.write_blocked(path.parent)
        if zone is not None:
            self.refuse(zone, "Zip sem nelze rozbalit", path)
            return None
        name = zip_ops.unzip_folder_name(path.name)
        dest = path.parent / folder_ops.free_name(name, lambda n: (path.parent / n).exists(), True)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            zip_ops.extract_zip(path, dest)
        except (OSError, zip_ops.ZipError) as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "Rozbalit ZIP", str(e))
            return None
        QApplication.restoreOverrideCursor()
        return dest

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
        protected = bool(paths) and any(self.modify_blocked(p) for p in paths)
        in_zone = len(paths) == 1 and self.write_blocked(paths[0]) is not None
        act_open = menu.addAction("Přejít do projektu" if in_zone else "Otevřít")
        act_open.setEnabled(len(paths) == 1 and paths[0].is_file())
        menu.addSeparator()
        act_copy = menu.addAction("Kopírovat" + many + "\tCtrl+C")
        act_copy_name = menu.addAction("Kopírovat název" + ("y" if len(paths) > 1 else ""))
        act_copy_name.setEnabled(bool(paths))
        act_cut = menu.addAction("Vyjmout" + many + "\tCtrl+X")
        act_paste = menu.addAction("Vložit\tCtrl+V")
        for act in (act_copy, act_cut):
            act.setEnabled(bool(paths))
        act_paste.setEnabled(self.can_paste() and self.write_blocked(target) is None)
        act_cut.setEnabled(bool(paths) and not protected)
        menu.addSeparator()
        act_new = menu.addAction("Nová složka…\tCtrl+Shift+N")
        act_rename = menu.addAction("Přejmenovat\tF2")
        act_rename.setEnabled(len(paths) == 1 and not protected)
        act_delete = menu.addAction("Smazat (do Koše)" + many + "\tDel")
        act_delete.setEnabled(bool(paths) and not protected)
        act_new.setEnabled(self.write_blocked(target) is None)
        menu.addSeparator()
        act_zip = menu.addAction("Zabalit do ZIP" + many)
        act_zip.setEnabled(bool(paths))
        act_unzip = None
        if len(paths) == 1 and paths[0].is_file() and zip_ops.is_zip(paths[0].name):
            act_unzip = menu.addAction("Rozbalit ZIP")
        chosen = menu.exec(self.tree.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen is act_open:
            self._open_index(index)
        elif chosen is act_copy:
            self.copy_selected(paths)
        elif chosen is act_copy_name:
            QApplication.clipboard().setText("\n".join(p.name for p in paths))
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
        elif chosen is act_zip:
            self.zip_paths(paths)
        elif chosen is act_unzip:
            self.unzip(paths[0])
