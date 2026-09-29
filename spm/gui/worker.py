"""Spouštění dlouhých operací (kopírování přes síť) mimo hlavní vlákno GUI."""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import QMessageBox, QProgressDialog, QWidget


class Task(QThread):
    progress = Signal(int, int, str)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn: Callable[..., Any], *args, **kwargs):
        super().__init__()
        self.fn, self.args, self.kwargs = fn, args, kwargs

    def run(self) -> None:
        try:
            result = self.fn(*self.args, progress=self.progress.emit, **self.kwargs)
        except Exception as e:  # noqa: BLE001 – chyba se zobrazí uživateli
            self.failed.emit(str(e) or type(e).__name__)
        else:
            self.done.emit(result)


def run_task(
    parent: QWidget,
    title: str,
    fn: Callable[..., Any],
    *args,
    on_done: Callable[[Any], None] | None = None,
    on_error: Callable[[str], None] | None = None,
    **kwargs,
) -> Task:
    dlg = QProgressDialog(title, None, 0, 0, parent)
    dlg.setWindowTitle(title)
    dlg.setWindowModality(Qt.WindowModality.WindowModal)
    dlg.setMinimumDuration(300)
    dlg.setMinimumWidth(480)
    dlg.setAutoClose(False)
    dlg.setAutoReset(False)

    task = Task(fn, *args, **kwargs)

    def on_progress(done: int, total: int, text: str) -> None:
        dlg.setMaximum(max(total, 1))
        dlg.setValue(done)
        dlg.setLabelText(f"{title}\n{text}" if text else title)

    def finish() -> None:
        dlg.close()
        parent._running_task = None
        task.wait()
        task.deleteLater()

    def ok(result: Any) -> None:
        finish()
        if on_done:
            on_done(result)

    def fail(message: str) -> None:
        finish()
        QMessageBox.critical(parent, title, message)
        if on_error:
            on_error(message)

    task.progress.connect(on_progress)
    task.done.connect(ok)
    task.failed.connect(fail)
    parent._running_task = task  # udrží referenci, dokud vlákno běží
    task.start()
    return task
