"""Nápověda skriptů s vyhledáváním (okno Skripty) a editor skriptu s odsazováním,
vyhledáváním (EditorSearchBar) a zvýrazněním stejného textu jako je označený."""

from __future__ import annotations

import unicodedata

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QKeyEvent, QKeySequence, QPalette, QShortcut, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QTextBrowser,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..scripting.help import HELP_HTML

COLOR_FOUND = QColor("#fff176")    # nalezený text (jako ve stromu projektu)
COLOR_CURRENT = QColor("#ffa726")  # aktuální výskyt
OCCURRENCE_ALPHA = (0.22, 0.30)  # stejný text jako označený: krytí barvy zvýraznění (světlý, tmavý)
OCCURRENCE_MIN = 2    # zvýrazňuje se označený text od 2 znaků (jen na jednom řádku)
MAX_MATCHES = 2000    # víc výskytů se nepodbarvuje (výkon)
INDENT = "\t"
INDENT_SPACES = 4  # Shift+Tab sebere i odsazení mezerami (nejvýš tolik)


def _fold(ch: str) -> str:
    """Znak pro hledání bez ohledu na velikost písmen a diakritiku (délka zůstává 1)."""
    base = unicodedata.normalize("NFD", ch)[0]
    return base.casefold()[0] if base.casefold() else base


def find_folded(text: str, needle: str) -> list[tuple[int, int]]:
    """Výskyty needle v text bez ohledu na velikost písmen a diakritiku: [(začátek, konec)]."""
    needle = "".join(_fold(c) for c in needle)
    if not needle:
        return []
    hay = "".join(_fold(c) for c in text)
    found, start = [], hay.find(needle)
    while start >= 0 and len(found) < MAX_MATCHES:
        found.append((start, start + len(needle)))
        start = hay.find(needle, start + len(needle))
    return found


def find_exact(text: str, needle: str) -> list[tuple[int, int]]:
    found, start = [], text.find(needle) if needle else -1
    while start >= 0 and len(found) < MAX_MATCHES:
        found.append((start, start + len(needle)))
        start = text.find(needle, start + len(needle))
    return found


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
        self.matches = find_folded(self.browser.document().toRawText(), self.search.text().strip())
        self.current = -1
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
    nebo vrátí odsazení všech označených řádků (Shift+Tab i aktuálního řádku bez výběru).

    Hledání (set_search / step – ovládá EditorSearchBar): výskyty podbarvené žlutě, aktuální
    oranžově. Označený text (od OCCURRENCE_MIN znaků, na jednom řádku) se jinde ve skriptu
    jemně podbarví barvou zvýraznění – okamžité hledání stejného textu."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.search_text = ""
        self.search_matches: list[tuple[int, int]] = []
        self.search_current = -1
        self.occurrences: list[tuple[int, int]] = []
        self.selectionChanged.connect(self._update_occurrences)
        self.document().contentsChanged.connect(self._on_text_changed)

    # --- hledání ---------------------------------------------------------------------

    def set_search(self, text: str) -> int:
        """Hledaný text (prázdný = zrušit). Přejde na první výskyt od kurzoru. Vrací počet."""
        self.search_text = text
        self.search_matches = find_folded(self.toPlainText(), text)
        self.search_current = -1
        if self.search_matches:
            pos = self.textCursor().selectionStart()
            self.search_current = next((i for i, (s, _) in enumerate(self.search_matches) if s >= pos), 0)
            self._go_to_match()
        self._apply_highlights()
        return len(self.search_matches)

    def step(self, direction: int) -> bool:
        """Další (1) / předchozí (-1) výskyt hledaného textu (dokola)."""
        if not self.search_matches:
            return False
        self.search_current = (self.search_current + direction) % len(self.search_matches)
        self._go_to_match()
        self._apply_highlights()
        return True

    def _go_to_match(self) -> None:
        start, end = self.search_matches[self.search_current]
        cursor = QTextCursor(self.document())
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        self.setTextCursor(cursor)
        self.ensureCursorVisible()

    def _on_text_changed(self) -> None:
        if self.search_text:  # úprava textu – výskyty znovu (bez skákání)
            self.search_matches = find_folded(self.toPlainText(), self.search_text)
            self.search_current = min(self.search_current, len(self.search_matches) - 1)
        self._update_occurrences()

    # --- stejný text jako označený -------------------------------------------------------

    def _update_occurrences(self) -> None:
        cursor = self.textCursor()
        selected = cursor.selectedText()
        self.occurrences = []
        if (len(selected) >= OCCURRENCE_MIN and selected.strip() and "\u2029" not in selected):
            start = cursor.selectionStart()
            self.occurrences = [m for m in find_exact(self.toPlainText(), selected) if m[0] != start]
        self._apply_highlights()

    def _apply_highlights(self) -> None:
        selections = []
        accent = QColor(self.palette().color(QPalette.ColorGroup.Active, QPalette.ColorRole.Highlight))
        dark = self.palette().color(QPalette.ColorRole.Base).lightness() < 128
        accent.setAlphaF(OCCURRENCE_ALPHA[1 if dark else 0])
        for start, end in self.occurrences:
            fmt = QTextCharFormat()
            fmt.setBackground(accent)
            selections.append(self._extra(start, end, fmt))
        for i, (start, end) in enumerate(self.search_matches):
            fmt = QTextCharFormat()
            fmt.setBackground(COLOR_CURRENT if i == self.search_current else COLOR_FOUND)
            fmt.setForeground(QColor("black"))  # čitelné i v tmavém režimu
            selections.append(self._extra(start, end, fmt))
        self.setExtraSelections(selections)

    def _extra(self, start: int, end: int, fmt: QTextCharFormat) -> QTextEdit.ExtraSelection:
        sel = QTextEdit.ExtraSelection()
        cursor = QTextCursor(self.document())
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        sel.cursor, sel.format = cursor, fmt
        return sel

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


