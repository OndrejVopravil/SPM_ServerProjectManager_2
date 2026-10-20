import os

import pytest
from conftest import settle_panel

from spm import config
from spm.config import Settings
from spm.gui.main_window import COL_STATE


@pytest.fixture
def window(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from spm.gui.main_window import MainWindow

    monkeypatch.setattr(config, "APP_DIR", tmp_path / "app")
    for name, files in {"Dobry": ["a.txt"], "Spatny": ["ok.txt", "Výkresy/a.dwg"]}.items():
        for f in files:
            path = tmp_path / "server" / name / f
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x")
    (tmp_path / "local" / "NovyLokalni").mkdir(parents=True)
    (tmp_path / "local" / "NovyLokalni" / "moje poznamky.txt").write_text("x")

    app = QApplication.instance() or QApplication([])
    w = MainWindow(Settings(server_root=str(tmp_path / "server"), local_root=str(tmp_path / "local")))
    w.refresh()
    w._name_check.wait()
    app.processEvents()  # doručí výsledek kontroly z vlákna
    yield w
    w.close()
    app.processEvents()


def select(w, name):
    w.table.selectRow([p.name for p in w.projects].index(name))
    return [p.name for p in w.projects].index(name)


def test_invalid_project_marked_and_not_activatable(window):
    row = select(window, "Spatny")
    assert window.table.item(row, COL_STATE).text() == "Nepovolené znaky"
    assert not window.act_activate.isEnabled()
    assert window.act_invalid.isEnabled()
    assert "Výkresy/" in window.table.item(row, 0).toolTip()


def test_valid_project_activatable(window):
    select(window, "Dobry")
    assert window.act_activate.isEnabled() and not window.act_invalid.isEnabled()


def test_local_only_with_invalid_names_publish_allowed_without_them(window):
    """Složku s vadnými názvy lze publikovat jen bez nich – dialog je nedovolí vybrat
    a jádro publikování je odmítne (viz test_publish)."""
    row = select(window, "NovyLokalni")
    assert window.table.item(row, COL_STATE).text() == "Nepovolené znaky"
    assert window.act_publish.isEnabled()
    assert window.pm.invalid_file_paths(window.pm.local_files("NovyLokalni")) == ["moje poznamky.txt"]


def test_tree_shown_for_any_project(window):
    """Strom se vykreslí i u volného projektu (i s nepovolenými znaky); bez akcí, které
    vyžadují aktivaci."""
    from PySide6.QtWidgets import QApplication
    select(window, "Spatny")
    assert not window.panel.isHidden()
    settle_panel(window.panel)
    QApplication.processEvents()
    assert sorted(e.path for e in window.panel.entries) == ["Výkresy/a.dwg", "ok.txt"]
    actions = window.panel.context_actions("ok.txt")
    assert not any(actions[k] for k in ("submit", "take", "delete", "rename", "publish"))


def test_create_project_from_context_menu(window, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QInputDialog
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("Projekt_X", True))
    window.create_project()
    assert (tmp_path / "local" / "Projekt_X").is_dir()
    p = window.selected()
    assert p is not None and p.name == "Projekt_X" and p.local_only
    assert window.act_rename.isEnabled()                         # neodevzdaný – lze přejmenovat
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("Projekt_Y", True))
    window.rename_project()
    assert (tmp_path / "local" / "Projekt_Y").is_dir() and not (tmp_path / "local" / "Projekt_X").exists()
    p = window.selected()
    assert p is not None and p.name == "Projekt_Y" and p.local_only


def test_status_text_in_tab_row(window):
    label = window.status_label
    assert label.text().startswith("Aktualizováno")     # po načtení seznamu a kontrole názvů
    window.task_status("Přenáším data…", 0, 0)
    assert label.text() == "Přenáším data…"
    window.task_status("Přenáším data…", 3, 10)
    assert label.text() == "Přenáším data… 3/10"
    window.set_busy("refresh", "Aktualizuji…")
    assert label.text() == "Přenáším data… 3/10"       # akce má přednost
    window.task_status(None, 0, 0)
    assert label.text() == "Aktualizuji…"
    window.set_busy("refresh", None)
    assert label.text().startswith("Aktualizováno")
    window._refreshed("cesta nenalezena")
    assert label.text() == "Síť nedostupná" and label.toolTip() == "cesta nenalezena"
