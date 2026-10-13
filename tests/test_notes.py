"""Předávací poznámky: adresář uživatelů, zveřejnění s odevzdáním, rozepsané, stav, souhrn."""
import stat
from pathlib import Path

import pytest

from spm import notes as notes_mod
from spm import users
from spm.config import Settings
from spm.notes import Draft
from spm.sync import ProjectManager


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.chmod(stat.S_IWRITE | stat.S_IREAD)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def env(tmp_path):
    proj = tmp_path / "server" / "Forma"
    write(proj / "dily" / "deska.elt", "deska")
    write(proj / "dily" / "E01.elt", "E01")
    write(proj / "sestava.elt", "sestava")

    def pm(user, nickname=""):
        s = Settings(server_root=str(tmp_path / "server"), local_root=str(tmp_path / f"l_{user}"),
                     nickname=nickname)
        return ProjectManager(s, user=user, pc=f"PC-{user}")

    return pm("alice", "Alča"), pm("Bob"), proj


# --- adresář uživatelů -------------------------------------------------------------------

def test_user_directory(env, tmp_path):
    alice, bob, proj = env
    alice.register_user()
    bob.register_user()
    found = {u.login: u for u in alice.users()}
    assert set(found) == {"alice", "Bob"}
    assert found["alice"].nickname == "Alča" and found["alice"].display == "Alča (alice)"
    assert not users.register(tmp_path / "server", users.UserInfo("alice", "Alča",
                                                                    found["alice"].full_name, "PC-alice"))
    alice.settings.nickname = "Alice N."                       # změna přezdívky se zapíše
    alice.register_user()
    assert {u.login: u for u in alice.users()}["alice"].nickname == "Alice N."
    assert [p.name for p in alice.list_projects()] == ["Forma"]   # adresář není projekt


def test_display_name_prefers_nickname(env):
    alice, bob, proj = env
    assert alice.display_name == "Alča"
    assert bob.display_name                                     # jméno z Windows nebo login


# --- zveřejnění s odevzdáním ------------------------------------------------------------

def submit_with_notes(pm, notes, edit=lambda local: None, end=False):
    local = pm.local_dir("Forma")
    edit(local)
    plan = pm.plan_upload("Forma")
    return pm.upload(plan, plan.default_assignments(), "zmena", end_session=end, notes=notes)


def test_drafts_published_with_submission(env):
    alice, bob, proj = env
    alice.activate("Forma", lock_paths=["dily/deska.elt", "sestava.elt"])
    drafts = [Draft("# Pozor\nDeska je **předběžná**", recipient="bob", recipient_name="Bob",
                    targets=["dily/deska.elt", "dily/"]),
              Draft("Zatím nepublikovat")]
    alice.save_drafts("Forma", drafts)
    assert [d.title for d in alice.drafts("Forma")] == ["Pozor", "Zatím nepublikovat"]

    write(alice.local_dir("Forma") / "dily" / "deska.elt", "deska v2")
    plan = alice.plan_upload("Forma")
    deska = next(bf.file_id for bf, _ in plan.modified)          # sestava.elt zůstane držená
    res = alice.upload(plan, plan.default_assignments(), "zmena", selected={deska}, notes=[drafts[0]])
    assert res.notes_published == 1 and not res.auto_deactivated
    assert [d.text for d in alice.drafts("Forma")] == ["Zatím nepublikovat"]   # zbytek zůstal
    note = bob.notes("Forma")[0]
    assert (note.author, note.recipient, note.targets) == ("Alča", "bob", ["dily/", "dily/deska.elt"])
    assert note.commit_id is not None and not note.resolved
    assert bob.list_projects()[0].notes_for_me == 1             # značka „poznámka pro vás“
    assert alice.list_projects()[0].notes_for_me == 0


def test_note_target_follows_file_rename(env):
    alice, bob, proj = env
    alice.activate("Forma", lock_paths=[])                       # založení evidence souborů
    alice.cancel_activation("Forma")
    assert bob.add_notes("Forma", [Draft("k desce", targets=["dily/deska.elt"])]) == 1
    alice.activate("Forma", lock_paths=["dily/deska.elt", "sestava.elt"])
    alice.rename_local("Forma", "dily/deska.elt", "deska_pevna.elt")
    plan = alice.plan_upload("Forma")
    alice.upload(plan, plan.default_assignments(), "prejmenovani")
    assert bob.notes("Forma")[0].targets == ["dily/deska_pevna.elt"]   # poznámka jde se souborem


def test_notes_without_submission_and_resolving(env):
    alice, bob, proj = env
    assert bob.add_notes("Forma", [Draft("Pro Alici", recipient="ALICE"), Draft("  ")]) == 1
    note = alice.notes("Forma")[0]
    assert note.is_for("alice") and note.commit_id is None
    assert alice.list_projects()[0].notes_for_me == 1
    alice.resolve_note("Forma", note.note_id)
    note = alice.notes("Forma")[0]
    assert note.resolved and note.resolved_by == "Alča"
    assert alice.list_projects()[0].notes_for_me == 0           # vyřízená už se nehlásí
    alice.resolve_note("Forma", note.note_id, resolved=False)
    assert not alice.notes("Forma")[0].resolved


def test_project_without_notes(env):
    alice, bob, proj = env
    assert alice.notes("Forma") == []
    assert notes_mod.read_summary(proj).open == 0
    alice.activate("Forma")
    assert alice.notes("Forma") == []                           # databáze bez tabulek poznámek


def test_deactivation_publishes_notes_before_local_copy_is_deleted(env):
    alice, bob, proj = env
    alice.activate("Forma", lock_paths=["sestava.elt"])
    alice.save_drafts("Forma", [Draft("Hotovo, předávám")])
    res = submit_with_notes(alice, alice.drafts("Forma"), end=True)
    assert res.local_deleted and res.notes_published == 1
    assert bob.notes("Forma")[0].text == "Hotovo, předávám"


def test_unpublished_drafts_survive_deactivation(env):
    """Místní kopie se po deaktivaci smaže – nezveřejněné poznámky se odloží a vrátí při aktivaci."""
    alice, bob, proj = env
    alice.activate("Forma", lock_paths=["sestava.elt"])
    alice.save_drafts("Forma", [Draft("Zveřejnit"), Draft("Rozepsané na příště")])
    res = submit_with_notes(alice, [alice.drafts("Forma")[0]], end=True)
    assert res.local_deleted
    assert not alice.local_dir("Forma").exists()
    assert [p.name for p in alice.list_projects()] == ["Forma"]   # odložené není „Jen místně“
    alice.activate("Forma", lock_paths=[])
    assert [d.text for d in alice.drafts("Forma")] == ["Rozepsané na příště"]
