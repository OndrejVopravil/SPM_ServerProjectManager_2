"""Kontrola jádra: evidence (hash v databázi) vždy odpovídá souborům na serveru, zálohy obsahují
předchozí verze, přerušené nahrání nepřijde o soubor, zámek zápisu nezávisí na hodinách PC,
zrcadlení snese chybějící kopii v cíli."""
import os
import stat
import time
from pathlib import Path

import pytest

from spm import db as dbm
from spm import lock as lock_mod
from spm.config import Settings
from spm.db import ProjectDB
from spm.lock import WriteLock
from spm.mirror import Mirror, MirrorItem, MirrorSource
from spm.restore import restore, restore_points
from spm.scanner import file_hash, to_abs
from spm.sync import ProjectManager, SyncError


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.chmod(stat.S_IWRITE | stat.S_IREAD)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def env(tmp_path):
    server = tmp_path / "server"
    proj = server / "P"
    write(proj / "a.txt", "a1")
    write(proj / "dir" / "b.txt", "b1")

    def pm(user: str) -> ProjectManager:
        s = Settings(server_root=str(server), local_root=str(tmp_path / f"local_{user}"), backup_count=5)
        return ProjectManager(s, user=user, pc=f"PC-{user}")
    return pm("alice"), pm("bob"), proj


def check_integrity(proj: Path) -> None:
    """Invarianty evidence: hash a velikost aktivních souborů = skutečnost na serveru; každá
    záloha z historie, která ještě existuje, obsahuje verzi souboru před danou změnou."""
    with ProjectDB.for_project(proj) as db:
        files = db.active_files()
        changes = db.all_changes()
    for path, f in files.items():
        real = to_abs(proj, path)
        assert real.is_file(), f"{path} chybí na serveru"
        assert file_hash(real) == f.sha256, f"{path}: hash v evidenci neodpovídá souboru na serveru"
        assert real.stat().st_size == f.size
    previous: dict[str, str] = {}
    for ch in changes:
        fid = ch["file_id"]
        if ch["backup_path"] and fid in previous and ch["change_type"] != dbm.DELETED:
            backup = to_abs(proj, ch["backup_path"])
            if backup.exists():
                assert file_hash(backup) == previous[fid], f"záloha {ch['backup_path']} má jiný obsah"
        if ch["change_type"] in (dbm.DELETED, dbm.EXTERNAL_DELETED):
            previous.pop(fid, None)
        elif ch["sha256"]:
            previous[fid] = ch["sha256"]


def submit(pm, comment="zmena", end=False):
    plan = pm.plan_upload("P")
    return pm.upload(plan, plan.default_assignments(), comment, end_session=end)


def test_workflow_keeps_evidence_and_backups_consistent(env):
    alice, bob, proj = env
    local = alice.local_dir("P")
    for i in range(2, 5):
        alice.activate("P")
        write(local / "a.txt", f"a{i}")
        write(local / "dir" / "b.txt", f"b{i}")
        submit(alice, f"verze {i}", end=True)
        check_integrity(proj)
    alice.activate("P")
    os.rename(local / "dir" / "b.txt", local / "dir" / "c.txt")           # přejmenování
    write(local / "novy.txt", "n")
    submit(alice, "prejmenovani", end=True)
    check_integrity(proj)
    # druhý uživatel stáhne přesně evidovaný obsah
    bob.activate("P")
    for path, f in ProjectDB.for_project(proj).active_files().items():
        assert file_hash(to_abs(bob.local_dir("P"), path)) == f.sha256
    # obnova k nejstaršímu stavu – každý soubor ověřen hashem
    catalog = restore_points(proj, "P")
    oldest = catalog.points[-1]
    res = restore(proj, bob.local_root, catalog, oldest)
    assert not res.failed and (res.folder / "a.txt").read_text(encoding="utf-8") == "a1"


def test_file_changed_after_plan_is_not_uploaded_with_wrong_hash(env):
    """Soubor se změní mezi porovnáním a nahráním (např. ho aplikace zrovna ukládá): na server
    se nesmí dostat obsah, který neodpovídá evidenci."""
    alice, bob, proj = env
    alice.activate("P")
    local = alice.local_dir("P")
    write(local / "a.txt", "a2")
    plan = alice.plan_upload("P")
    write(local / "a.txt", "a3 – uloženo až po porovnání")
    with pytest.raises(SyncError, match="změnil"):
        alice.upload(plan, plan.default_assignments(), "zmena")
    assert (proj / "a.txt").read_text(encoding="utf-8") == "a1"           # server beze změny
    check_integrity(proj)
    submit(alice, "znovu")                                                 # nový pokus projde
    assert (proj / "a.txt").read_text(encoding="utf-8").startswith("a3")
    check_integrity(proj)


