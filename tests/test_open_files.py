"""Kontrola souborů otevřených v jiném programu před odevzdáním (Restart Manager Windows) –
soubor drží jiný proces, jako CAD s otevřeným dílem."""
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from spm import config, file_locks
from spm.config import Settings
from spm.sync import ProjectManager

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Restart Manager Windows")

HOLDER = r"""
import sys, time
f = open(sys.argv[1], "r+b")   # drží soubor otevřený (Python povoluje sdílení – i to se musí najít)
print("ready", flush=True)
sys.stdin.readline()            # čeká na ukončení
"""


class HeldOpen:
    """Jiný proces, který má soubor otevřený, dokud se nezavolá close()."""

    def __init__(self, path: Path):
        self.proc = subprocess.Popen([sys.executable, "-c", HOLDER, str(path)], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, text=True)
        assert self.proc.stdout.readline().strip() == "ready"

    def close(self):
        if self.proc.poll() is None:
            self.proc.stdin.write("\n")
            self.proc.stdin.flush()
            self.proc.wait(10)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.chmod(stat.S_IWRITE | stat.S_IREAD)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def pm(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DIR", tmp_path / "app")
    write(tmp_path / "server" / "P" / "deska.elt", "d1")
    write(tmp_path / "server" / "P" / "sestava.elt", "s1")
    pm = ProjectManager(Settings(server_root=str(tmp_path / "server"), local_root=str(tmp_path / "l")),
                        user="alice", pc="PC")
    pm.activate("P")
    return pm


def test_restart_manager_names_the_program(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("a")
    assert file_locks.open_by([f]) == {}
    held = HeldOpen(f)
    try:
        found = file_locks.open_by([f, tmp_path / "neexistuje.txt"])
        assert list(found) == [f]
        assert any("python" in app.lower() for app in found[f])          # název programu / exe
    finally:
        held.close()
    assert file_locks.open_by([f]) == {}
    with open(f, "rb"):                                                  # vlastní proces SPM se nepočítá
        assert file_locks.open_by([f]) == {}


def test_detect_files_before_submit(pm):
    local = pm.local_dir("P")
    write(local / "deska.elt", "d2")
    plan = pm.plan_upload("P")
    held = HeldOpen(local / "deska.elt")
    try:
        busy = pm.files_open_elsewhere(plan)
        assert list(busy) == ["deska.elt"] and busy["deska.elt"]
    finally:
        held.close()
    assert pm.files_open_elsewhere(plan) == {}
    held = HeldOpen(local / "sestava.elt")                             # nezměněný, jen pro čtení
    try:
        assert pm.files_open_elsewhere(plan) == {}
        assert list(pm.files_open_elsewhere(plan, end_session=True)) == ["sestava.elt"]   # deaktivace
    finally:
        held.close()


def test_warning_dialog(pm, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QMessageBox

    from spm.gui.main_window import MainWindow
    app = QApplication.instance() or QApplication([])
    local = pm.local_dir("P")
    write(local / "deska.elt", "d2")
    w = MainWindow(pm.settings)
    w.pm = w.panel.pm = pm
    state = {"answers": [], "texts": [], "on_show": None}

    def exec_(box):
        state["texts"].append(box.text())
        box._choice = state["answers"].pop(0)
        if state["on_show"]:
            state["on_show"]()
        return 0
    monkeypatch.setattr(QMessageBox, "exec", exec_)
    monkeypatch.setattr(QMessageBox, "clickedButton",
                        lambda box: next(b for b in box.buttons() if b.text() == box._choice))
    held = HeldOpen(local / "deska.elt")
    try:
        plan = pm.plan_upload("P")
        state["answers"] = ["Zkusit znovu", "Zrušit"]                 # pořád otevřený -> zrušit
        assert w._check_open_files(plan, False) == "cancel"
        assert "deska.elt – otevřeno v programu" in state["texts"][0]
        state["answers"] = ["Zkusit znovu"]
        state["on_show"] = held.close                                 # uživatel zavřel program
        assert w._check_open_files(plan, False) == "replan"           # porovnat znovu (mohl se uložit)
        held = HeldOpen(local / "deska.elt")
        state["answers"], state["on_show"] = ["Přesto pokračovat"], None
        assert w._check_open_files(plan, False) == "ok"               # na vlastní riziko
        held.close()
        assert w._check_open_files(plan, False) == "ok"               # nic otevřeného – bez dotazu
    finally:
        held.close()
        w.close()
        app.processEvents()
