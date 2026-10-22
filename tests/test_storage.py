"""Místo zabrané zálohami a košem a jejich úklid (náhled, pak trvalé smazání)."""
import os
import stat
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from spm import storage
from spm.config import Settings
from spm.db import ProjectDB
from spm.sync import ProjectManager


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.chmod(stat.S_IWRITE | stat.S_IREAD)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def env(tmp_path):
    proj = tmp_path / "server" / "P"
    write(proj / "deska.elt", "d0")
    write(proj / "kolik.elt", "k0")
    pm = ProjectManager(Settings(server_root=str(tmp_path / "server"), local_root=str(tmp_path / "l"),
                                 backup_count=5), user="alice", pc="PC")
    for i in range(1, 5):                                    # 4 zálohy desky
        pm.activate("P")
        write(pm.local_dir("P") / "deska.elt", f"d{i}" * 100)
        if i == 4:
            (pm.local_dir("P") / "kolik.elt").unlink()        # kolík do koše
        plan = pm.plan_upload("P")
        pm.deactivate(plan, plan.default_assignments(), f"v{i}")
    write(proj / ".spm" / "backup" / "sirotek.elt.backup01", "x" * 50)   # neevidovaná záloha
    write(proj / ".spm" / "tmp" / "abc", "t")
    return pm, proj


def test_storage_info(env):
    pm, proj = env
    info = storage.project_storage(proj)
    assert info.backup_files == 5 and info.trash_files == 1 and info.tmp_bytes == 1
    assert info.total == info.backup_bytes + info.trash_bytes + 1
    listing = storage.server_storage(proj.parent)
    assert [s.project for s in listing] == ["P"]


def test_cleanup_preview_and_run(env):
    pm, proj = env
    plan = storage.plan_cleanup(proj, keep_backups=2, trash_days=None, orphans=False)
    assert len(plan.remove) == 2 and all("starší záloha" in r for _, _, r in plan.remove)
    assert storage.project_storage(proj).backup_files == 5          # náhled nic nesmazal
    done = storage.run_cleanup(proj, plan, "alice", "PC")
    assert len(done.remove) == 2 and done.freed == plan.freed
    left = sorted(p.name for p in (proj / ".spm" / "backup").iterdir())
    assert left == ["deska.elt.backup03", "deska.elt.backup04", "sirotek.elt.backup01"]
    # neevidované a dočasné
    plan = storage.plan_cleanup(proj, keep_backups=None, trash_days=None, orphans=True)
    assert sorted(r for _, _, r in plan.remove) == ["dočasný soubor", "neevidovaná záloha (přerušené odevzdání)"]
    storage.run_cleanup(proj, plan, "alice", "PC")
    assert not (proj / ".spm" / "tmp").exists()
    # obnova: nejnovější stavy jdou dál obnovit celé
    catalog = pm.restore_points("P")
    assert catalog.points[0].complete


def test_trash_by_age(env):
    pm, proj = env
    assert storage.plan_cleanup(proj, None, trash_days=30, orphans=False).remove == []   # čerstvě smazaný
    with ProjectDB.for_project(proj) as db:                          # smazání „před 40 dny“
        with db.conn:
            old = (datetime.now() - timedelta(days=40)).isoformat(timespec="seconds")
            db.conn.execute("UPDATE commits SET created_at = ?", (old,))
    plan = storage.plan_cleanup(proj, None, trash_days=30, orphans=False)
    assert [Path(p).name for p, _, _ in plan.remove] == ["kolik.elt"] and "v koši od" in plan.remove[0][2]
    storage.run_cleanup(proj, plan, "alice", "PC")
    assert storage.project_storage(proj).trash_files == 0
    assert (proj / "deska.elt").exists()                              # projekt sám beze změny


def test_storage_dialog(env, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QMessageBox

    from spm.gui.storage_dialog import StorageDialog
    QApplication.instance() or QApplication([])
    pm, proj = env
    infos = storage.server_storage(proj.parent)
    dlg = StorageDialog(proj.parent, infos, "alice", "PC", backup_count=2)
    assert dlg.selected_projects() == ["P"] and dlg.table.item(0, 1).text() == "5"
    assert "Celkem:" in dlg.total.text()
    shown = []
    monkeypatch.setattr(QMessageBox, "exec", lambda box: shown.append(box.detailedText()) or QMessageBox.StandardButton.Yes)
    dlg.trash_check.setChecked(False)
    removed = dlg.clean_selected()
    assert removed == 4                                              # 2 staré zálohy + sirotek + tmp
    assert "starší záloha" in shown[0] and "neevidovaná" in shown[0]
    assert dlg.table.item(0, 1).text() == "2"                        # tabulka se obnovila
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append("nic"))
    assert dlg.clean_selected() == 0 and shown[-1] == "nic"          # podruhé není co uklidit


def test_cleanup_only_for_archivers(env, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QMessageBox

    from spm import archive
    from spm.config import Settings
    from spm.gui.storage_dialog import StorageDialog
    QApplication.instance() or QApplication([])
    pm, proj = env
    assert archive.cleanup_problem(Settings(archivers=["alice"]), "Alice") is None
    assert "smí jen: alice" in archive.cleanup_problem(Settings(archivers=["alice"]), "bob")
    problem = archive.cleanup_problem(Settings(), "alice")
    assert "nesmí nikdo" in problem                                  # prázdný seznam = nikdo
    before = sorted(p for p in proj.rglob("*") if p.is_file())
    dlg = StorageDialog(proj.parent, storage.server_storage(proj.parent), "alice", "PC", 2,
                        clean_problem=problem)
    assert not dlg.btn_clean.isEnabled() and dlg.btn_clean.toolTip() == problem
    shown = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a[2]))
    assert dlg.clean_selected(confirm=False) == 0 and shown == [problem]
    assert sorted(p for p in proj.rglob("*") if p.is_file()) == before   # nic se nesmazalo
