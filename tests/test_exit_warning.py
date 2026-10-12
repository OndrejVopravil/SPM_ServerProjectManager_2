"""Upozornění při ukončení aplikace na projekty, které má uživatel stále aktivované."""
import os

import pytest

from spm import config
from spm.config import AppSettings, Settings
from spm.sync import ProjectManager


@pytest.fixture
def window(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from spm.gui.main_window import MainWindow
    monkeypatch.setattr(config, "APP_DIR", tmp_path / "app")
    for srv in ("O", "W"):
        for name in ("058768", "058770"):
            (tmp_path / srv / name).mkdir(parents=True)
            (tmp_path / srv / name / "a.txt").write_text("a")
    app = AppSettings.from_settings(Settings(server_root=str(tmp_path / "O"), local_root=str(tmp_path / "lO")),
                                    name="O")
    w_id = app.add_workspace("W")
    v = app.view(w_id)
    v.server_root, v.local_root = str(tmp_path / "W"), str(tmp_path / "lW")
    qapp = QApplication.instance() or QApplication([])
    win = MainWindow(app)
    win.refresh(check_names=False)
    win.show()
    yield win
    win._closing = True  # úklid bez dotazu
    win.close()
    qapp.processEvents()


def answer(monkeypatch, choice: str, dont_ask: bool = False) -> list[str]:
    """Na upozornění se „odpoví“ tlačítkem choice; vrací zobrazené texty."""
    from PySide6.QtWidgets import QMessageBox
    shown = []

    def fake_exec(box):
        shown.append(box.text())
        if box.checkBox() is not None:
            box.checkBox().setChecked(dont_ask)
        return 0
    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    monkeypatch.setattr(QMessageBox, "clickedButton",
                        lambda box: next(b for b in box.buttons() if b.text() == choice))
    return shown


def test_no_active_projects_closes_without_question(window, monkeypatch):
    shown = answer(monkeypatch, "Ukončit")
    assert window.close() and shown == []


def test_active_projects_listed_per_workspace_and_back_keeps_window(window, monkeypatch):
    window.pm.activate("058768")
    window.refresh(check_names=False)
    window.tabs.setCurrentIndex(1)                       # druhá záložka: aktivovat i tam
    window.pm.activate("058770")
    window.refresh(check_names=False)
    shown = answer(monkeypatch, "Zpět")
    assert not window.close() and window.isVisible()
    assert "O: 058768" in shown[0] and "W: 058770" in shown[0]


def test_quit_closes_and_dont_ask_is_remembered(window, monkeypatch):
    window.pm.activate("058768")
    window.refresh(check_names=False)
    answer(monkeypatch, "Ukončit", dont_ask=True)
    assert window.close()
    assert AppSettings.load().view(window.settings.workspace_id).warn_active_on_exit is False


def test_warning_switched_off_in_settings(window, monkeypatch):
    window.pm.activate("058768")
    window.refresh(check_names=False)
    window.settings.warn_active_on_exit = False
    shown = answer(monkeypatch, "Zpět")
    assert window.close() and shown == []
