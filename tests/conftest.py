import sys
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
