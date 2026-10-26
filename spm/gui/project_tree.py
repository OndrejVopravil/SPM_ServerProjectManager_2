"""Strom projektu s přetahováním myší.

- Ven (např. na plochu): označené soubory a složky se zkopírují. Kopie jsou zapisovatelné
  (i u souborů, které nemám aktivované – v místní kopii jsou jen pro čtení) a nestažené
  soubory se vezmou ze serveru – proto se připraví v dočasné složce, ale až ve chvíli, kdy si
  o ně cíl řekne (LazyFilesMime – puštění / vložení v jiném programu). Uvnitř SPM se dočasné
  kopie nedělají vůbec: cíl dostane jen odkud soubory jsou (MIME_SOURCE) a čte je z původního
  místa.
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

from PySide6.QtCore import QByteArray, QMetaType, QMimeData, QPoint, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDrag, QPainter, QPen
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTreeWidgetItem, QWidget

from .check_tree import PATH_ROLE, CheckTree

MIME_FILES = "application/x-spm-files"   # (cesta v projektu, cesta v cíli) – tažení uvnitř stromu
# schránka (Kopírovat / Vyjmout): odkud soubory jsou – záložka, projekt, položky, vyjmuto
MIME_CLIPBOARD = "application/x-spm-clipboard"
# odkud tažené soubory jsou (záložka, projekt) – puštění v SPM čte soubory z původního místa
MIME_SOURCE = "application/x-spm-source"
URI_LIST = "text/uri-list"
STAGING_ROOT = Path(tempfile.gettempdir()) / "spm_drag"
STAGING_KEEP_S = 600  # dočasné kopie pro tažení ven se mažou až po 10 min (Průzkumník je kopíruje)
CLIPBOARD_ROOT = Path(tempfile.gettempdir()) / "spm_clip"
CLIPBOARD_KEEP_S = 24 * 3600  # schránka se může vložit i mnohem později než tažení


def new_staging_dir(root: Path | None = None, keep_s: float = STAGING_KEEP_S) -> Path:
    """Nová dočasná složka pro tažení ven (nebo schránku – jiný root); staré se smažou."""
    root = root or STAGING_ROOT
    now = time.time()
    try:
        for old in root.iterdir():
            if now - old.stat().st_mtime > keep_s:
                shutil.rmtree(old, ignore_errors=True)
    except OSError:
        pass
    path = root / uuid.uuid4().hex[:12]
    path.mkdir(parents=True)
    return path


def clipboard_info(mime: QMimeData | None) -> dict | None:
    """Údaje SPM ve schránce: z projektu {"ws", "project", "items": [(cesta, cesta v cíli)],
    "cut"}, ze záložky Složka {"ws", "folder": True, "paths": [...], "cut"}."""
    if mime is None or not mime.hasFormat(MIME_CLIPBOARD):
        return None
    try:
        data = json.loads(bytes(mime.data(MIME_CLIPBOARD)).decode("utf-8"))
        data["items"] = [tuple(x) for x in data.get("items", [])]
        return data
    except (ValueError, KeyError, TypeError):
        return None


class LazyFilesMime(QMimeData):
    """Data tažení / schránky, jejichž soubory (text/uri-list) se připraví až ve chvíli, kdy
    si o ně cíl řekne – jiný program (Průzkumník) při puštění nebo vložení. SPM sám je nečte
    (bere MIME_FILES a MIME_SOURCE), takže uvnitř SPM žádné dočasné kopie nevzniknou.
    render: připraví soubory a vrátí jejich cesty (volá se nejvýš jednou)."""

    def __init__(self, render: Callable[[], list[Path]]):
        super().__init__()
        self._render = render
        self._paths: list[Path] | None = None

    def rendered(self) -> bool:
        return self._paths is not None

    def paths(self) -> list[Path]:
        if self._paths is None:
            try:
                self._paths = list(self._render())
            except Exception:  # noqa: BLE001 – voláno z Windows (puštění) – chybu ohlásí render
                self._paths = []
        return self._paths

    def formats(self) -> list[str]:  # noqa: D401 – Qt
        result = list(super().formats())
        return result if URI_LIST in result else [URI_LIST, *result]

    def hasFormat(self, mimetype: str) -> bool:  # noqa: N802 – Qt
        return mimetype == URI_LIST or super().hasFormat(mimetype)

    def retrieveData(self, mimetype: str, preferred_type):  # noqa: N802 – Qt
        if mimetype == URI_LIST:
            urls = [QUrl.fromLocalFile(str(p)) for p in self.paths()]
            if preferred_type is not None and preferred_type.id() == QMetaType.Type.QByteArray.value:
                return QByteArray("".join(u.toString() + "\r\n" for u in urls).encode("utf-8"))
            return urls  # QMimeData.urls() (i převod pro Windows – CF_HDROP) chce seznam QUrl
        return super().retrieveData(mimetype, preferred_type)


def forget_clipboard() -> None:
    """Při ukončení SPM: vyprázdnit schránku, pokud v ní jsou soubory zkopírované v SPM.
    Jinak by Windows při ukončení převzaly celý obsah schránky – u LazyFilesMime by se tím
    teprve zkopírovaly všechny soubory (i ze serveru) a zavření by se protáhlo. Po zavření
    SPM tedy zkopírované soubory vložit nejde."""
    import shiboken6
    from PySide6.QtGui import QGuiApplication
    clipboard = QGuiApplication.clipboard()
    mime = clipboard.mimeData()
    # jen data vložená do schránky tímto procesem (z jiného programu / jiné instance SPM je
    # objekt dat vytvořený v Qt, ne v Pythonu)
    if mime is not None and shiboken6.createdByPython(mime) and mime.hasFormat(MIME_CLIPBOARD):
        clipboard.clear()


def spm_source(mime: QMimeData | None) -> dict | None:
    """Odkud tažené / zkopírované soubory jsou: {"ws", "project", "local_only"} (None = odjinud)."""
    if mime is None or not mime.hasFormat(MIME_SOURCE):
        return None
    try:
        return json.loads(bytes(mime.data(MIME_SOURCE)).decode("utf-8"))
    except (ValueError, TypeError):
        return None


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
        # šířky sloupců: „Soubor“ zabírá zbytek místa, ostatní (Drží, Zrcadlo, Poznámky) jsou
        # co nejužší – dlouhý text se zkrátí (celý v bublině); šířky lze měnit myší
        header = self.header()
        header.setStretchLastSection(False)
        header.setMinimumSectionSize(28)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        metrics = header.fontMetrics()
        for i in range(1, len(headers)):
            header.setSectionResizeMode(i, QHeaderView.ResizeMode.Interactive)
            self.setColumnWidth(i, metrics.horizontalAdvance(headers[i]) + 24)
        self.setTextElideMode(Qt.TextElideMode.ElideRight)
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
        # puštění souborů z jiného stromu SPM (jiný projekt / panel): (odkud, položky, cílová složka)
        self.spm_drop_handler: Callable[[dict, list[tuple[str, str]], str], None] | None = None
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
        source = spm_source(mime) if internal is None else None
        items = mime_items(mime) if source is not None else None
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
        if source is not None and items is not None and self.spm_drop_handler is not None:
            # z jiného stromu SPM: soubory z původního místa (bez dočasných kopií)
            QTimer.singleShot(0, lambda: self.spm_drop_handler(source, items, target))
            return
        urls = [] if internal is not None else [Path(u.toLocalFile()) for u in mime.urls() if u.isLocalFile()]
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
