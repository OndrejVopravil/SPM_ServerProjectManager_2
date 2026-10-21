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
    from spm.gui.timeline_dialog import TimelineDialog

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
    dlg = TimelineDialog(db, "Forma", me="alice")
    yield dlg
    db.close()


def visible_comments(dlg):
    return [c["comment"] for c in dlg.visible_commits()]


def shown_changes(dlg):
    return dlg.shown_changes()


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


def test_path_cells_have_full_path_tooltip(history, tmp_path):
    history.search.setText("Úprava sestavy")
    items = [history.changes.topLevelItem(r) for r in range(history.changes.topLevelItemCount())]
    item = next(i for i in items if i.text(1) == "sestava/sestava.elt")
    full = tmp_path / "server" / "Forma" / "sestava" / "sestava.elt"
    assert item.toolTip(1) == f"sestava/sestava.elt\n{full}"
    assert item.toolTip(0) == ""                                       # druh změny bez bubliny


def test_filters_kind_user_mine_and_files(history):
    from spm import db as dbm
    from spm.gui.timeline_dialog import KIND_FILTERS
    assert len(visible_comments(history)) == 4
    history.only_mine.setChecked(True)                                 # me = alice
    assert all(c["user"] == "alice" for c in history.visible_commits())
    history.only_mine.setChecked(False)
    history.user_filter.setCurrentIndex(history.user_filter.findData("bob"))
    assert visible_comments(history) == ["NC program"]
    history.user_filter.setCurrentIndex(0)
    upload = next(i for i, (_, kinds) in enumerate(KIND_FILTERS) if kinds == {dbm.UPLOAD})
    history.kind_filter.setCurrentIndex(upload)
    assert visible_comments(history) == ["NC program", "Úprava sestavy", "Elektroda hotová"]
    history.kind_filter.setCurrentIndex(0)
    # k souborům vybraným ve stromu
    history.scopes = ["sestava/E02.elt"]
    history.only_scopes.setChecked(True)
    assert visible_comments(history) == ["Úprava sestavy"] and shown_changes(history) == ["sestava/E02.elt"]


def test_cards_and_day_rows(history):
    from spm.gui.timeline_dialog import ROLE, ROW_COMMIT, ROW_DAY
    rows = [history.timeline.item(r).data(ROLE) for r in range(history.timeline.count())]
    assert rows[0]["row"] == ROW_DAY and rows[0]["label"] == "Dnes"
    cards = [r for r in rows if r["row"] == ROW_COMMIT]
    assert cards[0]["user"] == "bob" and cards[0]["kind_label"]
    assert any("nov" in text for text, _ in cards[0]["chips"])          # „+1 nový“
