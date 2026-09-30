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
    item = dlg.list_added.findItems("sestava/elektrody/E03.elt", Qt.MatchFlag.MatchExactly)[0]
    item.setCheckState(Qt.CheckState.Checked)
    assert dlg.selected_keys() == {new_key("sestava/elektrody/E03.elt")}
    assert not ok.isEnabled()                      # chybí komentář
    dlg.comment.setPlainText("E03")
    assert ok.isEnabled()

    deact = UploadDialog(plan, end_session=True)
    assert deact.selected_keys() is None           # deaktivace = vše
