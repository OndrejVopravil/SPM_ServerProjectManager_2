import os

import pytest

from spm.config import Settings
from spm.sync import ProjectManager, new_key


@pytest.fixture(scope="module")
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def pms(tmp_path):
    proj = tmp_path / "server" / "Forma"
    for f in ("sestava/sestava.elt", "sestava/elektrody/E01.elt", "sestava/elektrody/E02.elt",
              "dily/deska.elt"):
        (proj / f).parent.mkdir(parents=True, exist_ok=True)
        (proj / f).write_text(f)

    def pm(user):
        s = Settings(server_root=str(tmp_path / "server"), local_root=str(tmp_path / f"local_{user}"))
        return ProjectManager(s, user=user, pc="PC-" + user)

    return pm("alice"), pm("bob")


def find(tree, path):
    from PySide6.QtWidgets import QTreeWidgetItemIterator
    it = QTreeWidgetItemIterator(tree)
    while it.value():
        item = it.value()
        names, parent = [item.text(0)], item.parent()
        while parent:
            names.insert(0, parent.text(0))
            parent = parent.parent()
        if "/".join(names) == path:
            return item
        it += 1
    raise KeyError(path)


def test_select_folder_and_uncheck_subfolder(app, pms):
    from PySide6.QtCore import Qt

    from spm.gui.file_select_dialog import FileSelectDialog

    alice, _ = pms
    dlg = FileSelectDialog("t", "", alice.file_entries("Forma"), "OK")
    find(dlg.tree, "sestava").setCheckState(0, Qt.CheckState.Checked)
    find(dlg.tree, "sestava/elektrody").setCheckState(0, Qt.CheckState.Unchecked)
    assert dlg.selected_paths() == ["sestava/sestava.elt"]
    assert find(dlg.tree, "sestava").checkState(0) == Qt.CheckState.PartiallyChecked


def test_files_held_by_others_cannot_be_selected(app, pms):
    from PySide6.QtCore import Qt

    from spm.gui.file_select_dialog import FileSelectDialog

    alice, bob = pms
    alice.activate("Forma", lock_paths=["sestava/sestava.elt"])
    dlg = FileSelectDialog("t", "", bob.file_entries("Forma"), "OK")
    dlg._set_all(Qt.CheckState.Checked)
    assert "sestava/sestava.elt" not in dlg.selected_paths()
    assert len(dlg.selected_paths()) == 3
    assert find(dlg.tree, "sestava/sestava.elt").text(1) == "alice (PC-alice)"


def test_upload_dialog_selection(app, pms):
    from PySide6.QtCore import Qt

    from spm.gui.upload_dialog import UploadDialog

    alice, _ = pms
    alice.activate("Forma", lock_paths=["sestava/sestava.elt"])
    local = alice.local_dir("Forma")
    (local / "sestava" / "elektrody" / "E03.elt").write_text("nova elektroda")
    (local / "sestava" / "elektrody" / "E04.elt").write_text("rozpracovana")
    plan = alice.plan_upload("Forma")

    dlg = UploadDialog(plan, end_session=False)
    ok = dlg.buttons.buttons()[0]
    assert not ok.isEnabled()                      # nic nevybráno
    find(dlg.tree_new, "sestava/elektrody/E03.elt").setCheckState(0, Qt.CheckState.Checked)
    assert dlg.selected_keys() == {new_key("sestava/elektrody/E03.elt")}
    # zaškrtnutí se přenese do záložky „Vše“
    assert find(dlg.tree_all, "sestava/elektrody/E03.elt").checkState(0) == Qt.CheckState.Checked
    assert not ok.isEnabled()                      # chybí komentář
    dlg.comment.setPlainText("E03")
    assert ok.isEnabled()

    deact = UploadDialog(plan, end_session=True)
    assert deact.selected_keys() is None           # deaktivace = vše
    assert not deact.tree_all.checkable


def test_upload_dialog_whole_folder_in_one_click(app, pms):
    from PySide6.QtCore import Qt

    from spm.gui.upload_dialog import UploadDialog

    alice, _ = pms
    alice.activate("Forma")  # zamkne vše
    local = alice.local_dir("Forma")
    (local / "sestava" / "elektrody" / "E01.elt").write_text("upravena")   # upravený
    (local / "sestava" / "elektrody" / "E02.elt").unlink()                  # smazaný
    (local / "sestava" / "elektrody" / "E03.elt").write_text("nova")       # nový
    (local / "dily" / "deska.elt").write_text("jina slozka")                # mimo složku
    plan = alice.plan_upload("Forma")
    dlg = UploadDialog(plan, end_session=False)
    ids = {bf.path: bf.file_id for bf in plan.missing + [m[0] for m in plan.modified]}
    # E02/E03 mají podobné názvy -> aplikace navrhne přejmenování; tady jde o smazání + nový
    dlg.combos[ids["sestava/elektrody/E02.elt"]].setCurrentIndex(0)

    find(dlg.tree_all, "sestava/elektrody").setCheckState(0, Qt.CheckState.Checked)
    assert dlg.selected_keys() == {ids["sestava/elektrody/E01.elt"], ids["sestava/elektrody/E02.elt"],
                                   new_key("sestava/elektrody/E03.elt")}
    assert find(dlg.tree_all, "sestava").checkState(0) == Qt.CheckState.PartiallyChecked

    dlg.comment.setPlainText("elektrody")
    res = alice.upload(plan, dlg.assignments(), dlg.comment_text(), dlg.selected_keys())
    assert (res.modified, res.deleted, res.added) == (1, 1, 1)