class EditorSearchBar(QWidget):
    """Hledání v editoru skriptu: pole, počet výskytů a šipky. Enter / F3 = další,
    Shift+Enter / Shift+F3 = předchozí, Esc = zrušit hledání."""

    def __init__(self, editor: ScriptEditor, parent: QWidget | None = None):
        super().__init__(parent)
        self.editor = editor
        self.field = QLineEdit()
        self.field.setPlaceholderText("Hledat ve skriptu…  (Ctrl+F)")
        self.field.setClearButtonEnabled(True)
        self.field.setToolTip("Bez ohledu na velikost písmen a diakritiku. Enter / F3 = další, "
                              "Shift+Enter / Shift+F3 = předchozí, Esc = zrušit")
        self.field.textChanged.connect(self._changed)
        self.field.returnPressed.connect(lambda: self.step(1))
        QShortcut(QKeySequence("Shift+Return"), self.field, activated=lambda: self.step(-1))
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self.field, activated=self.field.clear)
        self.count = QLabel()
        self.btn_prev = self._button("▲", "Předchozí výskyt (Shift+F3)", -1)
        self.btn_next = self._button("▼", "Další výskyt (F3)", 1)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.field, stretch=1)
        lay.addWidget(self.count)
        lay.addWidget(self.btn_prev)
        lay.addWidget(self.btn_next)
        self._show()

    def _button(self, text: str, tip: str, direction: int) -> QToolButton:
        b = QToolButton()
        b.setText(text)
        b.setToolTip(tip)
        b.setAutoRaise(True)
        b.clicked.connect(lambda: self.step(direction))
        return b

    def start(self) -> None:
        """Ctrl+F: zaměřit pole; označený text (na jednom řádku) se vezme jako hledaný."""
        selected = self.editor.textCursor().selectedText()
        if selected and "\u2029" not in selected:
            self.field.setText(selected)
        self.field.setFocus()
        self.field.selectAll()

    def _changed(self, text: str) -> None:
        self.editor.set_search(text)
        self._show()

    def step(self, direction: int) -> None:
        if self.editor.step(direction):
            self._show()

    def _show(self) -> None:
        e = self.editor
        if not self.field.text():
            self.count.setText("")
        elif not e.search_matches:
            self.count.setText("nenalezeno")
        else:
            self.count.setText(f"{e.search_current + 1} z {len(e.search_matches)}")
        for b in (self.btn_prev, self.btn_next):
            b.setEnabled(len(e.search_matches) > 0)
