"""Dva panely (jako v Total Commanderu): v každém jiná záložka, aktivní panel řídí lištu
a poznámky, schránka mezi panely, uložení režimu."""
import os
import stat
from pathlib import Path

import pytest
from conftest import settle_panel

from spm import config
from spm.config import AppSettings, Settings
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
def app_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DIR", tmp_path / "app")
    write(tmp_path / "serverA" / "Alfa" / "a.txt", "a")
    write(tmp_path / "serverB" / "Beta" / "dily" / "b.txt", "b")
    wa = AppSettings.new_workspace("A", Settings(server_root=str(tmp_path / "serverA"),
                                                 local_root=str(tmp_path / "localA")))
    wb = AppSettings.new_workspace("B", Settings(server_root=str(tmp_path / "serverB"),
                                                 local_root=str(tmp_path / "localB")))
    return AppSettings(Settings(), [wa, wb], wa["id"])


def make_window(qapp, app_settings):
    from spm.gui.main_window import MainWindow
    w = MainWindow(app_settings)
    w.refresh(check_names=False)
    return w


def close(w, qapp):
    w.close()
    qapp.processEvents()


def names(w) -> list[str]:
    return [p.name for p in w.projects]


def test_single_pane_by_default(qapp, app_settings):
    w = make_window(qapp, app_settings)
    try:
        assert not w.dual and w.panes[1].frame.isHidden() and w.visible_panes() == [w.panes[0]]
        assert w.status_box.parent() is w.panes[0].frame or w.panes[0].frame.isAncestorOf(w.status_box)
    finally:
        close(w, qapp)


def test_two_panes_with_different_tabs(qapp, app_settings):
    w = make_window(qapp, app_settings)
    try:
        w.act_dual.setChecked(True)
        left, right = w.panes
        assert w.dual and not right.frame.isHidden() and right.frame.isAncestorOf(w.status_box)
        assert right.projects and [p.name for p in right.projects] == ["Alfa"]   # stejná záložka jako vlevo
        ws_b = app_settings.workspaces[1]["id"]
        with w.on_pane(right):
            w.switch_workspace(ws_b)
        assert [p.name for p in right.projects] == ["Beta"] and names(w) == ["Alfa"]   # vlevo beze změny
        assert app_settings.active == app_settings.workspaces[0]["id"]
        assert app_settings.base.ui["pane2.workspace"] == ws_b
        assert right.tabs.tabData(right.tabs.currentIndex()) == ws_b
        assert left.tabs.tabData(left.tabs.currentIndex()) == app_settings.active
        # aktivní panel: lišta, zkratky i vlastnosti okna patří jemu
        w._activate_pane(right)
        assert w.table is right.table and names(w) == ["Beta"]
        right.table.selectRow(0)
        settle_panel(right.panel)
        assert w.act_activate.isEnabled() and w.selected().name == "Beta"
        assert right.side.indexOf(right.notes) >= 0 and left.side.indexOf(left.notes) >= 0  # každý své
        assert w.notes.name == "Beta"
        left.table.selectRow(0)                       # výběr v neaktivním panelu lištu nemění
        qapp.processEvents()
        assert w.selected().name == "Beta" and w.notes.name == "Beta"
        # vypnutí režimu: aktivní je zase levý
        w.act_dual.setChecked(False)
        assert w.active_pane is left and right.frame.isHidden() and left.frame.isAncestorOf(w.status_box)
    finally:
        close(w, qapp)


