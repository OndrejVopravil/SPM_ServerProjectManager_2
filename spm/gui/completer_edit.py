"""Textové pole s našeptáváním: po „@“ uživatelé, po „#“ projekty (jen ve vzkazech).

Nabídka se filtruje podle psaného textu (část loginu, přezdívky nebo názvu projektu); Tab nebo
Enter vloží vybranou položku, Esc nabídku zavře. Bez otevřené nabídky se Tab chová normálně.
"""

from __future__ import annotations

import re
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QStandardItem, QStandardItemModel, QTextCursor
from PySide6.QtWidgets import QCompleter, QPlainTextEdit, QWidget

_TRIGGER_RE = re.compile(r"(?:^|(?<=[\s(\[]))([@#])([^\s@#,;()\[\]]*)$")
INSERT_ROLE = Qt.ItemDataRole.UserRole + 1

# zdroj nabídky: [(text v nabídce, vložený text)] – volá se při každém otevření nabídky
Provider = Callable[[], list[tuple[str, str]]]


class CompleterEdit(QPlainTextEdit):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.providers: dict[str, Provider] = {}
        self.model = QStandardItemModel(self)
        self.completer = QCompleter(self)
        self.completer.setModel(self.model)
        self.completer.setWidget(self)
        self.completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.completer.setMaxVisibleItems(12)
        self.completer.activated.connect(self._insert_text)
        self._trigger = ""
        self._typed = ""
        self._loaded_for = ""

    # --- zdroje ----------------------------------------------------------------------

    @property
    def users(self) -> list:
        return getattr(self, "_users", [])

    @users.setter
    def users(self, users: list) -> None:
        """Uživatelé pro „@“ (UserInfo – zobrazí se jméno, vloží login). Neaktivní (SPM dlouho
        nespustili) se nabídnou až po napsání části jména – na konci, s popiskem."""
        self._users = list(users)
        self.providers["@"] = lambda: [(u.display, u.login) for u in self._users if not u.inactive()]
        self.providers["@+"] = lambda: (
            [(u.display, u.login) for u in self._users if not u.inactive()]
            + [(f"{u.display} – {u.seen_label}", u.login) for u in self._users if u.inactive()])

    def set_projects(self, names: Callable[[], list[str]] | None) -> None:
        """Projekty pro „#“ (None = „#“ nic nenabízí)."""
        if names is None:
            self.providers.pop("#", None)
        else:
            self.providers["#"] = lambda: [(n, n) for n in names()]

    # --- nabídka ---------------------------------------------------------------------

    def popup_visible(self) -> bool:
        return self.completer.popup().isVisible()

    def keyPressEvent(self, event) -> None:  # noqa: N802 – Qt
        if self.popup_visible() and event.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Return,
                                                    Qt.Key.Key_Enter, Qt.Key.Key_Escape, Qt.Key.Key_Backtab):
            if event.key() == Qt.Key.Key_Escape:
                self.completer.popup().hide()
            elif event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                self.completer.popup().hide()
                super().keyPressEvent(event)  # Ctrl+Enter = odeslat
            else:
                self.accept_completion()
            event.accept()
            return
        super().keyPressEvent(event)
        self.update_completion()

    def _prefix(self) -> tuple[str, str] | None:
        cursor = self.textCursor()
        before = cursor.block().text()[:cursor.positionInBlock()]
        m = _TRIGGER_RE.search(before)
        if not m or m.group(1) not in self.providers:
            return None
        return m.group(1), m.group(2)

    def update_completion(self) -> None:
        """Podle textu před kurzorem otevře / zúží / zavře nabídku."""
        found = self._prefix()
        if found is None:
            self.completer.popup().hide()
            self._trigger = ""
            return
        trigger, typed = found
        source = "@+" if trigger == "@" and typed and "@+" in self.providers else trigger
        if source != self._loaded_for or not self._trigger:
            self._load(source)
        self._trigger, self._typed = trigger, typed
        self.completer.setCompletionPrefix(typed)
        if self.completer.completionCount() == 0:
            self.completer.popup().hide()
            return
        popup = self.completer.popup()
        popup.setCurrentIndex(self.completer.completionModel().index(0, 0))
        rect = self.cursorRect()
        rect.setWidth(max(260, popup.sizeHintForColumn(0) + popup.verticalScrollBar().sizeHint().width() + 12))
        self.completer.complete(rect)

    def _load(self, trigger: str) -> None:
        self.model.clear()
        try:
            items = self.providers[trigger]()
        except Exception:  # noqa: BLE001 – např. nedostupná síť: nic nenabízet
            items = []
        for shown, value in items:
            item = QStandardItem(shown if shown.lower().find(value.lower()) >= 0 else f"{shown} ({value})")
            item.setData(value, INSERT_ROLE)
            self.model.appendRow(item)
        self._loaded_for = trigger

    def accept_completion(self) -> bool:
        """Vloží vybranou (nebo první) položku nabídky. Vrací, zda se něco vložilo."""
        popup = self.completer.popup()
        index = popup.currentIndex()
        if not index.isValid():
            index = self.completer.completionModel().index(0, 0)
        popup.hide()
        if not index.isValid():
            return False
        self._insert_value(index.data(INSERT_ROLE) or index.data())
        return True

    def _insert_text(self, shown: str) -> None:  # klepnutí do nabídky
        items = self.model.findItems(shown)
        self._insert_value(items[0].data(INSERT_ROLE) if items else shown)

    def _insert_value(self, value: str) -> None:
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.Left, QTextCursor.MoveMode.KeepAnchor, len(self._typed))
        cursor.insertText(value + " ")
        self.setTextCursor(cursor)
        self._trigger = self._typed = ""
