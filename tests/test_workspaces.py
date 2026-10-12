"""Záložky (pracovní prostory): vlastní síťová a místní složka a společná nastavení každé záložky."""
import json
import os

import pytest

from spm import config
from spm import shared_config as sc
from spm.config import AppSettings, Settings, settings_path


@pytest.fixture(autouse=True)
def app_dir(tmp_path, monkeypatch):
    # nikdy nepřepisovat skutečné nastavení uživatele
    monkeypatch.setattr(config, "APP_DIR", tmp_path / "app")
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))


def make_app(tmp_path) -> AppSettings:
    for srv in ("O", "W"):
        for name in ("058768", f"Jen{srv}"):
            (tmp_path / srv / name).mkdir(parents=True)
    app = AppSettings.from_settings(Settings(server_root=str(tmp_path / "O"), local_root=str(tmp_path / "lO")),
                                    name="Nástrojárna")
    w = app.add_workspace("Výroba")
    v = app.view(w)
    v.server_root, v.local_root = str(tmp_path / "W"), str(tmp_path / "lW")
    v.save()
    return app


def test_old_settings_file_becomes_one_workspace(tmp_path):
    settings_path().parent.mkdir(parents=True)
    settings_path().write_text(json.dumps({"server_root": str(tmp_path / "PROJEKTY"), "local_root": "C:\\L",
                                           "backup_count": 7, "ui": {"x": "1"}}), encoding="utf-8")
    app = AppSettings.load()
    assert [w["name"] for w in app.workspaces] == ["PROJEKTY"]
    s = app.view(app.active)
    assert (s.server_root, s.backup_count, s.ui) == (str(tmp_path / "PROJEKTY"), 7, {"x": "1"})
    data = json.loads(settings_path().read_text(encoding="utf-8"))      # uloženo už v novém formátu
    assert data["workspaces"][0]["backup_count"] == 7 and "server_root" not in data


def test_no_settings_file_gives_one_empty_workspace():
    app = AppSettings.load()
    assert len(app.workspaces) == 1 and not app.view(app.active).is_complete()
    assert not settings_path().exists()


def test_roundtrip_and_separate_values(tmp_path):
    app = make_app(tmp_path)
    o, w = (ws["id"] for ws in app.workspaces)
    app.view(o).backup_count = 5
    app.view(w).backup_count = 9
    app.view(w).rename_threshold = 0.8          # osobní -> platí pro všechny záložky
    app.view(w).save()
    assert app.view(o).rename_threshold == 0.8
    app.active = w
    app.save()

    loaded = AppSettings.load()
    assert loaded.active == w
    assert [loaded.view(x).backup_count for x in (o, w)] == [5, 9]
    assert [loaded.view(x).server_root for x in (o, w)] == [str(tmp_path / "O"), str(tmp_path / "W")]
    assert loaded.view(o).rename_threshold == 0.8
    assert loaded.view(o).ui is loaded.view(w).ui                    # stav rozhraní je společný


def test_network_config_applies_only_to_its_workspace(tmp_path):
    """spm_config.json na O: nezmění nastavení záložky W: (každá si převzaté hodnoty ukládá zvlášť)."""
    app = make_app(tmp_path)
    o, w = (ws["id"] for ws in app.workspaces)
    (tmp_path / "O" / sc.CONFIG_NAME).write_text(json.dumps({"backup_count": 12}), encoding="utf-8")
    assert sc.SharedConfig().sync(app.view(o))
    assert app.view(o).backup_count == 12 and app.view(w).backup_count == 3
    loaded = AppSettings.load()
    assert [loaded.view(x).backup_count for x in (o, w)] == [12, 3]


def test_folder_conflicts(tmp_path):
    app = make_app(tmp_path)
    o, w = (ws["id"] for ws in app.workspaces)
    assert app.folder_conflict(w, str(tmp_path / "W"), str(tmp_path / "lW")) is None
    assert "Nástrojárna" in app.folder_conflict(w, str(tmp_path / "O"), str(tmp_path / "lW"))
    assert "místní" in app.folder_conflict(w, str(tmp_path / "W"), str(tmp_path / "lO")).lower()
    assert app.folder_conflict(w, str(tmp_path / "W"), str(tmp_path / "lO" / "pod"))      # vnořená
    assert app.folder_conflict(w, str(tmp_path / "W"), str(tmp_path))                      # nadřazená


