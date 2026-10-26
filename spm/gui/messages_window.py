"""Okno Vzkazy – poznámky pro konkrétní uživatele, nevázané na projekt (viz messages.py).

- Nahoře filtry: Otevřené / Pro mě / Odeslané / Vše (vidět jsou jen vzkazy, které se mě týkají).
- Karty vzkazů jako u poznámek projektu: vyřízení (adresát), přečteno (na vědomí), odpověď
  a úprava přímo v kartě; „#projekt“ je odkaz (vybere projekt v seznamu).
- Dole psaní nového vzkazu: adresáti výběrem (aspoň jeden), „@jméno“ = na vědomí,
  „#“ nabídne projekty, Tab vloží, Ctrl+Enter odešle.
Vzkazy jsou ze všech síťových složek záložek projektů; s více složkami se u vzkazu ukazuje,
do které patří, a při psaní se složka vybírá.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from typing import Callable
from urllib.parse import unquote

from PySide6.QtCore import QEvent, QTimer, Qt, Signal
from PySide6.QtGui import QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import messages as msgs
from ..mentions import link_projects, mentions, project_refs
from ..messages import Message, MessageStore
from ..users import Names
from .completer_edit import CompleterEdit
from .notes_window import FOR_ME_DARK, FOR_ME_LIGHT, REPLY_BORDER, TextSizes, fmt_time, is_dark, markdown_to_html
from .user_picker import UsersPicker

FILTER_OPEN, FILTER_MINE, FILTER_SENT, FILTER_ALL = "open", "mine", "sent", "all"
FILTERS = ((FILTER_OPEN, "Otevřené"), (FILTER_MINE, "Pro mě"), (FILTER_SENT, "Odeslané"), (FILTER_ALL, "Vše"))
PROJECT_SCHEME = "spm-project:"
NEW_COLOR = "#2f80ed"


@dataclass
class Source:
    """Síťová složka s projekty, ze které se vzkazy čtou (záložky se stejnou složkou = jedna)."""
    label: str                     # název záložky (záložek)
    server: Path
    users: Callable[[], list]      # adresář uživatelů síťové složky
    store: MessageStore = field(default=None)  # type: ignore[assignment]
    archive_days: int = 90         # po kolika dnech vyřízené vzkazy do archivu (spm_config.json)

    def __post_init__(self) -> None:
        if self.store is None:
            self.store = MessageStore(self.server)


def for_me(m: Message, me: str) -> bool:
    """Čeká na mě: jsem adresát a není vyřízený, nebo jsem na vědomí a nepřečetl jsem ho."""
    if m.resolved:
        return False
    return m.is_recipient(me) or (m.is_cc(me) and not m.read_by(me))


def visible(m: Message, flt: str, me: str) -> bool:
    if not m.involves(me):
        return False
    if flt == FILTER_OPEN:
        return not m.resolved
    if flt == FILTER_MINE:
        return for_me(m, me)
    if flt == FILTER_SENT:
        return m.is_author(me)
    return True


def news(messages: list[Message], me: str, seen: dict[str, str]) -> list[Message]:
    """Vzkazy se změnou od někoho jiného od posledního zobrazení (nový vzkaz, úprava, odpověď).
    Autorovi se ohlásí jen odpověď, ne vyřízení."""
    return [m for m in messages if m.involves(me) and m.last_change_by_others(me) > seen.get(m.id, "")]


def _new_mark(new: bool) -> str:
    return f'<span style="color:{NEW_COLOR}">●</span> ' if new else ""


def _people(people, me: str, names: Names) -> str:
    return ", ".join("vy" if p.login == me.lower() else escape(names.person(p.login, p.name)) for p in people)


class MessageCard(QFrame):
    def __init__(self, window: "MessagesWindow", m: Message, seen: str, source_label: str):
        super().__init__()
        self.window, self.m = window, m
        me = window.me
        sizes = TextSizes.from_base(window.font().pointSizeF())
        colors = FOR_ME_DARK if is_dark(window) else FOR_ME_LIGHT
        mine = for_me(m, me)
        names = window.names_for(m.server)
        self.setObjectName("msgCard")
        if mine:
            self.setStyleSheet(f"#msgCard {{ background: {colors.background}; border: 1px solid "
                               f"{colors.border}; border-radius: 4px; }} #msgCard QLabel {{ color: {colors.text}; }}")
        else:
            self.setStyleSheet("#msgCard { border: 1px solid palette(mid); border-radius: 4px; }")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(3)

        self.is_new = m.last_change_by_others(me) > seen
        head = f"{_new_mark(self.is_new)}<b>{escape(names.author(m.user, m.author))}</b> · {fmt_time(m.created_at)}"
        if m.edited_at:
            head += f" · upraveno {fmt_time(m.edited_at)}"
        if source_label:
            head += f" · <i>{escape(source_label)}</i>"
        if m.archived:
            head += " · <i>v archivu</i>"
        self.head = self._label(head, sizes.meta)
        lay.addWidget(self.head)
        who = [f"pro {_people(m.recipients, me, names)}"]
        if m.is_author(me) and [p.login for p in m.recipients] == [me.lower()]:
            who[0] = f'<b style="color:{colors.label}">▶ připomínka pro vás</b>'   # vzkaz sám sobě
        elif m.is_recipient(me):
            who[0] = f'<b style="color:{colors.label}">▶ pro vás</b>' + (
                f" (a {_people([p for p in m.recipients if p.login != me.lower()], me, names)})"
                if len(m.recipients) > 1 else "")
        if m.cc:
            who.append(f'<b style="color:{colors.label}">▷ na vědomí</b>' if m.is_cc(me)
                       else f"na vědomí: {_people(m.cc, me, names)}")
        done = [s for s in m.resolvers if m.is_recipient(s.login)]
        if done:
            who.append("vyřízeno: " + ", ".join(f"{escape(names.author(s.login, s.name))} ({fmt_time(s.resolved_at)})"
                                               for s in done))
        by_author = next((s for s in m.resolvers if not m.is_recipient(s.login)), None)
        if by_author is not None:
            who.append(f"dokončeno autorem ({fmt_time(by_author.resolved_at)})")
        self.who = self._label(" · ".join(who), sizes.meta)
        lay.addWidget(self.who)
        self.body = self._label(self._html(m.text), sizes.text)
        if self.is_new and m.last_change_by_others(me) in (m.created_at, m.edited_at):
            self._bold(self.body)
        if m.resolved:
            self.body.setEnabled(False)
        lay.addWidget(self.body)
        for r in m.replies:
            new = not r.is_author(me) and max(r.created_at, r.edited_at or "") > seen
            box = QFrame()
            box.setObjectName("msgReply")
            box.setStyleSheet(f"#msgReply {{ border: none; border-left: 3px solid {REPLY_BORDER}; }}")
            rl = QVBoxLayout(box)
            rl.setContentsMargins(8, 0, 0, 0)
            rl.setSpacing(1)
            edited = f" · upraveno {fmt_time(r.edited_at)}" if r.edited_at else ""
            rl.addWidget(self._label(f"{_new_mark(new)}<b>{escape(names.author(r.user, r.author))}</b> · "
                                     f"{fmt_time(r.created_at)}{edited}", sizes.meta))
            text = self._label(self._html(r.text), sizes.text)
            if new:
                self._bold(text)
            rl.addWidget(text)
            lay.addWidget(box)

        row = QHBoxLayout()
        row.setSpacing(10)
        self.resolved = self.read = None
        if m.is_recipient(me):
            self.resolved = QCheckBox("Vyřízeno")
            self.resolved.setChecked(m.resolved_by(me))
            self.resolved.setToolTip("Vzkaz je vyřízený, když ho vyřeší aspoň jeden z adresátů")
            self.resolved.toggled.connect(lambda on: window.resolve(m, on))
            row.addWidget(self.resolved)
        elif m.is_author(me):
            self.resolved = QCheckBox("Dokončeno")
            self.resolved.setChecked(m.resolved_by(me))
            self.resolved.setToolTip("Označí vzkaz jako dokončený pro všechny (už není potřeba)")
            self.resolved.toggled.connect(lambda on: window.resolve(m, on))
            row.addWidget(self.resolved)
        elif m.is_cc(me):
            self.read = QCheckBox("Přečteno")
            self.read.setChecked(m.read_by(me))
            self.read.toggled.connect(lambda on: window.mark_read(m, on))
            row.addWidget(self.read)
        self.btn_reply = self._link_button("Odpovědět", self._start_reply)
        row.addWidget(self.btn_reply)
        self.btn_edit = None
        if m.is_author(me):
            self.btn_edit = self._link_button("Upravit", self._start_edit)
            row.addWidget(self.btn_edit)
        if m.versions:
            row.addWidget(self._link_button(f"Předchozí znění ({len(m.versions)})", self._show_versions))
        row.addStretch()
        lay.addLayout(row)

        # odpověď / úprava přímo v kartě
        self.editor_box = QWidget()
        el = QVBoxLayout(self.editor_box)
        el.setContentsMargins(0, 2, 0, 0)
        self.recipients: UsersPicker | None = None
        self.rec_row = QHBoxLayout()
        el.addLayout(self.rec_row)
        self.editor = CompleterEdit()
        self.editor.setMaximumHeight(110)
        window.setup_editor(self.editor, m.server)
        el.addWidget(self.editor)
        buttons = QHBoxLayout()
        buttons.addStretch()
        self.btn_send = QPushButton("Odeslat")
        self.btn_send.clicked.connect(self._finish)
        cancel = QPushButton("Zrušit")
        cancel.clicked.connect(self._cancel)
        buttons.addWidget(self.btn_send)
        buttons.addWidget(cancel)
        el.addLayout(buttons)
        self.editor_box.hide()
        lay.addWidget(self.editor_box)
        for key in ("Ctrl+Return", "Ctrl+Enter"):
            sc = QShortcut(QKeySequence(key), self.editor)
            sc.setContext(Qt.ShortcutContext.WidgetShortcut)
            sc.activated.connect(self._finish)
        self._mode = ""

    def _html(self, text: str) -> str:
        names = [p["name"] for p in self.m.projects]
        return markdown_to_html(link_projects(text, names, PROJECT_SCHEME))

    def _label(self, html: str, size: float) -> QLabel:
        label = QLabel(html)
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        label.setOpenExternalLinks(False)
        label.linkActivated.connect(self._link)
        font = QFont(label.font())
        font.setPointSizeF(size)
        label.setFont(font)
        return label

    @staticmethod
    def _bold(label: QLabel) -> None:
        font = QFont(label.font())
        font.setBold(True)
        label.setFont(font)

    def _link(self, href: str) -> None:
        if href.startswith(PROJECT_SCHEME):
            name = unquote(href[len(PROJECT_SCHEME):])
            pid = next((p.get("project_id", "") for p in self.m.projects if p["name"] == name), "")
            self.window.project_clicked.emit(str(self.m.server), name, pid)

    def _link_button(self, text: str, slot) -> QToolButton:
        b = QToolButton()
        b.setText(text)
        b.setAutoRaise(True)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.clicked.connect(lambda: slot())
        return b

    def editing(self) -> bool:
        """Rozepsaná odpověď / úprava (i ve skrytém okně – isHidden, ne isVisible)."""
        return bool(self._mode) and not self.editor_box.isHidden()

    def _start_reply(self) -> None:
        self._mode = "reply"
        self.editor.clear()
        self.editor.setPlaceholderText("Odpověď… (Ctrl+Enter = odeslat)")
        self.btn_send.setText("Odeslat")
        self.editor_box.show()
        self.editor.setFocus()

    def _start_edit(self) -> None:
        self._mode = "edit"
        self.editor.setPlainText(self.m.text)
        self.btn_send.setText("Uložit")
        if self.recipients is None:
            self.rec_row.addWidget(QLabel("Pro:"))
            self.recipients = UsersPicker([p.login for p in self.m.recipients],
                                          self.window.users_for(self.m.server), "Komu vzkaz",
                                          "vyberte aspoň jednoho adresáta")
            self.rec_row.addWidget(self.recipients, stretch=1)
        self.body.hide()
        self.editor_box.show()
        self.editor.setFocus()

    def _cancel(self) -> None:
        self.close_editor()

    def close_editor(self) -> None:
        self._mode = ""
        self.editor.clear()
        self.editor_box.hide()
        self.body.show()

    def _finish(self) -> None:
        text = self.editor.toPlainText().strip()
        if not text:
            return
        if self._mode == "reply":
            self.window.reply(self.m, text, self)
        elif self._mode == "edit":
            self.window.edit(self.m, text, self.recipients.people() if self.recipients else None, self)

    def _show_versions(self) -> None:
        lines = [f"Aktuální znění ({fmt_time(self.m.edited_at)}):\n{self.m.text}"]
        lines += [f"Nahrazeno {fmt_time(v.saved_at)}:\n{v.text}" for v in self.m.versions]
        box = QMessageBox(QMessageBox.Icon.Information, "Předchozí znění vzkazu",
                          f"Vzkaz byl upraven {len(self.m.versions)}×. Všechna znění jsou v podrobnostech.",
                          parent=self)
        box.setDetailedText("\n\n────────\n\n".join(lines))
        box.exec()


class MessagesWindow(QWidget):
    """Vzkazy ze všech síťových složek záložek projektů."""

    project_clicked = Signal(str, str, str)   # síťová složka, název projektu, identita
    changed = Signal()                        # vzkazy se změnily (počet na tlačítku)

    def __init__(self, me: str, me_name: str, pc: str, sources: Callable[[], list[Source]],
                 seen_get: Callable[[], dict], seen_set: Callable[[dict], None],
                 parent: QWidget | None = None):
        super().__init__(parent, Qt.WindowType.Window)
        self.me, self.me_name, self.pc = me, me_name, pc
        self.sources_fn = sources
        self.sources: list[Source] = []
        self.seen_get, self.seen_set = seen_get, seen_set
        self.messages: list[Message] = []
        self.errors: list[str] = []
        self._seen_shown: dict[str, str] = {}
        self._signature: tuple = ()
        self._pending_render = False
        self.setWindowTitle("Vzkazy")
        self.resize(560, 720)

        top = QHBoxLayout()
        top.setSpacing(2)
        self.filter_group = QButtonGroup(self)
        self.filter_buttons: dict[str, QToolButton] = {}
        for key, text in FILTERS:
            b = QToolButton()
            b.setText(text)
            b.setCheckable(True)
            b.setAutoRaise(True)
            self.filter_group.addButton(b)
            self.filter_buttons[key] = b
            top.addWidget(b)
        self.filter_buttons[FILTER_OPEN].setChecked(True)
        for b in self.filter_buttons.values():
            b.toggled.connect(lambda on: on and self.render())
        top.addStretch()
        refresh = QToolButton()
        refresh.setText("Obnovit")
        refresh.setAutoRaise(True)
        refresh.clicked.connect(lambda: self.reload())
        top.addWidget(refresh)
        self.show_archive = QCheckBox("Zobrazit i archiv")
        self.show_archive.setToolTip("Vyřízené vzkazy starší než nastavený počet dní (spm_config.json) "
                                     "jsou v archivu vzkazů – odpověď nebo změna je vrátí mezi aktivní")
        self.show_archive.toggled.connect(lambda _: self._archive_toggled())
        top.addWidget(self.show_archive)
        self._archived: list[Message] = []
        self.on_top = QCheckBox("Vždy navrchu")
        self.on_top.toggled.connect(self._set_on_top)
        top.addWidget(self.on_top)

        self.banner = QLabel()
        self.banner.setWordWrap(True)
        self.banner.hide()
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
        comp = QVBoxLayout()
        comp.setContentsMargins(0, 4, 0, 0)
        src_row = QHBoxLayout()
        self.source_label = QLabel("Síťová složka:")
        self.source = QComboBox()
        self.source.currentIndexChanged.connect(lambda _: self._source_changed())
        src_row.addWidget(self.source_label)
        src_row.addWidget(self.source, stretch=1)
        comp.addLayout(src_row)
        rec_row = QHBoxLayout()
        rec_row.addWidget(QLabel("Pro:"))
        self.recipients = UsersPicker([], lambda: self._current_source_users(), "Komu vzkaz",
                                      "vyberte aspoň jednoho adresáta")
        rec_row.addWidget(self.recipients, stretch=1)
        comp.addLayout(rec_row)
        self.editor = CompleterEdit()
        self.editor.setPlaceholderText("Napište vzkaz…  (@jméno = na vědomí, #projekt = odkaz, "
                                       "Ctrl+Enter = odeslat)")
        self.editor.setMaximumHeight(100)
        for key in ("Ctrl+Return", "Ctrl+Enter"):
            sc = QShortcut(QKeySequence(key), self.editor)
            sc.setContext(Qt.ShortcutContext.WidgetShortcut)
            sc.activated.connect(self.send)
        comp.addWidget(self.editor)
        send_row = QHBoxLayout()
        send_row.addStretch()
        self.btn_send = QPushButton("Odeslat")
        self.btn_send.setToolTip("Odešle vzkaz (Ctrl+Enter)")
        self.btn_send.clicked.connect(self.send)
        send_row.addWidget(self.btn_send)
        comp.addLayout(send_row)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.banner)
        layout.addWidget(self.scroll, stretch=1)
        layout.addWidget(self.empty, stretch=1)
        layout.addLayout(comp)

    # --- zdroje ------------------------------------------------------------------------

    def set_sources(self, sources: list[Source]) -> None:
        """Síťové složky (zachová se mezipaměť už známých)."""
        known = {s.server: s for s in self.sources}
        for s in sources:
            if s.server in known:
                s.store = known[s.server].store
        current = self.source.currentData()
        self.sources = sources
        self.source.blockSignals(True)
        self.source.clear()
        for s in sources:
            self.source.addItem(f"{s.label} – {s.server}", str(s.server))
        index = self.source.findData(current) if current else 0
        self.source.setCurrentIndex(max(index, 0))
        self.source.blockSignals(False)
        multi = len(sources) > 1
        self.source.setVisible(multi)
        self.source_label.setVisible(multi)
        self._source_changed()

    def composing(self) -> bool:
        """Rozepsaný vzkaz (text nebo vybraní adresáti)."""
        return bool(self.editor.toPlainText().strip() or self.recipients.logins())

    def follow_server(self, server: str) -> bool:
        """Síťová složka pro nový vzkaz podle aktivní záložky hlavního okna. U rozepsaného vzkazu
        se nemění (adresáti jsou uživatelé zvolené složky). Vrací, zda se výběr změnil."""
        if not server or self.composing():
            return False
        key = os.path.normcase(os.path.normpath(server))
        index = next((i for i in range(self.source.count())
                      if os.path.normcase(os.path.normpath(self.source.itemData(i) or "")) == key), -1)
        if index < 0 or index == self.source.currentIndex():
            return False
        self.source.setCurrentIndex(index)
        return True

    def current_source(self) -> Source | None:
        index = self.source.currentIndex()
        return self.sources[index] if 0 <= index < len(self.sources) else None

    def _current_source_users(self) -> list:
        s = self.current_source()
        return s.users() if s else []

    def names_for(self, server: Path) -> Names:
        """Aktuální jména uživatelů síťové složky (přezdívka změněná po napsání vzkazu)."""
        cache = getattr(self, "_names", None)
        if cache is None:
            cache = self._names = {}
        if server not in cache:
            try:
                cache[server] = Names(self.users_for(server)())
            except OSError:
                cache[server] = Names([])
        return cache[server]

    def users_for(self, server: Path) -> Callable[[], list]:
        s = next((s for s in self.sources if s.server == server), None)
        return s.users if s else (lambda: [])

    def setup_editor(self, editor: CompleterEdit, server: Path) -> None:
        """Našeptávání @ (uživatelé) a # (projekty síťové složky)."""
        try:
            editor.users = self.users_for(server)()
        except OSError:
            editor.users = []
        editor.set_projects(lambda: msgs.project_names(server))

    def _source_changed(self) -> None:
        s = self.current_source()
        if s is not None:
            self.setup_editor(self.editor, s.server)
            self.recipients.set_logins(self.recipients.logins())
        self.btn_send.setEnabled(s is not None)

    # --- načtení ----------------------------------------------------------------------

    def load(self) -> tuple[list[Message], list[str]]:
        """Aktivní vzkazy všech síťových složek (chyby nedostupných složek zvlášť); archiv se
        načte znovu, jen když je zobrazený."""
        result, errors = [], []
        for s in self.sources:
            try:
                result += s.store.load()
            except OSError as e:
                errors.append(f"{s.label}: {e}")
        if self.show_archive.isChecked():
            self._archived = self._load_archive()
        result.sort(key=lambda m: m.last_activity, reverse=True)
        return result, errors

    def _load_archive(self) -> list[Message]:
        archived = []
        for s in self.sources:
            try:
                archived += s.store.load_archive()
            except OSError:
                pass  # nedostupná složka je hlášená u aktivních vzkazů
        return archived

    def _archive_toggled(self) -> None:
        self._archived = self._load_archive() if self.show_archive.isChecked() else []
        self._signature = ()
        self.set_messages([m for m in self.messages if not m.archived], self.errors)

    def reload(self) -> None:
        self.set_sources(self.sources_fn())
        self.set_messages(*self.load())

    def set_messages(self, messages: list[Message], errors: list[str]) -> None:
        """Nová data (i z kontroly na pozadí). Překreslí se jen při změně a ne uprostřed psaní
        odpovědi v kartě. Zobrazený archiv se přidá (kontrola na pozadí ho nečte)."""
        if self.show_archive.isChecked():
            active = {m.id for m in messages}
            messages = sorted(messages + [m for m in self._archived if m.id not in active],
                              key=lambda m: m.last_activity, reverse=True)
        self.messages, self.errors = messages, errors
        signature = tuple((m.id, m.last_activity, tuple(sorted((k, s.resolved_at, s.read_at)
                                                           for k, s in m.states.items())))
                          for m in messages) + tuple(errors)
        if signature == self._signature:
            return
        self._signature = signature
        if any(c.editing() for c in self.card_widgets()):
            self._pending_render = True
            return
        self.render()

    def current_filter(self) -> str:
        return next(k for k, b in self.filter_buttons.items() if b.isChecked())

    def set_filter(self, key: str) -> None:
        self.filter_buttons[key].setChecked(True)

    def visible_messages(self) -> list[Message]:
        flt = self.current_filter()
        return [m for m in self.messages if visible(m, flt, self.me)]

    def open_for_me(self) -> int:
        return sum(1 for m in self.messages if for_me(m, self.me))

    def render(self) -> None:
        self._pending_render = False
        self._names = {}  # adresáře uživatelů se načtou jednou za překreslení
        bar = self.scroll.verticalScrollBar().value()
        self._clear_cards()
        if self.errors:
            self.banner.setText("Nedostupné: " + "; ".join(escape(e) for e in self.errors))
            self.banner.show()
        else:
            self.banner.hide()
        labels = len(self.sources) > 1
        shown = self.visible_messages()
        for m in shown:
            label = next((s.label for s in self.sources if s.server == m.server), "") if labels else ""
            seen = "\uffff" if m.archived else self._seen_shown.get(m.id, "")  # archiv není „nový“
            self.cards_lay.insertWidget(self.cards_lay.count() - 1, MessageCard(self, m, seen, label))
        if not shown:
            text = {FILTER_OPEN: "Žádné otevřené vzkazy.", FILTER_MINE: "Nic pro vás.",
                    FILTER_SENT: "Zatím jste neposlali žádný vzkaz."}.get(self.current_filter(), "Žádné vzkazy.")
            self._clear_cards()
            self.scroll.hide()
            self.empty.setText(text + "\n\nNový vzkaz napište dole.")
            self.empty.show()
        else:
            self.empty.hide()
            self.scroll.show()
            QTimer.singleShot(0, lambda: self.scroll.verticalScrollBar().setValue(bar))
        if self.isVisible():
            self.mark_seen()

    def _clear_cards(self) -> None:
        while self.cards_lay.count() > 1:
            w = self.cards_lay.takeAt(0).widget()
            if w is not None:
                w.hide()  # smaže se až v dalším průchodu událostí – do té doby nesmí překážet
                w.deleteLater()

    def card_widgets(self) -> list[MessageCard]:
        return [self.cards_lay.itemAt(i).widget() for i in range(self.cards_lay.count() - 1)
                if isinstance(self.cards_lay.itemAt(i).widget(), MessageCard)]

    # --- přečtené -----------------------------------------------------------------------

    def mark_seen(self) -> None:
        """Zobrazené vzkazy = viděné (značka „nové“ zmizí při dalším otevření okna). Záznamy
        vzkazů, které už mezi aktivními nejsou (archiv, smazané), se zahodí – jen když se
        načetly všechny síťové složky (nedostupná složka by jinak přišla o záznamy)."""
        seen = dict(self.seen_get())
        changed = False
        if not self.errors:
            active = {m.id for m in self.messages if not m.archived}
            stale = [k for k in seen if k not in active]
            for k in stale:
                del seen[k]
            changed = bool(stale)
        for m in self.messages:
            if m.archived:
                continue
            stamp = m.last_change_by_others(self.me)
            if m.involves(self.me) and stamp > seen.get(m.id, ""):
                seen[m.id] = stamp
                changed = True
        if changed:
            self.seen_set(seen)

    def show_messages(self, new_count: int = 0) -> None:
        """Otevře okno (i jako upozornění na nové vzkazy)."""
        self._seen_shown = dict(self.seen_get())  # „nové“ se počítají od posledního zobrazení
        if new_count:
            self.set_filter(FILTER_OPEN)
        self.render()
        if new_count:
            self.banner.setText(f"<b>Nové vzkazy: {new_count}</b>")
            self.banner.show()
        self.show()
        self.raise_()
        self.activateWindow()
        self.mark_seen()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.mark_seen()

    def changeEvent(self, event) -> None:  # noqa: N802
        if event.type() == QEvent.Type.PaletteChange and hasattr(self, "editor"):
            self.render()
        super().changeEvent(event)

    def _set_on_top(self, on: bool) -> None:
        shown = self.isVisible()
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, on)
        if shown:
            self.show()

    # --- akce -------------------------------------------------------------------------

    def _do(self, action, card: MessageCard | None = None) -> bool:
        """Provede akci a obnoví vzkazy. card: karta, ze které se odpovídalo / upravovalo –
        po úspěchu se její pole zavře (jinak by bránilo překreslení, viz set_messages);
        při chybě zůstane otevřené i s textem."""
        try:
            action()
        except Exception as e:  # noqa: BLE001 – např. nedostupná síť
            QMessageBox.critical(self, "Vzkazy", str(e))
            self.set_messages(*self.load())
            return False
        if card is not None:
            card.close_editor()
        self._signature = ()
        self.set_messages(*self.load())
        self.changed.emit()
        return True

    def _cc(self, text: str, server: Path, exclude: list[str]) -> list[tuple[str, str]]:
        try:
            users = self.users_for(server)()
        except OSError:
            users = []
        skip = {e.lower() for e in exclude} | {self.me.lower()}
        return [(login, name) for login, name in mentions(text, users) if login not in skip]

    def send(self) -> bool:
        s = self.current_source()
        text = self.editor.toPlainText().strip()
        if s is None or not text:
            return False
        people = self.recipients.people()
        if not people:
            QMessageBox.information(self, "Vzkazy", "Vyberte aspoň jednoho adresáta (Pro: Vybrat…).")
            return False
        cc = self._cc(text, s.server, [p[0] for p in people])
        projects = msgs.project_info(s.server, project_refs(text, msgs.project_names(s.server)))
        if self._do(lambda: msgs.send(s.server, self.me, self.pc, self.me_name, people, text, cc, projects)):
            self.editor.clear()
            self.recipients.set_logins([])
            return True
        return False

    def reply(self, m: Message, text: str, card: MessageCard | None = None) -> bool:
        return self._do(lambda: msgs.reply(m, self.me, self.me_name, text), card)

    def edit(self, m: Message, text: str, recipients: list | None, card: MessageCard | None = None) -> bool:
        exclude = [p[0] for p in recipients] if recipients is not None else [p.login for p in m.recipients]
        cc = self._cc(text, m.server, exclude)
        projects = msgs.project_info(m.server, project_refs(text, msgs.project_names(m.server)))
        return self._do(lambda: msgs.edit(m, self.me, text, recipients, cc, projects), card)

    def resolve(self, m: Message, on: bool) -> None:
        if on != m.resolved_by(self.me):
            QTimer.singleShot(0, lambda: self._do(lambda: msgs.set_resolved(m, self.me, self.me_name, on)))

    def mark_read(self, m: Message, on: bool) -> None:
        if on != m.read_by(self.me):
            QTimer.singleShot(0, lambda: self._do(lambda: msgs.set_read(m, self.me, self.me_name, on)))
