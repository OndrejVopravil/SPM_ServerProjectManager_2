"""Vzkazy v rozhraní: okno Vzkazy (psaní, adresáti, na vědomí, vyřízení, odkazy na projekty),
tlačítko v liště, upozornění na nové vzkazy, našeptávání @ a #."""
import os

import pytest

from spm import config, messages as msgs, users
from spm.config import Settings
from spm.messages import MessageStore
from spm.sync import ProjectManager


@pytest.fixture
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def pms(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DIR", tmp_path / "app")
    server = tmp_path / "server"
    for name in ("058768", "Forma"):
        (server / name).mkdir(parents=True)

    def pm(user, nick=""):
        s = Settings(server_root=str(server), local_root=str(tmp_path / f"l_{user}"), nickname=nick)
        p = ProjectManager(s, user=user, pc=f"PC-{user}")
        p.register_user()
        return p

    return pm("alice", "Alča"), pm("bob", "Bobík"), pm("jana", "Jana"), server


def window(pm, seen=None):
    from spm.gui.messages_window import MessagesWindow, Source
    state = {"seen": dict(seen or {})}
    src = Source("Projekty", pm.server_root, lambda: users.list_users(pm.server_root))
    w = MessagesWindow(pm.user, pm.display_name, pm.pc, lambda: [src],
                       lambda: state["seen"], lambda s: state.update(seen=s))
    w.state = state
    w.reload()
    return w


def test_send_with_recipients_cc_and_project_link(qapp, pms, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    alice, bob, jana, server = pms
    w = window(alice)
    assert not w.source.isVisible() and not w.source_label.isVisible()   # jen jedna síťová složka
    w.editor.setPlainText("Zkontroluj #058768, @jana pro info")
    shown = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a[2]))
    assert not w.send() and "aspoň jednoho adresáta" in shown[-1]       # bez adresáta nejde
    w.recipients.set_logins(["bob"])
    assert w.recipients.text.text() == "Bobík (bob)"
    assert w.send() and w.editor.toPlainText() == ""
    (m,) = MessageStore(server).load()
    assert [p.login for p in m.recipients] == ["bob"] and [p.login for p in m.cc] == ["jana"]
    assert m.projects == [{"name": "058768", "project_id": ""}]
    # autor vidí v Odeslaných, ne „pro mě“
    assert w.open_for_me() == 0
    w.set_filter("sent")
    (card,) = w.card_widgets()
    assert "pro Bobík (bob)" in card.who.text() and "na vědomí: jana" in card.who.text().lower()
    assert 'href="spm-project:058768"' in card.body.text()
    assert card.resolved.text() == "Dokončeno"                      # autor může dokončit
    clicked = []
    w.project_clicked.connect(lambda *a: clicked.append(a))
    card._link("spm-project:058768")
    assert clicked == [(str(server), "058768", "")]


def test_recipient_resolves_and_cc_marks_read(qapp, pms):
    alice, bob, jana, server = pms
    msgs.send(server, "alice", "PC", "Alča", [("bob", "Bobík"), ("jana", "Jana")], "Výkres", cc=[("petr", "")])
    wb = window(bob)
    assert wb.open_for_me() == 1
    (card,) = wb.card_widgets()
    assert "▶ pro vás" in card.who.text() and "jana" in card.who.text().lower() and card.is_new
    card.resolved.setChecked(True)
    qapp.processEvents()
    (m,) = MessageStore(server).load()
    assert m.resolved and [s.login for s in m.resolvers] == ["bob"]
    wj = window(jana)
    assert wj.open_for_me() == 0                                    # vyřešil ho Bob -> vyřízený
    wj.set_filter("all")
    (card,) = wj.card_widgets()
    assert "vyřízeno: Bobík" in card.who.text() and not card.resolved.isChecked()
    # na vědomí: jen přečteno
    msgs.send(server, "alice", "PC", "Alča", [("bob", "")], "Info", cc=[("jana", "Jana")])
    wj.reload()
    assert wj.open_for_me() == 1
    wj.set_filter("open")
    card = next(c for c in wj.card_widgets() if c.m.text == "Info")
    assert card.resolved is None and card.read is not None and "▷ na vědomí" in card.who.text()
    card.read.setChecked(True)
    qapp.processEvents()
    assert wj.open_for_me() == 0


