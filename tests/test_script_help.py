"""Nápověda skriptů (úplnost, platné ukázky, hledání) a editor skriptu (odsazování)."""
import html
import os
import re

import pytest

from spm.scripting import lang
from spm.scripting.help import COMMANDS, FUNCTIONS, HELP_HTML


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def anchors() -> set[str]:
    return set(re.findall(r'<a name="([^"]+)">', HELP_HTML))


def test_every_command_and_function_has_section():
    names = anchors()
    commands = {"SET", "FILES", "PARSE", "IF", "FOR", "LOG", "STOP", "EXIT", "COPY", "ZIP", "UNZIP",
                "MKDIR", "DELETE", "RENAME", "REPLACE", "WRITE", "MIRROR", "FORM", "CALL", "BEFORE",
                "RUN", "POST"}
    assert {a for a, _ in COMMANDS} == {c.lower() for c in commands}
    functions = {("replace_f" if f == "REPLACE" else f.lower()) for f in lang.FUNCTIONS}
    assert {a for a, _ in FUNCTIONS} == functions
    assert all(a in names for a, _ in COMMANDS + FUNCTIONS)


def test_links_point_to_existing_sections():
    targets = set(re.findall(r'<a href="#([^"]+)">', HELP_HTML))
    assert targets and targets <= anchors()


def test_all_examples_are_valid_scripts():
    examples = [html.unescape(m) for m in re.findall(r"<pre>(.*?)</pre>", HELP_HTML, re.S)]
    assert len(examples) > 30
    for text in examples:
        lang.parse_script(text, "ukazka")       # chyba zápisu = ScriptSyntaxError


def test_help_search_highlights_without_diacritics(qapp):
    from spm.gui.script_help import HelpPanel
    panel = HelpPanel()
    panel.search.setText("ZRCADLENI")                       # bez diakritiky a velkými
    count = len(panel.matches)
    assert count >= 3 and panel.current == 0
    assert len(panel.browser.extraSelections()) == count
    assert panel.count.text() == f"1 / {count}"
    start, end = panel.matches[0]
    raw = panel.browser.document().toRawText()
    assert raw[start:end].lower() == "zrcadlení"
    panel.step(1)
    assert panel.count.text() == f"2 / {count}"
    panel.step(-2)
    assert panel.current == count - 1                        # dokola
    panel.search.setText("xyzneexistuje")
    assert panel.count.text() == "nenalezeno" and panel.browser.extraSelections() == []
    panel.search.clear()
    assert panel.count.text() == "" and panel.browser.extraSelections() == []


# --- editor ----------------------------------------------------------------------------

def key(editor, k, mods=None):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    QTest.keyClick(editor, k, mods or Qt.KeyboardModifier.NoModifier)


def select_lines(editor, first, last, col_end=None):
    from PySide6.QtGui import QTextCursor
    doc = editor.document()
    c = QTextCursor(doc)
    c.setPosition(doc.findBlockByNumber(first).position() + 1)
    end = doc.findBlockByNumber(last)
    c.setPosition(end.position() + (col_end if col_end is not None else 1), QTextCursor.MoveMode.KeepAnchor)
    editor.setTextCursor(c)


def test_enter_keeps_indent(qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QTextCursor
    from spm.gui.script_help import ScriptEditor
    ed = ScriptEditor()
    ed.setPlainText("IF 1\n\t    LOG \"a\"")
    ed.moveCursor(QTextCursor.MoveOperation.End)
    key(ed, Qt.Key.Key_Return)
    ed.insertPlainText("LOG \"b\"")
    assert ed.toPlainText() == "IF 1\n\t    LOG \"a\"\n\t    LOG \"b\""
    ed.moveCursor(QTextCursor.MoveOperation.Start)
    ed.moveCursor(QTextCursor.MoveOperation.EndOfLine)
    key(ed, Qt.Key.Key_Return)                               # řádek bez odsazení
    assert ed.toPlainText().split("\n")[1] == ""


def test_tab_and_shift_tab_on_selected_lines(qapp):
    from PySide6.QtCore import Qt
    from spm.gui.script_help import ScriptEditor
    ed = ScriptEditor()
    ed.setPlainText("A\nB\nC\nD")
    select_lines(ed, 1, 3, col_end=0)                       # výběr končí na začátku D – D se nepočítá
    key(ed, Qt.Key.Key_Tab)
    assert ed.toPlainText() == "A\n\tB\n\tC\nD"
    assert ed.textCursor().selectedText().replace(" ", "\n") == "\tB\n\tC"   # výběr zůstal
    key(ed, Qt.Key.Key_Tab)
    assert ed.toPlainText() == "A\n\t\tB\n\t\tC\nD"
    key(ed, Qt.Key.Key_Backtab)
    assert ed.toPlainText() == "A\n\tB\n\tC\nD"
    ed.undo()                                                # jedno Zpět = celé odsazení
    assert ed.toPlainText() == "A\n\t\tB\n\t\tC\nD"


def test_shift_tab_spaces_and_single_line(qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QTextCursor
    from spm.gui.script_help import ScriptEditor
    ed = ScriptEditor()
    ed.setPlainText("      SET a = 1\nB")
    ed.moveCursor(QTextCursor.MoveOperation.Start)
    key(ed, Qt.Key.Key_Backtab)                              # bez výběru = aktuální řádek
    assert ed.toPlainText() == "  SET a = 1\nB"
    key(ed, Qt.Key.Key_Backtab)
    assert ed.toPlainText() == "SET a = 1\nB"
    key(ed, Qt.Key.Key_Tab)                                  # bez výběru = běžný tabulátor
    assert ed.toPlainText() == "\tSET a = 1\nB"
