"""Poznámky v rozhraní: panel vedle stromu (karty, psaní, filtry, odpojení), seznam projektů,
rozepsané poznámky ze starších verzí (odevzdání, zrušení aktivace)."""
import os
import stat
from pathlib import Path

import pytest
from conftest import settle_panel
from tests_helpers_tree import find_item

from spm import config
from spm.config import Settings
from spm.notes import Draft
from spm.sync import ProjectManager


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.chmod(stat.S_IWRITE | stat.S_IREAD)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def pms(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DIR", tmp_path / "app")
    proj = tmp_path / "server" / "Forma"
    write(proj / "dily" / "deska.elt", "deska")
    write(proj / "sestava.elt", "sestava")

    def pm(user):
        s = Settings(server_root=str(tmp_path / "server"), local_root=str(tmp_path / f"l_{user}"))
        return ProjectManager(s, user=user, pc=f"PC-{user}")

    alice, bob = pm("alice"), pm("bob")
    alice.activate("Forma", lock_paths=[])        # evidence souborů
    alice.cancel_activation("Forma")
    return alice, bob


def make_window(qapp, pm):
    from spm.gui.main_window import MainWindow
    w = MainWindow(pm.settings)
    w.pm = w.panel.pm = pm
    w.refresh(check_names=False)
    return w


def close(w, qapp):
    if w._name_check:
        w._name_check.wait()
    w.close()
    qapp.processEvents()


def select_forma(w):
    w.table.selectRow([p.name for p in w.projects].index("Forma"))
    settle_panel(w.panel)


def cards_text(panel) -> str:
    return "\n".join(c.head.text() + "\n" + c.body.text() for c in panel.card_widgets())


def make_panel(pm, name="Forma"):
    from spm.gui.notes_panel import NotesPanel
    panel = NotesPanel(pm)
    panel.set_project(pm, name)
    return panel


def test_recipient_from_text():
    from spm.gui.notes_window import recipient_from_text
    known = {"Alča (alice)": "alice"}
    assert recipient_from_text("Alča (alice)", known) == ("alice", "Alča (alice)")
    assert recipient_from_text("  novak ", known) == ("novak", "")
    assert recipient_from_text("Jan Novák (jnovak)", known) == ("jnovak", "")
    assert recipient_from_text("", known) == ("", "")


def test_mention_and_touches():
    from spm.gui.notes_panel import mention, touches
    from spm.users import UserInfo
    users = [UserInfo("bob", nickname="Bobík"), UserInfo("alice")]
    assert mention("Ahoj @bob, koukni", users) == ("bob", "Bobík (bob)")
    assert mention("pro @Bobík.", users) == ("bob", "Bobík (bob)")
    assert mention("pro @novak", users) == ("novak", "")             # ještě nespustil SPM
    assert mention("mail jan@firma.cz", users) == ("", "")            # e-mail není zmínka
    assert touches(["dily/deska.elt"], ["dily/"]) and touches(["dily/"], ["dily/deska.elt"])
    assert touches(["Dily/Deska.elt"], ["dily/deska.elt"]) and not touches(["sestava.elt"], ["dily/"])


def test_note_for_me_marked_in_list_and_shown_in_panel(qapp, pms):
    alice, bob = pms
    alice.add_notes("Forma", [Draft("**Pozor** na desku", recipient="bob", targets=["dily/deska.elt"])])
    w = make_window(qapp, bob)
    try:
        row = [p.name for p in w.projects].index("Forma")
        item = w.table.item(row, 0)
        assert not item.icon().isNull() and "Čeká na vás 1 poznámka" in item.toolTip()
        select_forma(w)                                   # nová pro mě -> přepne na záložku Poznámky
        assert w.side.currentWidget() is w.notes
        assert w.side.tabText(w.side.indexOf(w.notes)) == "Poznámky (1 · pro vás 1)"
        text = cards_text(w.notes)
        assert "Pozor" in text and "pro vás" in text and "●" in text
        assert w.notes.card_widgets()[0].news == "nová"
        assert find_item(w.panel.tree, "dily/deska.elt").text(3) == "📝 1"
        # odkaz na soubor -> označí ve stromu (a přepne na Soubory)
        (card,) = w.notes.card_widgets()
        card.targets.linkActivated.emit("dily/deska.elt")
        assert w.panel.tree.currentItem().text(0) == "deska.elt" and w.side.currentWidget() is w.panel
        # vyřízení zaškrtnutím
        card.resolved.setChecked(True)
        qapp.processEvents()
        assert bob.notes("Forma")[0].resolved
        assert w.notes.card_widgets() == [] and "Žádné otevřené" in w.notes.empty.text()
        w.notes.set_filter("all")
        assert "vyřízeno" in cards_text(w.notes)
        # znovu klepnutí – už viděná, záložka se sama nepřepíná
        w.side.setCurrentWidget(w.panel)
        w.table.clearSelection()
        select_forma(w)
        assert w.side.currentWidget() is w.panel
    finally:
        close(w, qapp)


def test_write_note_from_tree_publishes_immediately(qapp, pms):
    alice, bob = pms
    alice.activate("Forma", lock_paths=["dily/deska.elt"])
    w = make_window(qapp, alice)
    try:
        select_forma(w)
        w.panel.select_path("dily/deska.elt")             # výběr ve stromu -> „týká se“
        assert "deska.elt" in w.notes.about.text() and w.notes.about.isChecked()
        w.add_note(w.panel.selected_scopes())             # nabídka stromu / Ctrl+N
        assert w.side.currentWidget() is w.notes
        w.notes.editor.setPlainText("Deska je předběžná, @bob prosím zkontroluj")
        w.notes.send()
        (note,) = bob.notes("Forma")                       # zveřejněná hned, bez odevzdání
        assert (note.recipient, note.targets) == ("bob", ["dily/deska.elt"])
        assert w.notes.editor.toPlainText() == ""
        settle_panel(w.panel)
        assert find_item(w.panel.tree, "dily/deska.elt").text(3) == "📝 1"
        # bez vazby na soubory a adresát výběrem
        w.notes.about.setChecked(False)
        w.notes.recipient.addItem("Bob", "bob")
        w.notes.recipient.setCurrentIndex(w.notes.recipient.count() - 1)
        w.notes.editor.setPlainText("Pro celý projekt")
        w.notes.send()
        note = bob.notes("Forma")[0]
        assert (note.text, note.recipient, note.targets) == ("Pro celý projekt", "bob", [])
    finally:
        close(w, qapp)


def test_note_on_project_not_activated(qapp, pms):
    alice, bob = pms
    w = make_window(qapp, bob)
    try:
        select_forma(w)
        w.add_note(["sestava.elt"])
        w.notes.editor.setPlainText("hned zveřejněná")
        w.notes.send()
        assert [(n.text, n.targets) for n in alice.notes("Forma")] == [("hned zveřejněná", ["sestava.elt"])]
        settle_panel(w.panel)
        assert find_item(w.panel.tree, "sestava.elt").text(3) == "📝 1"
    finally:
        close(w, qapp)


def test_filters(qapp, pms):
    alice, bob = pms
    alice.add_notes("Forma", [Draft("k desce", targets=["dily/deska.elt"]), Draft("pro Boba", recipient="bob"),
                              Draft("obecná")])
    alice.resolve_note("Forma", [n for n in alice.notes("Forma") if n.text == "obecná"][0].note_id)
    panel = make_panel(bob)

    def texts():
        return sorted(c.note.text for c in panel.card_widgets())
    assert texts() == ["k desce", "pro Boba"]                         # otevřené
    panel.set_filter("mine")
    assert texts() == ["pro Boba"]
    panel.set_filter("selected")
    assert texts() == [] and "Vyberte ve stromu" in panel.empty.text()
    panel.set_selection(["dily/"])
    assert texts() == ["k desce"]
    panel.set_filter("all")
    assert texts() == ["k desce", "obecná", "pro Boba"]


def test_tree_note_column_click_filters_notes(qapp, pms):
    alice, bob = pms
    alice.add_notes("Forma", [Draft("k desce", targets=["dily/deska.elt"]), Draft("jiná")])
    w = make_window(qapp, bob)
    try:
        select_forma(w)
        item = find_item(w.panel.tree, "dily/deska.elt")
        w.panel.select_path("dily/deska.elt")
        w._on_tree_click(item, 3)
        assert w.side.currentWidget() is w.notes and w.notes.current_filter() == "selected"
        assert [c.note.text for c in w.notes.card_widgets()] == ["k desce"]
    finally:
        close(w, qapp)


def test_reply_and_edit_inline(qapp, pms, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    alice, bob = pms
    alice.add_notes("Forma", [Draft("Deska hotová", recipient="bob")])
    bob_panel = make_panel(bob)
    (card,) = bob_panel.card_widgets()
    assert card.btn_edit is None                                       # cizí poznámku neupraví
    card.btn_reply.click()
    assert card.editor_box.isVisibleTo(card)
    card.editor.setPlainText("Ano, rozumím.")
    card.btn_send.click()
    assert [r.text for r in bob.notes("Forma")[0].replies] == ["Ano, rozumím."]
    assert "Ano, rozumím." in "\n".join(lbl.text() for lbl in bob_panel.card_widgets()[0].findChildren(type(card.body)))

    alice_panel = make_panel(alice)
    (card,) = alice_panel.card_widgets()
    card.btn_edit.click()
    assert card.editor.toPlainText() == "Deska hotová" and card.body.isHidden()
    card.editor.setPlainText("Deska hotová, **díry posunuty**")
    card.btn_send.click()
    note = bob.notes("Forma")[0]
    assert note.text == "Deska hotová, **díry posunuty**" and note.recipient == "bob" and len(note.versions) == 1
    bob_panel.reload()
    assert bob_panel.card_widgets()[0].news == "upravená"
    shown = []
    monkeypatch.setattr(QMessageBox, "exec", lambda box: shown.append(box.detailedText()) or 0)
    bob_panel.show_versions(note)
    assert "Deska hotová" in shown[0]


def test_new_reply_to_my_note_shows_notes(qapp, pms):
    alice, bob = pms
    alice.add_notes("Forma", [Draft("Otázka pro všechny")])
    w = make_window(qapp, alice)
    try:
        select_forma(w)
        assert w.side.currentWidget() is w.panel                       # vlastní, bez odpovědi
        w.open_notes("Forma")                                           # Alice poznámky viděla
        w.side.setCurrentWidget(w.panel)
        bob.reply_note("Forma", bob.notes("Forma")[0].note_id, "Odpověď od Boba")
        w.refresh(check_names=False)
        w.table.clearSelection()
        select_forma(w)
        assert w.side.currentWidget() is w.notes and w.notes.card_widgets()[0].news == "nová odpověď"
    finally:
        close(w, qapp)


def test_own_changes_are_not_new(qapp, pms):
    alice, bob = pms
    alice.add_notes("Forma", [Draft("Otázka", recipient="bob")])
    note_id = alice.notes("Forma")[0].note_id
    seen = alice.notes("Forma")[0].seq
    alice.reply_note("Forma", note_id, "Moje doplnění")
    alice.edit_note("Forma", note_id, "Otázka upravená")
    panel = make_panel(alice)
    panel._seen_shown = seen
    panel._render()
    (card,) = panel.card_widgets()
    assert card.news == "" and "●" not in cards_text(panel)          # pro autorku nic nového
    panel = make_panel(bob)
    panel._seen_shown = seen
    panel._render()
    (card,) = panel.card_widgets()
    assert card.news == "upravená" and card.body.font().bold() and "upravena" in card.head.toolTip()


def test_for_me_card_colors_follow_dark_mode(qapp, pms):
    from PySide6.QtGui import QColor, QPalette

    from spm.gui.notes_window import FOR_ME_DARK, FOR_ME_LIGHT
    alice, bob = pms
    alice.add_notes("Forma", [Draft("pro Boba", recipient="bob"), Draft("pro všechny")])
    panel = make_panel(bob)
    styles = {c.note.text: c.styleSheet() for c in panel.card_widgets()}
    assert FOR_ME_LIGHT.background in styles["pro Boba"] and FOR_ME_LIGHT.background not in styles["pro všechny"]
    dark = QPalette(panel.palette())
    dark.setColor(QPalette.ColorRole.Base, QColor("#1e1e1e"))
    panel.setPalette(dark)                                              # změna palety -> překreslení
    styles = {c.note.text: c.styleSheet() for c in panel.card_widgets()}
    assert FOR_ME_DARK.background in styles["pro Boba"] and FOR_ME_DARK.text in styles["pro Boba"]


def test_note_text_larger_than_meta(qapp, pms):
    alice, bob = pms
    alice.add_notes("Forma", [Draft("Hlavní text", recipient="bob")])
    (card,) = make_panel(bob).card_widgets()
    assert card.body.font().pointSizeF() > card.head.font().pointSizeF()


def test_export_markdown(qapp, pms, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog
    alice, bob = pms
    alice.add_notes("Forma", [Draft("Text **poznámky**", targets=["sestava.elt"])])
    target = tmp_path / "export.md"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), ""))
    make_panel(bob).export_md()
    text = target.read_text(encoding="utf-8")
    assert text.startswith("# Poznámky – projekt Forma") and "Text **poznámky**" in text
    assert "`sestava.elt`" in text


