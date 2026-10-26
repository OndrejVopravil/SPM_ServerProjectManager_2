"""Okno Skripty: úprava skriptů odevzdání a výběr, které se spouštějí na aktuální záložce.

Místní skripty jsou soubory *.spm ve složce scripts vedle aplikace (stejné pro všechny
záložky). Zaškrtnuté se po odevzdání z této záložky spustí v pořadí seznamu (pořadí mění
šipky). Síťové skripty záložky (<síťová složka>/.spm/scripts) spouští místní skript
příkazem CALL SERVER; upravit je lze tady, pokud na to máte práva. Zkušební běh provede
skript z editoru nad vybraným projektem bez zápisu.
"""

from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QKeySequence, QShortcut, QSyntaxHighlighter, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..scripting import lang, store
from ..scripting.lang import ScriptSyntaxError
from ..scripting.model import TEST
from ..scripting.runtime import run_scripts
from .script_help import EditorSearchBar, HelpPanel, ScriptEditor
from .tab_bar import is_dark
from .worker import run_task

KEYWORDS = sorted(lang.RESERVED | {"PROJECT"}, key=len, reverse=True)
IN_WRITE = 1  # stav řádku ve zvýraznění: uvnitř textu WRITE / APPEND


class ScriptHighlighter(QSyntaxHighlighter):
    """Barvy: klíčová slova, texty, {proměnné} v textech a komentáře."""

    def __init__(self, document, dark: bool):
        super().__init__(document)

        def fmt(color: str, bold: bool = False, italic: bool = False) -> QTextCharFormat:
            f = QTextCharFormat()
            f.setForeground(QColor(color))
            if bold:
                f.setFontWeight(QFont.Weight.Bold)
            f.setFontItalic(italic)
            return f
        self.keyword = fmt("#82aaff" if dark else "#1f4fb5", bold=True)
        self.string = fmt("#c3e88d" if dark else "#2e7d32")
        self.var = fmt("#f78c6c" if dark else "#b5541f", bold=True)
        self.comment = fmt("#8a8a8a", italic=True)
        self.word_re = re.compile(r"\b(" + "|".join(KEYWORDS) + r")\b", re.IGNORECASE)

    def highlightBlock(self, text: str) -> None:  # noqa: N802 – Qt
        # text souboru v bloku WRITE / APPEND … END: jen {proměnné}
        if self.previousBlockState() == IN_WRITE:
            if text.strip().upper() == "END":
                self.setFormat(0, len(text), self.keyword)
                self.setCurrentBlockState(0)
                return
            for m in re.finditer(r"\{[^{}]*\}", text):
                self.setFormat(m.start(), m.end() - m.start(), self.var)
            self.setCurrentBlockState(IN_WRITE)
            return
        self.setCurrentBlockState(IN_WRITE if re.match(r"\s*(WRITE|APPEND)\b", text, re.I) else 0)
        for m in self.word_re.finditer(text):
            self.setFormat(m.start(), m.end() - m.start(), self.keyword)
        in_string = False
        depth = 0  # uvnitř {…} v textu uvozovky text neukončují
        start = 0
        i = 0
        while i < len(text):
            ch = text[i]
            if not in_string and ch == "#":
                self.setFormat(i, len(text) - i, self.comment)
                return
            if in_string and ch == "{" and not text.startswith("{{", i):
                depth = 1
            elif depth and ch == "}":
                depth = 0
            elif ch == '"' and not depth:
                if in_string and text.startswith('""', i):
                    i += 2
                    continue
                if in_string:
                    self.setFormat(start, i - start + 1, self.string)
                    for m in re.finditer(r"\{[^{}]*\}", text[start:i]):
                        self.setFormat(start + m.start(), m.end() - m.start(), self.var)
                else:
                    start = i
                in_string = not in_string
            i += 1
        if in_string:
            self.setFormat(start, len(text) - start, self.string)


