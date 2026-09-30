import os

import pytest

from spm.config import Settings
from spm.db import ProjectDB
from spm.sync import ProjectManager, new_key


@pytest.fixture(scope="module")
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def history(app, tmp_path):
    from spm.gui.history_dialog import HistoryDialog

    proj = tmp_path / "server" / "Forma"
    (proj / "sestava").mkdir(parents=True)
    (proj / "sestava" / "sestava.elt").write_text("s")

    def pm(user):
        s = Settings(server_root=str(tmp_path / "server"), local_root=str(tmp_path / f"l_{user}"))
        return ProjectManager(s, user=user, pc="PC-" + user)

    alice, bob = pm("alice"), pm("bob")
    alice.activate("Forma")
    (alice.local_dir("Forma") / "sestava" / "E01.elt").write_text("e1")
    plan = alice.plan_upload("Forma")
    alice.upload(plan, plan.default_assignments(), "Elektroda hotová",
                 selected={new_key("sestava/E01.elt")})  # sestava zůstane zamčená
    (alice.local_dir("Forma") / "sestava" / "sestava.elt").write_text("s2")
    (alice.local_dir("Forma") / "sestava" / "E02.elt").write_text("e2")
    plan = alice.plan_upload("Forma")
    alice.deactivate(plan, plan.default_assignments(), "Úprava sestavy")
    bob.activate("Forma")
    (bob.local_dir("Forma") / "sestava" / "E01_NC.nc").write_text("nc")
    plan = bob.plan_upload("Forma")
    bob.deactivate(plan, plan.default_assignments(), "NC program")

    db = ProjectDB.for_project(proj)
    dlg = HistoryDialog(db, "Forma")
    yield dlg
    db.close()


def visible_comments(dlg):
    return [dlg._session_rows[r]["comment"] for r in range(dlg.sessions.rowCount())
            if not dlg.sessions.isRowHidden(r)]


def shown_changes(dlg):
    return [dlg.changes.item(r, 2).text() for r in range(dlg.changes.rowCount())]


def test_search_by_comment_ignores_case_and_diacritics(history):
    history.search.setText("uprava SESTAVY")
    assert visible_comments(history) == ["Úprava sestavy"]
    assert sorted(shown_changes(history)) == ["sestava/E02.elt", "sestava/sestava.elt"]  # všechny změny


def test_search_by_user(history):
    history.search.setText("bob")
    assert visible_comments(history) == ["NC program"]


def test_search_by_file_shows_only_matching_changes(history):
    history.search.setText("E02")
    assert visible_comments(history) == ["Úprava sestavy"]
    assert shown_changes(history) == ["sestava/E02.elt"]   # jen změny hledaného souboru
    assert history.found.text() == "Nalezeno záznamů: 1 z 4"

    history.search.setText("e01")   # E01.elt i E01_NC.nc
    assert visible_comments(history) == ["NC program", "Elektroda hotová"]


def test_clear_search_shows_everything(history):
    history.search.setText("nic takoveho")
    assert visible_comments(history) == [] and shown_changes(history) == []
    history.search.clear()
    assert len(visible_comments(history)) == 4
    assert history.found.isHidden()
