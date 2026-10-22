"""Archivace dokončených projektů (zip s úklidem) a obnova z archivu."""
import json
import stat
import zipfile
from pathlib import Path

import pytest

from spm import archive
from spm import db as dbm
from spm.archive import ArchiveError
from spm.config import Settings
from spm.db import ProjectDB
from spm.notes import Draft
from spm.sync import ProjectManager, SyncError


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.chmod(stat.S_IWRITE | stat.S_IREAD)
    path.write_text(text, encoding="utf-8")


def manager(tmp_path, user="alice", archivers=("alice",)):
    s = Settings(server_root=str(tmp_path / "server"), local_root=str(tmp_path / f"l_{user}"),
                 backup_count=10, archive_root="..\\Archiv", archive_keep_backups=2,
                 archivers=list(archivers))
    return ProjectManager(s, user=user, pc=f"PC-{user}")


@pytest.fixture
def env(tmp_path):
    proj = tmp_path / "server" / "Forma"
    write(proj / "deska.elt", "d0")
    write(proj / "kolik.elt", "k0")
    alice = manager(tmp_path)
    for i in range(1, 5):                                        # 4 zálohy desky
        alice.activate("Forma")
        write(alice.local_dir("Forma") / "deska.elt", f"d{i}")
        if i == 4:
            (alice.local_dir("Forma") / "kolik.elt").unlink()    # kolík do koše
        plan = alice.plan_upload("Forma")
        alice.deactivate(plan, plan.default_assignments(), f"v{i}")
    alice.set_comment("Forma", "Hotová forma")
    return alice, proj, tmp_path


def test_permission_and_checks(env, tmp_path):
    alice, proj, _ = env
    assert archive.archive_dir(alice.settings) == tmp_path / "Archiv"     # vzhledem k síťové složce
    assert archive.archive_problems(alice, "Forma") == []
    bob = manager(tmp_path, "bob")
    assert "smí jen: alice" in archive.archive_problems(bob, "Forma")[0]
    nobody = manager(tmp_path, "alice", archivers=())
    assert "nesmí nikdo" in archive.archive_problems(nobody, "Forma")[0]  # prázdný seznam = nikdo
    bob.activate("Forma", lock_paths=[])
    assert any("aktivovaný" in p for p in archive.archive_problems(alice, "Forma"))
    bob.cancel_activation("Forma")
    alice.add_notes("Forma", [Draft("ještě zkontrolovat")])
    assert any("nevyřízené poznámky (1)" in p for p in archive.archive_problems(alice, "Forma"))
    alice.resolve_note("Forma", alice.notes("Forma")[0].note_id)
    assert archive.archive_problems(alice, "Forma") == []


def test_archive_and_restore(env, tmp_path):
    alice, proj, _ = env
    res = archive.archive_project(alice, "Forma", "zakázka předána")
    assert res.zip_path == tmp_path / "Archiv" / "Forma.zip" and not proj.exists() and not res.leftover
    with zipfile.ZipFile(res.zip_path) as zf:
        names = zf.namelist()
        info = json.loads(zf.read(archive.ARCHIVE_INFO))
    assert "deska.elt" in names and ".spm/project.db" in names
    assert sorted(n for n in names if "/backup/" in n) == [".spm/backup/deska.elt.backup03",
                                                          ".spm/backup/deska.elt.backup04"]   # jen 2
    assert not any("/trash/" in n or "/active/" in n for n in names)
    assert info["project"] == "Forma" and info["comment"] == "zakázka předána"
    assert info["project_comment"] == "Hotová forma" and info["files"] == 1
    # seznam archivu a náhled obsahu
    (entry,) = archive.list_archives(tmp_path / "Archiv")
    assert entry.project == "Forma" and entry.archived_by and entry.restores == []
    assert archive.archive_files(res.zip_path) == ["deska.elt"]
    assert archive.is_archived(alice.settings, "Forma") and not archive.is_archived(alice.settings, "Jina")
    # obnova
    path = archive.restore_archive(alice, res.zip_path)
    assert path == proj and (proj / "deska.elt").read_text(encoding="utf-8") == "d4"
    assert res.zip_path.exists()                                            # zip zůstává
    with ProjectDB.for_project(proj) as db:
        kinds = [c["kind"] for c in db.commits()]
    assert kinds[:2] == [dbm.UNARCHIVED, dbm.ARCHIVED]                      # historie pokračuje
    assert alice.restore_points("Forma").points[0].complete                 # aktuální stav obnovitelný
    (entry,) = archive.list_archives(tmp_path / "Archiv")
    assert entry.restores[0]["as"] == "Forma"
    # tentýž projekt (stejná identita) podruhé obnovit nejde – ani pod jiným názvem
    assert archive.existing_identity(alice, res.zip_path) == "Forma"
    with pytest.raises(ArchiveError, match="už v pracovní složce je"):
        archive.restore_archive(alice, res.zip_path)
    with pytest.raises(ArchiveError, match="už v pracovní složce je"):
        archive.restore_archive(alice, res.zip_path, "Forma_obnova")
    assert not (tmp_path / "server" / "Forma_obnova").exists()
    alice.activate("Forma")                                                 # obnovený projekt funguje
    assert (alice.local_dir("Forma") / "deska.elt").read_text(encoding="utf-8") == "d4"
    alice.cancel_activation("Forma")
    (tmp_path / "server" / "Forma").rename(tmp_path / "server" / "Forma2")   # přejmenovaný
    with pytest.raises(ArchiveError, match="jako Forma2"):
        archive.restore_archive(alice, res.zip_path)


