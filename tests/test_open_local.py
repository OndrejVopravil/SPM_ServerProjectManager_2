import os
from pathlib import Path

import pytest

from spm import config
from spm.config import Settings


@pytest.fixture
def window(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from spm.gui import main_window
    from spm.gui.main_window import MainWindow

    monkeypatch.setattr(config, "APP_DIR", tmp_path / "app")
    opened: list[Path] = []
    monkeypatch.setattr(main_window.os, "startfile", lambda p: opened.append(Path(p)), raising=False)
    for name in ("Aktivni", "Serverovy"):
        (tmp_path / "server" / name).mkdir(parents=True)
        (tmp_path / "server" / name / "a.txt").write_text("x")

    app = QApplication.instance() or QApplication([])
    w = MainWindow(Settings(server_root=str(tmp_path / "server"), local_root=str(tmp_path / "local")))
    w.pm.activate("Aktivni")
    w.refresh()
    w.opened = opened
    yield w
    w._name_check and w._name_check.wait()
    w.close()
    app.processEvents()


def select(w, name):
    w.table.selectRow([p.name for p in w.projects].index(name))


def test_no_selection_opens_local_root(window):
    window.table.clearSelection()
    assert window.act_open.isEnabled()
    window.open_local()
    assert window.opened == [window.pm.local_root]


def test_project_without_local_copy_opens_local_root(window):
    select(window, "Serverovy")
    window.open_local()
    assert window.opened == [window.pm.local_root]


def test_activated_project_opens_its_folder(window):
    select(window, "Aktivni")
    window.open_local()
    assert window.opened == [window.pm.local_dir("Aktivni")]


def test_double_click_does_not_fall_back_to_root(window):
    select(window, "Serverovy")
    window.open_local(root_fallback=False)
    assert window.opened == []
