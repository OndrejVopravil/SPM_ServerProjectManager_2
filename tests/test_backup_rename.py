"""Zálohy a obnova při přejmenování souborů a složek (i jen velikosti písmen), se zrcadlením."""
import os
import shutil
import stat
from pathlib import Path

import pytest

from spm.config import Settings
from spm.scripting.model import TabInfo
from spm.scripting.runtime import ScriptEnv, run_scripts
from spm.sync import ProjectManager


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.chmod(stat.S_IWRITE | stat.S_IREAD)
    path.write_text(text, encoding="utf-8")


def files(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()) \
        if root.exists() else []


def dirs_on_disk(root: Path) -> list[str]:
    """Skutečné názvy složek (s velikostí písmen, jak jsou na disku)."""
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*")
                  if p.is_dir() and ".spm" not in p.relative_to(root).parts)


@pytest.fixture
def env(tmp_path):
    o, w = tmp_path / "O", tmp_path / "W"
    write(o / "P" / "A" / "x.nc", "x v1")
    write(o / "P" / "A" / "y.nc", "y v1")
    w.mkdir()

    def pm(root, user, keep=3):
        return ProjectManager(Settings(server_root=str(root), local_root=str(tmp_path / f"l_{root.name}_{user}"),
                                       backup_count=keep), user=user, pc=f"PC-{user}")
    alice, alice_w, bob = pm(o, "alice"), pm(w, "alice"), pm(o, "bob")
    alice.activate("P")
    plan = alice.plan_upload("P")
    alice.deactivate(plan, plan.default_assignments(), "")
    tabs = [TabInfo("o", "O", "", str(o), alice.settings.local_root),
            TabInfo("w", "W", "", str(w), alice_w.settings.local_root)]

    def submit(edit, comment="zmena", mirror=True):
        alice.activate("P")
        edit(alice.local_dir("P"))
        plan = alice.plan_upload("P")
        res = alice.deactivate(plan, plan.default_assignments(), comment)
        if mirror and res.script_files:
            env_ = ScriptEnv(alice, "P", "deactivate", res.script_files, comment, res.commit_id, tabs[0],
                             tabs, lambda t: {"o": alice, "w": alice_w}[t.ws_id])
            report = run_scripts(env_, [("m.spm", 'MIRROR commit "*.nc" TO "{TAB:W}"')])
            assert report.ok, report.text
        return res

    class E:
        pass
    e = E()
    e.o, e.w, e.alice, e.bob, e.submit = o / "P", w / "P", alice, bob, submit
    return e


def backups(project: Path) -> list[str]:
    return files(project / ".spm" / "backup")


def test_backups_follow_file_across_folder_renames(env):
    env.submit(lambda l: write(l / "A/x.nc", "x v2"))
    env.submit(lambda l: write(l / "A/x.nc", "x v3"))
    assert backups(env.o) == ["A/x.nc.backup01", "A/x.nc.backup02"]
    res = env.submit(lambda l: (l / "A").rename(l / "B"))              # jen složka
    assert {f.status for f in res.script_files} == {"renamed"}
    assert backups(env.o) == ["A/x.nc.backup01", "A/x.nc.backup02"]    # staré zálohy pod původní cestou
    env.submit(lambda l: ((l / "B").rename(l / "C"), write(l / "C/x.nc", "x v4")))   # složka + změna
    assert backups(env.o) == ["A/x.nc.backup01", "A/x.nc.backup02", "C/x.nc.backup01"]
    env.submit(lambda l: write(l / "C/x.nc", "x v5"))                  # limit 3 – nejstarší z A pryč
    assert backups(env.o) == ["A/x.nc.backup02", "C/x.nc.backup01", "C/x.nc.backup02"]
    env.submit(lambda l: write(l / "C/x.nc", "x v6"))
    assert backups(env.o) == ["C/x.nc.backup01", "C/x.nc.backup02", "C/x.nc.backup03"]
    assert not (env.o / ".spm" / "backup" / "A").exists()              # prázdná složka zanikla
    # zrcadlo: stejně podle identity souboru v cíli
    assert backups(env.w) == ["C/x.nc.backup01", "C/x.nc.backup02", "C/x.nc.backup03"]
    assert not (env.w / ".spm" / "backup" / "A").exists()
    assert [p for p in files(env.w) if not p.startswith(".spm")] == ["C/x.nc", "C/y.nc"]


def test_new_file_with_old_name_does_not_rotate_foreign_backups(env):
    env.submit(lambda l: write(l / "A/x.nc", "x v2"))
    env.submit(lambda l: write(l / "A/x.nc", "x v3"))
    env.submit(lambda l: (l / "A").rename(l / "B"))                    # původní x je teď B/x.nc
    env.submit(lambda l: write(l / "A/x.nc", "jiny x 1"))              # nový soubor se starým názvem
    for v in (2, 3, 4):
        env.submit(lambda l, v=v: write(l / "A/x.nc", f"jiny x {v}"), mirror=False)
    names = backups(env.o)
    # 2 zálohy původního x zůstaly; nový A/x.nc má vlastní 3 (číslování navazuje, nic se nepřepíše)
    assert names == ["A/x.nc.backup01", "A/x.nc.backup02", "A/x.nc.backup03", "A/x.nc.backup04",
                     "A/x.nc.backup05"]
    contents = [(env.o / ".spm" / "backup" / n).read_text(encoding="utf-8") for n in names]
    assert contents == ["x v1", "x v2", "jiny x 1", "jiny x 2", "jiny x 3"]