def test_detach_and_attach(qapp, pms):
    alice, bob = pms
    alice.add_notes("Forma", [Draft("poznámka")])
    w = make_window(qapp, bob)
    try:
        select_forma(w)
        w.toggle_notes_detached()
        assert w._notes_window is not None and w.side.indexOf(w.notes) < 0
        assert w.notes.window() is w._notes_window and "Forma" in w._notes_window.windowTitle()
        assert w.settings.ui["notes.detached"] == "1"
        assert w.open_notes() is w.notes                                # vyzvedne okno
        w._notes_window.close()                                          # zavření = zpět do záložky
        assert w._notes_window is None and w.side.indexOf(w.notes) >= 0
        assert w.settings.ui["notes.detached"] == ""
    finally:
        close(w, qapp)


def test_local_only_project_has_no_notes(qapp, pms):
    alice, bob = pms
    (bob.local_root / "Mistni").mkdir(parents=True)
    w = make_window(qapp, bob)
    try:
        w.table.selectRow([p.name for p in w.projects].index("Mistni"))
        settle_panel(w.panel)
        assert not w.side.isTabEnabled(w.side.indexOf(w.notes)) and w.notes.name is None
        assert w.open_notes() is None
    finally:
        close(w, qapp)


def test_nickname_in_settings(qapp, pms):
    from spm.gui.settings_dialog import SettingsDialog
    alice, bob = pms
    dlg = SettingsDialog(alice.settings)
    dlg.nickname.setText("  Alča ")
    dlg._accept()
    assert alice.settings.nickname == "Alča" and alice.display_name == "Alča"