class ScriptsDialog(QDialog):
    def __init__(self, window, project: str | None = None):
        super().__init__(window)
        self.window_ = window
        self.settings = window.settings
        self.project = project
        self.current: str | None = None    # skript v editoru
        self.folder: Path | None = None    # jeho složka (None = místní skripty)
        self.net_folder = store.network_dir(Path(self.settings.server_root)) if self.settings.server_root else None
        tab = window.app_settings.workspace(self.settings.workspace_id)["name"]
        self.setWindowTitle(f"Skripty – záložka {tab}")
        self.resize(1100, 720)

        intro = QLabel(f"Zaškrtnuté místní skripty se spustí po každém odevzdání se změnami ze záložky "
                       f"<b>{tab}</b> – v pořadí seznamu. Místní skripty: <code>{store.scripts_dir()}</code>"
                       + (f"<br>Síťové skripty záložky (spouští je místní skript příkazem "
                          f"<code>CALL SERVER \"název\"</code>): <code>{self.net_folder}</code>"
                          if self.net_folder else ""))
        intro.setWordWrap(True)
        intro.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        self.list = QListWidget()
        self.list.currentItemChanged.connect(lambda item, prev: self._on_current(self.list, item, prev))
        self.list.itemChanged.connect(lambda _item: self._store_selection())
        self.btn_new = QPushButton("Nový…")
        self.btn_new.clicked.connect(lambda: self.new_script(None))
        self.btn_rename = QPushButton("Přejmenovat…")
        self.btn_rename.clicked.connect(lambda: self.rename_script())
        self.btn_delete = QPushButton("Smazat…")
        self.btn_delete.clicked.connect(lambda: self.delete_script())
        self.btn_up = QPushButton("▲")
        self.btn_up.setToolTip("Spustit dříve")
        self.btn_up.clicked.connect(lambda: self.move(-1))
        self.btn_down = QPushButton("▼")
        self.btn_down.setToolTip("Spustit později")
        self.btn_down.clicked.connect(lambda: self.move(1))

        self.net_list = QListWidget()
        self.net_list.setToolTip(str(self.net_folder or ""))
        self.net_list.currentItemChanged.connect(lambda item, prev: self._on_current(self.net_list, item, prev))
        self.btn_net_new = QPushButton("Nový síťový…")
        self.btn_net_new.setEnabled(self.net_folder is not None)
        self.btn_net_new.clicked.connect(lambda: self.new_script(self.net_folder))

        left = QWidget()
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.addWidget(QLabel("Místní skripty (✔ = spouštět na této záložce):"))
        left_lay.addWidget(self.list, 3)
        row = QHBoxLayout()
        for b in (self.btn_new, self.btn_rename, self.btn_delete):
            row.addWidget(b)
        left_lay.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(self.btn_up)
        row.addWidget(self.btn_down)
        row.addStretch()
        left_lay.addLayout(row)
        left_lay.addWidget(QLabel("Síťové skripty záložky (CALL SERVER):"))
        left_lay.addWidget(self.net_list, 2)
        row = QHBoxLayout()
        row.addWidget(self.btn_net_new)
        row.addStretch()
        left_lay.addLayout(row)

        self.editor = ScriptEditor()
        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        font.setPointSize(max(font.pointSize(), 10))
        self.editor.setFont(font)
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.editor.setTabStopDistance(4 * self.editor.fontMetrics().horizontalAdvance(" "))
        self.highlighter = ScriptHighlighter(self.editor.document(), is_dark(self))
        self.editor.document().modificationChanged.connect(lambda _m: self._update_buttons())
        self.cursor_label = QLabel()
        self.editor.cursorPositionChanged.connect(self._show_cursor)
        # hledání ve skriptu (Ctrl+F, F3 / Shift+F3)
        self.search_bar = EditorSearchBar(self.editor)
        QShortcut(QKeySequence.StandardKey.Find, self, activated=lambda: self.search_bar.start())
        QShortcut(QKeySequence(Qt.Key.Key_F3), self, activated=lambda: self.search_bar.step(1))
        QShortcut(QKeySequence("Shift+F3"), self, activated=lambda: self.search_bar.step(-1))
        self.btn_save = QPushButton("Uložit")
        self.btn_save.setToolTip("Ctrl+S")
        self.btn_save.clicked.connect(lambda: self.save())
        QShortcut(QKeySequence.StandardKey.Save, self, activated=lambda: self.save())
        self.btn_check = QPushButton("Zkontrolovat")
        self.btn_check.setToolTip("Kontrola zápisu skriptu (nic nespouští)")
        self.btn_check.clicked.connect(lambda: self.check())
        self.btn_test = QPushButton(f"Zkušební běh – {project}" if project else "Zkušební běh")
        self.btn_test.setToolTip(
            "Provede skript z editoru nad vybraným projektem, ale nic nezapíše – jen vypíše, co by "
            "se stalo (dialogy skriptu se zobrazí).\nU vašeho aktivovaného projektu použije soubory, "
            "které by se odevzdaly při deaktivaci, jinak všechny soubory projektu (beze změny)."
            if project else "Vyberte v seznamu projekt (na serveru), nad kterým se má skript vyzkoušet.")
        self.btn_test.clicked.connect(lambda: self.test_run())
        self.btn_help = QPushButton("Nápověda")
        self.btn_help.setCheckable(True)
        self.btn_help.toggled.connect(self._toggle_help)
        QShortcut(QKeySequence.StandardKey.HelpContents, self, activated=lambda: self.btn_help.setChecked(True))
        self.where = QLabel()
        tools = QHBoxLayout()
        for b in (self.btn_save, self.btn_check, self.btn_test):
            tools.addWidget(b)
        tools.addWidget(self.where)
        tools.addStretch()
        tools.addWidget(self.cursor_label)
        tools.addWidget(self.btn_help)

        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setFont(font)
        self.output.setPlaceholderText("Výsledek kontroly a zkušebního běhu")

        self.help = HelpPanel()
        self.help.hide()

        editor_split = QSplitter(Qt.Orientation.Vertical)
        editor_split.addWidget(self.editor)
        editor_split.addWidget(self.output)
        editor_split.setStretchFactor(0, 3)
        editor_split.setStretchFactor(1, 1)
        right = QWidget()
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(0, 0, 0, 0)
        right_lay.addLayout(tools)
        right_lay.addWidget(self.search_bar)
        right_lay.addWidget(editor_split)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(left)
        split.addWidget(right)
        split.addWidget(self.help)
        split.setStretchFactor(1, 3)
        split.setStretchFactor(2, 2)
        split.setSizes([250, 650, 450])

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(split, 1)
        layout.addWidget(buttons)

        self._fill()
        self._update_buttons()

    # --- seznamy ---

    def _fill(self, select: str | None = None, folder: Path | None = None) -> None:
        """Naplní oba seznamy a vybere skript (select ve složce folder; jinak dosavadní)."""
        if select is None and self.current is not None:
            select, folder = self.current, self.folder
        names = store.list_scripts()
        enabled = [n for n in self.settings.scripts if n in names]
        ordered = enabled + [n for n in names if n not in enabled]
        self.list.blockSignals(True)
        self.list.clear()
        for name in ordered:
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if name in enabled else Qt.CheckState.Unchecked)
            self.list.addItem(item)
        self.list.blockSignals(False)
        self.net_list.blockSignals(True)
        self.net_list.clear()
        if self.net_folder is not None:
            self.net_list.addItems(store.list_scripts(self.net_folder))
        self.net_list.blockSignals(False)
        missing = [n for n in self.settings.scripts if n not in names]
        if missing:
            self.output.setPlainText("Ve složce skriptů chybí: " + ", ".join(missing)
                                     + " – z výběru záložky byly odebrány.")
            self._store_selection()
        if select is None and ordered:
            select, folder = ordered[0], None
        self.current = None
        target_list = self.net_list if folder is not None else self.list
        for i in range(target_list.count()):
            if target_list.item(i).text() == select:
                target_list.setCurrentRow(i)
                return
        self._load(None, None)

    def names(self) -> list[str]:
        return [self.list.item(i).text() for i in range(self.list.count())]

    def net_names(self) -> list[str]:
        return [self.net_list.item(i).text() for i in range(self.net_list.count())]

    def _store_selection(self) -> None:
        """Zaškrtnuté skripty v pořadí seznamu = skripty záložky."""
        self.settings.scripts = [self.list.item(i).text() for i in range(self.list.count())
                                 if self.list.item(i).checkState() == Qt.CheckState.Checked]
        self.window_._save_settings()

    def move(self, step: int) -> None:
        row = self.list.currentRow()
        new = row + step
        if self.folder is not None or row < 0 or not 0 <= new < self.list.count():
            return
        self.list.blockSignals(True)
        item = self.list.takeItem(row)
        self.list.insertItem(new, item)
        self.list.setCurrentRow(new)
        self.list.blockSignals(False)
        self._store_selection()

    def _on_current(self, widget: QListWidget, item: QListWidgetItem | None,
                    previous: QListWidgetItem | None) -> None:
        if item is None:
            return
        folder = self.net_folder if widget is self.net_list else None
        if item.text() == self.current and folder == self.folder:
            return
        if not self._confirm_leave():
            widget.blockSignals(True)
            widget.setCurrentItem(previous)
            widget.blockSignals(False)
            return
        other = self.list if widget is self.net_list else self.net_list
        other.blockSignals(True)
        other.setCurrentRow(-1)
        other.clearSelection()
        other.blockSignals(False)
        self._load(item.text(), folder)

    def _load(self, name: str | None, folder: Path | None) -> None:
        self.current, self.folder = name, folder
        text = ""
        if name:
            try:
                text = store.load_script(name, folder)
            except OSError as e:
                QMessageBox.warning(self, "Skripty", f"Skript nelze načíst:\n{e}")
        self.editor.setPlainText(text)
        self.editor.document().setModified(False)
        self.editor.setEnabled(name is not None)
        self.where.setText("" if name is None else "síťový skript" if folder is not None else "místní skript")
        self._update_buttons()

    def _update_buttons(self) -> None:
        has = self.current is not None
        local = has and self.folder is None
        modified = self.editor.document().isModified()
        for b in (self.btn_rename, self.btn_up, self.btn_down):
            b.setEnabled(local)
        for b in (self.btn_delete, self.btn_check):
            b.setEnabled(has)
        self.btn_save.setEnabled(has and modified)
        self.btn_test.setEnabled(has and self.project is not None)

    def _toggle_help(self, on: bool) -> None:
        self.help.setVisible(on)
        if on:
            self.help.search.setFocus()

    def _show_cursor(self) -> None:
        c = self.editor.textCursor()
        self.cursor_label.setText(f"Řádek {c.blockNumber() + 1}, sloupec {c.positionInBlock() + 1}")

    # --- soubory ---

    def _confirm_leave(self) -> bool:
        """Neuložené změny: uložit / zahodit / zůstat."""
        if self.current is None or not self.editor.document().isModified():
            return True
        answer = QMessageBox.question(
            self, "Skripty", f"Skript {self.current} má neuložené změny. Uložit je?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel)
        if answer == QMessageBox.StandardButton.Save:
            return self.save()
        return answer == QMessageBox.StandardButton.Discard

    def save(self) -> bool:
        if self.current is None:
            return False
        try:
            store.save_script(self.current, self.editor.toPlainText(), self.folder)
        except PermissionError:
            QMessageBox.critical(self, "Skripty", "Nemáte oprávnění tento skript upravovat:\n"
                                 + str(store.script_path(self.current, self.folder)))
            return False
        except OSError as e:
            QMessageBox.critical(self, "Skripty", f"Skript nelze uložit:\n{e}")
            return False
        self.editor.document().setModified(False)
        self._update_buttons()
        self.check(quiet=True)
        return True

    def _ask_name(self, title: str, folder: Path | None, value: str = "") -> str | None:
        name = value
        while True:
            name, ok = QInputDialog.getText(self, title, "Název skriptu:", QLineEdit.EchoMode.Normal, name)
            if not ok:
                return None
            name = store.normalize_name(name)
            error = store.validate_name(name)
            if error is None and name.lower() != (value or "").lower() and \
                    name.lower() in (n.lower() for n in store.list_scripts(folder)):
                error = f"Skript {name} už existuje."
            if error is None:
                return name
            QMessageBox.warning(self, title, error)

    def new_script(self, folder: Path | None) -> None:
        if not self._confirm_leave():
            return
        title = "Nový síťový skript" if folder is not None else "Nový skript"
        name = self._ask_name(title, folder)
        if name is None:
            return
        try:
            store.save_script(name, store.NETWORK_TEMPLATE if folder is not None else store.TEMPLATE, folder)
        except PermissionError:
            QMessageBox.critical(self, title, f"Nemáte oprávnění zapisovat do:\n{folder}")
            return
        except OSError as e:
            QMessageBox.critical(self, title, f"Skript nelze uložit:\n{e}")
            return
        self.current = None
        self._fill(select=name, folder=folder)

    def rename_script(self) -> None:
        old = self.current
        if old is None or self.folder is not None or not self._confirm_leave():
            return
        new = self._ask_name("Přejmenovat skript", None, old)
        if new is None or new == old:
            return
        try:
            store.rename_script(old, new)
        except OSError as e:
            QMessageBox.critical(self, "Přejmenovat skript", str(e))
            return
        self._rename_in_tabs(old, new)
        self.current = None
        self._fill(select=new)

    def _rename_in_tabs(self, old: str, new: str | None) -> None:
        """Skript přejmenovaný / smazaný – upraví výběr skriptů ve všech záložkách."""
        app = self.window_.app_settings
        for ws in app.workspaces:
            view = app.view(ws["id"])
            if old in view.scripts:
                view.scripts = [new if n == old else n for n in view.scripts if new or n != old]
                app.store(view)
        self.window_._save_settings()

    def delete_script(self) -> None:
        name, folder = self.current, self.folder
        if name is None:
            return
        extra = ("\n\nJe to síťový skript – přestane fungovat CALL ve skriptech všech uživatelů."
                 if folder is not None else "")
        answer = QMessageBox.question(self, "Smazat skript",
                                      f"Smazat skript {name}? (Přesune se do Koše.){extra}")
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            store.delete_script(name, folder)
        except OSError as e:
            QMessageBox.critical(self, "Smazat skript", str(e))
            return
        if folder is None:
            self._rename_in_tabs(name, None)
        self.current = None
        self.editor.document().setModified(False)
        self._fill()

    # --- kontrola a zkušební běh ---

    def check(self, quiet: bool = False) -> bool:
        if self.current is None:
            return False
        try:
            lang.parse_script(self.editor.toPlainText(), self.current)
        except ScriptSyntaxError as e:
            self.output.setPlainText(f"CHYBA: {e}")
            self._goto_line(e.line)
            return False
        if not quiet or self.output.toPlainText().startswith("CHYBA"):
            self.output.setPlainText(f"Skript {self.current} je zapsaný správně.")
        return True

    def _goto_line(self, line: int) -> None:
        block = self.editor.document().findBlockByNumber(max(line - 1, 0))
        cursor = QTextCursor(block)
        cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
        self.editor.setTextCursor(cursor)
        self.editor.setFocus()

    def test_run(self) -> None:
        if self.current is None or self.project is None or not self.check():
            return
        window, project, name = self.window_, self.project, self.current
        scripts = [(name, self.editor.toPlainText())]

        def work(progress=None):
            files = window.pm.preview_files(project)
            env = window.script_env(project, TEST, files)
            return run_scripts(env, scripts, dry=True, progress=progress)

        def done(report) -> None:
            text = report.text
            text += "\n\nZkušební běh – nic se nezapsalo." if report.ok else ""
            self.output.setPlainText(text)
            if not report.ok:
                m = re.match(re.escape(name) + r", řádek (\d+)", report.error)
                if m:
                    self._goto_line(int(m.group(1)))

        run_task(self, f"Zkušební běh – {project}", work, on_done=done)

    def reject(self) -> None:
        if self._confirm_leave():
            super().reject()
