"""Předávací poznámky v rozhraní: okno, rozepsané, odevzdání, strom, seznam, zrušení aktivace."""
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


def test_recipient_from_text():
    from spm.gui.notes_window import recipient_from_text
    known = {"Alča (alice)": "alice"}
    assert recipient_from_text("Alča (alice)", known) == ("alice", "Alča (alice)")
    assert recipient_from_text("  novak ", known) == ("novak", "")
    assert recipient_from_text("Jan Novák (jnovak)", known) == ("jnovak", "")
    assert recipient_from_text("", known) == ("", "")


def test_note_for_me_marked_in_list_and_opens_window(qapp, pms):
    from PySide6.QtCore import QUrl
    alice, bob = pms
    alice.add_notes("Forma", [Draft("**Pozor** na desku", recipient="bob", targets=["dily/deska.elt"])])
    w = make_window(qapp, bob)
    try:
        row = [p.name for p in w.projects].index("Forma")
        item = w.table.item(row, 0)
        assert not item.icon().isNull() and "Čeká na vás 1 poznámka" in item.toolTip()
        select_forma(w)                                          # klepnutí -> okno se otevře samo
        window = w._notes_windows[(w.settings.workspace_id, "Forma")]
        assert window.isVisible()
        text = window.view.toPlainText()
        assert "Pozor na desku" in text and "pro vás" in text and "NOVÁ" in text
        assert not window.editable and window.drafts_box.isHidden()   # neaktivovaný – bez rozepsaných
        # sloupec Poznámky ve stromu
        assert find_item(w.panel.tree, "dily/deska.elt").text(3) == "📝 1"
        # odkaz na soubor -> označí ve stromu
        window._on_link(QUrl("spmfile:dily/deska.elt"))
        assert w.panel.tree.currentItem().text(0) == "deska.elt"
        # vyřízení
        note_id = bob.notes("Forma")[0].note_id
        window._on_link(QUrl(f"spmresolve:{note_id}"))
        assert bob.notes("Forma")[0].resolved
        assert "Projekt nemá žádné otevřené poznámky" in window.view.toPlainText()
        window.show_resolved.setChecked(True)
        assert "vyřízeno" in window.view.toPlainText()
        # znovu klepnutí – už viděná, okno se samo neotevírá
        window.close()
        w.table.clearSelection()
        select_forma(w)
        assert not window.isVisible()
    finally:
        close(w, qapp)


def test_drafts_written_and_published_with_submission(qapp, pms, monkeypatch):
    from spm.gui import main_window
    alice, bob = pms
    alice.activate("Forma", lock_paths=["dily/deska.elt", "sestava.elt"])
    w = make_window(qapp, alice)
    try:
        select_forma(w)
        w.add_note(["dily/deska.elt"])                           # z nabídky stromu
        window = w._notes_windows[(w.settings.workspace_id, "Forma")]
        assert window.editable and window.isVisible()
        window.editor.setPlainText("# Deska\nje předběžná")
        window.recipient.setEditText("bob")
        window.flush()
        drafts = alice.drafts("Forma")
        assert [(d.title, d.recipient, d.targets) for d in drafts] == [("Deska", "bob", ["dily/deska.elt"])]
        assert bob.notes("Forma") == []                          # ostatní rozepsané nevidí

        # odevzdání: dialog ukáže rozepsané (zaškrtnuté) a zveřejní je
        seen = {}

        def run_now(parent, title, fn, *args, on_done=None, on_error=None, **kwargs):
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
        plan = alice.plan_upload("Forma")
        w._confirm_upload(plan, end_session=False)
        assert seen["checks"] == ["Deska → pro bob  (1 souborů / složek)"]
        assert "Zveřejněné poznámky: 1" in seen["msg"]
        note = bob.notes("Forma")[0]
        assert (note.text, note.recipient, note.targets) == ("# Deska\nje předběžná", "bob", ["dily/deska.elt"])
        assert alice.drafts("Forma") == []
    finally:
        close(w, qapp)


