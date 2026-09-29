import pytest

from spm import db as dbm
from spm.config import Settings
from spm.db import ProjectDB
from spm.lock import read_lock
from spm.sync import ProjectManager, SyncError, validate_project_name


@pytest.fixture
def pm(tmp_path):
    for name in ("Hala", "Sklad"):
        (tmp_path / "server" / name / "vykresy").mkdir(parents=True)
        (tmp_path / "server" / name / "vykresy" / "a.dwg").write_text(name, encoding="utf-8")
    s = Settings(server_root=str(tmp_path / "server"), local_root=str(tmp_path / "local"))
    return ProjectManager(s, user="alice", pc="PC1")


def names(pm):
    return [p.name for p in pm.list_projects()]


def test_rename_moves_folder_and_logs_history(pm):
    pm.activate("Hala")
    pm.deactivate(*_plan(pm, "Hala"), "prace")
    pm.rename_project("Hala", "Hala_Brno_2026")

    assert names(pm) == ["Hala_Brno_2026", "Sklad"]
    new = pm.server_dir("Hala_Brno_2026")
    assert (new / "vykresy" / "a.dwg").exists()
    assert read_lock(new) is None
    with ProjectDB.for_project(new) as db:
        last = db.sessions()[0]
        assert last["status"] == dbm.PROJECT_RENAMED
        assert last["user"] == "alice"
        assert "Hala → Hala_Brno_2026" in last["comment"]
        # historie se přenesla s projektem: sezení práce (aktivace–deaktivace) + přejmenování
        assert [s["status"] for s in db.sessions()] == [dbm.PROJECT_RENAMED, "done"]


def test_rename_creates_db_for_never_activated_project(pm):
    pm.rename_project("Sklad", "Sklad_Ostrava")
    with ProjectDB.for_project(pm.server_dir("Sklad_Ostrava")) as db:
        assert db.sessions()[0]["status"] == dbm.PROJECT_RENAMED


def test_rename_then_activate_works(pm):
    pm.rename_project("Sklad", "Sklad2")
    pm.activate("Sklad2")
    assert (pm.local_dir("Sklad2") / "vykresy" / "a.dwg").exists()


def test_case_only_rename(pm):
    pm.rename_project("Hala", "HALA")
    assert "HALA" in names(pm)


def test_refused_when_active(pm):
    pm.activate("Hala")
    with pytest.raises(SyncError, match="deaktivovaný"):
        pm.rename_project("Hala", "Jiny")
    assert pm.server_dir("Hala").exists()


def test_refused_when_target_exists(pm):
    with pytest.raises(SyncError, match="existuje"):
        pm.rename_project("Hala", "sklad")
    assert read_lock(pm.server_dir("Hala")) is None


def test_refused_when_local_folder_with_new_name_exists(pm):
    pm.local_dir("Novy").mkdir(parents=True)
    with pytest.raises(SyncError, match="místním"):
        pm.rename_project("Hala", "Novy")


@pytest.mark.parametrize("bad", ["", "a/b", "x:y", "tecka.", " mezera", ".skryty", "CON", "nul.txt",
                                 "Hala_puvodni_20260101_120000"])
def test_invalid_names(bad):
    assert validate_project_name(bad)


def test_valid_name():
    assert validate_project_name("Hala_Brno-2026_v2") is None


def _plan(pm, name):
    plan = pm.plan_deactivation(name)
    return plan, plan.default_assignments()
