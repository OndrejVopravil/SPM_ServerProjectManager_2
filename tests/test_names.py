import pytest

from spm.config import Settings
from spm.names import find_invalid_names, forbidden_in, invalid_rel_paths
from spm.sync import ProjectManager, SyncError, validate_project_name


@pytest.mark.parametrize("ch", list("áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ") + list(" ()&,.;=^"))
def test_every_forbidden_char_rejected_in_project_name(ch):
    assert validate_project_name(f"Hala{ch}Brno")


def test_allowed_characters():
    assert validate_project_name("Hala_Brno-2026_v2") is None
    assert forbidden_in("abc_XYZ-09+#@!") == []


def test_extension_dot_allowed_but_not_in_stem_or_folder():
    paths = ["vykresy/pudorys.dwg", "vykresy/pudorys.v2.dwg", "foto 1.jpg",
             "Zprávy/zprava.docx", "Zprávy/sub/b.txt", "a.b/c.txt"]
    assert invalid_rel_paths(paths) == ["a.b/", "foto 1.jpg", "vykresy/pudorys.v2.dwg", "Zprávy/"]


def test_find_invalid_names_on_disk(tmp_path):
    (tmp_path / "ok" / "Špatná složka").mkdir(parents=True)
    (tmp_path / "ok" / "dobry.txt").write_text("x")
    (tmp_path / "ok" / "zlý(1).txt").write_text("x")
    (tmp_path / "ok" / "~$dočasný.docx").write_text("x")      # ignorovaný soubor
    (tmp_path / ".spm").mkdir()
    (tmp_path / ".spm" / "přehled.db").write_text("x")        # servisní složka se nekontroluje
    assert set(find_invalid_names(tmp_path, ["~$*"])) == {"ok/Špatná složka/", "ok/zlý(1).txt"}


@pytest.fixture
def pm(tmp_path):
    proj = tmp_path / "server" / "Projekt"
    proj.mkdir(parents=True)
    (proj / "dobry.txt").write_text("x")
    s = Settings(server_root=str(tmp_path / "server"), local_root=str(tmp_path / "local"))
    return ProjectManager(s, user="alice", pc="PC1")


def test_activation_blocked_until_fixed(pm):
    bad = pm.server_dir("Projekt") / "výkres.dwg"
    bad.write_text("x")
    with pytest.raises(SyncError, match="výkres.dwg"):
        pm.activate("Projekt")
    assert not pm.local_dir("Projekt").exists()
    [status] = pm.list_projects()
    assert not status.active and pm.invalid_names(status) == ["výkres.dwg"]

    bad.rename(bad.with_name("vykres.dwg"))
    pm.activate("Projekt")


def test_deactivation_blocked_with_invalid_local_names(pm):
    pm.activate("Projekt")
    (pm.local_dir("Projekt") / "nová verze.txt").write_text("x")
    plan = pm.plan_deactivation("Projekt")
    assert plan.invalid_names == ["nová verze.txt"]
    with pytest.raises(SyncError, match="nová verze.txt"):
        pm.deactivate(plan, plan.default_assignments(), "komentar")

    (pm.local_dir("Projekt") / "nová verze.txt").rename(pm.local_dir("Projekt") / "nova_verze.txt")
    plan = pm.plan_deactivation("Projekt")
    pm.deactivate(plan, plan.default_assignments(), "komentar")


def test_publish_blocked(pm):
    local = pm.local_dir("Novy")
    local.mkdir(parents=True)
    (local / "a b.txt").write_text("x")
    with pytest.raises(SyncError):
        pm.publish("Novy", "komentar")
    assert not pm.server_dir("Novy").exists()
