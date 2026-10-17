"""Nápověda skriptů s vyhledáváním (okno Skripty) a editor skriptu s odsazováním."""

from __future__ import annotations

import unicodedata

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QKeyEvent, QKeySequence, QShortcut, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..scripting.help import HELP_HTML

COLOR_FOUND = QColor("#fff176")    # nalezený text (jako ve stromu projektu)
COLOR_CURRENT = QColor("#ffa726")  # aktuální výskyt
INDENT = "\t"
INDENT_SPACES = 4  # Shift+Tab sebere i odsazení mezerami (nejvýš tolik)


def _fold(ch: str) -> str:
    """Znak pro hledání bez ohledu na velikost písmen a diakritiku (délka zůstává 1)."""
    base = unicodedata.normalize("NFD", ch)[0]
    return base.casefold()[0] if base.casefold() else base


class HelpPanel(QWidget):
    """Nápověda: pole Hledat zvýrazní všechny výskyty (bez ohledu na velikost písmen
    a diakritiku), Enter / Shift+Enter přejde na další / předchozí, Esc hledání zruší."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Hledat v nápovědě…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _t: self.find(jump=True))
        self.search.returnPressed.connect(lambda: self.step(1))
        QShortcut(QKeySequence("Shift+Return"), self.search, activated=lambda: self.step(-1))
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self.search, activated=self.search.clear)
        self.count = QLabel()
        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(False)
        self.browser.setHtml(HELP_HTML)
        self.matches: list[tuple[int, int]] = []  # (začátek, konec) v dokumentu
        self.current = -1
        top = QHBoxLayout()
        top.addWidget(self.search)
        top.addWidget(self.count)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(top)
        layout.addWidget(self.browser)

    def find(self, jump: bool = False) -> None:
        needle = "".join(_fold(c) for c in self.search.text().strip())
        self.matches, self.current = [], -1
        if needle:
            text = "".join(_fold(c) for c in self.browser.document().toRawText())
            start = text.find(needle)
            while start >= 0:
                self.matches.append((start, start + len(needle)))
                start = text.find(needle, start + len(needle))
        if self.matches and jump:
            self.current = 0
        self._show()

    def step(self, direction: int) -> None:
        if not self.matches:
            return
        self.current = (self.current + direction) % len(self.matches)
        self._show()

    def _show(self) -> None:
        selections = []
        for i, (start, end) in enumerate(self.matches):
            sel = QTextEdit.ExtraSelection()
            cursor = QTextCursor(self.browser.document())
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            fmt = QTextCharFormat()
            fmt.setBackground(COLOR_CURRENT if i == self.current else COLOR_FOUND)
            fmt.setForeground(QColor("black"))  # čitelné i v tmavém režimu
            sel.cursor, sel.format = cursor, fmt
            selections.append(sel)
        self.browser.setExtraSelections(selections)
        if 0 <= self.current < len(self.matches):
            cursor = QTextCursor(self.browser.document())
            cursor.setPosition(self.matches[self.current][0])
            self.browser.setTextCursor(cursor)
            self.browser.ensureCursorVisible()
        if not self.search.text().strip():
            self.count.setText("")
        elif not self.matches:
            self.count.setText("nenalezeno")
        else:
            self.count.setText(f"{self.current + 1} / {len(self.matches)}" if self.current >= 0
                               else f"{len(self.matches)}×")


class ScriptEditor(QPlainTextEdit):
    """Editor skriptu: Enter zachová odsazení předchozího řádku; Tab / Shift+Tab odsadí
    nebo vrátí odsazení všech označených řádků (Shift+Tab i aktuálního řádku bez výběru)."""

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 – Qt
        key = event.key()
        mods = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and mods == Qt.KeyboardModifier.NoModifier:
            self.new_line()
            return
        if key == Qt.Key.Key_Tab and mods == Qt.KeyboardModifier.NoModifier and self._multiline():
            self.indent(1)
            return
        if key == Qt.Key.Key_Backtab:
            self.indent(-1)
            return
        super().keyPressEvent(event)

    def new_line(self) -> None:
        cursor = self.textCursor()
        line = cursor.block().text()
        indent = line[:len(line) - len(line.lstrip(" \t"))]
        indent = indent[:cursor.positionInBlock()]  # Enter uvnitř odsazení
        cursor.insertText("\n" + indent)
        self.setTextCursor(cursor)
        self.ensureCursorVisible()

    def _multiline(self) -> bool:
        cursor = self.textCursor()
        if not cursor.hasSelection():
            return False
        doc = self.document()
        return doc.findBlock(cursor.selectionStart()) != doc.findBlock(cursor.selectionEnd())

    def _block_range(self) -> tuple[int, int]:
        """Čísla prvního a posledního řádku výběru (řádek, na jehož začátku výběr končí, se
        nepočítá)."""
        cursor = self.textCursor()
        doc = self.document()
        first = doc.findBlock(cursor.selectionStart())
        last = doc.findBlock(cursor.selectionEnd())
        if cursor.hasSelection() and last != first and cursor.selectionEnd() == last.position():
            last = last.previous()
        return first.blockNumber(), last.blockNumber()

    def indent(self, direction: int) -> None:
        first, last = self._block_range()
        doc = self.document()
        cursor = QTextCursor(doc)
        cursor.beginEditBlock()  # jedno Zpět pro všechny řádky
        for number in range(first, last + 1):
            block = doc.findBlockByNumber(number)
            cursor.setPosition(block.position())
            if direction > 0:
                cursor.insertText(INDENT)
                continue
            text = block.text()
            if text.startswith("\t"):
                remove = 1
            else:
                remove = min(len(text) - len(text.lstrip(" ")), INDENT_SPACES)
            if remove:
                cursor.setPosition(block.position() + remove, QTextCursor.MoveMode.KeepAnchor)
                cursor.removeSelectedText()
        cursor.endEditBlock()
        if first != last or self.textCursor().hasSelection():  # výběr celých řádků zůstane
            sel = QTextCursor(doc)
            sel.setPosition(doc.findBlockByNumber(first).position())
            end_block = doc.findBlockByNumber(last)
            sel.setPosition(end_block.position() + end_block.length() - 1, QTextCursor.MoveMode.KeepAnchor)
            self.setTextCursor(sel)
