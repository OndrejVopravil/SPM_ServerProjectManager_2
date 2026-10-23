"""Zadání názvu nové složky s našeptáváním podle sousedních složek; Kopírovat název."""
import os

import pytest


@pytest.fixture
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_child_folders():
    from spm.gui.name_dialog import child_folders
    paths = ["sestava/elektrody/E01.elt", "sestava/elektrody/E02.elt", "sestava/sestava.elt",
             "sestava/Vykresy/", "dily/deska.elt", "koren.txt", "Sestava/pod/x.txt"]
    assert child_folders(paths, "") == ["dily", "sestava"]               # bez souborů, bez duplicit
    assert child_folders(paths, "sestava/") == ["elektrody", "pod", "Vykresy"]
    assert child_folders(paths, "sestava/elektrody/") == []


def test_dialog_offers_sibling_names(qapp, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QInputDialog, QLineEdit

    from spm.gui import name_dialog
    seen = {}

    def exec_(dlg):
        edit = dlg.findChild(QLineEdit)
        completer = edit.completer()
        completer.setCompletionPrefix("e0")                          # část názvu, malými
        seen["matches"] = [completer.completionModel().index(i, 0).data()
                           for i in range(completer.completionCount())]
        seen["case"] = completer.caseSensitivity()
        dlg.setTextValue("E03")
        return QInputDialog.DialogCode.Accepted
    monkeypatch.setattr(QInputDialog, "exec", exec_)
    assert name_dialog.real_ask_text(None, "Nová složka", "Název:", "", ["E02", "E01", "Vykresy", "E01"]) \
        == ("E03", True)
    assert seen["matches"] == ["E01", "E02"] and seen["case"] == Qt.CaseSensitivity.CaseInsensitive
    monkeypatch.setattr(QInputDialog, "exec", lambda dlg: QInputDialog.DialogCode.Rejected)
    assert name_dialog.real_ask_text(None, "Nová složka", "Název:")[1] is False


def test_project_panel_suggests_and_copies_names(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from conftest import settle_panel
    from spm.config import Settings
    from spm.gui import name_dialog
    from spm.gui.project_panel import ProjectPanel
    from spm.sync import ProjectManager
    server = tmp_path / "server" / "Forma"
    for f in ("sestava/elektrody/E01.elt", "sestava/elektrody/E02.elt", "dily/deska.elt"):
        (server / f).parent.mkdir(parents=True, exist_ok=True)
        (server / f).write_text("x", encoding="utf-8")
    pm = ProjectManager(Settings(server_root=str(tmp_path / "server"), local_root=str(tmp_path / "local")),
                        user="alice", pc="PC")
    pm.activate("Forma", lock_paths=[])
    pm.create_local_folder("Forma", "sestava/", "Prazdna")              # prázdná místní složka
    panel = ProjectPanel(pm)
    panel.can_activate = lambda: True
    panel.show_project(pm.project_status("Forma"))
    settle_panel(panel)
    assert panel.folder_names("") == ["dily", "sestava"]
    assert panel.folder_names("sestava/") == ["elektrody", "Prazdna"]
    offered = []
    monkeypatch.setattr(name_dialog, "ask_text",
                        lambda *a, **k: offered.append(list(a[4])) or ("E03", True))
    panel.create_folder("sestava/elektrody/")
    assert offered == [[]] and (pm.local_dir("Forma") / "sestava/elektrody/E03").is_dir()
    panel.create_folder("sestava/")
    assert offered[-1] == ["elektrody", "Prazdna"]
    # Kopírovat název – jen názvy, bez cesty
    assert panel.copy_names(["sestava/elektrody/E01.elt"]) == "E01.elt"
    assert QApplication.clipboard().text() == "E01.elt"
    assert panel.copy_names(["sestava/elektrody/", "dily/deska.elt"]) == "elektrody\ndeska.elt"


def test_copy_project_name_and_suggest_project_names(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from spm import config
    from spm.config import Settings
    from spm.gui import name_dialog
    from spm.gui.main_window import MainWindow
    monkeypatch.setattr(config, "APP_DIR", tmp_path / "app")
    for name in ("058768", "058769"):
        (tmp_path / "server" / name).mkdir(parents=True)
    w = MainWindow(Settings(server_root=str(tmp_path / "server"), local_root=str(tmp_path / "local")))
    try:
        w.refresh(check_names=False)
        w.table.selectRow(0)
        assert w.copy_project_name() == "058768" and QApplication.clipboard().text() == "058768"
        offered = []
        monkeypatch.setattr(name_dialog, "ask_text",
                            lambda *a, **k: offered.append(list(a[4])) or ("058770", True))
        w.create_project()
        assert offered == [["058768", "058769"]] and (tmp_path / "local" / "058770").is_dir()
    finally:
        if w._name_check:
            w._name_check.wait()
        w._messages_check.wait()
        w.close()
        qapp.processEvents()
