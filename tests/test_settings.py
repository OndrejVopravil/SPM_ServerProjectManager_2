import json
import os

import pytest

from spm import config
from spm.config import Settings, legacy_settings_path, settings_path


@pytest.fixture(autouse=True)
def appdata(tmp_path, monkeypatch):
    # nikdy nepřepisovat skutečné nastavení uživatele
    monkeypatch.setattr(config, "APP_DIR", tmp_path / "app")
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    return tmp_path


def test_saved_in_app_dir(tmp_path):
    Settings(server_root="X").save()
    assert settings_path() == tmp_path / "app" / "settings.json"
    assert settings_path().is_file()


def test_migrates_from_appdata():
    legacy = legacy_settings_path()
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"server_root": "O:\\S", "local_root": "D:\\L"}), encoding="utf-8")
    s = Settings.load()
    assert (s.server_root, s.local_root) == ("O:\\S", "D:\\L")
    assert Settings.load(settings_path()).server_root == "O:\\S"  # převzato do složky aplikace


def test_roundtrip():
    s = Settings(server_root=r"\\srv\projekty", local_root=r"C:\P", backup_count=5,
                 backup_enabled=False, ui={"last_project": "A"})
    s.save()
    loaded = Settings.load()
    assert loaded == s
    assert settings_path().is_file()


def test_missing_or_broken_file_gives_defaults():
    assert Settings.load() == Settings()
    settings_path().parent.mkdir(parents=True)
    settings_path().write_text("{nesmysl", encoding="utf-8")
    assert Settings.load() == Settings()


def test_unknown_keys_ignored_and_missing_keys_defaulted():
    settings_path().parent.mkdir(parents=True)
    settings_path().write_text('{"server_root": "X", "stary_klic": 1}', encoding="utf-8")
    s = Settings.load()
    assert s.server_root == "X" and s.backup_count == 3 and s.ui == {}


def test_new_default_ignore_added_once_to_old_settings():
    settings_path().parent.mkdir(parents=True)
    # nastavení ze starší verze: bez *.lck$ a bez known_default_ignore, s vlastním vzorem
    settings_path().write_text(json.dumps({"server_root": "X", "ignore_patterns": ["~$*", "*.bak"]}),
                               encoding="utf-8")
    s = Settings.load()
    assert "*.lck$" in s.ignore_patterns and "*.bak" in s.ignore_patterns
    assert "*.lck$" in json.loads(settings_path().read_text(encoding="utf-8"))["ignore_patterns"]

    # uživatel vzor vědomě odebere -> už se nevrací
    s.ignore_patterns.remove("*.lck$")
    s.save()
    assert "*.lck$" not in Settings.load().ignore_patterns


def test_main_window_remembers_state(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from spm.gui.main_window import MainWindow

    for name in ("Alfa", "Beta", "Gama"):
        (tmp_path / "server" / name).mkdir(parents=True)
    app = QApplication.instance() or QApplication([])
    Settings(server_root=str(tmp_path / "server"), local_root=str(tmp_path / "local")).save()

    w = MainWindow(Settings.load())
    w.refresh()
    w.resize(700, 400)
    w.table.setColumnWidth(1, 222)
    w.table.selectRow(1)
    w.close()

    w2 = MainWindow(Settings.load())
    w2.refresh()
    assert w2.size().width() == 700
    assert w2.table.columnWidth(1) == 222
    assert w2.selected().name == "Beta"
    w2.close()
    app.processEvents()
