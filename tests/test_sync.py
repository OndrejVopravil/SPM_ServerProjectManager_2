import os
import stat
import time
from pathlib import Path

import pytest

from spm import db as dbm
from spm.config import Settings
from spm.db import ProjectDB
from spm.lock import active_users
from spm.scanner import file_hash
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
    bob.activate("Alfa")    # cizí aktivace až za místními (neodevzdanými), před volnými
    assert [p.name for p in alice.list_projects()] == ["Beta", "Gama", "Cizi_lokalni", "Alfa", "Delta"]
    assert [p.name for p in bob.list_projects()] == ["Alfa", "Beta", "Gama", "Delta"]


# --- automatická deaktivace po odevzdání posledního drženého souboru -------------

def test_submitting_last_held_file_deactivates(electrodes):
    alice, bob, proj = electrodes
    local = alice.local_dir("ProjektA")
    write(local / "sestava" / "sestava.elt", "sestava v2")
    plan = alice.plan_upload("ProjektA")
    held = set(plan.held)
    # nejdřív jen část -> zůstává aktivováno
    part = {bf.file_id for bf, _ in plan.modified}
    res = alice.upload(plan, plan.default_assignments(), "sestava", selected=part)
    assert not res.auto_deactivated and [u.user for u in active_users(proj)] == ["alice"]
    # zbytek -> poslední držené soubory -> deaktivace
    plan = alice.plan_upload("ProjektA")
    res = alice.upload(plan, plan.default_assignments(), "", selected=set(plan.held))
    assert res.auto_deactivated and active_users(proj) == []
    assert not local.exists()                      # jako při deaktivaci (smazat místní kopii)
    with ProjectDB.for_project(proj) as db:
        assert db.active_sessions() == [] and db.lock_owners() == {}
    assert held


def test_no_auto_deactivation_for_user_without_locks(electrodes):
    """B nic nedrží, jen přidává NC soubory – po odevzdání pracuje dál."""
    alice, bob, proj = electrodes
    bob.activate("ProjektA", lock_paths=[])
    write(bob.local_dir("ProjektA") / "E01_NC.nc", "nc")
    plan = bob.plan_upload("ProjektA")
    res = bob.upload(plan, plan.default_assignments(), "NC")
    assert not res.auto_deactivated
    assert "bob" in [u.user for u in active_users(proj)]


def test_no_auto_deactivation_with_unsubmitted_new_files(electrodes):
    alice, bob, proj = electrodes
    write(alice.local_dir("ProjektA") / "rozpracovany.txt", "x")
    plan = alice.plan_upload("ProjektA")
    res = alice.upload(plan, plan.default_assignments(), "", selected=set(plan.held))
    assert not res.auto_deactivated and "neodevzdaných nových" in res.note
    assert [u.user for u in active_users(proj)] == ["alice"]
    assert alice.local_dir("ProjektA").exists()


# --- mazání do Koše ------------------------------------------------------------

@pytest.fixture
def fake_recycle(monkeypatch, tmp_path):
    """Místo skutečného Koše Windows přesouvá do dočasné složky (testy Koš nezaplní)."""
    import shutil

    from spm import recycle
    kos = tmp_path / "kos"
    kos.mkdir()
    moved = []

    def fake(paths):
        for p in paths:
            shutil.move(str(p), str(kos / f"{len(moved)}_{p.name}"))
            moved.append(p)
    monkeypatch.setattr(recycle, "to_recycle_bin", fake)
    return moved


def test_delete_moves_own_files_to_recycle_bin(electrodes, fake_recycle):
    alice, bob, proj = electrodes
    local = alice.local_dir("ProjektA")
    write(local / "sestava" / "novy.txt", "novy")
    deleted = alice.delete_local("ProjektA", ["sestava/sestava.elt", "sestava/novy.txt"])
    assert sorted(deleted) == ["sestava/novy.txt", "sestava/sestava.elt"]
    assert [p.name for p in fake_recycle] == ["sestava.elt", "novy.txt"]
    assert not (local / "sestava").exists()            # prázdná složka uklizena
    assert (proj / "sestava" / "sestava.elt").exists()  # na serveru až při odevzdání
    assert alice.local_deleted_files("ProjektA") == ["sestava/sestava.elt"]

    plan = alice.plan_upload("ProjektA")
    assert [bf.path for bf in plan.missing] == ["sestava/sestava.elt"]
    fid = plan.missing[0].file_id
    alice.upload(plan, {fid: None}, "smazana sestava", selected={fid})
    assert not (proj / "sestava" / "sestava.elt").exists()
    assert list((proj / ".spm" / "trash").rglob("sestava.elt"))   # na serveru do koše projektu