def test_notes_open_in_both_panes_at_once(qapp, app_settings):
    """Každý panel má své poznámky – v obou můžou být najednou otevřené poznámky jiného projektu."""
    ProjectManager(app_settings.view(app_settings.workspaces[0]["id"])).add_notes("Alfa", [Draft("k Alfě")])
    ProjectManager(app_settings.view(app_settings.workspaces[1]["id"])).add_notes("Beta", [Draft("k Betě")])
    w = make_window(qapp, app_settings)
    try:
        w.show()
        w.act_dual.setChecked(True)
        left, right = w.panes
        assert left.notes is not right.notes
        with w.on_pane(right):
            w.switch_workspace(app_settings.workspaces[1]["id"])
            right.table.selectRow(0)
            w.open_notes()
        left.table.selectRow(0)
        w._activate_pane(left)
        w.open_notes()
        qapp.processEvents()
        assert left.side.currentWidget() is left.notes and right.side.currentWidget() is right.notes
        assert left.notes.isVisible() and right.notes.isVisible()             # obě najednou
        assert [c.note.text for c in left.notes.card_widgets()] == ["k Alfě"]
        assert [c.note.text for c in right.notes.card_widgets()] == ["k Betě"]
        w._activate_pane(right)                                                # aktivace nic neschová
        qapp.processEvents()
        assert left.notes.isVisible() and right.notes.isVisible()
        assert left.side.indexOf(left.notes) >= 0 and right.side.indexOf(right.notes) >= 0
        # odpojení poznámek jednoho panelu druhý panel neovlivní
        with w.on_pane(left):
            w.toggle_notes_detached()
        assert left._notes_window is not None and right._notes_window is None
        assert right.side.indexOf(right.notes) >= 0 and left.side.indexOf(left.notes) < 0
        assert w.settings.ui["notes.detached"] == "1" and not w.settings.ui.get("notes.detached.2")
        left._notes_window.close()
        assert left._notes_window is None and left.side.indexOf(left.notes) >= 0
    finally:
        close(w, qapp)


def test_same_project_notes_in_both_panes_stay_in_sync(qapp, app_settings):
    w = make_window(qapp, app_settings)
    try:
        w.act_dual.setChecked(True)
        left, right = w.panes
        for pane in (left, right):
            pane.table.selectRow(0)                                            # obě – projekt Alfa
        left.notes.editor.setPlainText("nová poznámka")
        with w.on_pane(left):
            left.notes.send()
        assert [c.note.text for c in right.notes.card_widgets()] == ["nová poznámka"]
    finally:
        close(w, qapp)


def test_copy_between_panes(qapp, app_settings, tmp_path):
    view_a = app_settings.view(app_settings.workspaces[0]["id"])
    me = ProjectManager(view_a)
    me.activate("Alfa", lock_paths=[])
    w = make_window(qapp, app_settings)
    try:
        w.act_dual.setChecked(True)
        left, right = w.panes
        with w.on_pane(right):
            w.switch_workspace(app_settings.workspaces[1]["id"])
            right.table.selectRow(0)
        settle_panel(right.panel)
        left.table.selectRow(0)
        settle_panel(left.panel)
        assert right.panel.copy_selected(["dily/"])     # kopie z projektu Beta (pravý panel)
        left.panel.paste_into("")                        # do aktivovaného projektu Alfa (levý)
        settle_panel(left.panel)
        assert (Path(view_a.local_root) / "Alfa" / "dily" / "b.txt").read_text(encoding="utf-8") == "b"
    finally:
        close(w, qapp)


def test_folder_tab_in_second_pane(qapp, app_settings, tmp_path):
    write(tmp_path / "data" / "x.txt", "x")
    ws = AppSettings.new_workspace("Data")
    ws["folder_root"] = str(tmp_path / "data")
    app_settings.workspaces.append(ws)
    w = make_window(qapp, app_settings)
    try:
        w.act_dual.setChecked(True)
        left, right = w.panes
        with w.on_pane(right):
            w.switch_workspace(ws["id"])
        assert right.pages.currentWidget() is right.folder_panel
        assert left.pages.currentWidget() is left.projects_page and names(w) == ["Alfa"]
        w._activate_pane(right)
        assert "Složka:" in w.statusBar().currentMessage()
        w._activate_pane(left)
        assert "Server:" in w.statusBar().currentMessage()
    finally:
        close(w, qapp)


def test_dual_mode_and_tabs_remembered(qapp, app_settings):
    w = make_window(qapp, app_settings)
    ws_b = app_settings.workspaces[1]["id"]
    w.act_dual.setChecked(True)
    with w.on_pane(w.panes[1]):
        w.switch_workspace(ws_b)
    close(w, qapp)
    w = make_window(qapp, app_settings)
    try:
        assert w.dual and w.panes[1].settings.workspace_id == ws_b
        assert w.panes[0].settings.workspace_id == app_settings.workspaces[0]["id"]
    finally:
        close(w, qapp)


