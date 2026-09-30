from pathlib import Path

import pytest

from spm import db as dbm
from spm.config import Settings
from spm.db import ProjectDB
from spm.lock import active_users
from spm.sync import ProjectManager, SyncError


@pytest.fixture
def pm(tmp_path):
    (tmp_path / "server" / "Existujici").mkdir(parents=True)
    local = tmp_path / "local"
    novy = local / "Novy projekt"
    (novy / "vykresy").mkdir(parents=True)
    (novy / "vykresy" / "pudorys.dwg").write_text("pudorys", encoding="utf-8")
    (novy / "zprava.docx").write_text("zprava", encoding="utf-8")
    (novy / "~$zprava.docx").write_text("docasny soubor Wordu", encoding="utf-8")
    (local / "Existujici").mkdir()                       # místní kopie serverového projektu
    (local / "Existujici_puvodni_20260101_120000").mkdir()  # odložená kopie z aktivace
    s = Settings(server_root=str(tmp_path / "server"), local_root=str(local))
    return ProjectManager(s, user="alice", pc="PC1")


def statuses(pm):
    return {p.name: p for p in pm.list_projects()}


def test_local_only_projects_listed(pm):
    st = statuses(pm)
    assert st["Novy projekt"].local_only
    assert not st["Existujici"].local_only
    assert "Existujici_puvodni_20260101_120000" not in st


def test_publish_moves_project_to_server(pm):
    assert pm.publish("Novy projekt", "zalozeni projektu")
    server = pm.server_dir("Novy projekt")
    assert (server / "vykresy" / "pudorys.dwg").read_text(encoding="utf-8") == "pudorys"
    assert not (server / "~$zprava.docx").exists()   # ignorované soubory se nenahrávají
    assert not pm.local_dir("Novy projekt").exists()
    assert active_users(server) == []

    st = statuses(pm)["Novy projekt"]
    assert not st.local_only and not st.active

    with ProjectDB.for_project(server) as db:
        assert set(db.active_files()) == {"vykresy/pudorys.dwg", "zprava.docx"}
        [commit] = db.commits()
        assert (commit["user"], commit["comment"], commit["kind"]) == ("alice", "zalozeni projektu", dbm.PUBLISH)
        assert {c["change_type"] for c in db.changes(commit["commit_id"])} == {dbm.PUBLISHED}


def test_published_project_can_be_activated(pm):
    pm.publish("Novy projekt", "zalozeni")
    pm.activate("Novy projekt")
    plan = pm.plan_deactivation("Novy projekt")
    assert not plan.has_changes


def test_comment_required(pm):
    with pytest.raises(SyncError):
        pm.publish("Novy projekt", "  ")
    assert not pm.server_dir("Novy projekt").exists()
    assert pm.local_dir("Novy projekt").exists()


def test_refuses_when_server_project_exists(pm, tmp_path):
    pm.server_dir("Novy projekt").mkdir()
    with pytest.raises(SyncError):
        pm.publish("Novy projekt", "x")
    assert pm.local_dir("Novy projekt").exists()


def test_failed_upload_keeps_local_and_cleans_server(pm, monkeypatch):
    import spm.sync as sync

    calls = {"n": 0}
    real = sync.copy_with_hash

    def flaky(src: Path, dst: Path) -> str:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("sit spadla")
        return real(src, dst)

    monkeypatch.setattr(sync, "copy_with_hash", flaky)
    with pytest.raises(OSError):
        pm.publish("Novy projekt", "x")
    assert not pm.server_dir("Novy projekt").exists()
    assert (pm.local_dir("Novy projekt") / "zprava.docx").exists()
