"""Plovoucí okno předávacích poznámek k projektu.

Nahoře zveřejněné poznámky (Markdown) – nové od posledního zobrazení a adresované mně jsou
zvýrazněné, soubory jsou odkazy (označí soubor ve stromu), poznámku lze označit jako vyřízenou.
Dole (jen u projektu, který mám aktivovaný) moje rozepsané poznámky – průběžně se ukládají do
místní kopie a zveřejní se s odevzdáním. Okno může zůstat otevřené vedle práce.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from html import escape
from pathlib import Path
from urllib.parse import quote, unquote

from PySide6.QtCore import QEvent, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QPalette, QTextDocument
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
    """Poznámky jako jeden Markdown dokument (export do .md)."""
    parts = []
    for n in notes:
        if n.resolved and not show_resolved:
            continue
        flags = []
        if n.is_for(me):
            flags.append("**pro vás**")
        elif n.recipient:
            flags.append(f"pro {n.recipient_name or n.recipient}")
        if n.edited_at:
            flags.append(f"upraveno {fmt_time(n.edited_at)}")
        if n.resolved:
            flags.append(f"vyřízeno ({n.resolved_by}, {fmt_time(n.resolved_at)})")
        head = f"### {n.author} · {fmt_time(n.created_at)}"
        lines = [head + (" · " + " · ".join(flags) if flags else "")]
        if n.targets:
            lines.append("Týká se: " + ", ".join(f"`{t}`" for t in n.targets))
        lines.append("")
        lines.append(n.text)
        for r in n.replies:
            edited = f" · upraveno {fmt_time(r.edited_at)}" if r.edited_at else ""
            lines.append("")
            lines.append(f"> **{r.author}** · {fmt_time(r.created_at)}{edited}  ")
            lines += [f"> {line}" for line in r.text.splitlines()]
        parts.append("\n".join(lines))
    return "\n\n---\n\n".join(parts)


@dataclass(frozen=True)
class ForMeColors:
    """Barvy karty poznámky „pro mě“."""
    background: str
    border: str
    label: str      # nápis „PRO VÁS“
    text: str


# světlý režim: tlumená teplá béžová, tmavý text
FOR_ME_LIGHT = ForMeColors(background="#faf6ea", border="#d8c9a0", label="#8a6d1f", text="#000000")
# tmavý režim: šedožlutá (tmavá olivově šedá), bílý text
FOR_ME_DARK = ForMeColors(background="#4a4636", border="#7d7350", label="#e3c86f", text="#ffffff")
FOR_ME_BACKGROUND = FOR_ME_LIGHT.background  # (pro testy a zpětnou kompatibilitu)


def is_dark(widget: QWidget) -> bool:
    """Tmavý režim – podle jasu pozadí (funguje se systémovým tmavým motivem i vlastní paletou)."""
    return widget.palette().color(QPalette.ColorRole.Base).lightness() < 128
REPLY_BORDER = "#9e9e9e"        # odsazení doplnění


def markdown_to_html(text: str) -> str:
    """Text poznámky (Markdown) jako HTML fragment (obsah <body>)."""
    doc = QTextDocument()
    doc.setMarkdown(text)
    html = doc.toHtml()
    match = re.search(r"<body[^>]*>(.*)</body>", html, re.S)
    return match.group(1) if match else html


def _links(*items: tuple[str, str]) -> str:
    return "<p>" + " &nbsp;·&nbsp; ".join(f'<a href="{href}">{escape(text)}</a>'
                                           for href, text in items) + "</p>"


def _edited(n: Note) -> str:
    if not n.edited_at:
        return ""
    return f" · upraveno {fmt_time(n.edited_at)}"


def _reply_html(r: Note, me: str, seen: int) -> str:
    new = " · <b>NOVÉ</b>" if r.is_new(seen, me) else ""
    actions = []
    if r.is_author(me):
        actions.append((f"spmedit:{r.note_id}", "Upravit"))
    if r.versions:
        actions.append((f"spmversions:{r.note_id}", f"Předchozí znění ({len(r.versions)})"))
    return (f'<table width="100%" cellpadding="4" cellspacing="0" border="0"><tr>'
            f'<td width="6" bgcolor="{REPLY_BORDER}"></td><td>'
            f"<p><b>{escape(r.author)}</b> · {fmt_time(r.created_at)}{_edited(r)}{new}</p>"
            f"{markdown_to_html(r.text)}{_links(*actions) if actions else ''}</td></tr></table>")


def notes_html(notes: list[Note], me: str, seen: int, show_resolved: bool,
               colors: ForMeColors = FOR_ME_LIGHT) -> str:
    """Poznámky pro okno: každá jako karta s doplněními pod sebou; poznámka pro mě je
    zvýrazněná barvou a rámečkem. Odkazy: soubory (spmfile), vyřídit / znovu otevřít,
    doplnit (spmreply), upravit (spmedit – jen autor), předchozí znění (spmversions)."""
    cards = []
    for n in notes:
        if n.resolved and not show_resolved:
            continue
        for_me = n.is_for(me)
        flags = []
        if n.is_new(seen, me):
            flags.append("<b>NOVÁ</b>" if not n.edited_at else "<b>UPRAVENÁ</b>")
        elif any(r.is_new(seen, me) for r in n.replies):
            flags.append("<b>nové doplnění</b>")
        if for_me:
            flags.append("<b>pro vás</b>")
        elif n.recipient:
            flags.append(f"pro {escape(n.recipient_name or n.recipient)}")
        if n.resolved:
            flags.append(f"vyřízeno ({escape(n.resolved_by or '')}, {fmt_time(n.resolved_at)})")
        head = f"<b>{escape(n.author)}</b> · {fmt_time(n.created_at)}{_edited(n)}"
        if flags:
            head += " · " + " · ".join(flags)
        rows = []
        if for_me:
            rows.append(f'<p style="color:{colors.label};"><b>▶ PRO VÁS</b></p>')
        rows.append(f"<p>{head}</p>")
        if n.targets:
            rows.append("<p>Týká se: " + ", ".join(
                f'<a href="spmfile:{quote(t)}">{escape(t)}</a>' for t in n.targets) + "</p>")
        rows.append(markdown_to_html(n.text))
        rows += [_reply_html(r, me, seen) for r in n.replies]
        actions = [(f"spmreply:{n.note_id}", "Doplnit")]
        if n.is_author(me):
            actions.append((f"spmedit:{n.note_id}", "Upravit"))
        if n.versions:
            actions.append((f"spmversions:{n.note_id}", f"Předchozí znění ({len(n.versions)})"))
        actions.append((f"spmreopen:{n.note_id}", "Znovu otevřít") if n.resolved
                       else (f"spmresolve:{n.note_id}", "Označit jako vyřízené"))
        rows.append(_links(*actions))
        body = "".join(rows)
        if for_me:
            cards.append(f'<table width="100%" cellpadding="8" cellspacing="0" border="1" '
                         f'style="border-color:{colors.border}; border-style:solid;" '
                         f'bgcolor="{colors.background}"><tr><td style="color:{colors.text};">'
                         f"{body}</td></tr></table>")
        else:
            cards.append(f'<table width="100%" cellpadding="8" cellspacing="0" border="0">'
                         f"<tr><td>{body}</td></tr></table>")
    return "<hr>".join(cards)


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
        html = notes_html(self.notes, self.pm.user, seen, self.show_resolved.isChecked(),
                          FOR_ME_DARK if is_dark(self.view) else FOR_ME_LIGHT)
        if not html:
            html = ("<p><i>Projekt nemá žádné otevřené poznámky.</i></p>" if self.notes
                    else "<p><i>Projekt zatím nemá žádné poznámky.</i></p>")
        bar = self.view.verticalScrollBar().value()
        self.view.setHtml(html)
        self.view.verticalScrollBar().setValue(bar)
        if self.notes:
            self._seen_set(max(n.last_seq for n in self.notes))  # zobrazeno = přečteno

    def _find(self, note_id: int) -> Note | None:
        for n in self.notes:
            if n.note_id == note_id:
                return n
            for r in n.replies:
                if r.note_id == note_id:
                    return r
        return None

    def _on_link(self, url: QUrl) -> None:
        scheme, value = url.scheme(), unquote(url.path() or url.toString().split(":", 1)[-1])
        if scheme == "spmfile":
            self.file_clicked.emit(value)
            return
        note = self._find(int(value)) if value.isdigit() else None
        if note is None:
            return
        try:
            if scheme in ("spmresolve", "spmreopen"):
                self.pm.resolve_note(self.name, note.note_id, resolved=scheme == "spmresolve")
            elif scheme == "spmreply":
                dlg = NoteEditDialog(f"Doplnit poznámku – {note.author}", "", None, [], self,
                                     hint="Doplnění se zveřejní hned. Původní poznámka se nemění.",
                                     ok_text="Doplnit")
                if not dlg.exec():
                    return
                self.pm.reply_note(self.name, note.note_id, dlg.text_value())
            elif scheme == "spmedit":
                users = [] if note.parent_id else self._user_list()
                dlg = NoteEditDialog("Upravit poznámku", note.text,
                                     None if note.parent_id else (note.recipient, note.recipient_name),
                                     users, self, ok_text="Uložit",
                                     hint="Předchozí znění se uloží; ostatním se poznámka znovu "
                                          "označí jako upravená.")
                if not dlg.exec():
                    return
                recipient, recipient_name = dlg.recipient_value() if not note.parent_id else (None, None)
                self.pm.edit_note(self.name, note.note_id, dlg.text_value(), recipient, recipient_name)
            elif scheme == "spmversions":
                self._show_versions(note)
                return
            else:
                return
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Poznámky", str(e))
        self.notes = self.pm.notes(self.name)
        self._render()
        self.changed.emit()

    def _user_list(self) -> list:
        try:
            return self.pm.users()
        except OSError:
            return []

    def _show_versions(self, note: Note) -> None:
        lines = [f"Aktuální znění ({fmt_time(note.edited_at)}):\n{note.text}"]
        for v in note.versions:
            to = f" – pro {v.recipient_name or v.recipient}" if v.recipient else ""
            lines.append(f"Nahrazeno {fmt_time(v.saved_at)}{to}:\n{v.text}")
        box = QMessageBox(QMessageBox.Icon.Information, "Předchozí znění poznámky",
                          f"Poznámka byla upravena {len(note.versions)}×. Všechna znění jsou "
                          "v podrobnostech (nejnovější první).", parent=self)
        box.setDetailedText("\n\n────────\n\n".join(lines))
        box.exec()

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

    def changeEvent(self, event) -> None:
        # přepnutí světlého / tmavého motivu za běhu -> překreslit barvy karet
        if event.type() == QEvent.Type.PaletteChange and hasattr(self, "view"):
            self._render()
        super().changeEvent(event)

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


class NoteEditDialog(QDialog):
    """Úprava zveřejněné poznámky autorem, nebo doplnění k poznámce (bez adresáta)."""

    def __init__(self, title: str, text: str, recipient: tuple[str, str] | None, users: list,
                 parent: QWidget | None = None, hint: str = "", ok_text: str = "OK"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(520, 360)
        layout = QVBoxLayout(self)
        if hint:
            info = QLabel(hint)
            info.setWordWrap(True)
            layout.addWidget(info)
        self.recipient = None
        if recipient is not None:  # poznámka (ne doplnění): adresát lze změnit
            self.recipient = QComboBox()
            self.recipient.setEditable(True)
            self.recipient.addItem("", "")
            for u in users:
                self.recipient.addItem(u.display, u.login)
            login, name = recipient
            index = self.recipient.findData(login) if login else 0
            if login and index < 0:
                self.recipient.setEditText(name or login)
            else:
                self.recipient.setCurrentIndex(max(index, 0))
            row = QHBoxLayout()
            row.addWidget(QLabel("Pro:"))
            row.addWidget(self.recipient, stretch=1)
            layout.addLayout(row)
        self.text = QPlainTextEdit(text)
        self.text.setPlaceholderText("Text (Markdown: # nadpis, **tučně**, - odrážka)…")
        self.text.textChanged.connect(self._validate)
        layout.addWidget(self.text, stretch=1)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                        | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText(ok_text)
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Zrušit")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self._validate()
        self.text.setFocus()

    def _validate(self) -> None:
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(bool(self.text.toPlainText().strip()))

    def text_value(self) -> str:
        return self.text.toPlainText()

    def recipient_value(self) -> tuple[str, str]:
        if self.recipient is None:
            return "", ""
        return recipient_from_text(self.recipient.currentText(),
                                   {self.recipient.itemText(i): self.recipient.itemData(i)
                                    for i in range(1, self.recipient.count())})