def test_cannot_delete_foreign_files(electrodes, fake_recycle):
    alice, bob, proj = electrodes
    bob.activate("ProjektA", lock_paths=[])
    with pytest.raises(SyncError, match="nemáte zamčené"):
        bob.delete_local("ProjektA", ["sestava/sestava.elt"])
    assert fake_recycle == []
    assert (bob.local_dir("ProjektA") / "sestava" / "sestava.elt").exists()


# --- souběh: jiný uživatel pracuje, zatímco se aktivace kopíruje -------------------

def _during_copy(monkeypatch, when_path, action):
    """Spustí `action` hned po zkopírování souboru `when_path` při aktivaci."""
    import spm.sync as sync
    real = sync.copy_with_hash
    done = []

    def wrapper(src, dst):
        sha = real(src, dst)
        if not done and src.as_posix().endswith(when_path):
            done.append(True)
            action()
        return sha
    monkeypatch.setattr(sync, "copy_with_hash", wrapper)
    return done


def test_locks_are_taken_before_copying(env, monkeypatch):
    alice, bob, proj, local = env
    bob.activate("ProjektA", lock_paths=[])
    monkeypatch.undo()
    seen = {}

    def bob_tries():
        assert [u.user for u in active_users(proj)] == ["alice", "bob"]   # alice už je vidět
        with pytest.raises(SyncError, match="alice"):
            bob.take("ProjektA", ["zprava.docx"])                        # už zamčeno
        seen["ok"] = True
    _during_copy(monkeypatch, "foto.jpg", bob_tries)
    alice.activate("ProjektA", lock_paths=["zprava.docx"])
    assert seen == {"ok": True}


def test_file_submitted_by_other_during_copy_is_downloaded_again(env, monkeypatch):
    alice, bob, proj, local = env
    bob.activate("ProjektA", lock_paths=[])

    def bob_changes_foto():
        bob.take("ProjektA", ["foto.jpg"])
        write(bob.local_dir("ProjektA") / "foto.jpg", "foto od boba")
        plan = bob.plan_upload("ProjektA")
        fid = next(bf.file_id for bf, _ in plan.modified)
        bob.upload(plan, plan.default_assignments(), "nova fotka", selected={fid})
    done = _during_copy(monkeypatch, "foto.jpg", bob_changes_foto)
    alice.activate("ProjektA", lock_paths=["zprava.docx"])
    assert done

    # alice má aktuální verzi a databáze nebyla přepsána starým obsahem
    assert (local / "foto.jpg").read_text(encoding="utf-8") == "foto od boba"
    with ProjectDB.for_project(proj) as db:
        kinds = [c["change_type"] for c in db.all_changes()]
        assert dbm.EXTERNAL_MODIFIED not in kinds                     # Bobova změna nebyla přepsána
        foto = db.active_files()["foto.jpg"]
    assert foto.sha256 == file_hash(proj / "foto.jpg")                # databáze = skutečnost
    plan = alice.plan_upload("ProjektA")
    assert not plan.conflicts and not plan.modified
    assert alice.refresh("ProjektA").total == 0                        # nic dalšího ke stažení


def test_failed_copy_releases_locks(env, monkeypatch):
    alice, bob, proj, local = env

    def boom():
        raise OSError("sit spadla")
    _during_copy(monkeypatch, "foto.jpg", boom)
    with pytest.raises(OSError):
        alice.activate("ProjektA", lock_paths=["zprava.docx"])
    assert active_users(proj) == []
    with ProjectDB.for_project(proj) as db:
        assert db.lock_owners() == {} and db.active_sessions() == []
    assert not local.exists()


# --- „Nestahovat neaktivované soubory“ ----------------------------------------------

def test_activate_only_locked_files(env):
    alice, bob, proj, local = env
    alice.activate("ProjektA", lock_paths=["zprava.docx"], only_locked=True)
    assert sorted(p.name for p in local.rglob("*") if p.is_file() and ".spm" not in p.parts) == ["zprava.docx"]
    assert not readonly(local / "zprava.docx")

    # nic nestaženého se nehlásí jako smazané ani konfliktní
    plan = alice.plan_upload("ProjektA")
    assert not plan.missing and not plan.conflicts and not plan.new

    # Aktualizovat: nové / nestažené soubory nestahuje, stažené aktualizuje
    bob.activate("ProjektA", lock_paths=["foto.jpg"])
    write(bob.local_dir("ProjektA") / "foto.jpg", "nova foto")
    write(bob.local_dir("ProjektA") / "nove.txt", "novy od boba")
    plan_b = bob.plan_upload("ProjektA")
    bob.deactivate(plan_b, plan_b.default_assignments(), "bob")
    assert alice.refresh("ProjektA").total == 0
    assert not (local / "foto.jpg").exists() and not (local / "nove.txt").exists()

    # Převzít soubor stáhne
    r = alice.take("ProjektA", ["foto.jpg"])
    assert r.downloaded == ["foto.jpg"]
    assert (local / "foto.jpg").read_text(encoding="utf-8") == "nova foto"

    # odevzdání a deaktivace fungují normálně
    write(local / "zprava.docx", "zprava nova")
    plan = alice.plan_upload("ProjektA")
    assert [lf.path for _, lf in plan.modified] == ["zprava.docx"]
    alice.deactivate(plan, plan.default_assignments(), "jen cast")
    assert (proj / "zprava.docx").read_text(encoding="utf-8") == "zprava nova"
    assert (proj / "vykresy" / "pudorys.dwg").exists()       # nestažené soubory na serveru zůstaly


