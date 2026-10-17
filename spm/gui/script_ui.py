"""Dialogy skriptů (ASK, INPUT, CHOOSE, FORM) v okně aplikace.

Skript běží ve vlákně na pozadí; dialog se ukáže v hlavním vlákně a vlákno skriptu na
odpověď počká (signál s BlockingQueuedConnection).
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from ..scripting.model import ScriptUI
from .worker import progress_paused


class FormDialog(QDialog):
    """FORM: víc položek v jednom okně (text, výběr ze seznamu, zaškrtnutí)."""

    def __init__(self, parent: QWidget | None, title: str, text: str, fields: list[dict]):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(420)
        self.fields = fields
        self.widgets: dict[str, QWidget] = {}
        layout = QVBoxLayout(self)
        if text:
            label = QLabel(text)
            label.setWordWrap(True)
            layout.addWidget(label)
        form = QFormLayout()
        for f in fields:
            if f["kind"] == "INPUT":
                w = QLineEdit(f["default"] or "")
                form.addRow(f["label"], w)
            elif f["kind"] == "CHOOSE":
                w = QComboBox()
                w.addItems(f["options"])
                w.setCurrentIndex(f["default"] or 0)
                form.addRow(f["label"], w)
            else:
                w = QCheckBox(f["label"])
                w.setChecked(bool(f["default"]))
                form.addRow(w)
            self.widgets[f["name"]] = w
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> dict:
        result = {}
        for f in self.fields:
            w = self.widgets[f["name"]]
            if f["kind"] == "INPUT":
                result[f["name"]] = w.text()
            elif f["kind"] == "CHOOSE":
                result[f["name"]] = w.currentIndex()
            else:
                result[f["name"]] = w.isChecked()
        return result


class GuiScriptUI(QObject, ScriptUI):
    _request = Signal(object)

    def __init__(self, parent: QWidget):
        super().__init__()
        self.parent_widget = parent
        self._request.connect(self._handle, Qt.ConnectionType.BlockingQueuedConnection)

    def _call(self, kind: str, *args):
        box = {"kind": kind, "args": args, "result": None}
        if QThread.currentThread() == self.thread():
            self._handle(box)
        else:  # vlákno skriptu čeká, než uživatel dialog zavře
            self._request.emit(box)
        return box["result"]

    @Slot(object)
    def _handle(self, box: dict) -> None:
        with progress_paused():  # okno průběhu nesmí zakrýt dialog, na který se čeká
            box["result"] = getattr(self, "_" + box["kind"])(*box["args"])

    def _parent(self) -> QWidget:
        # nad oknem průběhu (je modální), jinak nad hlavním oknem
        return QApplication.activeModalWidget() or self.parent_widget

    # --- ScriptUI ---

    def ask(self, title: str, text: str) -> bool:
        return bool(self._call("ask", title, text))

    def input(self, title: str, prompt: str, default: str) -> str | None:
        return self._call("input", title, prompt, default)

    def choose(self, title: str, prompt: str, options: list[str], default: int) -> int | None:
        return self._call("choose", title, prompt, options, default)

    def form(self, title: str, text: str, fields: list[dict]) -> dict | None:
        return self._call("form", title, text, fields)

    # --- v hlavním vlákně ---

    def _ask(self, title: str, text: str) -> bool:
        answer = QMessageBox.question(self._parent(), title, text,
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                      QMessageBox.StandardButton.Yes)
        return answer == QMessageBox.StandardButton.Yes

    def _input(self, title: str, prompt: str, default: str) -> str | None:
        text, ok = QInputDialog.getText(self._parent(), title, prompt, QLineEdit.EchoMode.Normal, default)
        return text if ok else None

    def _choose(self, title: str, prompt: str, options: list[str], default: int) -> int | None:
        item, ok = QInputDialog.getItem(self._parent(), title, prompt, options, default, False)
        if not ok:
            return None
        return options.index(item) if item in options else None

    def _form(self, title: str, text: str, fields: list[dict]) -> dict | None:
        dlg = FormDialog(self._parent(), title, text, fields)
        try:
            return dlg.values() if dlg.exec() else None
        finally:
            dlg.deleteLater()
