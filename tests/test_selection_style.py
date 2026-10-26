"""Výraznější označení vybraných řádků: podbarvení barvou zvýraznění a proužek vlevo – strom
projektu (CheckTree), strom záložky Složka a seznam projektů; světlý i tmavý režim."""
import os

import pytest


@pytest.fixture
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def dark_palette():
    from PySide6.QtGui import QColor, QPalette
    pal = QPalette()
    for role, c in ((QPalette.ColorRole.Window, "#202020"), (QPalette.ColorRole.Base, "#1c1c1c"),
                    (QPalette.ColorRole.Text, "#ffffff"), (QPalette.ColorRole.Highlight, "#4cc2ff")):
        pal.setColor(role, QColor(c))
    return pal


_styles = []


def make_tree():
    from PySide6.QtWidgets import QApplication

    from spm.gui.check_tree import CheckTree, TreeEntry
    from spm.gui.style import AppStyle
    tree = CheckTree(["Soubor", "Drží"], checkable=False)
    _styles.append(AppStyle(QApplication.style().name()))           # jako v aplikaci (__main__)
    tree.setStyle(_styles[-1])
    tree.set_entries([TreeEntry("dily/deska.elt", "a", ["já"]), TreeEntry("dily/kolik.elt", "b", [""]),
                      TreeEntry("sestava.elt", "c", [""])])
    tree.resize(400, 200)
    return tree


@pytest.mark.parametrize("dark", [False, True])
def test_selected_row_fill_and_bar(qapp, dark):
    from PySide6.QtGui import QPalette

    from spm.gui import selection_style as sel
    from spm.gui.style import OWN_SELECTION
    tree = make_tree()
    if dark:
        tree.setPalette(dark_palette())
    assert tree.property(OWN_SELECTION) and isinstance(tree.itemDelegate(), sel.SelectionDelegate)
    item = tree._items["a"]
    item.setSelected(True)
    tree.show()
    qapp.processEvents()
    img = tree.viewport().grab().toImage()
    rect = tree.visualItemRect(item)
    other = tree.visualItemRect(tree._items["c"])
    base = tree.palette().color(QPalette.ColorRole.Base)
    accent = tree.palette().color(QPalette.ColorRole.Highlight)
    y = rect.center().y()
    bar = img.pixelColor(1, y)                                       # proužek úplně vlevo
    assert abs(bar.red() - accent.red()) < 40 and abs(bar.blue() - accent.blue()) < 40
    fill = img.pixelColor(rect.right() - 2, y)                      # podbarvení řádku
    assert fill != base and img.pixelColor(rect.right() - 2, other.center().y()) == base
    indent = img.pixelColor(rect.left() - 4, y)                     # i v odsazení stromu
    assert indent == fill
    tree.close()


def test_found_background_kept_under_selection(qapp):
    from PySide6.QtGui import QColor
    tree = make_tree()
    tree.highlight(lambda name: name == "deska.elt", QColor("#fff176"))
    item = tree._items["a"]
    tree.show()
    qapp.processEvents()
    rect = tree.visualItemRect(item)
    yellow = tree.viewport().grab().toImage().pixelColor(rect.right() - 2, rect.center().y())
    assert yellow == QColor("#fff176")                               # nalezený: žlutě
    item.setSelected(True)
    qapp.processEvents()
    both = tree.viewport().grab().toImage().pixelColor(rect.right() - 2, rect.center().y())
    assert both != yellow and both.red() > 150                       # výběr přes žlutou, žlutá zůstala
    tree.close()


def test_installed_in_project_list_and_folder_tab(qapp, tmp_path, monkeypatch):
    from spm import config
    from spm.config import Settings
    from spm.gui import selection_style as sel
    from spm.gui.folder_panel import FolderTree
    from spm.gui.main_window import MainWindow
    monkeypatch.setattr(config, "APP_DIR", tmp_path / "app")
    (tmp_path / "server").mkdir()
    w = MainWindow(Settings(server_root=str(tmp_path / "server"), local_root=str(tmp_path / "l")))
    try:
        for pane in w.panes:                                         # oba panely
            assert isinstance(pane.table.itemDelegate(), sel.SelectionDelegate)
            assert isinstance(pane.panel.tree.itemDelegate(), sel.SelectionDelegate)
        assert isinstance(w.folder_panel.tree, FolderTree)
        assert isinstance(w.folder_panel.tree.itemDelegate(), sel.SelectionDelegate)
    finally:
        w._messages_check.wait()
        w.close()
        qapp.processEvents()


def test_project_list_text_indented_from_bar(qapp):
    from PySide6.QtWidgets import QStyledItemDelegate, QTableWidget, QTableWidgetItem

    from spm.gui import selection_style as sel
    table = QTableWidget(2, 2)
    for r, name in enumerate(("Forma", "Alfa")):
        table.setItem(r, 0, QTableWidgetItem(name))
        table.setItem(r, 1, QTableWidgetItem("x"))
    from PySide6.QtWidgets import QStyleOptionViewItem
    plain = QStyledItemDelegate(table)
    width = plain.sizeHint(QStyleOptionViewItem(), table.model().index(0, 0)).width()
    sel.install(table)
    delegate = table.itemDelegate()
    assert delegate.sizeHint(QStyleOptionViewItem(), table.model().index(0, 0)).width() == width + sel.TEXT_INDENT
    assert delegate.sizeHint(QStyleOptionViewItem(), table.model().index(0, 1)).width() == \
        plain.sizeHint(QStyleOptionViewItem(), table.model().index(0, 1)).width()   # jen první sloupec
    table.deleteLater()
