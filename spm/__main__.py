import sys
import traceback

from PySide6.QtWidgets import QApplication, QMessageBox

from .config import AppSettings
from .gui.app_icon import app_icon
from .gui.main_window import MainWindow
from .gui.style import AppStyle


def _show_unhandled(exc_type, exc, tb) -> None:
    """Při spuštění přes pythonw není konzole – neošetřenou chybu ukážeme v okně."""
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    sys.__stderr__ and sys.__stderr__.write(text)
    box = QMessageBox(QMessageBox.Icon.Critical, "SPM – neočekávaná chyba", str(exc) or exc_type.__name__)
    box.setDetailedText(text)
    box.exec()


def _set_app_id() -> bool:
    """Windows: vlastní identita procesu – hlavní panel pak ukazuje ikonu SPM, ne Pythonu
    (jen při spuštění přes python / pythonw). Sestavené SPM.exe identitu nenastavuje: Windows
    ho pozná podle souboru a ikonu vezme z něj – s vlastní identitou by ikona připnutého .exe
    na hlavním panelu zmizela (Windows by k ní hledal zástupce, který neexistuje).
    Vrací, zda se identita nastavila."""
    if sys.platform == "win32" and not getattr(sys, "frozen", False):
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("SPM.ServerProjectManager")
            return True
        except (AttributeError, OSError):
            pass
    return False


def main() -> int:
    _set_app_id()
    app = QApplication(sys.argv)
    app.setApplicationName("SPM")
    app.setWindowIcon(app_icon())  # všechna okna aplikace
    app.setStyle(AppStyle(app.style().name()))
    sys.excepthook = _show_unhandled
    window = MainWindow(AppSettings.load())
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
