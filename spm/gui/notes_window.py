"""Plovoucí okno předávacích poznámek k projektu.

Nahoře zveřejněné poznámky (Markdown) – nové od posledního zobrazení a adresované mně jsou
zvýrazněné, soubory jsou odkazy (označí soubor ve stromu), poznámku lze označit jako vyřízenou.
Dole (jen u projektu, který mám aktivovaný) moje rozepsané poznámky – průběžně se ukládají do
místní kopie a zveřejní se s odevzdáním. Okno může zůstat otevřené vedle práce.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from urllib.parse import quote, unquote

from PySide6.QtCore import QTimer, Qt, QUrl, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..notes import Draft, Note
from ..sync import ProjectManager

AUTOSAVE_MS = 800


def fmt_time(iso: str | None) -> str:
    try:
        t = datetime.fromisoformat(iso or "")
    except ValueError:
        return iso or ""
    return f"{t.day}. {t.month}. {t.year} {t:%H:%M}"


def recipient_from_text(text: str, known: dict[str, str]) -> tuple[str, str]:
    """Adresát z textu pole „Pro“: známé jméno -> (login, jméno); jinak text = login."""
    text = text.strip()
    if not text:
        return "", ""
    if text in known:
        return known[text], text
    if text.endswith(")") and " (" in text:
        return text.rsplit(" (", 1)[1][:-1].strip(), ""
    return text, ""


def notes_markdown(notes: list[Note], me: str, seen: int, show_resolved: bool,
                   links: bool = True) -> str:
    """Poznámky jako jeden Markdown dokument (pro okno i export do .md)."""
    parts = []
    for n in notes:
        if n.resolved and not show_resolved:
            continue
        flags = []
        if links and n.note_id > seen:
            flags.append("**NOVÁ**")
        if n.is_for(me):
            flags.append("**pro vás**")
        elif n.recipient:
            flags.append(f"pro {n.recipient_name or n.recipient}")
        if n.resolved:
            flags.append(f"vyřízeno ({n.resolved_by}, {fmt_time(n.resolved_at)})")
        head = f"### {n.author} · {fmt_time(n.created_at)}"
        lines = [head + (" · " + " · ".join(flags) if flags else "")]
        if n.targets:
            if links:
                lines.append("Týká se: " + ", ".join(f"[{t}](spmfile:{quote(t)})" for t in n.targets))
            else:
                lines.append("Týká se: " + ", ".join(f"`{t}`" for t in n.targets))
        lines.append("")
        lines.append(n.text)
        if links:
            lines.append("")
            lines.append(f"[Znovu otevřít](spmreopen:{n.note_id})" if n.resolved
                         else f"[Označit jako vyřízené](spmresolve:{n.note_id})")
        parts.append("\n".join(lines))
    return "\n\n---\n\n".join(parts)


class NotesWindow(QWidget):
    """Okno poznámek jednoho projektu (jedné záložky)."""

    file_clicked = Signal(str)       # cesta souboru / složky – označit ve stromu
    changed = Signal()               # zveřejněné poznámky se změnily (vyřízení)

    def __init__(self, pm: ProjectManager, name: str, parent: QWidget | None = None,
                 seen_get=lambda: 0, seen_set=lambda note_id: None):
        super().__init__(parent, Qt.WindowType.Window)
        self.pm, self.name = pm, name
        self._seen_get, self._seen_set = seen_get, seen_set
        self.setWindowTitle(f"Poznámky – projekt {name}")
        self.resize(560, 680)
        self.notes: list[Note] = []
        self.drafts: list[Draft] = []
        self._current: Draft | None = None
        self._loading = False
        self._users = []

        self.on_top = QCheckBox("Vždy navrchu")
        self.on_top.toggled.connect(self._set_on_top)
        self.show_resolved = QCheckBox("Zobrazit vyřízené")
        self.show_resolved.toggled.connect(lambda _: self._render())
        refresh = QPushButton("Obnovit")
        refresh.clicked.connect(lambda: self.reload())
        export = QPushButton("Export do .md…")
        export.clicked.connect(lambda: self.export_md())
        top = QHBoxLayout()
        top.addWidget(self.show_resolved)
        top.addWidget(self.on_top)
        top.addStretch()
        top.addWidget(refresh)
        top.addWidget(export)

        self.view = QTextBrowser()
        self.view.setOpenLinks(False)
        self.view.anchorClicked.connect(self._on_link)

        # --- rozepsané poznámky ---
        self.drafts_box = QWidget()
        self.draft_list = QListWidget()
        self.draft_list.setMaximumHeight(110)
        self.draft_list.currentRowChanged.connect(self._select_draft)
        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText("Text poznámky (Markdown: # nadpis, **tučně**, - odrážka)…")
        self.editor.textChanged.connect(self._edited)
        self.recipient = QComboBox()
        self.recipient.setEditable(True)
        self.recipient.setToolTip("Adresát poznámky (login nebo výběr ze seznamu); prázdné = pro všechny")
        self.recipient.currentTextChanged.connect(lambda _: self._edited())
        self.targets = QLabel()
        self.targets.setWordWrap(True)
        btn_new = QPushButton("Nová poznámka")
        btn_new.clicked.connect(lambda: self.add_draft([]))
        self.btn_delete = QPushButton("Smazat rozepsanou")
        self.btn_delete.clicked.connect(lambda: self._delete_draft())
        self.btn_clear_targets = QPushButton("Zrušit vazbu na soubory")
        self.btn_clear_targets.clicked.connect(lambda: self._clear_targets())
        buttons = QHBoxLayout()
        buttons.addWidget(btn_new)
        buttons.addWidget(self.btn_delete)
        buttons.addWidget(self.btn_clear_targets)
        buttons.addStretch()
        rec_row = QHBoxLayout()
        rec_row.addWidget(QLabel("Pro:"))
        rec_row.addWidget(self.recipient, stretch=1)
        lay = QVBoxLayout(self.drafts_box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(QLabel("<b>Moje rozepsané poznámky</b> – ostatní je zatím nevidí, "
                             "zveřejní se s odevzdáním"))
        lay.addWidget(self.draft_list)
        lay.addLayout(rec_row)
        lay.addWidget(self.targets)
        lay.addWidget(self.editor, stretch=1)
        lay.addLayout(buttons)

        self._autosave = QTimer(self)
        self._autosave.setSingleShot(True)
        self._autosave.timeout.connect(self.flush)

        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.view)
        split.addWidget(self.drafts_box)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(split)
        self.reload()

    # --- zveřejněné poznámky -----------------------------------------------------------

    @property
    def editable(self) -> bool:
        """Rozepsané poznámky jen u projektu, který mám aktivovaný (má místní kopii)."""
        return self.pm.local_dir(self.name).is_dir() and self._is_mine()

    def _is_mine(self) -> bool:
        status = self.pm.project_status(self.name)
        return bool(status and status.mine)

    def reload(self) -> None:
        try:
            self.notes = self.pm.notes(self.name)
        except Exception as e:  # noqa: BLE001 – např. nedostupná síť
            self.notes = []
            self.view.setPlainText(f"Poznámky nelze načíst: {e}")
            return
        self._render()
        self.drafts_box.setVisible(self.editable)
        if self.editable:
            self._load_drafts()

    def _render(self) -> None:
        seen = self._seen_get()
        text = notes_markdown(self.notes, self.pm.user, seen, self.show_resolved.isChecked())
        if not text:
            text = ("*Projekt nemá žádné otevřené poznámky.*" if self.notes
                    else "*Projekt zatím nemá žádné poznámky.*")
        bar = self.view.verticalScrollBar().value()
        self.view.setMarkdown(text)
        self.view.verticalScrollBar().setValue(bar)
        if self.notes:
            self._seen_set(max(n.note_id for n in self.notes))  # zobrazeno = přečteno

    def _on_link(self, url: QUrl) -> None:
        scheme, value = url.scheme(), unquote(url.path() or url.toString().split(":", 1)[-1])
        if scheme == "spmfile":
            self.file_clicked.emit(value)
        elif scheme in ("spmresolve", "spmreopen"):
            try:
                self.pm.resolve_note(self.name, int(value), resolved=scheme == "spmresolve")
            except Exception as e:  # noqa: BLE001
                QMessageBox.critical(self, "Poznámky", str(e))
            self.notes = self.pm.notes(self.name)
            self._render()
            self.changed.emit()

    def export_md(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export poznámek", f"{self.name}_poznamky.md",
                                              "Markdown (*.md)")
        if not path:
            return
        text = f"# Poznámky – projekt {self.name}\n\n" + notes_markdown(
            self.notes, self.pm.user, 0, show_resolved=True, links=False)
        try:
            Path(path).write_text(text + "\n", encoding="utf-8")
        except OSError as e:
            QMessageBox.critical(self, "Export poznámek", str(e))

    def _set_on_top(self, on: bool) -> None:
        visible = self.isVisible()
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, on)
        if visible:
            self.show()  # změna příznaku okno skryje

    # --- rozepsané poznámky -------------------------------------------------------------

    def _load_drafts(self) -> None:
        self._loading = True
        try:
            self.drafts = self.pm.drafts(self.name)
            try:
                self._users = self.pm.users()
            except OSError:
                self._users = []
            self.recipient.clear()
            self.recipient.addItem("", "")
            for u in self._users:
                self.recipient.addItem(u.display, u.login)
            self._fill_list()
            self.draft_list.setCurrentRow(0 if self.drafts else -1)
            self._select_draft(self.draft_list.currentRow())
        finally:
            self._loading = False

    def _fill_list(self) -> None:
        row = self.draft_list.currentRow()
        self.draft_list.blockSignals(True)
        self.draft_list.clear()
        for d in self.drafts:
            to = f" → {d.recipient_name or d.recipient}" if d.recipient else ""
            QListWidgetItem(d.title + to, self.draft_list)
        self.draft_list.setCurrentRow(min(row, len(self.drafts) - 1))
        self.draft_list.blockSignals(False)

    def _select_draft(self, row: int) -> None:
        self.flush()
        self._current = self.drafts[row] if 0 <= row < len(self.drafts) else None
        self._loading = True
        try:
            d = self._current
            for w in (self.editor, self.recipient, self.btn_delete, self.btn_clear_targets):
                w.setEnabled(d is not None)
            self.editor.setPlainText(d.text if d else "")
            index = self.recipient.findData(d.recipient) if d and d.recipient else 0
            if d and d.recipient and index < 0:
                self.recipient.setEditText(d.recipient_name or d.recipient)
            else:
                self.recipient.setCurrentIndex(max(index, 0))
            self.targets.setText(("Týká se: " + ", ".join(d.targets)) if d and d.targets
                                 else "Týká se: celého projektu")
        finally:
            self._loading = False

    def _edited(self) -> None:
        if self._loading or self._current is None:
            return
        d = self._current
        d.text = self.editor.toPlainText()
        d.recipient, d.recipient_name = self._recipient()
        self._autosave.start(AUTOSAVE_MS)
        item = self.draft_list.currentItem()
        if item is not None:
            to = f" → {d.recipient_name or d.recipient}" if d.recipient else ""
            item.setText(d.title + to)

    def _recipient(self) -> tuple[str, str]:
        """(login, zobrazované jméno) adresáta z výběru; ručně napsaný text = login uživatele,
        který aplikaci ještě nespustil („Jméno (login)“ se rozpozná také)."""
        return recipient_from_text(self.recipient.currentText(),
                                   {self.recipient.itemText(i): self.recipient.itemData(i)
                                    for i in range(1, self.recipient.count())})

    def flush(self) -> None:
        """Uloží rozepsané poznámky do místní kopie (hned – např. před odevzdáním)."""
        self._autosave.stop()
        if self.editable or self.drafts:
            try:
                self.pm.save_drafts(self.name, self.drafts)
            except OSError as e:
                QMessageBox.warning(self, "Poznámky", f"Rozepsané poznámky nelze uložit:\n{e}")

    def add_draft(self, targets: list[str]) -> None:
        """Nová rozepsaná poznámka (volitelně k souborům / složkám) – a zaměří editor."""
        if not self.editable:
            return
        self.flush()
        self.drafts.append(Draft(targets=list(targets)))
        self._fill_list()
        self.draft_list.setCurrentRow(len(self.drafts) - 1)
        self._select_draft(len(self.drafts) - 1)
        self.flush()
        self.show()
        self.raise_()
        self.activateWindow()
        self.editor.setFocus()

    def _delete_draft(self) -> None:
        if self._current is None:
            return
        if self._current.text.strip():
            answer = QMessageBox.question(self, "Poznámky", f"Smazat rozepsanou poznámku „{self._current.title}“?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.drafts.remove(self._current)
        self._current = None
        self._fill_list()
        self._select_draft(self.draft_list.currentRow())
        self.flush()

    def _clear_targets(self) -> None:
        if self._current is not None:
            self._current.targets = []
            self.targets.setText("Týká se: celého projektu")
            self.flush()

    def closeEvent(self, event) -> None:
        self.flush()
        super().closeEvent(event)


class QuickNoteDialog(QDialog):
    """Poznámka k projektu, který nemám aktivovaný – zveřejní se hned po potvrzení."""

    def __init__(self, name: str, targets: list[str], users: list, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(f"Přidat poznámku – projekt {name}")
        self.resize(520, 360)
        self.targets = list(targets)
        info = QLabel("Projekt nemáte aktivovaný – poznámka se zveřejní hned po potvrzení "
                      "a uvidí ji všichni (adresátovi se zvýrazní).")
        info.setWordWrap(True)
        self.recipient = QComboBox()
        self.recipient.setEditable(True)
        self.recipient.addItem("", "")
        for u in users:
            self.recipient.addItem(u.display, u.login)
        self.text = QPlainTextEdit()
        self.text.setPlaceholderText("Text poznámky (Markdown: # nadpis, **tučně**, - odrážka)…")
        self.text.textChanged.connect(self._validate)
        about = QLabel("Týká se: " + (", ".join(self.targets) if self.targets else "celého projektu"))
        about.setWordWrap(True)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                        | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Zveřejnit")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Zrušit")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        rec_row = QHBoxLayout()
        rec_row.addWidget(QLabel("Pro:"))
        rec_row.addWidget(self.recipient, stretch=1)
        layout = QVBoxLayout(self)
        layout.addWidget(info)
        layout.addLayout(rec_row)
        layout.addWidget(about)
        layout.addWidget(self.text, stretch=1)
        layout.addWidget(self.buttons)
        self._validate()

    def _validate(self) -> None:
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(bool(self.text.toPlainText().strip()))

    def draft(self) -> Draft:
        login, name = recipient_from_text(self.recipient.currentText(),
                                          {self.recipient.itemText(i): self.recipient.itemData(i)
                                           for i in range(1, self.recipient.count())})
        return Draft(self.text.toPlainText(), login, name, self.targets)
