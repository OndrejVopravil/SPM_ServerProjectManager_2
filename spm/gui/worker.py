"""Spouštění dlouhých operací (kopírování přes síť) mimo hlavní vlákno GUI."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Callable, Iterator

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import QMessageBox, QProgressDialog, QWidget


# okna průběhu běžících úloh (viz progress_paused)
_active_progress: list[QProgressDialog] = []


@contextmanager
def progress_paused() -> Iterator[None]:
    """Dialog, na jehož odpověď čeká úloha na pozadí (např. ASK ve skriptu): okna průběhu se
    po dobu dialogu skryjí – jinak by se samo zobrazilo (po minimumDuration) a stálo by nad
    dialogem, ač se nic neděje. Potom se znovu ukáže až po minimumDuration."""
    paused = list(_active_progress)
    for dlg in paused:
        dlg.reset()  # zastaví časovač, který okno samo zobrazí
        dlg.hide()
    try:
        yield
    finally:
        for dlg in paused:
            if dlg in _active_progress:
                dlg.setValue(dlg.minimum())  # znovu spustí časovač zobrazení


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
    status_text: str | None = None,
    **kwargs,
) -> Task:
    """status_text: krátký text stavu pro okno, které má metodu task_status(text, hotovo,
    celkem) – stavový text v řádku záložek (text None = úloha skončila)."""
    notify = getattr(parent, "task_status", None)
    status = status_text or title
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
        if notify:
            notify(status, done, total)

    def finish() -> None:
        if dlg in _active_progress:
            _active_progress.remove(dlg)
        dlg.close()
        parent._running_task = None
        if notify:
            notify(None, 0, 0)
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
    _active_progress.append(dlg)
    if notify:
        notify(status, 0, 0)
    task.start()
    return task