def test_reply_and_edit_in_card(qapp, pms):
    alice, bob, jana, server = pms
    msgs.send(server, "alice", "PC", "Alča", [("bob", "")], "Otázka")
    wb = window(bob)
    (card,) = wb.card_widgets()
    assert card.btn_edit is None                                   # upravit může jen autor
    card._start_reply()
    assert card.editing()                                          # i ve skrytém okně
    card.editor.setPlainText("Odpověď")
    card._finish()
    (m,) = MessageStore(server).load()
    assert [r.text for r in m.replies] == ["Odpověď"]
    (card,) = wb.card_widgets()                                    # karta se překreslila s odpovědí
    assert not card.editing() and card.editor_box.isHidden()
    assert "Odpověď" in "".join(lbl.text() for lbl in card.findChildren(type(card.body)))
    # rozepsaná odpověď v jiné kartě se obnovením na pozadí nezahodí
    msgs.send(server, "jana", "PC", "Jana", [("bob", "")], "Druhý")
    wb.reload()
    other = next(c for c in wb.card_widgets() if c.m.text == "Druhý")
    other._start_reply()
    other.editor.setPlainText("rozepsáno")
    msgs.send(server, "jana", "PC", "Jana", [("bob", "")], "Třetí")
    wb.set_messages(*wb.load())
    assert other in wb.card_widgets() and other.editor.toPlainText() == "rozepsáno"
    # chyba při odeslání – pole zůstane otevřené i s textem
    import shutil
    shutil.rmtree(other.m.folder)
    from PySide6.QtWidgets import QMessageBox
    errors = []
    original = QMessageBox.critical
    QMessageBox.critical = staticmethod(lambda *a, **k: errors.append(a[2]))
    try:
        other._finish()
    finally:
        QMessageBox.critical = original
    assert errors and "neexistuje" in errors[-1]
    wa = window(alice)
    wa.set_filter("sent")
    (card,) = wa.card_widgets()
    assert card.is_new                                             # autorovi je nová odpověď
    card._start_edit()
    card.recipients.set_logins(["bob", "jana"])
    card.editor.setPlainText("Otázka upravená")
    card._finish()
    m = next(x for x in MessageStore(server).load() if x.user == "alice")
    assert m.text == "Otázka upravená" and [p.login for p in m.recipients] == ["bob", "jana"]
    (card,) = wa.card_widgets()
    assert not card.editing() and "Otázka upravená" in card.body.text()


def test_news_and_seen(qapp, pms):
    from spm.gui.messages_window import news
    alice, bob, jana, server = pms
    msgs.send(server, "alice", "PC", "Alča", [("bob", "")], "A")
    store = MessageStore(server)
    assert [m.text for m in news(store.load(), "bob", {})] == ["A"]
    assert news(store.load(), "alice", {}) == []                    # vlastní vzkaz není nový
    assert news(store.load(), "jana", {}) == []                     # netýká se jí
    wb = window(bob)
    wb.show_messages(new_count=1)
    assert news(store.load(), "bob", wb.state["seen"]) == []        # zobrazený = viděný
    (m,) = store.load()
    msgs.set_resolved(m, "bob", "Bob")
    assert news(store.load(), "alice", {}) == []                    # vyřízení se autorovi nehlásí
    msgs.reply(m, "bob", "Bob", "hotovo")
    assert len(news(store.load(), "alice", {})) == 1                # odpověď ano
    wb.close()


def test_completer_offers_users_and_projects(qapp, pms):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    alice, bob, jana, server = pms
    w = window(alice)
    ed = w.editor
    ed.show()
    QTest.keyClicks(ed, "viz #05")
    assert ed.popup_visible() and ed.completer.completionCount() == 1
    QTest.keyClick(ed, Qt.Key.Key_Tab)                              # Tab potvrdí
    assert ed.toPlainText() == "viz #058768 " and not ed.popup_visible()
    QTest.keyClicks(ed, "a @bob")
    assert ed.popup_visible()
    assert ed.completer.currentCompletion().startswith("Bobík")
    QTest.keyClick(ed, Qt.Key.Key_Return)                           # Enter taky
    assert ed.toPlainText() == "viz #058768 a @bob "
    QTest.keyClick(ed, Qt.Key.Key_Tab)                              # bez nabídky normální Tab
    assert ed.toPlainText().endswith("\t")
    ed.clear()
    QTest.keyClicks(ed, "#2 bod")
    assert not ed.popup_visible()                                    # nic neodpovídá
    w.close()


