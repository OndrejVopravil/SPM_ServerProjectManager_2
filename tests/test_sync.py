import os
import stat
import time
from pathlib import Path

import pytest

from spm import db as dbm
from spm.config import Settings
from spm.db import ProjectDB
from spm.lock import active_users
from spm.sync import ProjectManager, SyncError, new_key


def write(path: Path, text: str, age: float = 0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.chmod(stat.S_IWRITE | stat.S_IREAD)
    path.write_text(text, encoding="utf-8")
    if age:
        t = time.time() - age
        os.utime(path, (t, t))


def readonly(path: Path) -> bool:
    return not os.access(path, os.W_OK)


@pytest.fixture
def env(tmp_path):
    server = tmp_path / "server"
    proj = server / "ProjektA"
    write(proj / "vykresy" / "pudorys.dwg", "pudorys " * 100, age=100)
    write(proj / "zprava.docx", "zprava verze 1 " * 50, age=100)
    write(proj / "foto.jpg", "jpg data", age=100)

    def manager(user: str) -> ProjectManager:
        s = Settings(server_root=str(server), local_root=str(tmp_path / f"local_{user}"), backup_count=2)
        return ProjectManager(s, user=user, pc=f"PC-{user}")

    alice, bob = manager("alice"), manager("bob")
    return alice, bob, proj, alice.local_dir("ProjektA")


def run_deactivate(pm, name="ProjektA", comment="hotovo", assignments=None):
    plan = pm.plan_upload(name)
    a = plan.default_assignments()
    if assignments:
        a.update(assignments)
    return plan, pm.deactivate(plan, a, comment)


def last_commit(proj):
    with ProjectDB.for_project(proj) as db:
        c = db.commits()[0]
        return c, db.changes(c["commit_id"])


# --- jeden uživatel (chování jako dřív) ---------------------------------------

def test_activate_copies_and_locks_everything_by_default(env):
    alice, bob, proj, local = env
    alice.activate("ProjektA")
    assert (local / "vykresy" / "pudorys.dwg").exists()
    assert not readonly(local / "zprava.docx")
    status = {p.name: p for p in bob.list_projects()}["ProjektA"]
    assert status.active and [u.user for u in status.users] == ["alice"] and not status.mine
    with pytest.raises(SyncError):
        alice.activate("ProjektA")  # podruhé nelze


def test_comment_required_only_with_changes(env):
    alice, _, proj, local = env
    alice.activate("ProjektA")
    plan = alice.plan_upload("ProjektA")
    alice.deactivate(plan, plan.default_assignments(), "")  # bez změn -> komentář není nutný
    assert active_users(proj) == []

    alice.activate("ProjektA")
    write(alice.local_dir("ProjektA") / "zprava.docx", "nova")
    plan = alice.plan_upload("ProjektA")
    with pytest.raises(SyncError, match="komentáře"):
        alice.deactivate(plan, plan.default_assignments(), "  ")


def test_modify_add_delete_with_backups(env):
    alice, _, proj, local = env
    for i in range(3):
        alice.activate("ProjektA")
        write(local / "zprava.docx", f"zprava verze {i + 2} " * 50)
        if i == 0:
            write(local / "novy.txt", "novy")
            (local / "foto.jpg").unlink()
        _, res = run_deactivate(alice, comment=f"uprava {i}")
        assert res.modified == 1
        assert not local.exists()  # místní kopie smazána po deaktivaci

    assert (proj / "novy.txt").exists()
    assert not (proj / "foto.jpg").exists()
    assert list((proj / ".spm" / "trash").rglob("foto.jpg"))
    backups = sorted(p.name for p in (proj / ".spm" / "backup").iterdir())
    assert backups == ["zprava.docx.backup02", "zprava.docx.backup03"]  # ponechány poslední 2
    assert active_users(proj) == []
    with ProjectDB.for_project(proj) as db:
        uploads = [c for c in db.commits() if c["kind"] == dbm.UPLOAD]
        assert [c["comment"] for c in uploads] == ["uprava 2", "uprava 1", "uprava 0"]
        kinds = {c["change_type"] for c in db.changes(uploads[-1]["commit_id"])}
        assert kinds == {dbm.MODIFIED, dbm.ADDED, dbm.DELETED}
        assert db.lock_owners() == {}


def test_rename_detected_and_history_kept(env):
    alice, _, proj, local = env
    alice.activate("ProjektA")
    with ProjectDB.for_project(proj) as db:
        fid = db.active_files()["vykresy/pudorys.dwg"].file_id

    (local / "podklady").mkdir()
    (local / "vykresy" / "pudorys.dwg").rename(local / "podklady" / "pudorys_final.dwg")
    (local / "zprava.docx").unlink()
    write(local / "zprava_2026.docx", "zprava verze 1 " * 48 + "doplneno")

    plan = alice.plan_upload("ProjektA")
    sugg = {s.old_path: s for s in plan.suggestions}
    assert sugg["vykresy/pudorys.dwg"].new_path == "podklady/pudorys_final.dwg"
    assert sugg["zprava.docx"].new_path == "zprava_2026.docx"

    alice.deactivate(plan, plan.default_assignments(), "prejmenovani")
    assert (proj / "podklady" / "pudorys_final.dwg").exists()
    assert not (proj / "vykresy").exists()
    assert (proj / ".spm" / "backup" / "zprava_2026.docx.backup01").exists()
    with ProjectDB.for_project(proj) as db:
        assert db.active_files()["podklady/pudorys_final.dwg"].file_id == fid
        assert db.file_history(fid)[0]["old_path"] == "vykresy/pudorys.dwg"


def test_swap_names(env):
    alice, _, proj, local = env
    alice.activate("ProjektA")
    (local / "zprava.docx").rename(local / "tmp")
    (local / "foto.jpg").rename(local / "zprava.docx")
    (local / "tmp").rename(local / "foto.jpg")
    plan = alice.plan_upload("ProjektA")
    assert len(plan.modified) == 2
    alice.deactivate(plan, plan.default_assignments(), "zamena")
    assert (proj / "foto.jpg").read_text(encoding="utf-8").startswith("zprava")


def test_manual_rename_on_server_keeps_history(env):
    alice, _, proj, local = env
    alice.activate("ProjektA")
    run_deactivate(alice)
    with ProjectDB.for_project(proj) as db:
        fid = db.active_files()["vykresy/pudorys.dwg"].file_id
    (proj / "vykresy" / "pudorys.dwg").rename(proj / "vykresy" / "pudorys_final.dwg")
    write(proj / "vykresy" / "Půdorys.dwg.lck$", "zamek")
    (proj / "foto.jpg").unlink()

    alice.activate("ProjektA")
    assert not (local / "vykresy" / "Půdorys.dwg.lck$").exists()
    with ProjectDB.for_project(proj) as db:
        assert db.active_files()["vykresy/pudorys_final.dwg"].file_id == fid
        sync = [c for c in db.commits() if c["kind"] == dbm.SYNC][0]
        kinds = {c["change_type"]: c for c in db.changes(sync["commit_id"])}
    assert kinds[dbm.EXTERNAL_RENAMED]["old_path"] == "vykresy/pudorys.dwg"
    assert dbm.EXTERNAL_DELETED in kinds and dbm.IMPORTED not in kinds


def test_external_change_of_locked_file_is_reported(env):
    alice, _, proj, local = env
    alice.activate("ProjektA")
    write(proj / "foto.jpg", "zmeneno primo na serveru")
    plan = alice.plan_upload("ProjektA")
    assert any("foto.jpg" in c for c in plan.conflicts)


def test_old_database_version_reported_clearly(env):
    import sqlite3

    alice, _, proj, _ = env
    (proj / ".spm").mkdir()
    conn = sqlite3.connect(proj / ".spm" / "project.db")
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO meta VALUES ('schema_version', '1')")
    conn.commit()
    conn.close()
    with pytest.raises(dbm.DbVersionError, match="starší verze"):
        alice.activate("ProjektA")


# --- spolupráce více uživatelů --------------------------------------------------

@pytest.fixture
def electrodes(env):
    """A (alice) aktivuje sestavu elektrod; B (bob) pracuje souběžně."""
    alice, bob, proj, _ = env
    write(proj / "sestava" / "sestava.elt", "sestava")
    alice.activate("ProjektA")
    return alice, bob, proj


def test_second_user_can_activate_with_readonly_files(electrodes):
    alice, bob, proj = electrodes
    bob.activate("ProjektA", lock_paths=[])
    local_b = bob.local_dir("ProjektA")
    assert readonly(local_b / "sestava" / "sestava.elt")
    status = {p.name: p for p in bob.list_projects()}["ProjektA"]
    assert status.mine and [u.user for u in status.others] == ["alice"]
    entries = {e.path: e for e in bob.file_entries("ProjektA")}
    assert entries["sestava/sestava.elt"].owner.user == "alice"


def test_cannot_lock_file_held_by_other(electrodes):
    alice, bob, proj = electrodes
    with pytest.raises(SyncError, match="alice"):
        bob.activate("ProjektA", lock_paths=["sestava/sestava.elt"])
    assert not bob.local_dir("ProjektA").exists()
    assert [u.user for u in active_users(proj)] == ["alice"]


def test_electrode_workflow(electrodes):
    """A tvoří elektrody a odevzdává je, B z nich průběžně dělá NC soubory."""
    alice, bob, proj = electrodes
    local_a = alice.local_dir("ProjektA")
    bob.activate("ProjektA", lock_paths=[])
    local_b = bob.local_dir("ProjektA")

    # A vytvoří dvě elektrody a odevzdá jen první
    write(local_a / "sestava" / "elektrody" / "E01.elt", "elektroda 1")
    write(local_a / "sestava" / "elektrody" / "E02.elt", "elektroda 2 rozpracovana")
    plan = alice.plan_upload("ProjektA")
    res = alice.upload(plan, plan.default_assignments(), "E01 hotova",
                       selected={new_key("sestava/elektrody/E01.elt")})
    assert res.added == 1 and (proj / "sestava" / "elektrody" / "E01.elt").exists()
    assert not (proj / "sestava" / "elektrody" / "E02.elt").exists()
    assert readonly(local_a / "sestava" / "elektrody" / "E01.elt")   # odevzdáno = uvolněno

    # B si stáhne E01 (jen pro čtení) a vytvoří k ní NC soubor ve stejné složce
    r = bob.refresh("ProjektA")
    assert r.downloaded == ["sestava/elektrody/E01.elt"]
    assert readonly(local_b / "sestava" / "elektrody" / "E01.elt")
    write(local_b / "sestava" / "elektrody" / "E01_NC.nc", "nc program E01")
    plan_b = bob.plan_upload("ProjektA")
    assert [lf.path for lf in plan_b.new] == ["sestava/elektrody/E01_NC.nc"]
    bob.upload(plan_b, plan_b.default_assignments(), "NC pro E01")

    # A mezitím dokončí E02 a deaktivuje; A si NC od B stáhne aktualizací
    assert alice.refresh("ProjektA").downloaded == ["sestava/elektrody/E01_NC.nc"]
    write(local_a / "sestava" / "elektrody" / "E02.elt", "elektroda 2 hotova")
    run_deactivate(alice, comment="E02 hotova, sestava hotova")
    assert [u.user for u in active_users(proj)] == ["bob"]

    assert bob.refresh("ProjektA").downloaded == ["sestava/elektrody/E02.elt"]
    write(local_b / "sestava" / "elektrody" / "E02_NC.nc", "nc program E02")
    run_deactivate(bob, comment="NC pro E02")
    assert active_users(proj) == []
    assert sorted(p.name for p in (proj / "sestava" / "elektrody").iterdir()) == \
        ["E01.elt", "E01_NC.nc", "E02.elt", "E02_NC.nc"]
    with ProjectDB.for_project(proj) as db:
        assert db.lock_owners() == {}
        comments = [c["comment"] for c in db.commits() if c["kind"] == dbm.UPLOAD]
    assert comments == ["NC pro E02", "E02 hotova, sestava hotova", "NC pro E01", "E01 hotova"]


def test_change_of_foreign_file_is_not_uploaded(electrodes):
    alice, bob, proj = electrodes
    bob.activate("ProjektA", lock_paths=[])
    f = bob.local_dir("ProjektA") / "sestava" / "sestava.elt"
    write(f, "bob to prepsal")  # obejde atribut jen pro čtení
    plan = bob.plan_upload("ProjektA")
    assert any("sestava.elt" in c and "alice" in c for c in plan.conflicts)
    assert not plan.modified
    run_deactivate(bob)
    assert (proj / "sestava" / "sestava.elt").read_text(encoding="utf-8") == "sestava"


def test_new_file_name_conflict(electrodes):
    alice, bob, proj = electrodes
    bob.activate("ProjektA", lock_paths=[])
    write(alice.local_dir("ProjektA") / "spolecny.txt", "od alice")
    write(bob.local_dir("ProjektA") / "spolecny.txt", "od boba")
    plan_a = alice.plan_upload("ProjektA")
    plan_b = bob.plan_upload("ProjektA")
    alice.upload(plan_a, plan_a.default_assignments(), "a")
    with pytest.raises(SyncError, match="stejným názvem"):
        bob.upload(plan_b, plan_b.default_assignments(), "b")
    assert any("spolecny.txt" in c for c in bob.plan_upload("ProjektA").conflicts)
    assert (proj / "spolecny.txt").read_text(encoding="utf-8") == "od alice"


def test_take_released_file(electrodes):
    alice, bob, proj = electrodes
    bob.activate("ProjektA", lock_paths=[])
    with pytest.raises(SyncError, match="alice"):
        bob.take("ProjektA", ["sestava/sestava.elt"])

    write(alice.local_dir("ProjektA") / "sestava" / "sestava.elt", "sestava v2")
    plan = alice.plan_upload("ProjektA")
    alice.upload(plan, plan.default_assignments(), "sestava v2")

    r = bob.take("ProjektA", ["sestava/sestava.elt"])
    f = bob.local_dir("ProjektA") / "sestava" / "sestava.elt"
    assert r.downloaded == ["sestava/sestava.elt"] and not readonly(f)
    assert f.read_text(encoding="utf-8") == "sestava v2"
    write(f, "sestava v3 od boba")
    run_deactivate(bob, comment="v3")
    assert (proj / "sestava" / "sestava.elt").read_text(encoding="utf-8") == "sestava v3 od boba"


def test_release_unchanged_locked_files(electrodes):
    alice, bob, proj = electrodes
    plan = alice.plan_upload("ProjektA")
    fid = next(bf.file_id for bf in plan.locked_unchanged if bf.path == "foto.jpg")
    res = alice.upload(plan, plan.default_assignments(), "", selected={fid})
    assert res.released == 1
    bob.activate("ProjektA", lock_paths=["foto.jpg"])  # teď je volný


def test_refresh_follows_rename_and_delete_by_other(electrodes):
    alice, bob, proj = electrodes
    bob.activate("ProjektA", lock_paths=[])
    local_a = alice.local_dir("ProjektA")
    (local_a / "zprava.docx").rename(local_a / "zprava_final.docx")
    (local_a / "foto.jpg").unlink()
    plan = alice.plan_upload("ProjektA")
    alice.upload(plan, plan.default_assignments(), "uklid")
    r = bob.refresh("ProjektA")
    local_b = bob.local_dir("ProjektA")
    assert r.moved == ["zprava.docx → zprava_final.docx"] and r.removed == ["foto.jpg"]
    assert (local_b / "zprava_final.docx").exists() and not (local_b / "foto.jpg").exists()


def test_force_unlock_and_cancel(electrodes):
    alice, bob, proj = electrodes
    bob.force_unlock("ProjektA", "alice", "PC-alice")
    assert active_users(proj) == []
    with pytest.raises(SyncError, match="není aktivován"):
        alice.plan_upload("ProjektA")
    bob.activate("ProjektA")
    bob.cancel_activation("ProjektA")
    with ProjectDB.for_project(proj) as db:
        kinds = [c["kind"] for c in db.commits()][:2]
        assert kinds == [dbm.CANCELLED, dbm.FORCE_UNLOCK]
        assert db.lock_owners() == {} and db.active_sessions() == []


def test_my_active_projects_listed_first(tmp_path):
    for name in ("Alfa", "Beta", "Gama", "Delta"):
        (tmp_path / "server" / name).mkdir(parents=True)
        (tmp_path / "server" / name / "a.txt").write_text("x")
    (tmp_path / "local_alice" / "Cizi_lokalni").mkdir(parents=True)

    def pm(user):
        s = Settings(server_root=str(tmp_path / "server"), local_root=str(tmp_path / f"local_{user}"))
        return ProjectManager(s, user=user, pc="PC-" + user)

    alice, bob = pm("alice"), pm("bob")
    alice.activate("Gama")
    alice.activate("Beta")
    bob.activate("Alfa")    # cizí aktivace se neřadí nahoru
    assert [p.name for p in alice.list_projects()] == ["Beta", "Gama", "Alfa", "Cizi_lokalni", "Delta"]
    assert [p.name for p in bob.list_projects()] == ["Alfa", "Beta", "Delta", "Gama"]