def test_quick_note_on_project_not_activated(qapp, pms, monkeypatch):
    from spm.gui import main_window
    alice, bob = pms

    class FakeQuick:
        def __init__(self, name, targets, users, parent):
            self.targets = targets
        def draft(self):
            return Draft("hned zveřejněná", recipient="alice", targets=self.targets)
        def deleteLater(self):
            pass
    monkeypatch.setattr(main_window, "QuickNoteDialog", FakeQuick)
    w = make_window(qapp, bob)
    try:
        monkeypatch.setattr(w, "_exec_dialog", lambda dlg, key: True)
        select_forma(w)
        w.add_note(["sestava.elt"])
        assert [(n.text, n.targets) for n in alice.notes("Forma")] == [("hned zveřejněná", ["sestava.elt"])]
        settle_panel(w.panel)
        assert find_item(w.panel.tree, "sestava.elt").text(3) == "📝 1"
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


def test_export_markdown(qapp, pms, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    from spm.gui.notes_window import NotesWindow
    alice, bob = pms
    alice.add_notes("Forma", [Draft("Text **poznámky**", targets=["sestava.elt"])])
    target = tmp_path / "export.md"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), ""))
    window = NotesWindow(bob, "Forma")
    window.export_md()
    text = target.read_text(encoding="utf-8")
    assert text.startswith("# Poznámky – projekt Forma") and "Text **poznámky**" in text
    assert "`sestava.elt`" in text and "spmresolve" not in text


def test_nickname_in_settings(qapp, pms):
    from spm.gui.settings_dialog import SettingsDialog
    alice, bob = pms
    dlg = SettingsDialog(alice.settings)
    dlg.nickname.setText("  Alča ")
    dlg._accept()
    assert alice.settings.nickname == "Alča" and alice.display_name == "Alča"


def test_note_for_me_is_highlighted(qapp, pms):
    from spm.gui.notes_window import FOR_ME_BACKGROUND, NotesWindow, notes_html
    alice, bob = pms
    alice.add_notes("Forma", [Draft("pro Boba", recipient="bob"), Draft("pro všechny"),
                              Draft("pro Alici", recipient="alice")])
    notes = bob.notes("Forma")
    html = notes_html(notes, "bob", seen=0, show_resolved=False)
    cards = html.split("<hr>")
    by_text = {n.text: card for n, card in zip(notes, cards)}
    assert FOR_ME_BACKGROUND in by_text["pro Boba"] and "PRO VÁS" in by_text["pro Boba"]
    assert FOR_ME_BACKGROUND not in by_text["pro všechny"]
    assert FOR_ME_BACKGROUND not in by_text["pro Alici"] and "pro alice" in by_text["pro Alici"]
    window = NotesWindow(bob, "Forma")
    assert "PRO VÁS" in window.view.toPlainText()
    assert window.view.toPlainText().count("PRO VÁS") == 1


