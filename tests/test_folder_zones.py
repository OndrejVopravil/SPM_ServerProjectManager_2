"""Chráněné oblasti v záložce Složka: složky spolupráce jsou vidět, ale nic se do nich nezapíše;
dvojklik na soubor přepne do jeho projektu."""
import os
import stat
from pathlib import Path

import pytest
from conftest import settle_panel

from spm import config
from spm.config import AppSettings, Settings
from spm.folder_zones import LOCAL, PROJECT, SERVER, ProtectedZones
from spm.sync import ProjectManager


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.chmod(stat.S_IWRITE | stat.S_IREAD)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def env(tmp_path, monkeypatch):
    """C:\\…\\Work = záložka Složka; obsahuje síťovou i místní složku záložky projektů a cizí
    projekt SPM (jen se servisní složkou .spm)."""
    monkeypatch.setattr(config, "APP_DIR", tmp_path / "app")
    work = tmp_path / "Work"
    write(work / "server" / "Forma" / "dily" / "deska.elt", "deska")
    write(work / "data" / "x.txt", "x")
    write(work / "cizi" / "Projekt" / ".spm" / "project.db", "")
    write(work / "cizi" / "Projekt" / "a.txt", "a")
    ws = AppSettings.new_workspace("Formy", Settings(server_root=str(work / "server"),
                                                    local_root=str(work / "local")))
    folder_ws = AppSettings.new_workspace("Work")
    folder_ws["folder_root"] = str(work)
    app = AppSettings(Settings(), [ws, folder_ws], ws["id"])
    pm = ProjectManager(app.view(ws["id"]))
    pm.activate("Forma")                                      # místní kopie work/local/Forma
    return app, work, pm


def test_zone_rules(env):
    app, work, pm = env
    zones = ProtectedZones.from_workspaces(app.workspaces)
    assert zones.zone_of(work / "server").kind == SERVER
    assert zones.zone_of(work / "local" / "Forma" / "dily" / "deska.elt").kind == LOCAL
    assert zones.zone_of(work / "cizi" / "Projekt" / "a.txt", work).kind == PROJECT
    assert zones.zone_of(work / "data" / "x.txt", work) is None
    assert zones.zone_of(work / "cizi", work) is None
    # složka, která chráněnou oblast obsahuje: zapisovat do ní lze, přejmenovat / smazat ne
    assert zones.write_blocked(work / "cizi", work) is None
    assert zones.modify_blocked(work / "cizi", work).kind == PROJECT
    assert zones.modify_blocked(work, work) is not None
    assert zones.modify_blocked(work / "data", work) is None
    local = zones.zone_of(work / "local")
    assert ProtectedZones.project_of(local, work / "local" / "Forma" / "dily" / "deska.elt") == ("Forma", "dily/deska.elt")
    assert ProtectedZones.project_of(local, work / "local") == ("", "")


@pytest.fixture
def panel(env, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from spm.gui.folder_panel import FolderPanel
    app, work, pm = env
    fp = FolderPanel()
    zones = ProtectedZones.from_workspaces(app.workspaces)
    fp.zones_get = lambda: zones
    fp.set_root(str(work), app.workspaces[1]["id"])
    refused = []
    monkeypatch.setattr(fp, "refuse", lambda zone, what, path=None: refused.append((what, zone.kind)))
    fp.refused = refused
    QApplication.clipboard().clear()
    return fp


def test_nothing_written_into_protected(panel, env, monkeypatch):
    app, work, pm = env
    deska = work / "local" / "Forma" / "dily" / "deska.elt"
    assert panel.put([work / "data" / "x.txt"], work / "local" / "Forma") is None
    assert not (work / "local" / "Forma" / "x.txt").exists() and panel.refused[-1][0] == "Sem nelze nic vložit"
    from PySide6.QtWidgets import QInputDialog
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("nova", True))
    panel.create_folder(work / "server")
    assert not (work / "server" / "nova").exists()
    assert not panel.copy_selected([deska], cut=True)                   # vyjmout ne
    assert panel.put([deska], work / "data", move=True) is None         # přesun ne
    assert deska.exists()
    assert panel.zip_paths([work / "cizi" / "Projekt" / "a.txt"]) is None   # zip do projektu ne
    # kopírovat ven ano
    assert panel.copy_selected([deska])
    panel.paste(work / "data")
    assert (work / "data" / "deska.elt").read_text(encoding="utf-8") == "deska"
    # vložit do složky, která chráněnou oblast jen obsahuje, ano
    assert panel.put([work / "data" / "x.txt"], work / "cizi")
    assert (work / "cizi" / "x.txt").exists()


def test_rename_and_delete_blocked(panel, env, monkeypatch):
    from spm import recycle
    app, work, pm = env
    removed = []
    monkeypatch.setattr(recycle, "to_recycle_bin", lambda paths: removed.extend(paths))
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    index = panel.model.index(str(work / "cizi"))
    panel.tree.selectionModel().select(index, panel.tree.selectionModel().SelectionFlag.ClearAndSelect
                                       | panel.tree.selectionModel().SelectionFlag.Rows)
    panel.delete_selected()                                             # obsahuje cizí projekt
    assert removed == [] and panel.refused
    assert not panel.model.setData(index, "jine")                      # přejmenování
    assert (work / "cizi").exists()
    data = panel.model.index(str(work / "data"))
    panel.tree.selectionModel().select(data, panel.tree.selectionModel().SelectionFlag.ClearAndSelect
                                       | panel.tree.selectionModel().SelectionFlag.Rows)
    panel.delete_selected()                                             # obyčejná složka – ano
    assert removed == [work / "data"]


def test_protected_shown_grey_with_lock_and_double_click(panel, env, monkeypatch):
    from PySide6.QtCore import Qt
    app, work, pm = env
    local = panel.model.index(str(work / "local"))
    assert panel.model.data(local, Qt.ItemDataRole.DisplayRole).endswith("🔒")
    assert panel.model.data(local, Qt.ItemDataRole.ForegroundRole) is not None
    assert "místní složka záložky „Formy“" in panel.model.data(local, Qt.ItemDataRole.ToolTipRole)
    inner = panel.model.index(str(work / "local" / "Forma"))
    assert not panel.model.data(inner, Qt.ItemDataRole.DisplayRole).endswith("🔒")   # zámek jen u kořene
    assert not (panel.model.flags(inner) & Qt.ItemFlag.ItemIsEditable)
    assert panel.model.data(panel.model.index(str(work / "data")), Qt.ItemDataRole.ForegroundRole) is None
    opened, started = [], []
    panel.open_in_project.connect(lambda p: opened.append(Path(p)))
    monkeypatch.setattr(os, "startfile", lambda p: started.append(p), raising=False)
    deska = work / "local" / "Forma" / "dily" / "deska.elt"
    panel._open_index(panel.model.index(str(deska)))
    assert opened == [deska] and started == []                         # neotevírá se


def test_double_click_switches_to_project(env):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from spm.gui.main_window import MainWindow
    qapp = QApplication.instance() or QApplication([])
    app, work, pm = env
    w = MainWindow(app)
    try:
        w.switch_workspace(app.workspaces[1]["id"])                     # záložka Složka
        assert w.pages.currentWidget() is w.folder_panel
        w.folder_panel.open_in_project.emit(work / "local" / "Forma" / "dily" / "deska.elt")
        assert w.settings.workspace_id == app.workspaces[0]["id"]       # přepnuto do Formy
        assert w.selected().name == "Forma"
        settle_panel(w.panel)
        assert w.panel.tree.currentItem().text(0) == "deska.elt"
    finally:
        w.close()
        qapp.processEvents()
