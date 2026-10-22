"""Oblíbené (★, Ctrl+D) a naposledy otevřené projekty – filtr seznamu, pamatuje se po záložkách."""
import os
from pathlib import Path

import pytest

from spm import config
from spm.config import Settings


@pytest.fixture
def window(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from spm.gui.main_window import MainWindow
    monkeypatch.setattr(config, "APP_DIR", tmp_path / "app")
    for name in ("Alfa", "Beta", "Gama", "Delta"):
        (tmp_path / "server" / name).mkdir(parents=True)
        (tmp_path / "server" / name / "a.txt").write_text("a")
    app = QApplication.instance() or QApplication([])
    w = MainWindow(Settings(server_root=str(tmp_path / "server"), local_root=str(tmp_path / "local")))
    w.refresh(check_names=False)
    yield w
    w.close()
    app.processEvents()


def visible(w) -> list[str]:
    return [p.name for r, p in enumerate(w.projects) if not w.table.isRowHidden(r)]


def select(w, name):
    w.table.selectRow([p.name for p in w.projects].index(name))


def test_favorites_star_and_filter(window):
    from spm.gui.main_window import COL_NAME, FILTER_FAVORITES
    w = window
    select(w, "Gama")
    assert w.act_favorite.text().startswith("Přidat")
    w.toggle_favorite()                                             # Ctrl+D
    row = [p.name for p in w.projects].index("Gama")
    assert w.table.item(row, COL_NAME).text() == "★ Gama"
    assert w.act_favorite.text().startswith("Odebrat")
    w.toggle_favorite("Alfa")
    w.list_filter.setCurrentIndex(FILTER_FAVORITES)
    assert visible(w) == ["Alfa", "Gama"]
    w.search.setText("gam")                                         # filtr + hledání
    assert visible(w) == ["Gama"]
    w.search.clear()
    w.toggle_favorite("Gama")                                       # odebrat
    assert visible(w) == ["Alfa"]
    w.refresh(check_names=False)                                    # zůstane i po obnovení
    assert visible(w) == ["Alfa"] and w.favorites() == {"Alfa"}


def test_recent_projects(window):
    from spm.gui.main_window import FILTER_ALL, FILTER_RECENT
    w = window
    for name in ("Delta", "Beta", "Delta"):
        select(w, name)
    assert w.recent() == ["Delta", "Beta"]                          # nejnovější první, bez duplicit
    w.list_filter.setCurrentIndex(FILTER_RECENT)
    assert visible(w) == ["Beta", "Delta"]
    w.list_filter.setCurrentIndex(FILTER_ALL)
    assert len(visible(w)) == 4


def test_rename_keeps_favorite(window, monkeypatch):
    from PySide6.QtWidgets import QInputDialog, QMessageBox
    w = window
    w.toggle_favorite("Beta")
    select(w, "Beta")
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("Beta2", True))
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    w.rename_project()
    assert w.favorites() == {"Beta2"} and "Beta2" in w.recent()