# --- rozepsané poznámky ze starších verzí ---------------------------------------------------

def test_old_drafts_published_with_submission(qapp, pms, monkeypatch):
    from spm.gui import main_window
    alice, bob = pms
    alice.activate("Forma", lock_paths=["dily/deska.elt"])
    alice.save_drafts("Forma", [Draft("# Deska\nje předběžná", "bob", "", ["dily/deska.elt"])])
    w = make_window(qapp, alice)
    try:
        seen = {}

        def run_now(parent, title, fn, *args, on_done=None, on_error=None, status_text=None, **kwargs):
            res = fn(*args, progress=None, **kwargs)
            if on_done:
                on_done(res)
        monkeypatch.setattr(main_window, "run_task", run_now)
        from PySide6.QtWidgets import QMessageBox
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: seen.setdefault("msg", a[2]))

        def accept(dlg, key):
            seen["checks"] = [c.text() for c in dlg.draft_checks]
            dlg._check_all(True)
            dlg.comment.setPlainText("deska v2")
            return True
        monkeypatch.setattr(w, "_exec_dialog", accept)
        write(alice.local_dir("Forma") / "dily" / "deska.elt", "deska v2")
        w._confirm_upload(alice.plan_upload("Forma"), end_session=False)
        assert seen["checks"] == ["Deska → pro bob  (1 souborů / složek)"]
        assert "Zveřejněné poznámky: 1" in seen["msg"]
        assert bob.notes("Forma")[0].targets == ["dily/deska.elt"] and alice.drafts("Forma") == []
    finally:
        close(w, qapp)


