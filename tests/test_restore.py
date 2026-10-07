import os
import time
from pathlib import Path

import pytest

from spm.config import Settings
from spm.restore import RESTORE_DIR_RE, RestoreError
from spm.sync import ProjectManager


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        os.chmod(path, 0o666)
    path.write_text(text, encoding="utf-8")


def files_in(folder: Path) -> dict[str, str]:
    return {p.relative_to(folder).as_posix(): p.read_text(encoding="utf-8")
            for p in folder.rglob("*") if p.is_file() and ".spm" not in p.parts}


@pytest.fixture
def history(tmp_path):
    """Projekt se 4 odevzdáními:
       v1: zprava=1, deska=1        (první aktivace = načtení do evidence)
       v2: zprava=2, nova.txt
       v3: zprava=3, deska smazána, vykres přejmenován
       v4: zprava=4
    """
    proj = tmp_path / "server" / "Forma"
    write(proj / "zprava.txt", "zprava 1")
    write(proj / "dily" / "deska.elt", "deska 1")
    write(proj / "vykres.dwg", "vykres")
    s = Settings(server_root=str(tmp_path / "server"), local_root=str(tmp_path / "local"),
                 backup_count=10, delete_local_after_deactivate=True)
    pm = ProjectManager(s, user="alice", pc="PC1")
    local = pm.local_dir("Forma")

    def session(comment, edit):
        pm.activate("Forma")
        edit(local)
        time.sleep(0.01)
        plan = pm.plan_upload("Forma")
        pm.deactivate(plan, plan.default_assignments(), comment)

    session("v2", lambda l: (write(l / "zprava.txt", "zprava 2"), write(l / "nova.txt", "nova")))

    def v3(l):
        write(l / "zprava.txt", "zprava 3")
        (l / "dily" / "deska.elt").unlink()
        (l / "vykres.dwg").rename(l / "vykres_final.dwg")
    session("v3", v3)
    session("v4", lambda l: write(l / "zprava.txt", "zprava 4"))
    return pm, proj


def point(catalog, comment):
    return next(p for p in catalog.points if p.comment == comment)


def test_all_points_complete_and_restorable(history):
    pm, proj = history
    cat = pm.restore_points("Forma")
    assert [p.comment for p in cat.points][:3] == ["v4", "v3", "v2"]
    assert all(p.complete for p in cat.points)
    assert cat.oldest_complete is cat.points[-1]

    res = pm.restore(cat, point(cat, "v2"))
    assert RESTORE_DIR_RE.search(res.folder.name) and not res.failed
    assert files_in(res.folder) == {"zprava.txt": "zprava 2", "nova.txt": "nova",
                                    "dily/deska.elt": "deska 1", "vykres.dwg": "vykres"}

    res = pm.restore(cat, point(cat, "v3"))
    assert files_in(res.folder) == {"zprava.txt": "zprava 3", "nova.txt": "nova",
                                    "vykres_final.dwg": "vykres"}
    assert res.folder.name.endswith("_2") or res.folder != pm.restore(cat, point(cat, "v3")).folder


def test_restore_folder_listed_as_local_only(history):
    pm, proj = history
    cat = pm.restore_points("Forma")
    res = pm.restore(cat, cat.points[0])
    status = {p.name: p for p in pm.list_projects()}
    assert status[res.folder.name].local_only


def test_pruned_backups_limit_how_far_back(history):
    pm, proj = history
    # smazání nejstarší zálohy zprávy (verze 1) – jako by ji promazalo „posledních N“
    oldest = sorted((proj / ".spm" / "backup").glob("zprava.txt.backup*"))[0]
    assert oldest.read_text(encoding="utf-8") == "zprava 1"
    oldest.unlink()
    cat = pm.restore_points("Forma")
    v1 = cat.points[-1]
    assert not v1.complete and v1.missing == ["zprava.txt"]
    assert point(cat, "v2").complete
    assert cat.oldest_complete is point(cat, "v2")


def test_corrupted_backup_detected_on_restore(history):
    pm, proj = history
    cat = pm.restore_points("Forma")
    bad = sorted((proj / ".spm" / "backup").glob("zprava.txt.backup*"))[1]   # verze 2
    write(bad, "zprava 2 POSKOZENA")  # stejná velikost by šla taky – rozhodne hash
    res = pm.restore(cat, point(cat, "v2"))
    assert res.failed == ["zprava.txt"]
    assert "zprava.txt" not in files_in(res.folder)


def test_project_without_history(tmp_path):
    (tmp_path / "server" / "Prazdny").mkdir(parents=True)
    pm = ProjectManager(Settings(server_root=str(tmp_path / "server"), local_root=str(tmp_path / "l")))
    with pytest.raises(RestoreError):
        pm.restore_points("Prazdny")


def test_restore_dialog(history):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from spm.gui.restore_dialog import RestoreDialog

    app = QApplication.instance() or QApplication([])
    pm, proj = history
    sorted((proj / ".spm" / "backup").glob("zprava.txt.backup*"))[0].unlink()   # v1 nekompletní
    cat = pm.restore_points("Forma")
    dlg = RestoreDialog(cat, "cil")
    ok = dlg.buttons.buttons()[0]
    assert dlg.selected_point() is cat.points[0] and ok.isEnabled()         # nejnovější předvybrán
    last = len(cat.points) - 1
    assert dlg.table.item(last, 3).text().startswith("chybí 1 z")
    dlg.table.selectRow(last)                                                # nekompletní nejde vybrat
    assert dlg.selected_point() is not cat.points[-1]
    assert "zprava.txt" in dlg.table.item(last, 0).toolTip()
    dlg.table.selectRow(1)
    assert dlg.selected_point() is cat.points[1]


def test_restore_refreshes_project_list(history, monkeypatch, tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QMessageBox

    from spm import config
    from spm.gui import main_window
    from spm.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(config, "APP_DIR", tmp_path / "app")
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)          # hlášení nezobrazovat
    pm, proj = history
    cat = pm.restore_points("Forma")

    class FakeDialog:                                                  # výběr nejnovějšího stavu
        def __init__(self, *a, **k): pass
        def selected_point(self): return cat.points[0]
        def deleteLater(self): pass
    monkeypatch.setattr(main_window, "RestoreDialog", FakeDialog)

    def run_now(parent, title, fn, *args, on_done=None, on_error=None, **kwargs):
        res = fn(*args, progress=None, **kwargs)
        if on_done:
            on_done(res)
    monkeypatch.setattr(main_window, "run_task", run_now)

    w = MainWindow(pm.settings)
    w.pm = w.panel.pm = pm
    monkeypatch.setattr(w, "_exec_dialog", lambda dlg, key: True)
    w.refresh(check_names=False)
    assert [p.name for p in w.projects] == ["Forma"]
    w.table.selectRow(0)
    w.restore_project()
    names = [p.name for p in w.projects]
    assert len(names) == 2 and any("_obnova_" in n for n in names)     # seznam se obnovil sám
    w.panel._thread and w.panel._thread.wait()
    w.close()
