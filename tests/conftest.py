import sys

import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def settle_panel(panel) -> None:
    """Počká, až panel stavu projektu dokončí všechna načítání stromu (i opakovaná – výběr
    projektu nebo akce mohou během načítání spustit další) a výsledky se zobrazí."""
    from PySide6.QtWidgets import QApplication
    for _ in range(50):
        if panel._thread is not None:
            panel._thread.wait()
        QApplication.processEvents()
        if (panel._thread is None or not panel._thread.isRunning()) and not panel._again:
            QApplication.processEvents()
            return


@pytest.fixture(autouse=True)
def _name_dialog_via_get_text(monkeypatch):
    """Zadání názvu s našeptáváním (gui.name_dialog.ask_text) jde v testech přes
    QInputDialog.getText – testy si ho nahrazují odpovědí. Samotné okno s našeptáváním
    testuje test_name_dialog.py (přes name_dialog.real_ask_text)."""
    from PySide6.QtWidgets import QInputDialog, QLineEdit

    from spm.gui import name_dialog
    if not hasattr(name_dialog, "real_ask_text"):
        name_dialog.real_ask_text = name_dialog.ask_text
    monkeypatch.setattr(name_dialog, "ask_text", lambda parent, title, label, text="", suggestions=():
                        QInputDialog.getText(parent, title, label, QLineEdit.EchoMode.Normal, text))
    yield
