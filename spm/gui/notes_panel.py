"""Panel poznámek projektu – záložka vedle stromu souborů (lze odpojit do vlastního okna).

- Nahoře filtry: Otevřené / Pro mě / K vybraným souborům / Vše.
- Uprostřed karty poznámek: vyřízení zaškrtnutím, odpověď a úprava přímo v kartě (bez dialogů),
  soubory jsou odkazy (označí soubor ve stromu).
- Dole psaní nové poznámky jako v chatu: týká se souborů vybraných ve stromu, adresát výběrem,
  „@jméno“ v textu = na vědomí (po „@“ se nabídnou uživatelé, Tab vloží), odeslání Ctrl+Enter.
  Poznámka se zveřejní hned.
"""

from __future__ import annotations

from html import escape
from pathlib import Path
from urllib.parse import quote, unquote

from PySide6.QtCore import QEvent, QTimer, Qt, Signal
from PySide6.QtGui import QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..mentions import mentions
from ..notes import Draft, Note
from ..sync import ProjectManager
from ..users import Names
from .completer_edit import CompleterEdit
from .notes_window import (
    FOR_ME_DARK,
    FOR_ME_LIGHT,
    REPLY_BORDER,
    TextSizes,
    fmt_time,
    is_dark,
    markdown_to_html,
    notes_markdown,
)

FILTER_OPEN, FILTER_MINE, FILTER_SELECTED, FILTER_ALL = "open", "mine", "selected", "all"
FILTERS = ((FILTER_OPEN, "Otevřené"), (FILTER_MINE, "Pro mě"),
           (FILTER_SELECTED, "K vybraným souborům"), (FILTER_ALL, "Vše"))
TARGETS_SHOWN = 4  # kolik vybraných souborů vypsat u psaní poznámky (zbytek „a další“)


def touches(targets: list[str], selection: list[str]) -> bool:
    """Týká se poznámka (její soubory / složky) něčeho z výběru? Složka zahrnuje svůj obsah
    a poznámka ke složce se týká i souborů v ní."""
    sel = [s.lower() for s in selection]
    for t in (t.lower() for t in targets):
        for s in sel:
            if t == s or (s.endswith("/") and t.startswith(s)) or (t.endswith("/") and s.startswith(t)):
                return True
    return False


def note_visible(note: Note, flt: str, me: str, selection: list[str]) -> bool:
    if flt == FILTER_OPEN:
        return not note.resolved
    if flt == FILTER_MINE:
        return not note.resolved and (note.concerns(me) or note.is_cc(me))
    if flt == FILTER_SELECTED:
        return touches(note.targets, selection)
    return True


def cc_of(text: str, users: list, recipient: str = "") -> list[list[str]]:
    """Na vědomí ze zmínek „@jméno“ (bez adresáta – ten je „pro“, ne na vědomí)."""
    return [[login, name] for login, name in mentions(text, users) if login != recipient.lower()]


def cc_label(cc, me: str, names: Names | None = None) -> str:
    """„na vědomí: Pepa, Jana“ – já jako „vy“ (jména aktuální z adresáře uživatelů)."""
    names_of = names or Names([])
    names = ["vy" if login == me.lower() else names_of.person(login, name) for login, name in cc]
    return "na vědomí: " + ", ".join(escape(n) for n in names) if names else ""


NEW_COLOR = "#2f80ed"  # tečka u nového


def _new_mark(new: bool) -> str:
    return f'<span style="color:{NEW_COLOR}">●</span> ' if new else ""


def _ctrl_enter(editor: QPlainTextEdit, slot) -> None:
    for key in ("Ctrl+Return", "Ctrl+Enter"):
        shortcut = QShortcut(QKeySequence(key), editor)
        shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
        shortcut.activated.connect(slot)


def recipient_combo(users: list, login: str = "", name: str = "") -> QComboBox:
    combo = QComboBox()
    combo.addItem("všem", "")
    for u in users:
        combo.addItem(u.display, u.login)
    if login:
        index = combo.findData(login.lower())
        if index < 0:  # uživatel, který SPM ještě nespustil
            combo.addItem(name or login, login.lower())
            index = combo.count() - 1
        combo.setCurrentIndex(index)
    return combo