@pytest.mark.parametrize("choice, published, kept", [
    ("Zveřejnit", 1, []), ("Ponechat na příště", 0, ["rozepsaná"]), ("Zahodit", 0, [])])
def test_cancel_activation_with_drafts(qapp, pms, monkeypatch, choice, published, kept):
    from PySide6.QtWidgets import QMessageBox
    alice, bob = pms
    alice.activate("Forma", lock_paths=["sestava.elt"])
    alice.save_drafts("Forma", [Draft("rozepsaná")])
    w = make_window(qapp, alice)
    try:
        select_forma(w)
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
        monkeypatch.setattr(QMessageBox, "exec", lambda box: 0)
        monkeypatch.setattr(QMessageBox, "clickedButton",
                            lambda box: next(b for b in box.buttons() if b.text() == choice))
        w.cancel_activation()
        assert len(bob.notes("Forma")) == published
        alice.activate("Forma", lock_paths=[])                   # další aktivace
        assert [d.text for d in alice.drafts("Forma")] == kept
    finally:
        close(w, qapp)


def test_upload_dialog_lists_drafts(qapp, pms):
    from spm.gui.upload_dialog import UploadDialog
    alice, bob = pms
    alice.activate("Forma", lock_paths=["sestava.elt"])
    drafts = [Draft("Hotovo"), Draft("   ")]
    dlg = UploadDialog(alice.plan_upload("Forma"), True, drafts=drafts)
    assert [c.isChecked() for c in dlg.draft_checks] == [True, False]   # prázdná nejde zveřejnit
    assert [d.text for d in dlg.selected_notes()] == ["Hotovo"]


