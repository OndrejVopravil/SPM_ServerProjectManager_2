"""Komentář projektu: uložení v .spm/comment.json, historie v databázi, zobrazení v seznamu."""
import json
import os

import pytest

from spm import config
from spm import db as dbm
from spm.comment import MAX_LENGTH, comment_path, normalize_text, read_comment
from spm.config import Settings
from spm.db import ProjectDB
from spm.sync import ProjectManager, SyncError


@pytest.fixture
def env(tmp_path):
    proj = tmp_path / "server" / "Forma"
    proj.mkdir(parents=True)
    (proj / "a.txt").write_text("a")

    def manager(user):
        s = Settings(server_root=str(tmp_path / "server"), local_root=str(tmp_path / f"l_{user}"))
        return ProjectManager(s, user=user, pc=f"PC-{user}")

    return manager("alice"), manager("bob"), proj


def comment_commits(proj):
    with ProjectDB.for_project(proj) as db:
        return [(c["user"], c["comment"]) for c in db.commits() if c["kind"] == dbm.PROJECT_COMMENT]


def test_set_comment_on_never_activated_project(env):
    alice, bob, proj = env
    assert alice.set_comment("Forma", "Čeká na NC\nelektrody E01–E05  \n\n")
    c = read_comment(proj)
    assert c.text == "Čeká na NC\nelektrody E01–E05"          # bez mezer a prázdných řádků na konci
    assert (c.user, c.pc, c.first_line) == ("alice", "PC-alice", "Čeká na NC")
    assert comment_commits(proj) == [("alice", "Čeká na NC\nelektrody E01–E05")]
    status = {p.name: p for p in bob.list_projects()}["Forma"]
    assert status.comment.text == c.text


def test_unchanged_comment_not_recorded(env):
    alice, bob, proj = env
    alice.set_comment("Forma", "text")
    assert not bob.set_comment("Forma", "text\n")
    assert comment_commits(proj) == [("alice", "text")]


def test_anyone_can_change_even_when_activated_by_other(env):
    alice, bob, proj = env
    alice.activate("Forma")
    alice.set_comment("Forma", "rozpracováno")
    assert bob.set_comment("Forma", "rozpracováno – alice")
    assert read_comment(proj).user == "bob"
    assert comment_commits(proj) == [("bob", "rozpracováno – alice"), ("alice", "rozpracováno")]


def test_empty_text_deletes_comment_and_is_recorded(env):
    alice, bob, proj = env
    alice.set_comment("Forma", "x")
    assert bob.set_comment("Forma", "  \n ")
    assert read_comment(proj) is None and not comment_path(proj).exists()
    assert comment_commits(proj)[0] == ("bob", "")


def test_too_long_rejected(env):
    alice, bob, proj = env
    with pytest.raises(SyncError, match="příliš dlouhý"):
        alice.set_comment("Forma", "x" * (MAX_LENGTH + 1))
    assert read_comment(proj) is None


def test_history_written_before_comment(env, monkeypatch):
    """Když se zápis do historie nepovede, komentář zůstane původní."""
    alice, bob, proj = env
    alice.set_comment("Forma", "puvodni")

    def fail(*a, **k):
        raise OSError("databáze nedostupná")
    monkeypatch.setattr(ProjectDB, "add_commit", fail)
    with pytest.raises(OSError):
        bob.set_comment("Forma", "novy")
    assert read_comment(proj).text == "puvodni"


def test_comment_survives_project_rename(env):
    alice, bob, proj = env
    alice.set_comment("Forma", "popis")
    alice.rename_project("Forma", "Forma2")
    assert read_comment(proj.parent / "Forma2").text == "popis"


def test_broken_comment_file_ignored(env):
    alice, bob, proj = env
    comment_path(proj).parent.mkdir(parents=True, exist_ok=True)
    comment_path(proj).write_text("{nesmysl", encoding="utf-8")
    assert read_comment(proj) is None
    comment_path(proj).write_text(json.dumps({"text": 5}), encoding="utf-8")
    assert read_comment(proj) is None


def test_normalize_text():
    assert normalize_text("\r\n a \r\nb  \r\n\r\n") == " a\nb"


def test_restore_points_ignore_comment_changes(env):
    alice, bob, proj = env
    alice.activate("Forma")
    plan = alice.plan_upload("Forma")
    alice.deactivate(plan, plan.default_assignments(), "")
    before = len(alice.restore_points("Forma").points)
    alice.set_comment("Forma", "komentar")
    assert len(alice.restore_points("Forma").points) == before


# --- GUI -------------------------------------------------------------------------------

