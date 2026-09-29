import os

import pytest

from spm import config
from spm.config import Settings


@pytest.fixture
def window(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from spm.gui.main_window import MainWindow

    monkeypatch.setattr(config, "APP_DIR", tmp_path / "app")
    for name in ("Hala Brno", "Škola Ústí", "Sklad Ostrava", "BRNO – garáž"):
        (tmp_path / "server" / name).mkdir(parents=True)
    app = QApplication.instance() or QApplication([])
    w = MainWindow(Settings(server_root=str(tmp_path / "server"), local_root=str(tmp_path / "local")))
    w.refresh()
    yield w
    w.close()
    app.processEvents()


def visible(w):
    return [w.projects[r].name for r in range(w.table.rowCount()) if not w.table.isRowHidden(r)]


def test_contains_case_insensitive(window):
    window.search.setText("brno")
    assert visible(window) == ["BRNO – garáž", "Hala Brno"]


def test_ignores_diacritics(window):
    window.search.setText("usti")
    assert visible(window) == ["Škola Ústí"]
    window.search.setText("GARAZ")
    assert visible(window) == ["BRNO – garáž"]


def test_clear_shows_all_and_hidden_selection_dropped(window):
    window.table.selectRow(visible(window).index("Sklad Ostrava"))
    window.search.setText("brno")
    assert window.selected() is None
    window.search.clear()
    assert len(visible(window)) == 4


def test_filter_survives_refresh(window):
    window.search.setText("sklad")
    window.refresh()
    assert visible(window) == ["Sklad Ostrava"]
