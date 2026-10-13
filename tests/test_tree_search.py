"""Hledání souborů ve stromu panelu stavu projektu."""
import os

import pytest
from conftest import settle_panel
from tests_helpers_tree import find_item

from spm import config
from spm.config import Settings


@pytest.fixture
def window(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from spm.gui.main_window import MainWindow
    monkeypatch.setattr(config, "APP_DIR", tmp_path / "app")
    for f in ("dily/deska_E01.elt", "dily/nc/abcdxyz.nc", "dily/nc/bcdxyz.nc", "sestava.elt"):
        path = tmp_path / "server" / "Forma" / f
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
    qapp = QApplication.instance() or QApplication([])
    w = MainWindow(Settings(server_root=str(tmp_path / "server"), local_root=str(tmp_path / "local")))
    w.refresh(check_names=False)
    w.table.selectRow(0)
    settle_panel(w.panel)
    yield w
    w.close()
    qapp.processEvents()


def highlighted(tree):
    from PySide6.QtCore import Qt
    return sorted(i.data(0, 256 + 1) for i in tree.leaf_items()
                  if i.background(0).style() != Qt.BrushStyle.NoBrush)


def test_search_highlights_matching_files(window):
    panel = window.panel
    panel.search.setText("*xyz")
    assert highlighted(panel.tree) == ["dily/nc/abcdxyz.nc", "dily/nc/bcdxyz.nc"]
    assert panel.found.text() == "Nalezeno: 2"
    assert find_item(panel.tree, "dily/nc/bcdxyz.nc").background(0).color().name() == "#fff176"
    panel.search.setText("E01")
    assert highlighted(panel.tree) == ["dily/deska_E01.elt"]


def test_search_survives_tree_reload(window):
    panel = window.panel
    panel.search.setText("sestava")
    panel.reload()
    settle_panel(panel)
    assert highlighted(panel.tree) == ["sestava.elt"]


def test_escape_clears_search(window):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QShortcut
    panel = window.panel
    panel.search.setText("xyz")
    shortcut = next(s for s in panel.findChildren(QShortcut) if s.key().toString() == "Esc")
    assert shortcut.context() == Qt.ShortcutContext.WidgetWithChildrenShortcut
    shortcut.activated.emit()
    assert panel.search.text() == "" and highlighted(panel.tree) == [] and panel.found.text() == ""


def test_found_text_is_black_and_original_color_restored(window):
    from PySide6.QtGui import QColor
    panel = window.panel
    item = find_item(panel.tree, "sestava.elt")
    item.setForeground(1, QColor("#2e8b57"))              # např. zelená „já“ ve sloupci Drží
    original = [item.foreground(c) for c in range(panel.tree.columnCount())]
    panel.search.setText("sestava")
    assert all(item.foreground(c).color() == QColor("black") for c in range(panel.tree.columnCount()))
    panel.search.setText("sestava.e")                     # opakované podbarvení nepřepíše uložené barvy
    panel.search.clear()
    assert [item.foreground(c) for c in range(panel.tree.columnCount())] == original
    assert item.foreground(1).color() == QColor("#2e8b57")


def test_collapsed_folders_stay_collapsed_after_reload(window, tmp_path):
    panel = window.panel
    dily, nc = find_item(panel.tree, "dily"), find_item(panel.tree, "dily/nc")
    assert dily.isExpanded() and nc.isExpanded()               # poprvé vše rozbalené
    nc.setExpanded(False)
    (tmp_path / "server" / "Forma" / "nova" / "x.txt").parent.mkdir()
    (tmp_path / "server" / "Forma" / "nova" / "x.txt").write_text("x")
    window.refresh(check_names=False)                          # F5
    settle_panel(panel)
    assert not find_item(panel.tree, "dily/nc").isExpanded()   # zůstala sbalená
    assert find_item(panel.tree, "dily").isExpanded()
    assert find_item(panel.tree, "nova").isExpanded()          # nová složka rozbalená
    # jiný projekt se zobrazí celý rozbalený
    (tmp_path / "server" / "Druhy" / "dily" / "nc").mkdir(parents=True)
    (tmp_path / "server" / "Druhy" / "dily" / "nc" / "a.nc").write_text("a")
    window.refresh(check_names=False)
    window.table.selectRow([p.name for p in window.projects].index("Druhy"))
    settle_panel(panel)
    assert find_item(panel.tree, "dily/nc").isExpanded()


def test_folders_collapsed_by_default_and_expanded_stay_expanded(window, tmp_path):
    panel = window.panel
    window.settings.tree_expanded = False                      # jako po změně v Nastavení
    panel.tree.default_expanded = False
    (tmp_path / "server" / "Druhy" / "dily" / "nc").mkdir(parents=True)
    (tmp_path / "server" / "Druhy" / "dily" / "nc" / "a.nc").write_text("a")
    window.refresh(check_names=False)
    window.table.selectRow([p.name for p in window.projects].index("Druhy"))
    settle_panel(panel)
    assert not find_item(panel.tree, "dily").isExpanded()      # výchozí stav: sbaleno
    find_item(panel.tree, "dily").setExpanded(True)             # uživatel rozbalí
    (tmp_path / "server" / "Druhy" / "nova").mkdir()
    (tmp_path / "server" / "Druhy" / "nova" / "b.txt").write_text("b")
    window.refresh(check_names=False)                          # F5
    settle_panel(panel)
    assert find_item(panel.tree, "dily").isExpanded()          # zůstala rozbalená
    assert not find_item(panel.tree, "dily/nc").isExpanded()   # vnořená zůstává podle výchozího
    assert not find_item(panel.tree, "nova").isExpanded()      # nová složka podle výchozího stavu


def test_tree_expanded_setting_in_dialog(window):
    from spm.gui.settings_dialog import SettingsDialog
    dlg = SettingsDialog(window.settings)
    assert dlg.tree_expanded.isChecked()
    dlg.tree_expanded.setChecked(False)
    dlg._accept()
    assert window.settings.tree_expanded is False