def test_removed_tab_switches_pane_showing_it(qapp, app_settings, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    w = make_window(qapp, app_settings)
    try:
        w.act_dual.setChecked(True)
        ws_b = app_settings.workspaces[1]["id"]
        with w.on_pane(w.panes[1]):
            w.switch_workspace(ws_b)
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
        w.remove_workspace(ws_b)
        assert w.panes[1].settings.workspace_id == app_settings.active
        assert w.panes[1].tabs.count() == 1 and w.panes[0].tabs.count() == 1
    finally:
        close(w, qapp)


# --- synchronizace panelů ------------------------------------------------------------------

def state_of(pane, name):
    from spm.gui.main_window import COL_STATE
    row = [p.name for p in pane.projects].index(name)
    return pane.table.item(row, COL_STATE).text()


def test_same_tab_in_both_panes_stays_in_sync(qapp, app_settings):
    w = make_window(qapp, app_settings)
    try:
        w.act_dual.setChecked(True)
        left, right = w.panes
        right.table.selectRow(0)
        settle_panel(right.panel)
        assert state_of(right, "Alfa") == "Volný"
        left.pm.activate("Alfa")                         # akce v levém panelu…
        w.refresh()                                       # …po ní okno obnoví seznam
        assert state_of(left, "Alfa") == "Aktivováno mnou"
        assert state_of(right, "Alfa") == "Aktivováno mnou"   # pravý bez vlastního obnovení
        settle_panel(right.panel)
        assert right.panel.project.mine                  # strom pravého panelu také
        # počet aktivovaných v záložce v liště obou panelů
        assert left.tabs.tabText(0) == right.tabs.tabText(0) == "A (1)"
        # stav jednoho projektu (klepnutí) – i řádek ve druhém panelu
        left.pm.cancel_activation("Alfa")
        with w.on_pane(left):
            left.table.clearSelection()
            left.table.selectRow(0)                      # _refresh_selected_row
        assert state_of(right, "Alfa") == "Volný"
    finally:
        close(w, qapp)


def test_other_tab_refreshed_after_action(qapp, app_settings):
    w = make_window(qapp, app_settings)
    try:
        w.act_dual.setChecked(True)
        left, right = w.panes
        ws_b = app_settings.workspaces[1]["id"]
        with w.on_pane(right):
            w.switch_workspace(ws_b)
        write(Path(right.settings.server_root) / "Gama" / "g.txt", "g")    # např. zrcadlení do B
        w.refresh()                                       # obnovení v levém panelu (záložka A)
        right._bg_refresh.wait()
        qapp.processEvents()
        assert [p.name for p in right.projects] == ["Beta", "Gama"]
    finally:
        close(w, qapp)


def test_tree_change_reloads_other_pane(qapp, app_settings, tmp_path):
    left_pm = ProjectManager(app_settings.view(app_settings.workspaces[0]["id"]))
    left_pm.activate("Alfa")
    w = make_window(qapp, app_settings)
    try:
        w.act_dual.setChecked(True)
        left, right = w.panes
        for pane in (left, right):
            pane.table.selectRow(0)
            settle_panel(pane.panel)
        write(tmp_path / "zdroj" / "novy.txt", "n")
        left.panel.drop_files([tmp_path / "zdroj" / "novy.txt"], None, "")   # kopie do projektu vlevo
        settle_panel(left.panel)
        settle_panel(right.panel)
        assert any(e.path == "novy.txt" for e in right.panel.entries)       # vpravo bez obnovení
    finally:
        close(w, qapp)


# --- rozložení panelů ---------------------------------------------------------------------

def test_splitters_move_together_and_tree_columns(qapp, app_settings):
    w = make_window(qapp, app_settings)
    try:
        w.resize(1200, 700)
        w.show()
        w.act_dual.setChecked(True)
        left, right = w.panes
        for pane in (left, right):
            pane.table.selectRow(0)
        qapp.processEvents()
        left.splitter.moveSplitter(150, 1)                                   # jako tažení myší
        qapp.processEvents()
        assert right.splitter.sizes() == left.splitter.sizes()
        assert not hasattr(left.frame, "set_active")                         # bez rámečku aktivního panelu
        # sloupce stromu: Soubor nejširší, ostatní úzké a nastavitelné
        from PySide6.QtWidgets import QHeaderView
        tree = left.panel.tree
        header = tree.header()
        assert header.sectionResizeMode(0) == QHeaderView.ResizeMode.Stretch
        assert all(header.sectionResizeMode(i) == QHeaderView.ResizeMode.Interactive for i in (1, 2, 3))
        assert tree.columnWidth(0) > max(tree.columnWidth(i) for i in (1, 2, 3))
        assert max(tree.columnWidth(i) for i in (1, 2, 3)) < 100
    finally:
        close(w, qapp)