def test_project_panel_shows_locks_only_for_active_projects(app, pms, tmp_path, monkeypatch):
    from spm import config
    from spm.gui.main_window import MainWindow

    monkeypatch.setattr(config, "APP_DIR", tmp_path / "app")
    alice, bob = pms
    (tmp_path / "server" / "Volny").mkdir()
    alice.activate("Forma", lock_paths=["sestava/sestava.elt"])
    bob.activate("Forma", lock_paths=["dily/deska.elt"])

    w = MainWindow(bob.settings)
    w.pm = bob
    w.panel.pm = bob
    w.refresh()
    names = [p.name for p in w.projects]

    w.table.selectRow(names.index("Volny"))
    assert w.panel.isHidden()

    w.table.selectRow(names.index("Forma"))
    assert not w.panel.isHidden()
    w.panel._thread.wait()
    app.processEvents()
    owners = {e.path: (e.owner.user if e.owner else None, e.mine) for e in w.panel.entries}
    assert owners["sestava/sestava.elt"] == ("alice", False)
    assert owners["dily/deska.elt"] == ("bob", True)
    assert owners["sestava/elektrody/E01.elt"] == (None, False)
    assert "alice (PC-alice): <b>1</b>" in w.panel.summary.text()
    assert find(w.panel.tree, "sestava/sestava.elt").text(1) == "alice (PC-alice)"

    w.panel.only_locked.setChecked(True)
    assert sorted(i.data(0, 256 + 1) for i in w.panel.tree.leaf_items()) == ["dily/deska.elt", "sestava/sestava.elt"]
    w._name_check and w._name_check.wait()
    w.close()


def test_click_refreshes_row_without_full_refresh(app, pms, tmp_path, monkeypatch):
    from spm import config
    from spm.gui.main_window import MainWindow

    monkeypatch.setattr(config, "APP_DIR", tmp_path / "app")
    alice, bob = pms
    (tmp_path / "server" / "Volny").mkdir()
    w = MainWindow(bob.settings)
    w.pm = w.panel.pm = bob
    w.refresh(check_names=False)
    names = [p.name for p in w.projects]
    w.table.selectRow(names.index("Volny"))
    assert w.table.item(names.index("Forma"), 1).text() == "Volný"

    alice.activate("Forma", lock_paths=[])   # mezitím, bez obnovení seznamu
    w.table.selectRow(names.index("Forma"))  # klepnutí na projekt
    row = names.index("Forma")
    assert w.table.item(row, 1).text() == "Pracují jiní"
    assert w.table.item(row, 2).text() == "alice"
    assert not w.panel.isHidden()
    w.panel._thread.wait()
    w.close()