@pytest.fixture
def window(env, tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from spm.gui.main_window import MainWindow
    monkeypatch.setattr(config, "APP_DIR", tmp_path / "app")
    alice, bob, proj = env
    (proj.parent / "Jina").mkdir()
    alice.set_comment("Forma", "Čeká na NC\ndruhý řádek")
    app = QApplication.instance() or QApplication([])
    w = MainWindow(bob.settings)
    w.pm = bob
    w.refresh(check_names=False)
    yield w
    w.close()
    app.processEvents()


def row_of(w, name):
    return [p.name for p in w.projects].index(name)


def test_column_shows_first_line_and_tooltip(window):
    from spm.gui.main_window import COL_COMMENT
    item = window.table.item(row_of(window, "Forma"), COL_COMMENT)
    assert item.text() == "Čeká na NC …"
    assert "druhý řádek" in item.toolTip() and "alice (PC-alice)" in item.toolTip()
    assert window.table.item(row_of(window, "Jina"), COL_COMMENT).text() == ""


def test_search_finds_comment(window):
    window.search.setText("ceka na nc")                      # bez diakritiky a velikosti písmen
    assert not window.table.isRowHidden(row_of(window, "Forma"))
    assert window.table.isRowHidden(row_of(window, "Jina"))


def test_edit_comment_via_dialog(window, monkeypatch):
    from spm.gui import main_window as mw
    from spm.gui.main_window import COL_COMMENT
    window.table.selectRow(row_of(window, "Jina"))
    assert window.act_comment.isEnabled()

    class FakeDialog:
        def __init__(self, name, comment, parent):
            assert name == "Jina" and comment is None
        def text(self):
            return "nový komentář"
        def deleteLater(self):
            pass
    monkeypatch.setattr(mw, "CommentDialog", FakeDialog)
    monkeypatch.setattr(window, "_exec_dialog", lambda dlg, key: True)
    window.edit_comment()
    assert window.table.item(row_of(window, "Jina"), COL_COMMENT).text() == "nový komentář"
    assert comment_commits(window.pm.server_dir("Jina")) == [("bob", "nový komentář")]


def test_comment_dialog_limits_length():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QDialogButtonBox

    from spm.gui.comment_dialog import CommentDialog
    QApplication.instance() or QApplication([])
    dlg = CommentDialog("Forma", None)
    ok = dlg.buttons.button(QDialogButtonBox.StandardButton.Ok)
    dlg.edit.setPlainText("x" * MAX_LENGTH)
    assert ok.isEnabled()
    dlg.edit.setPlainText("x" * (MAX_LENGTH + 1))
    assert not ok.isEnabled() and "příliš dlouhý" in dlg.counter.text()


def test_history_shows_comment_change(env):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from spm.gui.timeline_dialog import TimelineDialog
    QApplication.instance() or QApplication([])
    alice, bob, proj = env
    alice.set_comment("Forma", "a\nb")
    bob.set_comment("Forma", "")
    with ProjectDB.for_project(proj) as db:
        dlg = TimelineDialog(db, "Forma")
        from spm.gui.timeline_dialog import ROLE, ROW_COMMIT
        cards = [dlg.timeline.item(r).data(ROLE) for r in range(dlg.timeline.count())]
        texts = [(c["kind_label"], c["comment"]) for c in cards if c["row"] == ROW_COMMIT]
        assert texts == [("Komentář projektu", "(komentář smazán)"), ("Komentář projektu", "a ↵ b")]
        dlg.search.setText("komentar smazan")
        assert [c["comment"] or "" for c in dlg.visible_commits()] == [""]    # jen smazání komentáře


def test_double_click_name_toggles_activation(window, monkeypatch):
    from spm.gui.main_window import COL_COMMENT, COL_NAME, COL_STATE
    calls = []
    for name in ("activate", "deactivate", "open_local", "edit_comment"):
        monkeypatch.setattr(window, name, lambda *a, n=name, **k: calls.append(n))
    r = row_of(window, "Forma")
    window.table.selectRow(r)
    model = window.table.model()
    window._on_double_click(model.index(r, COL_NAME))
    assert calls == ["activate"]
    window._on_double_click(model.index(r, COL_COMMENT))
    window._on_double_click(model.index(r, COL_STATE))
    assert calls == ["activate", "edit_comment", "open_local"]
    # můj aktivovaný projekt -> deaktivace
    window.pm.activate("Forma")
    window.refresh(check_names=False)
    window.table.selectRow(row_of(window, "Forma"))
    calls.clear()
    window._on_double_click(model.index(row_of(window, "Forma"), COL_NAME))
    assert calls == ["deactivate"]


def test_comment_colored_like_project_name(window, env):
    from spm.gui.main_window import COL_COMMENT, COL_NAME, COLOR_MINE, COLOR_OTHER
    alice, bob, proj = env

    def colors(name):
        r = row_of(window, name)
        return (window.table.item(r, COL_NAME).foreground().color(),
                window.table.item(r, COL_COMMENT).foreground().color())
    bob.activate("Forma")                                   # aktivováno mnou – zeleně
    window.refresh(check_names=False)
    name, comment = colors("Forma")
    assert name == comment == COLOR_MINE
    bob.cancel_activation("Forma")
    alice.activate("Forma")                                 # pracují jiní – modře
    window.refresh(check_names=False)
    name, comment = colors("Forma")
    assert name == comment == COLOR_OTHER
    r = row_of(window, "Forma")
    assert "druhý řádek" in window.table.item(r, COL_COMMENT).toolTip()   # bublina = celý komentář
