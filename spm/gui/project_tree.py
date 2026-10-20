"""Strom projektu s přetahováním myší.

- Ven (např. na plochu): označené soubory a složky se zkopírují. Kopie jsou zapisovatelné
  (i u souborů, které nemám aktivované – v místní kopii jsou jen pro čtení) a nestažené
  soubory se vezmou ze serveru – proto se před tažením připraví v dočasné složce.
- Dovnitř (z Průzkumníka nebo mezi složkami stromu): kopie do složky pod myší; co se smí
  přidat a nahradit, rozhoduje panel (ProjectManager.plan_copy_in).
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QMimeData, QPoint, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDrag, QPainter, QPen
from PySide6.QtWidgets import QAbstractItemView, QTreeWidgetItem, QWidget

from .check_tree import PATH_ROLE, CheckTree

MIME_FILES = "application/x-spm-files"   # (cesta v projektu, cesta v cíli) – tažení uvnitř stromu
STAGING_ROOT = Path(tempfile.gettempdir()) / "spm_drag"
STAGING_KEEP_S = 600  # dočasné kopie pro tažení ven se mažou až po 10 min (Průzkumník je kopíruje)


def new_staging_dir() -> Path:
    """Nová dočasná složka pro tažení ven; staré (dávno přetažené) se smažou."""
    now = time.time()
    try:
        for old in STAGING_ROOT.iterdir():
            if now - old.stat().st_mtime > STAGING_KEEP_S:
                shutil.rmtree(old, ignore_errors=True)
    except OSError:
        pass
    path = STAGING_ROOT / uuid.uuid4().hex[:12]
    path.mkdir(parents=True)
    return path


def files_mime(items: list[tuple[str, str]], urls: list[Path]) -> QMimeData:
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(p)) for p in urls])
    mime.setData(MIME_FILES, json.dumps(items).encode("utf-8"))
    return mime


def mime_items(mime: QMimeData) -> list[tuple[str, str]] | None:
    if not mime.hasFormat(MIME_FILES):
        return None
    try:
        return [tuple(x) for x in json.loads(bytes(mime.data(MIME_FILES)).decode("utf-8"))]
    except ValueError:
        return None


class ProjectTree(CheckTree):
    def __init__(self, headers: list[str], parent: QWidget | None = None):
        super().__init__(headers, checkable=False, parent=parent)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.setDropIndicatorShown(False)  # cílovou složku zvýrazní paintEvent
        # nastaví panel: data pro tažení ven (označené položky -> mime), zpracování puštění
        # (urls ze systému, položky z vlastního stromu, cílová složka) a zda lze sem kopírovat
        self.drag_handler: Callable[[list[str]], QMimeData | None] | None = None
        self.drop_handler: Callable[[list[Path], list[tuple[str, str]] | None, str], None] | None = None
        self.can_drop: Callable[[], bool] = lambda: False
        self._drop_item: QTreeWidgetItem | None = None
        self._drop_active = False

    # --- tažení ven ---

    def startDrag(self, actions) -> None:  # noqa: N802 – Qt
        scopes = [item.data(0, PATH_ROLE) for item in self.selectedItems()]
        mime = self.drag_handler(scopes) if self.drag_handler and scopes else None
        if mime is None:
            return
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.CopyAction, Qt.DropAction.CopyAction)  # jen kopie, nikdy přesun

    # --- puštění dovnitř ---

    def drop_folder(self, pos: QPoint) -> tuple[str, QTreeWidgetItem | None]:
        """Cílová složka v projektu ('' = kořen, jinak 'a/b/') a její položka ve stromu."""
        item = self.itemAt(pos)
        while item is not None and not item.data(0, PATH_ROLE).endswith("/"):
            item = item.parent()  # puštěno na soubor -> jeho složka
        return (item.data(0, PATH_ROLE) if item is not None else ""), item

    def _accepts(self, event) -> bool:
        mime = event.mimeData()
        return self.can_drop() and (mime.hasUrls() or mime.hasFormat(MIME_FILES))

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if self._accepts(event):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
            self._drop_active = True
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if not self._accepts(event):
            event.ignore()
            return
        _, item = self.drop_folder(event.position().toPoint())
        if item is not self._drop_item:
            self._drop_item = item
            self.viewport().update()
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self._clear_drop()
        event.accept()

    def dropEvent(self, event) -> None:  # noqa: N802
        target, _ = self.drop_folder(event.position().toPoint())
        self._clear_drop()
        if not self._accepts(event) or self.drop_handler is None:
            event.ignore()
            return
        mime = event.mimeData()
        internal = mime_items(mime) if event.source() is self else None
        urls = [Path(u.toLocalFile()) for u in mime.urls() if u.isLocalFile()]
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
        # zpracovat až po skončení tažení (dotaz nesmí blokovat Průzkumník, ze kterého se táhne)
        QTimer.singleShot(0, lambda: self.drop_handler(urls, internal, target))

    def _clear_drop(self) -> None:
        self._drop_active = False
        if self._drop_item is not None:
            self._drop_item = None
            self.viewport().update()

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        if not self._drop_active:
            return
        painter = QPainter(self.viewport())
        pen = QPen(QColor(self.palette().highlight().color()))
        pen.setWidth(2)
        painter.setPen(pen)
        if self._drop_item is not None:
            rect = self.visualItemRect(self._drop_item)
            rect.setLeft(0)
            rect.setRight(self.viewport().width() - 1)
        else:  # kořen projektu
            rect = self.viewport().rect()
        painter.drawRect(rect.adjusted(1, 1, -1, -1))
        painter.end()