def test_f3_toggles_activation(app, pms, tmp_path, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeySequence

    from spm import config
    from spm.gui.main_window import MainWindow

    monkeypatch.setattr(config, "APP_DIR", tmp_path / "app")
    alice, bob = pms
    (tmp_path / "server" / "Moje").mkdir()
    bob.activate("Moje")
    w = MainWindow(bob.settings)
    w.pm = w.panel.pm = bob
    calls = []
    monkeypatch.setattr(w, "activate", lambda: calls.append("activate"))
    monkeypatch.setattr(w, "deactivate", lambda: calls.append("deactivate"))
    w.refresh(check_names=False)
    names = [p.name for p in w.projects]

    keys = {a.shortcut().toString() for a in w.actions() + w.findChildren(type(w.act_refresh))}
    assert {"F3", "F5"} <= keys

    w.table.selectRow(names.index("Forma"))   # volný -> aktivovat
    w.toggle_activation()
    w.table.selectRow(names.index("Moje"))    # můj -> deaktivovat
    w.toggle_activation()
    w.table.clearSelection()                  # nic vybráno -> nic
    w.toggle_activation()
    assert calls == ["activate", "deactivate"]
    w.panel._thread and w.panel._thread.wait()
    w.close()


def test_panel_double_click_opens_only_my_files(app, pms, tmp_path, monkeypatch):
    from pathlib import Path

    from spm import config
    from spm.gui import project_panel
    from spm.gui.main_window import MainWindow

    monkeypatch.setattr(config, "APP_DIR", tmp_path / "app")
    opened = []
    monkeypatch.setattr(project_panel.os, "startfile", lambda p: opened.append(Path(p)), raising=False)
    alice, bob = pms
    alice.activate("Forma", lock_paths=["sestava/sestava.elt"])
    bob.activate("Forma", lock_paths=["dily/deska.elt"])
    w = MainWindow(bob.settings)
    w.pm = w.panel.pm = bob
    w.refresh(check_names=False)
    w.table.selectRow([p.name for p in w.projects].index("Forma"))
    w.panel._thread.wait()
    app.processEvents()

    for path in ("sestava/sestava.elt", "sestava/elektrody/E01.elt", "dily/deska.elt"):
        w.panel._on_double_click(find(w.panel.tree, path), 0)
    assert opened == [bob.local_dir("Forma") / "dily" / "deska.elt"]
    assert "Dvojklikem" in find(w.panel.tree, "dily/deska.elt").toolTip(0)
    w.close()


def test_panel_context_actions_and_preselection(app, pms, tmp_path, monkeypatch):
    from spm import config
    from spm.gui.file_select_dialog import FileSelectDialog
    from spm.gui.main_window import MainWindow
    from spm.gui.upload_dialog import UploadDialog

    monkeypatch.setattr(config, "APP_DIR", tmp_path / "app")
    alice, bob = pms
    alice.activate("Forma", lock_paths=["sestava/sestava.elt"])
    bob.activate("Forma", lock_paths=["sestava/elektrody/E01.elt", "sestava/elektrody/E02.elt"])
    w = MainWindow(bob.settings)
    w.pm = w.panel.pm = bob
    w.refresh(check_names=False)
    w.table.selectRow([p.name for p in w.projects].index("Forma"))
    w.panel._thread.wait()
    app.processEvents()

    ca = w.panel.context_actions
    assert ca("sestava/elektrody/E01.elt") == {"open": True, "submit": True, "take": False}
    assert ca("sestava/sestava.elt") == {"open": False, "submit": False, "take": False}  # drží alice
    assert ca("dily/deska.elt") == {"open": False, "submit": False, "take": True}         # volný
    assert ca("sestava/") == {"open": False, "submit": True, "take": False}

    # menu panelu -> dialogy hlavního okna s předvýběrem
    got = []
    monkeypatch.setattr(w, "submit", lambda scope=None: got.append(("submit", scope)))
    monkeypatch.setattr(w, "take", lambda scope=None: got.append(("take", scope)))
    w.panel.submit_requested.disconnect()
    w.panel.take_requested.disconnect()
    w.panel.submit_requested.connect(w.submit)
    w.panel.take_requested.connect(w.take)
    w.panel.submit_requested.emit("sestava/elektrody/")
    w.panel.take_requested.emit("dily/")
    assert got == [("submit", "sestava/elektrody/"), ("take", "dily/")]

    # odevzdání: předvybrána změněná elektroda ve složce, ne soubor mimo ni
    local = bob.local_dir("Forma")
    (local / "sestava" / "elektrody" / "E01.elt").write_text("zmena")
    (local / "sestava" / "elektrody" / "E01_NC.nc").write_text("nc")
    (local / "jinde.txt").write_text("x")
    plan = bob.plan_upload("Forma")
    dlg = UploadDialog(plan, end_session=False, preselect="sestava/elektrody/")
    e01 = next(bf.file_id for bf, _ in plan.modified)
    e02 = next(bf.file_id for bf in plan.locked_unchanged)
    assert dlg.selected_keys() == {e01, e02, new_key("sestava/elektrody/E01_NC.nc")}

    # převzetí: předvybrány jen volné soubory ve složce
    sel = FileSelectDialog("t", "", bob.file_entries("Forma"), "OK", preselect="sestava/")
    assert sel.selected_paths() == []            # ve složce sestava nic volného není
    sel = FileSelectDialog("t", "", bob.file_entries("Forma"), "OK", preselect="dily/deska.elt")
    assert sel.selected_paths() == ["dily/deska.elt"]
    w.close()


def test_panel_colors_file_names_by_owner(app, pms, tmp_path, monkeypatch):
    from spm import config
    from spm.gui.main_window import MainWindow
    from spm.gui.project_panel import COLOR_MINE, COLOR_OTHER

    monkeypatch.setattr(config, "APP_DIR", tmp_path / "app")
    alice, bob = pms
    alice.activate("Forma", lock_paths=["sestava/sestava.elt"])
    bob.activate("Forma", lock_paths=["dily/deska.elt"])
    w = MainWindow(bob.settings)
    w.pm = w.panel.pm = bob
    w.refresh(check_names=False)
    w.table.selectRow([p.name for p in w.projects].index("Forma"))
    w.panel._thread.wait()
    app.processEvents()
    tree = w.panel.tree
    assert find(tree, "dily/deska.elt").foreground(0).color() == COLOR_MINE
    assert find(tree, "sestava/sestava.elt").foreground(0).color() == COLOR_OTHER
    free = find(tree, "sestava/elektrody/E01.elt").foreground(0)
    assert free.color() not in (COLOR_MINE, COLOR_OTHER)
    w.close()