def test_remove_workspace(tmp_path):
    app = make_app(tmp_path)
    o, w = (ws["id"] for ws in app.workspaces)
    app.base.ui[f"last_project.{w}"] = "058768"
    app.active = w
    app.remove_workspace(w)
    assert [x["id"] for x in app.workspaces] == [o] and app.active == o
    assert f"last_project.{w}" not in app.base.ui
    with pytest.raises(ValueError):
        app.remove_workspace(o)


def test_new_default_ignore_added_to_each_workspace(tmp_path):
    settings_path().parent.mkdir(parents=True)
    ws = {"id": "a1", "name": "A", "ignore_patterns": ["*.bak"], "known_default_ignore": ["*.bak"]}
    settings_path().write_text(json.dumps({"workspaces": [ws]}), encoding="utf-8")
    view = AppSettings.load().view("a1")
    assert "*.bak" in view.ignore_patterns and "*.lck$" in view.ignore_patterns


# --- GUI -------------------------------------------------------------------------------

@pytest.fixture
def window(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from spm.gui.main_window import MainWindow
    qapp = QApplication.instance() or QApplication([])
    app = make_app(tmp_path)
    w = MainWindow(app)
    w.refresh(check_names=False)
    yield w
    if w._name_check:
        w._name_check.wait()
    w.close()
    qapp.processEvents()


def names(w):
    return sorted(p.name for p in w.projects)


def test_tabs_switch_projects(window):
    w = window
    assert [w.tabs.tabText(i) for i in range(w.tabs.count())] == ["Nástrojárna", "Výroba"]
    assert names(w) == ["058768", "JenO"]
    w.table.selectRow([p.name for p in w.projects].index("JenO"))
    w.tabs.setCurrentIndex(1)
    assert names(w) == ["058768", "JenW"]
    assert w.pm.server_root.name == "W" and w.panel.pm is w.pm
    assert w.app_settings.active == w.settings.workspace_id
    w.tabs.setCurrentIndex(0)                       # poslední vybraný projekt si záložka pamatuje
    assert w.selected().name == "JenO"


def test_tab_shows_count_of_my_activated_projects(window):
    w = window
    w.pm.activate("058768")
    w.refresh(check_names=False)
    assert w.tabs.tabText(0) == "Nástrojárna (1)"
    w.tabs.setCurrentIndex(1)
    assert w.tabs.tabText(0) == "Nástrojárna (1)" and w.tabs.tabText(1) == "Výroba"


def test_remove_tab_refused_with_activated_projects(window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    w = window
    w.pm.activate("058768")
    w.refresh(check_names=False)
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))
    w.remove_workspace(w.settings.workspace_id)
    assert warned and len(w.app_settings.workspaces) == 2


def test_add_tab_cancelled_settings_removes_it(window, monkeypatch):
    from PySide6.QtWidgets import QInputDialog
    w = window
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("Třetí", True))
    monkeypatch.setattr(w, "open_settings", lambda: False)
    before = w.settings.workspace_id
    w.add_workspace()
    assert len(w.app_settings.workspaces) == 2 and w.settings.workspace_id == before
    assert w.tabs.count() == 2


def test_add_tab_suggests_local_folder(window, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QInputDialog
    w = window
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("Treti", True))
    seen = {}

    def fake_settings():
        seen["local"] = w.settings.local_root
        w.settings.server_root = str(tmp_path / "O")   # jen aby byla záložka úplná
        return True
    monkeypatch.setattr(w, "open_settings", fake_settings)
    w.add_workspace()
    assert seen["local"] == str(tmp_path / "Treti") and w.tabs.count() == 3
    assert w.tabs.tabText(w.tabs.currentIndex()) == "Treti"