def test_interrupted_copy_keeps_current_file_on_server(env, monkeypatch):
    """Výpadek během kopírování na server: aktuální soubor na serveru zůstane (záloha se dělá
    až po úspěšném nahrání dočasné kopie)."""
    from spm import sync as sync_mod
    alice, bob, proj = env
    alice.activate("P")
    write(alice.local_dir("P") / "a.txt", "a2")
    plan = alice.plan_upload("P")
    real = sync_mod.copy_with_hash

    def broken(src, dst, *a, **k):
        if Path(dst).parent == proj or proj in Path(dst).parents:
            raise OSError("síť nedostupná")
        return real(src, dst, *a, **k)
    monkeypatch.setattr(sync_mod, "copy_with_hash", broken)
    with pytest.raises(OSError):
        alice.upload(plan, plan.default_assignments(), "zmena")
    assert (proj / "a.txt").read_text(encoding="utf-8") == "a1"
    monkeypatch.setattr(sync_mod, "copy_with_hash", real)
    submit(alice, "znovu")                                                 # opakování projde
    assert (proj / "a.txt").read_text(encoding="utf-8") == "a2"
    check_integrity(proj)


def test_write_lock_staleness_uses_server_clock(tmp_path, monkeypatch):
    """Stáří zámku zápisu se měří hodinami serveru (čas souboru), ne hodinami PC – PC s hodinami
    napřed nesmí brát cizí platný zámek."""
    proj = tmp_path / "P"
    lock = WriteLock(proj, "bob", "PC-bob", timeout=0.5)
    lock.__enter__()                                                       # bob drží zámek
    try:
        future = time.time() + 600                                         # alice má hodiny o 10 min napřed
        monkeypatch.setattr(lock_mod.time, "time", lambda: future)
        with pytest.raises(lock_mod.LockError):
            with WriteLock(proj, "alice", "PC-alice", timeout=0.5):
                pass
    finally:
        monkeypatch.undo()
        lock.__exit__(None, None, None)
    # skutečně starý zámek (spadlá aplikace) se uvolní
    WriteLock(proj, "bob", "PC-bob").__enter__()
    old = time.time() - 3600
    os.utime(proj / ".spm" / "write.lock", (old, old))
    with WriteLock(proj, "alice", "PC-alice", timeout=1):
        pass


def test_mirror_rename_when_target_copy_missing(tmp_path):
    src_dir = tmp_path / "src"
    write(src_dir / "a.txt", "obsah")
    target_pm = ProjectManager(Settings(server_root=str(tmp_path / "w"), local_root=str(tmp_path / "lw")),
                               user="alice", pc="PC")
    (tmp_path / "w").mkdir()
    source = MirrorSource("src-key", str(src_dir))
    sha = file_hash(src_dir / "a.txt")
    Mirror(source, target_pm, "P").run([MirrorItem("s1", "added", "a.txt", sha, src_dir / "a.txt", "a.txt")])
    target = tmp_path / "w" / "P"
    assert (target / "a.txt").exists()
    (target / "a.txt").unlink()                                            # kopie v cíli zmizela
    res = Mirror(source, target_pm, "P").run(
        [MirrorItem("s1", "renamed", "b.txt", sha, src_dir / "a.txt", "b.txt")])
    assert res.ok and (target / "b.txt").read_text(encoding="utf-8") == "obsah"
    check_integrity(target)


def test_content_changed_during_copy_is_detected_by_hash(env):
    """Obsah se změní, ale velikost i čas změny zůstanou (předběžná kontrola ho nepozná):
    nahrání ho odhalí hashem dočasné kopie a server zůstane beze změny."""
    alice, bob, proj = env
    alice.activate("P")
    local = alice.local_dir("P")
    write(local / "a.txt", "a2")
    plan = alice.plan_upload("P")
    lf = plan.local_files["a.txt"]
    write(local / "a.txt", "a9")                                           # stejná délka
    os.utime(local / "a.txt", (lf.mtime, lf.mtime))
    with pytest.raises(SyncError, match="změnil"):
        alice.upload(plan, plan.default_assignments(), "zmena")
    assert (proj / "a.txt").read_text(encoding="utf-8") == "a1"
    assert not list(proj.rglob("*.spmtmp"))                                 # žádné dočasné kopie
    check_integrity(proj)
