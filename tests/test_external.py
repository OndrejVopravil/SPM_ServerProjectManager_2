"""Kontrola hashe: změny souborů přímo na síti mimo aplikaci se poznají a nahlásí."""
import os
import stat
import time
from pathlib import Path

import pytest

from spm import db as dbm
from spm.config import Settings
from spm.db import ProjectDB
from spm.sync import ProjectManager


def write(path: Path, text: str, age: float = 0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.chmod(stat.S_IWRITE | stat.S_IREAD)
    path.write_text(text, encoding="utf-8")
    if age:
        t = time.time() - age
        os.utime(path, (t, t))


@pytest.fixture
def env(tmp_path):
    server = tmp_path / "server"
    proj = server / "Forma"
    write(proj / "sestava.elt", "sestava", age=100)
    write(proj / "deska.elt", "deska", age=100)
    write(proj / "zprava.docx", "zprava", age=100)

    def manager(user):
        s = Settings(server_root=str(server), local_root=str(tmp_path / f"l_{user}"))
        return ProjectManager(s, user=user, pc=f"PC-{user}")

    alice, bob = manager("alice"), manager("bob")
    # založení evidence: první aktivace a deaktivace (hashe jsou v databázi)
    alice.activate("Forma")
    plan = alice.plan_upload("Forma")
    alice.deactivate(plan, plan.default_assignments(), "")
    return alice, bob, proj


def external_changes(proj):
    with ProjectDB.for_project(proj) as db:
        return [(c["change_type"], c["new_path"] or c["old_path"]) for c in db.all_changes()
                if c["change_type"].startswith("external")]


def test_first_activation_is_not_reported(tmp_path):
    proj = tmp_path / "server" / "Nova"
    write(proj / "a.txt", "a")
    pm = ProjectManager(Settings(server_root=str(tmp_path / "server"), local_root=str(tmp_path / "l")))
    assert pm.activate("Nova").external == []          # jen načtení do evidence


def test_activation_reports_changes_made_on_network(env):
    alice, bob, proj = env
    write(proj / "deska.elt", "deska prepsana v pruzkumniku")
    (proj / "zprava.docx").unlink()
    write(proj / "cizi.txt", "pridano rucne")
    res = bob.activate("Forma")
    assert sorted(res.external) == ["cizi.txt – přidán mimo aplikaci",
                                    "deska.elt – změněn mimo aplikaci",
                                    "zprava.docx – smazán mimo aplikaci"]
    assert sorted(external_changes(proj)) == [("external-deleted", "zprava.docx"),
                                              ("external-modified", "deska.elt")]


def test_clean_activation_reports_nothing(env):
    alice, bob, proj = env
    assert bob.activate("Forma").external == []


def test_take_detects_change_on_network(env):
    alice, bob, proj = env
    bob.activate("Forma", lock_paths=[])
    write(proj / "deska.elt", "deska prepsana")             # mimo aplikaci, soubor je volný
    res = bob.take("Forma", ["deska.elt"])
    assert res.external == ["deska.elt – změněn mimo aplikaci"]
    assert (bob.local_dir("Forma") / "deska.elt").read_text(encoding="utf-8") == "deska prepsana"
    assert ("external-modified", "deska.elt") in external_changes(proj)
    # zapsáno -> evidence odpovídá, odevzdání nehlásí nic navíc
    plan = bob.plan_upload("Forma")
    assert not plan.conflicts and not plan.modified


def test_refresh_detects_change_of_free_file_once(env):
    alice, bob, proj = env
    bob.activate("Forma", lock_paths=[])
    # alice soubor převezme a odevzdá – legitimní změna, nehlásí se
    alice.activate("Forma", lock_paths=["sestava.elt"])
    write(alice.local_dir("Forma") / "sestava.elt", "sestava v2")
    plan = alice.plan_upload("Forma")
    alice.upload(plan, plan.default_assignments(), "v2")
    res = bob.refresh("Forma")
    assert res.downloaded == ["sestava.elt"] and res.external == []
    # někdo změní volný soubor přímo na síti; Aktualizovat to pozná jen, když se soubor stahuje
    # (obsah se čte jen při stažení) – tady ho stáhne až převzetí
    write(proj / "zprava.docx", "zprava prepsana")
    assert bob.refresh("Forma").external == []
    assert bob.take("Forma", ["zprava.docx"]).external == ["zprava.docx – změněn mimo aplikaci"]


def test_refresh_warns_about_file_held_by_other_without_touching_db(env):
    alice, bob, proj = env
    # alice drží dva soubory – odevzdáním jednoho se projekt automaticky nedeaktivuje
    alice.activate("Forma", lock_paths=["deska.elt", "sestava.elt"])
    bob.activate("Forma", lock_paths=[], only_locked=False)
    # alice odevzdá deska.elt, ale během toho ji někdo na síti přepíše (mimo aplikaci)
    write(alice.local_dir("Forma") / "deska.elt", "deska od alice")
    plan = alice.plan_upload("Forma")
    fid = plan.modified[0][0].file_id
    alice.upload(plan, plan.default_assignments(), "deska", selected={fid})
    alice.take("Forma", ["deska.elt"])                         # alice ji zase drží
    write(proj / "deska.elt", "prepsano na siti")
    res = bob.refresh("Forma")
    assert res.external == ["deska.elt – změněn mimo aplikaci (soubor drží alice (PC-alice))"]
    with ProjectDB.for_project(proj) as db:                     # evidence se nezměnila
        assert db.active_files()["deska.elt"].sha256 != db.active_files()["sestava.elt"].sha256
        kinds = [c["change_type"] for c in db.all_changes() if c["new_path"] == "deska.elt"]
    assert dbm.EXTERNAL_MODIFIED not in kinds
    assert bob.refresh("Forma").external == []                  # podruhé už nehlásí
    # alice při odevzdání dostane varování (evidence nezměněna)
    write(alice.local_dir("Forma") / "deska.elt", "deska od alice v2")
    assert any("mimo aplikaci" in c for c in alice.plan_upload("Forma").conflicts)