def test_case_only_folder_rename_is_rename_not_delete(env):
    env.submit(lambda l: write(l / "A/x.nc", "x v2"))
    res = env.submit(lambda l: (l / "A").rename(l / "a"))
    assert sorted((f.path, f.status, f.old_path) for f in res.script_files) == [
        ("a/x.nc", "renamed", "A/x.nc"), ("a/y.nc", "renamed", "A/y.nc")]
    assert dirs_on_disk(env.o) == ["a"] and dirs_on_disk(env.w) == ["a"]   # i složka na disku
    assert (env.o / "a" / "x.nc").read_text(encoding="utf-8") == "x v2"
    assert not list((env.o / ".spm").glob("trash/**/*.nc"))                # nic do koše
    # další změna: záloha pod novým názvem, starší zůstala
    env.submit(lambda l: write(l / "a/x.nc", "x v3"))
    # složka záloh se liší jen velikostí písmen – na Windows je to tatáž složka, číslování navazuje
    assert sorted(b.lower() for b in backups(env.o)) == ["a/x.nc.backup01", "a/x.nc.backup02"]


def test_case_only_file_rename(env):
    res = env.submit(lambda l: (l / "A/x.nc").rename(l / "A/X.nc"))
    assert [(f.path, f.status) for f in res.script_files if f.status != "unchanged"] == [("A/X.nc", "renamed")]
    assert sorted(os.listdir(env.o / "A")) == ["X.nc", "y.nc"]
    assert sorted(os.listdir(env.w / "A")) == ["X.nc", "y.nc"]


def test_other_user_refresh_follows_case_rename(env):
    env.bob.activate("P", lock_paths=[])
    env.submit(lambda l: (l / "A").rename(l / "a"), mirror=False)
    res = env.bob.refresh("P")
    assert not res.conflicts and sorted(res.moved) == ["A/x.nc → a/x.nc", "A/y.nc → a/y.nc"]
    assert dirs_on_disk(env.bob.local_dir("P")) == ["a"]


def test_restore_after_folder_renames(env):
    env.submit(lambda l: write(l / "A/x.nc", "x v2"))
    env.submit(lambda l: (l / "A").rename(l / "B"))
    env.submit(lambda l: ((l / "B").rename(l / "a"), write(l / "a/x.nc", "x v3")))
    catalog = env.alice.restore_points("P")
    assert all(p.complete for p in catalog.points)

    def restored(point) -> dict[str, str]:
        result = env.alice.restore(catalog, point)
        assert not result.failed
        return {p: (result.folder / p).read_text(encoding="utf-8") for p in files(result.folder)}
    assert restored(catalog.points[-1]) == {"A/x.nc": "x v1", "A/y.nc": "y v1"}   # před změnami
    assert [restored(p) for p in catalog.points[:3]] == [
        {"a/x.nc": "x v3", "a/y.nc": "y v1"},     # složka B -> a + změna
        {"B/x.nc": "x v2", "B/y.nc": "y v1"},     # jen přejmenování A -> B
        {"A/x.nc": "x v2", "A/y.nc": "y v1"},     # změna x
    ]


# --- přejmenování složky v aplikaci (rename_local_folder) ------------------------------------

def test_rename_local_folder_and_submit_keeps_history(env):
    from spm.sync import SyncError
    env.submit(lambda l: write(l / "A/x.nc", "x v2"))
    alice = env.alice
    alice.activate("P")
    assert alice.rename_local_folder("P", "A/", "B") == "B"
    assert alice.local_renames("P") == {"A/x.nc": "B/x.nc", "A/y.nc": "B/y.nc"}
    write(alice.local_dir("P") / "B/x.nc", "x v3")                  # i se změnou obsahu
    plan = alice.plan_upload("P")
    res = alice.deactivate(plan, plan.default_assignments(), "prejmenovani")
    assert sorted((f.path, f.status, f.old_path) for f in res.script_files) == [
        ("B/x.nc", "renamed", "A/x.nc"), ("B/y.nc", "renamed", "A/y.nc")]
    assert files(env.o) == [p for p in files(env.o) if p.startswith(".spm")] + ["B/x.nc", "B/y.nc"]
    assert backups(env.o) == ["A/x.nc.backup01", "B/x.nc.backup01"]
    with pytest.raises(SyncError, match="neexistuje"):
        alice.rename_local_folder("P", "B/", "C")                   # projekt už není aktivovaný


def test_rename_local_folder_conditions(env):
    from spm.sync import SyncError
    write(env.o / "Z" / "q.nc", "q")
    write(env.o / "C" / "cizi.nc", "c")                             # při aktivaci se převezme do evidence
    env.submit(lambda l: write(l / "Z/sub/n.nc", "n"), mirror=False)     # Z/q.nc, Z/sub/n.nc
    env.bob.activate("P", lock_paths=["Z/sub/n.nc"])
    env.alice.activate("P", lock_paths=["A/x.nc", "A/y.nc", "Z/q.nc"])
    alice = env.alice
    with pytest.raises(SyncError, match="(?s)všechny soubory.*Z/sub/n.nc"):
        alice.rename_local_folder("P", "Z", "Y")                    # podsložka obsahuje cizí soubor
    with pytest.raises(SyncError, match="nepovolené znaky"):
        alice.rename_local_folder("P", "A", "Díly")
    with pytest.raises(SyncError, match="už existuje"):
        alice.rename_local_folder("P", "A", "Z")
    (alice.local_dir("P") / "C" / "cizi.nc").chmod(stat.S_IWRITE | stat.S_IREAD)
    shutil.rmtree(alice.local_dir("P") / "C")                       # místně není, na serveru ano
    with pytest.raises(SyncError, match="Na serveru už je složka"):
        alice.rename_local_folder("P", "A", "C")
    assert alice.rename_local_folder("P", "A", "a") == "a"          # jen velikost písmen
    assert sorted(os.listdir(alice.local_dir("P"))) == [".spm", "Z", "a"]