def test_activate_only_locked_first_activation(env):
    """První aktivace (projekt ještě bez databáze): nestažené soubory se do evidence zapíší
    až aktivací, která je stáhne; do té doby je seznam souborů bere ze serveru jako volné."""
    alice, bob, proj, local = env
    alice.activate("ProjektA", lock_paths=["foto.jpg"], only_locked=True)
    entries = {e.path: e for e in bob.file_entries("ProjektA")}
    assert entries["foto.jpg"].owner.user == "alice"
    assert entries["zprava.docx"].free and entries["vykresy/pudorys.dwg"].free
    bob.activate("ProjektA", lock_paths=["zprava.docx"])
    assert (bob.local_dir("ProjektA") / "vykresy" / "pudorys.dwg").exists()
    with ProjectDB.for_project(proj) as db:
        assert set(db.active_files()) == {"vykresy/pudorys.dwg", "zprava.docx", "foto.jpg"}


# --- přejmenování souboru v místní kopii (panel, F2) ---------------------------------

def test_rename_own_file_and_submit(electrodes):
    alice, bob, proj = electrodes
    local = alice.local_dir("ProjektA")
    with ProjectDB.for_project(proj) as db:
        fid = db.active_files()["sestava/sestava.elt"].file_id
    assert alice.rename_local("ProjektA", "sestava/sestava.elt", "sestava_hlavni.elt") == "sestava/sestava_hlavni.elt"
    assert (local / "sestava" / "sestava_hlavni.elt").exists()
    assert (proj / "sestava" / "sestava.elt").exists()                  # server až při odevzdání
    assert alice.local_renames("ProjektA") == {"sestava/sestava.elt": "sestava/sestava_hlavni.elt"}

    plan = alice.plan_upload("ProjektA")
    [s] = plan.suggestions
    assert (s.old_path, s.new_path, s.same_content) == ("sestava/sestava.elt", "sestava/sestava_hlavni.elt", True)
    alice.upload(plan, plan.default_assignments(), "prejmenovani", selected={fid})
    assert (proj / "sestava" / "sestava_hlavni.elt").exists() and not (proj / "sestava" / "sestava.elt").exists()
    with ProjectDB.for_project(proj) as db:
        assert db.file_history(fid)[0]["change_type"] == dbm.RENAMED


def test_rename_rules(electrodes):
    alice, bob, proj = electrodes
    bob.activate("ProjektA", lock_paths=[])
    with pytest.raises(SyncError, match="nemáte zamčený"):
        bob.rename_local("ProjektA", "sestava/sestava.elt", "jiny.elt")    # cizí soubor
    with pytest.raises(SyncError, match="nepovolené"):
        alice.rename_local("ProjektA", "zprava.docx", "zpráva.docx")       # diakritika
    with pytest.raises(SyncError, match="Windows"):
        alice.rename_local("ProjektA", "zprava.docx", "a:b.docx")
    with pytest.raises(SyncError, match="už ve složce"):
        alice.rename_local("ProjektA", "zprava.docx", "foto.jpg")          # obsazeno lokálně
    write(bob.local_dir("ProjektA") / "nc.txt", "nc")
    plan = bob.plan_upload("ProjektA")
    bob.upload(plan, plan.default_assignments(), "nc")
    with pytest.raises(SyncError, match="Na serveru už je"):
        alice.rename_local("ProjektA", "zprava.docx", "nc.txt")           # obsazeno na serveru
    write(alice.local_dir("ProjektA") / "muj_novy.txt", "x")
    assert alice.rename_local("ProjektA", "muj_novy.txt", "muj_novy_2.txt") == "muj_novy_2.txt"  # nový soubor