def test_restore_other_project_with_same_name(env, tmp_path):
    alice, proj, _ = env
    res = archive.archive_project(alice, "Forma")
    write(proj / "jiny.elt", "x")                                           # jiný projekt, stejný název
    alice.activate("Forma")
    alice.cancel_activation("Forma")
    assert archive.existing_identity(alice, res.zip_path) is None
    with pytest.raises(ArchiveError, match="už projekt Forma existuje"):
        archive.restore_archive(alice, res.zip_path)
    assert archive.restore_archive(alice, res.zip_path, "Forma_obnova").name == "Forma_obnova"


def test_second_archive_and_failed_verification(env, tmp_path, monkeypatch):
    alice, proj, _ = env
    write(tmp_path / "Archiv" / "Forma.zip", "jiny zip")                    # název je obsazený
    monkeypatch.setattr(archive, "_verify", lambda *a: (_ for _ in ()).throw(ArchiveError("poškozený")))
    with pytest.raises(ArchiveError, match="poškozený"):
        archive.archive_project(alice, "Forma")
    assert (proj / "deska.elt").exists()                                    # projekt nedotčený
    assert not (proj / ".spm" / archive.MARKER).exists()
    with ProjectDB.for_project(proj) as db:
        assert dbm.ARCHIVED not in [c["kind"] for c in db.commits()]        # záznam se vrátil
    assert [p.name for p in (tmp_path / "Archiv").iterdir()] == ["Forma.zip"]   # bez dočasných
    monkeypatch.undo()
    res = archive.archive_project(alice, "Forma")
    assert res.zip_path.name.startswith("Forma_") and res.zip_path.name != "Forma.zip"


def test_failed_history_write_does_not_block_project(env, monkeypatch):
    alice, proj, _ = env

    def broken(*a, **k):
        raise OSError("síť vypadla")
    monkeypatch.setattr(ProjectDB, "add_commit", broken)
    with pytest.raises(OSError, match="síť vypadla"):
        archive.archive_project(alice, "Forma")
    assert not (proj / ".spm" / archive.MARKER).exists()                   # projekt není zablokovaný
    assert (proj / "deska.elt").exists()
    monkeypatch.undo()
    alice.activate("Forma")


def test_foreign_marker_is_kept(env, monkeypatch):
    alice, proj, _ = env
    monkeypatch.setattr(archive, "archive_problems", lambda *a: [])          # značka vznikla po kontrole
    write(proj / ".spm" / archive.MARKER, '{"user": "bob"}')
    with pytest.raises(ArchiveError, match="někdo jiný"):
        archive.archive_project(alice, "Forma")
    assert (proj / ".spm" / archive.MARKER).exists()                       # cizí značka zůstala


def test_archiving_marker_blocks_activation(env):
    alice, proj, _ = env
    write(proj / ".spm" / archive.MARKER, "{}")
    with pytest.raises(SyncError, match="archivuje"):
        alice.activate("Forma")