class NoteCard(QFrame):
    """Jedna poznámka s doplněními; akce přímo v kartě."""

    def __init__(self, panel: "NotesPanel", note: Note, seen: int):
        super().__init__()
        self.panel, self.note = panel, note
        me = panel.pm.user
        sizes = TextSizes.from_base(panel.font().pointSizeF())
        for_me = note.is_for(me)
        colors = FOR_ME_DARK if is_dark(panel) else FOR_ME_LIGHT
        self.setObjectName("noteCard")
        if for_me:
            self.setStyleSheet(f"#noteCard {{ background: {colors.background}; border: 1px solid "
                               f"{colors.border}; border-radius: 4px; }} #noteCard QLabel {{ color: {colors.text}; }}")
        else:
            self.setStyleSheet("#noteCard { border: 1px solid palette(mid); border-radius: 4px; }")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(3)

        # nové od posledního zobrazení: tečka a tučné (jako nepřečtený e-mail), vysvětlení v bublině
        if note.is_new(seen, me):
            self.news = "upravená" if note.edited_at else "nová"
        elif any(r.is_new(seen, me) for r in note.replies):
            self.news = "nová odpověď"
        else:
            self.news = ""
        flags = []
        if for_me:
            flags.append(f'<b style="color:{colors.label}">▶ pro vás</b>')
        elif note.recipient:
            flags.append(f"pro {escape(panel.names.person(note.recipient, note.recipient_name))}")
        if note.cc:
            flags.append(f'<b style="color:{colors.label}">▷ na vědomí</b>' if note.is_cc(me) and not for_me
                         else cc_label(note.cc, me, panel.names))
        if note.resolved:
            flags.append(f"vyřízeno ({escape(note.resolved_by or '')}, {fmt_time(note.resolved_at)})")
        head = f"<b>{escape(panel.names.author(note.user, note.author))}</b> · {fmt_time(note.created_at)}"
        if note.edited_at:
            head += f" · upraveno {fmt_time(note.edited_at)}"
        self.head = self._label(_new_mark(bool(self.news)) + head
                                + (" · " + " · ".join(flags) if flags else ""), sizes.meta)
        if self.news:
            self.head.setToolTip({"nová": "Nová poznámka od posledního zobrazení",
                                  "upravená": "Poznámka byla od posledního zobrazení upravena",
                                  "nová odpověď": "Nová odpověď od posledního zobrazení"}[self.news])
        lay.addWidget(self.head)
        if note.targets:
            links = ", ".join(f'<a href="{quote(t)}">{escape(t)}</a>' for t in note.targets)
            self.targets = self._label("Týká se: " + links, sizes.meta)
            self.targets.linkActivated.connect(lambda href: panel.file_clicked.emit(unquote(href)))
            lay.addWidget(self.targets)
        self.body = self._label(markdown_to_html(note.text), sizes.text)
        if self.news in ("nová", "upravená"):
            self._bold(self.body)
        if note.resolved:
            self.body.setEnabled(False)  # vyřízené tlumeně
        lay.addWidget(self.body)
        for r in note.replies:
            new = r.is_new(seen, me)
            edited = f" · upraveno {fmt_time(r.edited_at)}" if r.edited_at else ""
            reply = QFrame()
            reply.setObjectName("noteReply")
            reply.setStyleSheet(f"#noteReply {{ border: none; border-left: 3px solid {REPLY_BORDER}; }}")
            reply_lay = QVBoxLayout(reply)
            reply_lay.setContentsMargins(8, 0, 0, 0)
            reply_lay.setSpacing(1)
            reply_lay.addWidget(self._label(
                f"{_new_mark(new)}<b>{escape(panel.names.author(r.user, r.author))}</b> · "
                f"{fmt_time(r.created_at)}{edited}", sizes.meta))
            text = self._label(markdown_to_html(r.text), sizes.text)
            if new:
                self._bold(text)
            reply_lay.addWidget(text)
            lay.addWidget(reply)

        # akce
        row = QHBoxLayout()
        row.setSpacing(10)
        self.resolved = QCheckBox("Vyřízeno")
        self.resolved.setChecked(note.resolved)
        self.resolved.toggled.connect(lambda on: panel.resolve(note, on))
        row.addWidget(self.resolved)
        self.btn_reply = self._link_button("Odpovědět", self._start_reply)
        row.addWidget(self.btn_reply)
        self.btn_edit = None
        if note.is_author(me):
            self.btn_edit = self._link_button("Upravit", self._start_edit)
            row.addWidget(self.btn_edit)
        if note.versions:
            row.addWidget(self._link_button(f"Předchozí znění ({len(note.versions)})",
                                            lambda: panel.show_versions(note)))
        row.addStretch()
        lay.addLayout(row)

        # odpověď / úprava (skryté, dokud se nezačne)
        self.editor_box = QWidget()
        ed_lay = QVBoxLayout(self.editor_box)
        ed_lay.setContentsMargins(0, 2, 0, 0)
        self.editor = CompleterEdit()
        self.editor.users = panel.users
        self.editor.setMaximumHeight(110)
        self.recipient: QComboBox | None = None
        self.rec_row = QHBoxLayout()
        ed_lay.addWidget(self.editor)
        buttons = QHBoxLayout()
        buttons.addLayout(self.rec_row)
        buttons.addStretch()
        self.btn_send = QPushButton("Odeslat")
        self.btn_send.clicked.connect(self._finish)
        cancel = QPushButton("Zrušit")
        cancel.clicked.connect(self._cancel)
        buttons.addWidget(self.btn_send)
        buttons.addWidget(cancel)
        ed_lay.addLayout(buttons)
        self.editor_box.hide()
        lay.addWidget(self.editor_box)
        _ctrl_enter(self.editor, self._finish)
        self._mode = ""

    @staticmethod
    def _label(html: str, size: float) -> QLabel:
        label = QLabel(html)
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        label.setOpenExternalLinks(False)
        font = QFont(label.font())
        font.setPointSizeF(size)
        label.setFont(font)
        return label

    @staticmethod
    def _bold(label: QLabel) -> None:
        font = QFont(label.font())
        font.setBold(True)
        label.setFont(font)

    def _link_button(self, text: str, slot) -> QToolButton:
        button = QToolButton()
        button.setText(text)
        button.setAutoRaise(True)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(lambda: slot())
        return button

    def _start_reply(self) -> None:
        self._mode = "reply"
        self.editor.clear()
        self.editor.setPlaceholderText("Odpověď… (Ctrl+Enter = odeslat)")
        self.btn_send.setText("Odeslat")
        self.editor_box.show()
        self.editor.setFocus()

    def _start_edit(self) -> None:
        self._mode = "edit"
        self.editor.setPlainText(self.note.text)
        self.btn_send.setText("Uložit")
        if self.recipient is None:
            self.rec_row.addWidget(QLabel("Pro:"))
            self.recipient = recipient_combo(self.panel.users, self.note.recipient,
                                              self.note.recipient_name)
            self.rec_row.addWidget(self.recipient)
        self.body.hide()
        self.editor_box.show()
        self.editor.setFocus()

    def _cancel(self) -> None:
        self._mode = ""
        self.editor_box.hide()
        self.body.show()

    def _finish(self) -> None:
        text = self.editor.toPlainText().strip()
        if not text:
            return
        if self._mode == "reply":
            self.panel.reply(self.note, text, cc_of(text, self.panel.users))
        elif self._mode == "edit":
            login = self.recipient.currentData() if self.recipient else self.note.recipient
            name = self.recipient.currentText() if self.recipient and login else ""
            self.panel.edit(self.note, text, login, name, cc_of(text, self.panel.users, login or ""))