def test_project_list_reads_small_files_only_when_changed(tmp_path, monkeypatch):
    """Seznam projektů čte značky aktivace a komentář jen při jejich změně (rychlé obnovení)."""
    from spm import sync as sync_mod
    from spm.comment import read_comment as real_read_comment
    from spm.lock import read_marker as real_read_marker
    for name in ("A", "B", "C"):
        (tmp_path / "server" / name).mkdir(parents=True)
        (tmp_path / "server" / name / "x.txt").write_text("x")
    pm = ProjectManager(Settings(server_root=str(tmp_path / "server"), local_root=str(tmp_path / "l")),
                        user="alice", pc="PC")
    other = ProjectManager(Settings(server_root=str(tmp_path / "server"), local_root=str(tmp_path / "l2")),
                           user="bob", pc="PC2")
    other.activate("A", lock_paths=[])
    other.set_comment("B", "komentář")
    reads = []
    monkeypatch.setattr(sync_mod, "read_marker", lambda p: reads.append(p.name) or real_read_marker(p))
    monkeypatch.setattr(sync_mod, "read_comment", lambda s: reads.append("comment") or real_read_comment(s))
    first = {p.name: p for p in pm.list_projects()}
    assert first["A"].others[0].user == "bob" and first["B"].comment.text == "komentář"
    assert sorted(reads) == ["bob@pc2.json", "comment"]
    reads.clear()
    second = {p.name: p for p in pm.list_projects()}             # beze změn – nic se nečte
    assert reads == [] and second["A"].others[0].user == "bob" and second["B"].comment.text == "komentář"
    other.set_comment("B", "nový komentář")                     # změna -> přečte se jen ona
    reads.clear()                                               # (set_comment čte komentář sám)
    third = {p.name: p for p in pm.list_projects()}
    assert reads == ["comment"] and third["B"].comment.text == "nový komentář"
    other.cancel_activation("A")
    assert not {p.name: p for p in pm.list_projects()}["A"].active


# --- soubor smazaný na serveru mimo aplikaci, zatímco ho držím -----------------------------

def _changes_of(proj):
    from spm.db import ProjectDB
    with ProjectDB.for_project(proj) as db:
        return [(c["change_type"], c["old_path"] or c["new_path"]) for c in db.all_changes()]


def test_unchanged_held_file_deleted_on_server_is_restored(tmp_path):
    proj = tmp_path / "server" / "Forma"
    for f in ("a.txt", "b.txt"):
        (proj / f).parent.mkdir(parents=True, exist_ok=True)
        (proj / f).write_text(f)
    pm = ProjectManager(Settings(server_root=str(tmp_path / "server"), local_root=str(tmp_path / "l")))
    pm.activate("Forma", lock_paths=["a.txt", "b.txt"])
    (proj / "a.txt").unlink()                                   # mimo aplikaci (Průzkumník)
    plan = pm.plan_upload("Forma")
    assert any("a.txt" in c and "obnoví se" in c for c in plan.conflicts)
    a_id = next(bf.file_id for bf in plan.locked_unchanged if bf.path == "a.txt")
    res = pm.upload(plan, plan.default_assignments(), "", selected={a_id})   # jen uvolnit a.txt
    assert res.restored == ["a.txt"] and not res.lost_versions
    assert (proj / "a.txt").read_text() == "a.txt"
    assert ("restored", "a.txt") in _changes_of(proj)


def test_modified_held_file_deleted_on_server_is_uploaded_and_recorded(tmp_path):
    proj = tmp_path / "server" / "Forma"
    for f in ("a.txt", "b.txt"):
        (proj / f).parent.mkdir(parents=True, exist_ok=True)
        (proj / f).write_text(f)
    pm = ProjectManager(Settings(server_root=str(tmp_path / "server"), local_root=str(tmp_path / "l")))
    pm.activate("Forma", lock_paths=["a.txt", "b.txt"])
    local = pm.local_dir("Forma")
    (local / "a.txt").write_text("a v2")
    (proj / "a.txt").unlink()
    plan = pm.plan_upload("Forma")
    a_id = next(bf.file_id for bf, _ in plan.modified if bf.path == "a.txt")
    res = pm.upload(plan, plan.default_assignments(), "uprava", selected={a_id})
    assert res.restored == ["a.txt"] and res.lost_versions == ["a.txt"]   # původní verze nezálohována
    assert (proj / "a.txt").read_text() == "a v2"
    changes = _changes_of(proj)
    assert changes.index(("external-deleted", "a.txt")) < changes.index(("modified", "a.txt"))
    # obnova ze záloh s ním počítá (soubor existuje)
    latest = pm.restore_points("Forma").points[0]
    assert "a.txt" in {path for path, _ in latest.files.values()} and not latest.missing