# --- hlavní okno ---------------------------------------------------------------------------

def make_main(qapp, pm):
    from spm.gui.main_window import MainWindow
    w = MainWindow(pm.settings)
    w.pm = w.panel.pm = pm
    return w


def wait_messages(w, qapp):
    w._messages_check.wait()
    qapp.processEvents()   # výsledek se doručí signálem v hlavním vlákně


def test_toolbar_button_and_popup(qapp, pms, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication
    alice, bob, jana, server = pms
    w = make_main(qapp, bob)
    try:
        from PySide6.QtWidgets import QToolBar
        bar = w.findChild(QToolBar).actions()
        assert w.act_messages in bar and w.act_notes not in bar     # Poznámky v liště nahradily Vzkazy
        state = {"active": False}
        monkeypatch.setattr(QGuiApplication, "applicationState", staticmethod(
            lambda: Qt.ApplicationState.ApplicationActive if state["active"] else Qt.ApplicationState.ApplicationInactive))
        alerts = []
        monkeypatch.setattr(QApplication, "alert", staticmethod(lambda *a: alerts.append(a)))
        msgs.send(server, "alice", "PC", "Alča", [("bob", "Bobík")], "Ahoj #Forma")
        w.check_messages()
        wait_messages(w, qapp)
        assert w.act_messages.text() == "Vzkazy (1)"
        assert alerts and w._messages_pending == 1                  # SPM není aktivní -> jen bliknout
        assert not w._messages_window.isVisible()
        state["active"] = True
        w._on_app_state(Qt.ApplicationState.ApplicationActive)       # návrat do aplikace
        qapp.processEvents()
        assert w._messages_window.isVisible() and "Nové vzkazy: 1" in w._messages_window.banner.text()
        w._messages_window.close()
        w.check_messages()                                           # podruhé už neukazovat
        wait_messages(w, qapp)
        assert not w._messages_window.isVisible()
    finally:
        wait_messages(w, qapp)
        w.close()
        qapp.processEvents()


def test_project_link_selects_project(qapp, pms, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    alice, bob, jana, server = pms
    w = make_main(qapp, bob)
    try:
        w.refresh(check_names=False)
        assert w.open_project_link(str(server), "Forma")
        assert w.selected().name == "Forma"
        shown = []
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a[2]))
        assert not w.open_project_link(str(server), "Neni")
        assert "v síťové složce není" in shown[-1]
    finally:
        wait_messages(w, qapp)
        if w._name_check:
            w._name_check.wait()
        w.close()
        qapp.processEvents()


def test_current_nickname_shown(qapp, pms):
    from spm.gui.notes_panel import NotesPanel
    from spm.notes import Draft
    alice, bob, jana, server = pms
    msgs.send(server, "alice", "PC", "Alča", [("bob", "Bobík (bob)")], "Ahoj", cc=[("jana", "Jana")])
    alice.settings.nickname = "Alice N."                              # přezdívky se změnily
    alice.register_user()
    jana.settings.nickname = "Janička"
    jana.register_user()
    wb = window(bob)
    (card,) = wb.card_widgets()
    assert "Alice N." in card.head.text() and "Alča" not in card.head.text()
    assert "Janička" in card.who.text()
    # neznámý uživatel (v adresáři není) – uložené jméno
    msgs.send(server, "karel", "PC", "Karel K.", [("bob", "")], "Od Karla")
    wb.reload()
    assert any("Karel K." in c.head.text() for c in wb.card_widgets())
    # poznámky projektu
    alice.add_notes("Forma", [Draft("Pro Boba", recipient="bob", recipient_name="Bobík (bob)")])
    bob.settings.nickname = "Bob B."
    bob.register_user()
    panel = NotesPanel(alice)
    panel.set_project(alice, "Forma")
    (note_card,) = panel.card_widgets()
    assert "Alice N." in note_card.head.text() and "Bob B. (bob)" in note_card.head.text()
    wb.close()


