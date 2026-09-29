import os
import time
from pathlib import Path

import pytest

from spm import db as dbm
from spm.config import Settings
from spm.db import ProjectDB
from spm.lock import LockError, read_lock
from spm.sync import ProjectManager, SyncError


def write(path: Path, text: str, age: float = 0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if age:
        t = time.time() - age
        os.utime(path, (t, t))


@pytest.fixture
def env(tmp_path):
    server = tmp_path / "server"
    local = tmp_path / "local"
    proj = server / "ProjektA"
    write(proj / "vykresy" / "pudorys.dwg", "pudorys " * 100, age=100)
    write(proj / "zprava.docx", "zprava verze 1 " * 50, age=100)
    write(proj / "foto.jpg", "jpg data", age=100)
    settings = Settings(server_root=str(server), local_root=str(local), backup_count=2)
    alice = ProjectManager(settings, user="alice", pc="PC1")
    bob = ProjectManager(settings, user="bob", pc="PC2")
    return settings, alice, bob, proj, local / "ProjektA"


def run_deactivate(pm, name, comment="hotovo", assignments=None):
    plan = pm.plan_deactivation(name)
    a = plan.default_assignments()
    if assignments:
        a.update(assignments)
    return plan, pm.deactivate(plan, a, comment)


def test_activate_copies_and_locks(env):
    _, alice, bob, proj, local = env
    alice.activate("ProjektA")
    assert (local / "vykresy" / "pudorys.dwg").exists()
    assert not (local / ".spm").exists()

    status = {p.name: p for p in bob.list_projects()}["ProjektA"]
    assert status.active and status.lock.user == "alice" and not status.mine
    with pytest.raises(LockError):
        bob.activate("ProjektA")

    with ProjectDB.for_project(proj) as db:
        assert len(db.active_files()) == 3


def test_comment_required(env):
    _, alice, _, _, _ = env
    alice.activate("ProjektA")
    plan = alice.plan_deactivation("ProjektA")
    with pytest.raises(SyncError):
        alice.deactivate(plan, plan.default_assignments(), "   ")
    assert read_lock(Path(alice.server_dir("ProjektA"))) is not None


def test_modify_add_delete_with_backups(env):
    _, alice, _, proj, local = env
    for i in range(3):
        alice.activate("ProjektA")
        write(local / "zprava.docx", f"zprava verze {i + 2} " * 50)
        if i == 0:
            write(local / "novy.txt", "novy")
            (local / "foto.jpg").unlink()
        plan, res = run_deactivate(alice, "ProjektA", comment=f"uprava {i}")
        assert res.modified == 1
        assert not local.exists()  # místní kopie smazána po deaktivaci

    assert (proj / "novy.txt").exists()
    assert not (proj / "foto.jpg").exists()
    assert list((proj / ".spm" / "trash").rglob("foto.jpg"))
    backups = sorted(p.name for p in (proj / ".spm" / "backup").iterdir())
    assert backups == ["zprava.docx.backup02", "zprava.docx.backup03"]  # ponechány poslední 2
    assert read_lock(proj) is None

    with ProjectDB.for_project(proj) as db:
        sessions = db.sessions()
        assert [s["comment"] for s in sessions] == ["uprava 2", "uprava 1", "uprava 0"]
        kinds = {c["change_type"] for c in db.changes(sessions[-1]["session_id"])}
        assert kinds == {dbm.IMPORTED, dbm.MODIFIED, dbm.ADDED, dbm.DELETED}


def test_rename_detected_and_history_kept(env):
    _, alice, _, proj, local = env
    alice.activate("ProjektA")
    with ProjectDB.for_project(proj) as db:
        fid = db.active_files()["vykresy/pudorys.dwg"].file_id

    # čisté přejmenování + přesun do jiné složky
    (local / "podklady").mkdir()
    (local / "vykresy" / "pudorys.dwg").rename(local / "podklady" / "pudorys_final.dwg")
    # přejmenování a zároveň úprava
    (local / "zprava.docx").unlink()
    write(local / "zprava_2026.docx", "zprava verze 1 " * 48 + "doplneno")

    plan = alice.plan_deactivation("ProjektA")
    sugg = {s.old_path: s for s in plan.suggestions}
    assert sugg["vykresy/pudorys.dwg"].new_path == "podklady/pudorys_final.dwg"
    assert sugg["vykresy/pudorys.dwg"].same_content
    assert sugg["zprava.docx"].new_path == "zprava_2026.docx"
    assert not sugg["zprava.docx"].same_content

    alice.deactivate(plan, plan.default_assignments(), "prejmenovani")
    assert (proj / "podklady" / "pudorys_final.dwg").exists()
    assert not (proj / "vykresy").exists()  # prázdná složka uklizena
    assert (proj / ".spm" / "backup" / "zprava_2026.docx.backup01").exists()

    with ProjectDB.for_project(proj) as db:
        assert db.active_files()["podklady/pudorys_final.dwg"].file_id == fid
        hist = db.file_history(fid)
        assert hist[0]["change_type"] == dbm.RENAMED
        assert hist[0]["old_path"] == "vykresy/pudorys.dwg"


def test_rejected_rename_becomes_delete_and_add(env):
    _, alice, _, proj, local = env
    alice.activate("ProjektA")
    (local / "foto.jpg").rename(local / "foto2.jpg")
    plan = alice.plan_deactivation("ProjektA")
    fid = plan.missing[0].file_id
    alice.deactivate(plan, {fid: None}, "bez parovani")
    with ProjectDB.for_project(proj) as db:
        kinds = [c["change_type"] for c in db.changes(db.sessions()[0]["session_id"])]
    assert sorted(k for k in kinds if k != dbm.IMPORTED) == [dbm.ADDED, dbm.DELETED]


def test_swap_names(env):
    settings, alice, _, proj, local = env
    alice.activate("ProjektA")
    (local / "zprava.docx").rename(local / "tmp")
    (local / "foto.jpg").rename(local / "zprava.docx")
    (local / "tmp").rename(local / "foto.jpg")
    plan = alice.plan_deactivation("ProjektA")
    # obsah se změnil pod stejným názvem -> vidí se jako úprava, ne přejmenování
    assert len(plan.modified) == 2
    alice.deactivate(plan, plan.default_assignments(), "zamena")
    assert (proj / "foto.jpg").read_text(encoding="utf-8").startswith("zprava")


def test_manual_rename_on_server_keeps_history(env):
    _, alice, _, proj, local = env
    alice.activate("ProjektA")
    run_deactivate(alice, "ProjektA")
    with ProjectDB.for_project(proj) as db:
        fid = db.active_files()["vykresy/pudorys.dwg"].file_id

    # ručně přímo na serveru (projekt je deaktivovaný): přejmenování + zámek Cimatronu
    (proj / "vykresy" / "pudorys.dwg").rename(proj / "vykresy" / "pudorys_final.dwg")
    write(proj / "vykresy" / "Půdorys.dwg.lck$", "zamek")
    (proj / "foto.jpg").unlink()

    alice.activate("ProjektA")
    assert not (local / "vykresy" / "Půdorys.dwg.lck$").exists()  # zámky se nekopírují
    with ProjectDB.for_project(proj) as db:
        files = db.active_files()
        assert files["vykresy/pudorys_final.dwg"].file_id == fid
        assert "foto.jpg" not in files
        kinds = {c["change_type"]: c for c in db.changes(db.sessions()[0]["session_id"])}
        assert kinds[dbm.EXTERNAL_RENAMED]["old_path"] == "vykresy/pudorys.dwg"
        assert dbm.EXTERNAL_DELETED in kinds and dbm.IMPORTED not in kinds


def test_external_change_is_reported(env):
    _, alice, bob, proj, local = env
    alice.activate("ProjektA")
    write(proj / "foto.jpg", "zmeneno primo na serveru")
    plan = alice.plan_deactivation("ProjektA")
    assert any("foto.jpg" in c for c in plan.conflicts)


def test_force_unlock_and_cancel(env):
    _, alice, bob, proj, _ = env
    alice.activate("ProjektA")
    bob.force_unlock("ProjektA")
    assert read_lock(proj) is None
    bob.activate("ProjektA")
    bob.cancel_activation("ProjektA")
    with ProjectDB.for_project(proj) as db:
        assert [s["status"] for s in db.sessions()] == ["cancelled", "cancelled"]