def test_edit_and_reply_in_window(qapp, pms, monkeypatch):
    from PySide6.QtCore import QUrl
    from PySide6.QtWidgets import QMessageBox

    from spm.gui import notes_window
    from spm.gui.notes_window import NotesWindow, notes_html
    alice, bob = pms
    alice.add_notes("Forma", [Draft("Deska hotová", recipient="bob")])
    note_id = bob.notes("Forma")[0].note_id
    # odkaz Upravit jen u autora, Doplnit u všech
    assert f"spmedit:{note_id}" in notes_html(alice.notes("Forma"), "alice", 0, False)
    bob_html = notes_html(bob.notes("Forma"), "bob", 0, False)
    assert f"spmedit:{note_id}" not in bob_html and f"spmreply:{note_id}" in bob_html

    answers = {}

    class FakeEdit:
        def __init__(self, title, text, recipient, users, parent, hint="", ok_text=""):
            answers["prefill"] = text
        def exec(self):
            return True
        def text_value(self):
            return answers["text"]
        def recipient_value(self):
            return ("bob", "Bob")
    monkeypatch.setattr(notes_window, "NoteEditDialog", FakeEdit)

    seen = {"bob": 0}
    bob_window = NotesWindow(bob, "Forma", seen_get=lambda: seen["bob"],
                             seen_set=lambda v: seen.__setitem__("bob", v))
    assert "NOVÁ" in bob_window.view.toPlainText()
    answers["text"] = "Ano, rozumím."
    bob_window._on_link(QUrl(f"spmreply:{note_id}"))                 # Bob doplní
    assert [r.text for r in bob.notes("Forma")[0].replies] == ["Ano, rozumím."]
    assert "Ano, rozumím." in bob_window.view.toPlainText()

    alice_window = NotesWindow(alice, "Forma")
    answers["text"] = "Deska hotová, **díry posunuty**"
    alice_window._on_link(QUrl(f"spmedit:{note_id}"))               # Alice upraví
    assert answers["prefill"] == "Deska hotová"
    bob_window.reload()
    text = bob_window.view.toPlainText()
    assert "UPRAVENÁ" in text and "díry posunuty" in text and "Předchozí znění (1)" in text
    shown = []
    monkeypatch.setattr(QMessageBox, "exec", lambda box: shown.append(box.detailedText()) or 0)
    bob_window._on_link(QUrl(f"spmversions:{note_id}"))
    assert "Deska hotová" in shown[0]
    # Bob upravit cizí poznámku nemůže (ani podvrženým odkazem)
    errors = []
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: errors.append(a[2]))
    bob_window._on_link(QUrl(f"spmedit:{note_id}"))
    assert errors and "autor" in errors[0]


def test_new_reply_to_my_note_opens_window(qapp, pms):
    alice, bob = pms
    alice.add_notes("Forma", [Draft("Otázka pro všechny")])
    w = make_window(qapp, alice)
    try:
        select_forma(w)
        assert (w.settings.workspace_id, "Forma") not in w._notes_windows   # vlastní, bez doplnění
        w.open_notes("Forma").close()                              # Alice poznámky viděla
        bob.reply_note("Forma", bob.notes("Forma")[0].note_id, "Odpověď od Boba")
        w.refresh(check_names=False)
        w.table.clearSelection()
        select_forma(w)
        window = w._notes_windows[(w.settings.workspace_id, "Forma")]
        assert window.isVisible() and "nové doplnění" in window.view.toPlainText()
    finally:
        close(w, qapp)


def test_own_changes_are_not_new(qapp, pms):
    from spm.gui.notes_window import notes_html
    alice, bob = pms
    alice.add_notes("Forma", [Draft("Otázka", recipient="bob")])
    note_id = alice.notes("Forma")[0].note_id
    seen = alice.notes("Forma")[0].seq
    alice.reply_note("Forma", note_id, "Moje doplnění")
    alice.edit_note("Forma", note_id, "Otázka upravená")
    html = notes_html(alice.notes("Forma"), "alice", seen, False)
    assert "NOV" not in html and "UPRAVENÁ" not in html            # pro autorku nic nového
    html = notes_html(bob.notes("Forma"), "bob", seen, False)
    assert "UPRAVENÁ" in html and "NOVÉ" in html                    # pro Boba ano


def test_for_me_card_colors_follow_dark_mode(qapp, pms):
    from PySide6.QtGui import QColor, QPalette

    from spm.gui.notes_window import FOR_ME_DARK, FOR_ME_LIGHT, NotesWindow
    alice, bob = pms
    alice.add_notes("Forma", [Draft("pro Boba", recipient="bob")])
    window = NotesWindow(bob, "Forma")
    html = window.view.toHtml().lower()
    assert FOR_ME_LIGHT.background in html                          # světlý režim (výchozí paleta)
    dark = QPalette(window.palette())
    dark.setColor(QPalette.ColorRole.Base, QColor("#1e1e1e"))
    dark.setColor(QPalette.ColorRole.Text, QColor("#ffffff"))
    window.view.setPalette(dark)
    window.setPalette(dark)                                          # změna palety -> překreslení
    html = window.view.toHtml().lower()
    assert FOR_ME_DARK.background in html and FOR_ME_LIGHT.background not in html
    assert FOR_ME_DARK.text in html                                 # bílý text