class NotesPanel(QWidget):
    """Poznámky vybraného projektu."""

    file_clicked = Signal(str)        # soubor / složka z poznámky – označit ve stromu
    changed = Signal()                # poznámky se změnily (značky v seznamu projektů)
    counts_changed = Signal(int, int)  # otevřené, pro mě – popisek záložky
    detach_toggled = Signal()

    def __init__(self, pm: ProjectManager, parent: QWidget | None = None):
        super().__init__(parent)
        self.pm = pm
        self.name: str | None = None
        self.notes: list[Note] = []
        self.users: list = []
        self.names = Names([])              # aktuální jména uživatelů (změněná přezdívka)
        self.selection: list[str] = []      # vybrané ve stromu
        self.seen_get = lambda name: 0      # poslední zobrazená změna (nastaví okno)
        self.seen_set = lambda name, seq: None
        self._seen_shown = 0                # „nové“ se počítají od stavu při otevření projektu

        # --- filtry ---
        top = QHBoxLayout()
        top.setSpacing(2)
        self.filter_group = QButtonGroup(self)
        self.filter_buttons: dict[str, QToolButton] = {}
        for key, text in FILTERS:
            button = QToolButton()
            button.setText(text)
            button.setCheckable(True)
            button.setAutoRaise(True)
            self.filter_group.addButton(button)
            self.filter_buttons[key] = button
            top.addWidget(button)
        self.filter_buttons[FILTER_OPEN].setChecked(True)
        for button in self.filter_buttons.values():
            button.toggled.connect(lambda on: on and self._render())
        top.addStretch()
        more = QToolButton()
        more.setText("⋯")
        more.setAutoRaise(True)
        more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        more_menu = QMenu(more)
        more_menu.addAction("Obnovit", lambda: self.reload())
        more_menu.addAction("Export do .md…", lambda: self.export_md())
        more.setMenu(more_menu)
        top.addWidget(more)
        self.btn_detach = QToolButton()
        self.btn_detach.setAutoRaise(True)
        self.btn_detach.clicked.connect(lambda: self.detach_toggled.emit())
        self.set_detached(False)
        top.addWidget(self.btn_detach)

        # --- karty ---
        self.cards = QWidget()
        self.cards_lay = QVBoxLayout(self.cards)
        self.cards_lay.setContentsMargins(2, 2, 2, 2)
        self.cards_lay.setSpacing(6)
        self.cards_lay.addStretch()
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setWidget(self.cards)
        self.empty = QLabel()
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setWordWrap(True)

        # --- psaní ---
        self.composer = QWidget()
        comp = QVBoxLayout(self.composer)
        comp.setContentsMargins(0, 4, 0, 0)
        self.about = QCheckBox()
        self.about.setChecked(True)
        self.about.toggled.connect(lambda _: self._update_about())
        self.about_project = QLabel("Týká se celého projektu")
        about_row = QHBoxLayout()
        about_row.addWidget(self.about)
        about_row.addWidget(self.about_project)
        about_row.addStretch()
        comp.addLayout(about_row)
        self.editor = CompleterEdit()
        self.editor.setPlaceholderText("Napište poznámku…  (@jméno = na vědomí, Ctrl+Enter = odeslat)")
        self.editor.setMaximumHeight(90)
        _ctrl_enter(self.editor, self.send)
        comp.addWidget(self.editor)
        send_row = QHBoxLayout()
        send_row.addWidget(QLabel("Pro:"))
        self.recipient = recipient_combo([])
        self.recipient.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        send_row.addWidget(self.recipient)
        send_row.addStretch()
        self.btn_send = QPushButton("Odeslat")
        self.btn_send.setToolTip("Zveřejní poznámku hned (Ctrl+Enter)")
        self.btn_send.clicked.connect(self.send)
        send_row.addWidget(self.btn_send)
        comp.addLayout(send_row)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.addLayout(top)
        layout.addWidget(self.scroll, stretch=1)
        layout.addWidget(self.empty, stretch=1)
        layout.addWidget(self.composer)
        self._update_about()
        self.set_project(pm, None)

    # --- projekt a výběr ---------------------------------------------------------------

    def set_project(self, pm: ProjectManager, name: str | None) -> None:
        """Poznámky projektu (None = žádný / složka „Jen místně“ – poznámky nejsou)."""
        changed = name != self.name or pm is not self.pm
        self.pm = pm
        self.name = name
        if changed:
            self.editor.clear()
            self._seen_shown = self.seen_get(name) if name else 0
            try:
                self.users = pm.users() if name else []
            except OSError:
                self.users = []
            self.editor.users = self.users
            self.names = Names(self.users)
            self.recipient.clear()
            self.recipient.addItem("všem", "")
            for u in self.users:
                self.recipient.addItem(u.display, u.login)
        self.reload()

    def set_selection(self, scopes: list[str]) -> None:
        """Soubory / složky vybrané ve stromu: týká se jich nová poznámka (a filtr)."""
        self.selection = list(scopes)
        self._update_about()
        if self.current_filter() == FILTER_SELECTED:
            self._render()

    def compose(self, scopes: list[str]) -> None:
        """Nová poznámka k souborům (z nabídky stromu / Ctrl+N): zaměří pole pro psaní."""
        if scopes:
            self.selection = list(scopes)
        self.about.setChecked(bool(scopes))
        self._update_about()
        self.editor.setFocus()

    def set_filter(self, key: str) -> None:
        self.filter_buttons[key].setChecked(True)

    def current_filter(self) -> str:
        return next(k for k, b in self.filter_buttons.items() if b.isChecked())

    def set_detached(self, detached: bool) -> None:
        self.btn_detach.setText("Připojit zpět" if detached else "Odpojit do okna")
        self.btn_detach.setToolTip("Vrátí poznámky do záložky vedle stromu" if detached
                                   else "Poznámky ve vlastním okně (např. na druhém monitoru)")

    def _update_about(self) -> None:
        if self.selection:
            shown = ", ".join(s.rstrip("/").rpartition("/")[2] + ("/" if s.endswith("/") else "")
                              for s in self.selection[:TARGETS_SHOWN])
            more = f" a další ({len(self.selection) - TARGETS_SHOWN})" if len(self.selection) > TARGETS_SHOWN else ""
            self.about.setText(f"Týká se vybraných: {shown}{more}")
            self.about.setToolTip("\n".join(self.selection))
        self.about.setVisible(bool(self.selection))
        self.about_project.setVisible(not self.selection or not self.about.isChecked())

    def targets(self) -> list[str]:
        return list(self.selection) if self.selection and self.about.isChecked() else []

    # --- načtení a zobrazení -------------------------------------------------------------

    def reload(self) -> None:
        if not self.name:
            self.notes = []
        else:
            try:
                self.notes = self.pm.notes(self.name)
            except Exception as e:  # noqa: BLE001 – např. nedostupná síť
                self.notes = []
                self._show_empty(f"Poznámky nelze načíst: {e}")
                return
        self._render()

    def visible_notes(self) -> list[Note]:
        flt = self.current_filter()
        return [n for n in self.notes if note_visible(n, flt, self.pm.user, self.selection)]

    def _render(self) -> None:
        open_notes = [n for n in self.notes if not n.resolved]
        self.counts_changed.emit(len(open_notes), sum(1 for n in open_notes if n.concerns(self.pm.user)))
        self.composer.setVisible(bool(self.name))
        if not self.name:
            self._show_empty("Poznámky jsou jen u projektů na serveru – vyberte projekt.")
            return
        bar = self.scroll.verticalScrollBar().value()
        self._clear_cards()
        shown = self.visible_notes()
        for note in shown:
            self.cards_lay.insertWidget(self.cards_lay.count() - 1, NoteCard(self, note, self._seen_shown))
        if not shown:
            flt = self.current_filter()
            text = {FILTER_OPEN: "Žádné otevřené poznámky.",
                    FILTER_MINE: "Nic pro vás.",
                    FILTER_SELECTED: ("K vybraným souborům nejsou žádné poznámky." if self.selection
                                      else "Vyberte ve stromu soubory nebo složky."),
                    }.get(flt, "Projekt zatím nemá žádné poznámky.")
            self._show_empty(text + "\n\nNovou poznámku napište dole.")
            return
        self.empty.hide()
        self.scroll.show()
        QTimer.singleShot(0, lambda: self.scroll.verticalScrollBar().setValue(bar))
        if self.isVisible():
            self.mark_seen()

    def _show_empty(self, text: str) -> None:
        self._clear_cards()
        self.scroll.hide()
        self.empty.setText(text)
        self.empty.show()

    def _clear_cards(self) -> None:
        while self.cards_lay.count() > 1:
            widget = self.cards_lay.takeAt(0).widget()
            if widget is not None:
                widget.deleteLater()

    def card_widgets(self) -> list[NoteCard]:
        return [self.cards_lay.itemAt(i).widget() for i in range(self.cards_lay.count() - 1)]

    def mark_seen(self) -> None:
        """Zobrazené = přečtené (značka „nová“ zmizí při dalším otevření projektu)."""
        if self.name and self.notes:
            self.seen_set(self.name, max(n.last_seq for n in self.notes))

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.mark_seen()

    def changeEvent(self, event) -> None:  # noqa: N802
        # přepnutí světlého / tmavého motivu za běhu -> překreslit barvy karet
        if event.type() == QEvent.Type.PaletteChange and hasattr(self, "composer"):
            self._render()
        super().changeEvent(event)

    # --- akce ------------------------------------------------------------------------

    def _after_change(self) -> None:
        self.reload()
        self.changed.emit()

    def _do(self, action) -> bool:
        try:
            action()
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Poznámky", str(e))
            self.reload()
            return False
        self._after_change()
        return True

    def send(self) -> None:
        """Zveřejní napsanou poznámku."""
        text = self.editor.toPlainText().strip()
        if not text or not self.name:
            return
        login = self.recipient.currentData() or ""
        name = self.recipient.currentText() if login else ""
        draft = Draft(text, login, name, self.targets(), cc=cc_of(text, self.users, login))
        if self._do(lambda: self.pm.add_notes(self.name, [draft])):
            self.editor.clear()
            self.recipient.setCurrentIndex(0)

    def resolve(self, note: Note, resolved: bool) -> None:
        if resolved != note.resolved:
            # až po doběhnutí signálu zaškrtnutí – karta se při obnovení smaže
            QTimer.singleShot(0, lambda: self._do(
                lambda: self.pm.resolve_note(self.name, note.note_id, resolved=resolved)))

    def reply(self, note: Note, text: str, cc: list | None = None) -> None:
        self._do(lambda: self.pm.reply_note(self.name, note.note_id, text, cc=cc))

    def edit(self, note: Note, text: str, recipient: str | None, recipient_name: str,
             cc: list | None = None) -> None:
        if note.parent_id:
            recipient, recipient_name = None, None
        self._do(lambda: self.pm.edit_note(self.name, note.note_id, text, recipient, recipient_name, cc=cc))

    def show_versions(self, note: Note) -> None:
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
        if not self.name:
            return
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


class DetachedNotes(QWidget):
    """Samostatné okno s panelem poznámek; zavření = vrácení do záložky."""

    closed = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent, Qt.WindowType.Window)
        self.resize(520, 680)
        self.on_top = QCheckBox("Vždy navrchu")
        self.on_top.toggled.connect(self._set_on_top)
        self.lay = QVBoxLayout(self)
        self.lay.addWidget(self.on_top, alignment=Qt.AlignmentFlag.AlignRight)

    def _set_on_top(self, on: bool) -> None:
        visible = self.isVisible()
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, on)
        if visible:
            self.show()  # změna příznaku okno skryje

    def closeEvent(self, event) -> None:  # noqa: N802
        super().closeEvent(event)
        self.closed.emit()