def test_mirror_does_not_recreate_archived_project(env, tmp_path):
    from spm.mirror import Mirror, MirrorError, MirrorItem, MirrorSource
    from spm.scanner import file_hash
    alice, proj, _ = env
    archive.archive_project(alice, "Forma")
    src = tmp_path / "zdroj" / "a.txt"
    write(src, "a")
    with pytest.raises(MirrorError, match="archivovaný"):
        Mirror(MirrorSource("k", str(src.parent)), alice, "Forma").run(
            [MirrorItem("s1", "added", "a.txt", file_hash(src), src, "a.txt")])
    assert not proj.exists()


# --- společná konfigurace -------------------------------------------------------------------

def test_shared_config_archive_fields():
    from spm import shared_config as sc
    archive_root = str(Path("..") / "Archiv")
    values = sc.parse(json.dumps({"archive_root": f" {archive_root} ", "archive_keep_backups": 3,
                                  "archivers": ["novak", " "]}))
    assert values == {"archive_root": archive_root, "archive_keep_backups": 3, "archivers": ["novak"]}
    with pytest.raises(sc.SharedConfigError, match="archivers"):
        sc.parse('{"archivers": "novak"}')
    with pytest.raises(sc.SharedConfigError, match="archive_keep_backups"):
        sc.parse('{"archive_keep_backups": -1}')
    s = Settings()
    assert sc.apply(s, values) and s.archivers == ["novak"] and s.archive_keep_backups == 3


# --- rozhraní -------------------------------------------------------------------------------

@pytest.fixture
def window(env, monkeypatch):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from spm import config
    from spm.gui import main_window
    from spm.gui.main_window import MainWindow
    alice, proj, tmp = env
    monkeypatch.setattr(config, "APP_DIR", tmp / "app")
    app = QApplication.instance() or QApplication([])

    def run_now(parent, title, fn, *args, on_done=None, on_error=None, status_text=None, **kwargs):
        res = fn(*args, progress=None, **kwargs)
        if on_done:
            on_done(res)
    monkeypatch.setattr(main_window, "run_task", run_now)
    w = MainWindow(alice.settings)
    w.pm = w.panel.pm = alice
    w.refresh(check_names=False)
    yield w
    w.close()
    app.processEvents()


def test_archive_and_restore_from_window(window, env, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from spm.gui.archive_dialog import ArchiveListDialog
    alice, proj, tmp = env
    w = window
    shown = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a[2]))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(w, "_exec_dialog", lambda dlg, key: True)
    w.table.selectRow(0)
    assert w.act_archive.isEnabled()
    w.archive_selected()
    assert "je archivovaný" in shown[-1] and [p.name for p in w.projects] == []
    # seznam archivu a obnova
    dialogs = []
    monkeypatch.setattr(w, "_exec_dialog", lambda dlg, key: dialogs.append(dlg) or True)
    w.show_archive()
    dlg: ArchiveListDialog = dialogs[0]
    assert dlg.table.rowCount() == 1 and dlg.table.item(0, 0).text() == "Forma"
    assert [dlg.files.item(i).text() for i in range(dlg.files.count())] == ["deska.elt"]
    assert dlg.btn_restore.isEnabled()
    assert dlg.restore_selected()
    assert [p.name for p in w.projects] == ["Forma"] and "obnovený" in shown[-1]
    assert dlg.table.item(0, 7).text()                                  # sloupec Obnoveno


def test_archive_refused_with_reasons_and_no_restore_without_permission(window, env, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QMessageBox

    from spm.gui.archive_dialog import ArchiveListDialog
    alice, proj, tmp = env
    w = window
    alice.add_notes("Forma", [Draft("otevřená")])
    shown = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a[2]))
    w.table.selectRow(0)
    w.archive_selected()
    assert "nelze archivovat" in shown[-1] and "nevyřízené poznámky" in shown[-1] and proj.exists()
    alice.resolve_note("Forma", alice.notes("Forma")[0].note_id)
    archive.archive_project(alice, "Forma")
    bob = manager(tmp, "bob")
    dlg = ArchiveListDialog(archive.archive_dir(bob.settings), lambda e: True,
                            archive.permission_problem(bob.settings, bob.user))
    assert not dlg.btn_restore.isEnabled() and "smí jen: alice" in dlg.btn_restore.toolTip()