def test_handover_note_in_upload_dialog(qapp, pms):
    from spm.gui.upload_dialog import UploadDialog
    from spm.users import UserInfo
    alice, bob = pms
    alice.activate("Forma", lock_paths=["dily/deska.elt", "sestava.elt"])
    write(alice.local_dir("Forma") / "dily" / "deska.elt", "deska v2")
    plan = alice.plan_upload("Forma")
    dlg = UploadDialog(plan, True, users=[UserInfo("bob", nickname="Bob")])
    assert dlg.selected_notes() == []                                   # nepovinná
    dlg.handover.setPlainText("Deska je hotová, @bob zkontroluj díry")
    dlg.handover_files.setChecked(True)
    (note,) = dlg.selected_notes()
    assert (note.recipient, note.targets) == ("bob", ["dily/deska.elt", "sestava.elt"])
    alice.upload(plan, plan.default_assignments(), "deska v2", end_session=True, notes=dlg.selected_notes())
    (published,) = bob.notes("Forma")
    assert published.text.startswith("Deska je hotová") and published.commit_id is not None


def test_for_me_counter_in_tab_row(qapp, pms):
    alice, bob = pms
    alice.add_notes("Forma", [Draft("pro Boba", recipient="bob"), Draft("taky pro Boba", recipient="bob"),
                              Draft("pro všechny")])
    w = make_window(qapp, bob)
    try:
        assert w.btn_for_me.isVisibleTo(w) and w.btn_for_me.text() == "📝 2 pro vás"
        w.show_notes_for_me("Forma")
        assert w.selected().name == "Forma" and w.side.currentWidget() is w.notes
        assert w.notes.current_filter() == "mine" and len(w.notes.card_widgets()) == 2
        for card in w.notes.card_widgets():
            card.resolved.setChecked(True)
            qapp.processEvents()
        assert not w.btn_for_me.isVisibleTo(w)                          # vše vyřízeno
    finally:
        close(w, qapp)


def test_switching_project_keeps_notes_tab_on_top(qapp, pms, tmp_path):
    alice, bob = pms
    write(tmp_path / "server" / "Druhy" / "a.txt", "a")
    alice.add_notes("Forma", [Draft("poznámka")])
    w = make_window(qapp, bob)
    try:
        w.show()
        select_forma(w)
        w.open_notes()
        assert w.notes.isVisible() and not w.panel.isVisible()
        w.table.selectRow([p.name for p in w.projects].index("Druhy"))   # jiný projekt
        settle_panel(w.panel)
        qapp.processEvents()
        assert w.side.currentWidget() is w.notes
        assert w.notes.isVisible() and not w.panel.isVisible()           # strom nesmí překrýt poznámky
        w.side.setCurrentWidget(w.panel)
        assert w.panel.isVisible() and not w.notes.isVisible()
        w.table.selectRow([p.name for p in w.projects].index("Forma"))
        settle_panel(w.panel)
        assert w.panel.isVisible() and not w.notes.isVisible()
    finally:
        close(w, qapp)
