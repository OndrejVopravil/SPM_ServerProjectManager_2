import sys
import traceback

from PySide6.QtWidgets import QApplication, QMessageBox

from .config import Settings
from .gui.main_window import MainWindow
from .gui.style import AppStyle


def _show_unhandled(exc_type, exc, tb) -> None:
    """Při spuštění přes pythonw není konzole – neošetřenou chybu ukážeme v okně."""
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    sys.__stderr__ and sys.__stderr__.write(text)
    box = QMessageBox(QMessageBox.Icon.Critical, "SPM – neočekávaná chyba", str(exc) or exc_type.__name__)
    box.setDetailedText(text)
    box.exec()


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("SPM")
    app.setStyle(AppStyle(app.style().name()))
    sys.excepthook = _show_unhandled
    window = MainWindow(Settings.load())
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