def test_message_to_self_and_done_by_author(qapp, pms, monkeypatch):
    from spm.gui import user_picker
    alice, bob, jana, server = pms
    w = window(alice)
    offered = []

    def exec_(dlg):
        from PySide6.QtCore import Qt
        offered.extend(dlg.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(dlg.list.count()))
        for i in range(dlg.list.count()):
            if dlg.list.item(i).data(Qt.ItemDataRole.UserRole) == "alice":
                dlg.list.item(i).setCheckState(Qt.CheckState.Checked)
        return dlg.DialogCode.Accepted
    monkeypatch.setattr(user_picker.UsersDialog, "exec", exec_)
    assert w.recipients.choose() and "alice" in offered               # sám sebe lze vybrat
    w.editor.setPlainText("Zavolat zákazníkovi")
    assert w.send()
    assert w.open_for_me() == 1                                       # připomínka čeká na mě
    (card,) = w.card_widgets()
    assert "připomínka pro vás" in card.who.text() and card.resolved.text() == "Vyřízeno"
    card.resolved.setChecked(True)
    qapp.processEvents()
    assert w.open_for_me() == 0
    # autor vzkazu pro jiného: Dokončeno
    msgs.send(server, "alice", "PC", "Alča", [("bob", "")], "Pro Boba")
    w.reload()
    w.set_filter("sent")
    card = next(c for c in w.card_widgets() if c.m.text == "Pro Boba")
    assert card.resolved.text() == "Dokončeno" and not card.resolved.isChecked()
    card.resolved.setChecked(True)
    qapp.processEvents()
    wb = window(bob)
    assert wb.open_for_me() == 0                                      # dokončený i pro adresáta
    wb.set_filter("all")
    card = next(c for c in wb.card_widgets() if c.m.text == "Pro Boba")
    assert "dokončeno autorem" in card.who.text()
    w.close()


def test_archive_view_and_seen_cleanup(qapp, pms):
    from datetime import datetime, timedelta
    alice, bob, jana, server = pms
    old = msgs.send(server, "alice", "PC", "Alča", [("bob", "")], "Dávno vyřízený")
    msgs.set_resolved(next(m for m in MessageStore(server).load() if m.id == old), "bob", "Bob")
    wb = window(bob, seen={"neexistujici_vzkaz": "2020-01-01T00:00:00"})
    wb.show_messages()
    assert "neexistujici_vzkaz" not in wb.state["seen"] and old in wb.state["seen"]   # úklid
    store = wb.sources[0].store
    assert store.archive_old(store.load(), 90, datetime.now() + timedelta(days=91)) == [old]
    wb.reload()
    wb.set_filter("all")
    assert wb.card_widgets() == []                                   # archiv se běžně nezobrazuje
    wb.mark_seen()
    assert old not in wb.state["seen"]                               # archivovaný -> záznam pryč
    wb.show_archive.setChecked(True)
    (card,) = wb.card_widgets()
    assert card.m.archived and "v archivu" in card.head.text() and not card.is_new
    wb.set_messages([m for m in wb.load()[0] if not m.archived], [])   # kontrola na pozadí
    assert len(wb.card_widgets()) == 1                               # zobrazený archiv zůstane
    card._start_reply()                                              # odpověď vrátí mezi aktivní
    card.editor.setPlainText("Otevírám znovu")
    card._finish()
    assert [m.id for m in store.load()] == [old] and store.load_archive() == []
    wb.close()


def test_settings_dialog_messages_days(qapp, pms):
    from spm.gui.settings_dialog import SettingsDialog
    alice, bob, jana, server = pms
    dlg = SettingsDialog(alice.settings)
    assert dlg.messages_days.value() == 90
    dlg.messages_days.setValue(0)
    assert dlg.messages_days.text() == "nearchivovat"
    assert dlg._shared_values()["messages_archive_days"] == 0


def test_background_check_archives_with_configured_days(qapp, pms, monkeypatch):
    from datetime import datetime, timedelta
    alice, bob, jana, server = pms
    old = msgs.send(server, "alice", "PC", "Alča", [("bob", "")], "Vyřízený")
    msgs.set_resolved(next(m for m in MessageStore(server).load() if m.id == old), "bob", "Bob")
    bob.settings.messages_archive_days = 10                         # ze spm_config.json
    w = make_main(qapp, bob)
    try:
        (src,) = w.message_sources()
        assert src.archive_days == 10
        real = msgs.datetime
        monkeypatch.setattr(msgs, "datetime", type("D", (), {"now": staticmethod(
            lambda: real.now() + timedelta(days=11))}))
        w.check_messages()
        wait_messages(w, qapp)
        assert MessageStore(server).load() == [] and [m.id for m in MessageStore(server).load_archive()] == [old]
    finally:
        wait_messages(w, qapp)
        w.close()
        qapp.processEvents()
