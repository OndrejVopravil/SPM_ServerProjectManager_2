"""Historie jednoho souboru: verze, jejich dostupnost (aktuální / záloha / koš) a obnova verze."""
import stat
from pathlib import Path

import pytest

from spm import db as dbm
from spm.config import Settings
from spm.scanner import file_hash
from spm.sync import ProjectManager, SyncError


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.chmod(stat.S_IWRITE | stat.S_IREAD)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def env(tmp_path):
    server = tmp_path / "server"
    write(server / "P" / "deska.elt", "v1")

    def pm(user, keep=2):
        s = Settings(server_root=str(server), local_root=str(tmp_path / f"l_{user}"), backup_count=keep)
        return ProjectManager(s, user=user, pc=f"PC-{user}")
    alice = pm("alice")
    for text, comment in (("v2", "druhá"), ("v3", "třetí"), ("v4", "čtvrtá")):
        alice.activate("P")
        write(alice.local_dir("P") / "deska.elt", text)
        plan = alice.plan_upload("P")
        alice.deactivate(plan, plan.default_assignments(), comment)
    return alice, server / "P", tmp_path


def test_versions_newest_first_with_availability(env):
    alice, proj, tmp = env
    versions = alice.file_versions("P", "deska.elt")
    assert [v.comment for v in versions][:3] == ["čtvrtá", "třetí", "druhá"]
    assert versions[-1].change_type == dbm.IMPORTED                   # první načtení do evidence
    # zálohy: ponechávají se 2 -> v4 aktuální, v3 a v2 v zálohách, v1 už ne
    avail = [(v.comment, v.available) for v in versions]
    assert avail[:3] == [("čtvrtá", True), ("třetí", True), ("druhá", True)]
    assert not versions[-1].available and "záloha už byla smazána" in versions[-1].reason
    assert "posledních 2" in versions[-1].reason
    assert versions[0].sources[0].current


def test_restore_version_verified_by_hash(env):
    alice, proj, tmp = env
    v2 = next(v for v in alice.file_versions("P", "deska.elt") if v.comment == "druhá")
    dest = tmp / "venku" / "deska.elt"
    assert alice.restore_file_version("P", v2, dest)
    assert dest.read_text(encoding="utf-8") == "v2" and file_hash(dest) == v2.sha256
    # poškozená záloha: obnova selže a cíl zůstane, jak byl
    for b in v2.sources:
        (proj / b.path).chmod(stat.S_IWRITE | stat.S_IREAD)
        (proj / b.path).write_text("xx", encoding="utf-8")
    write(dest, "puvodni")
    assert not alice.restore_file_version("P", v2, dest)
    assert dest.read_text(encoding="utf-8") == "puvodni"


def test_deleted_file_version_in_trash_and_unknown_file(env):
    alice, proj, tmp = env
    with pytest.raises(SyncError):
        alice.file_versions("P", "neexistuje.elt")
    alice.activate("P")
    write(alice.local_dir("P") / "druhy.elt", "d1")
    plan = alice.plan_upload("P")
    alice.upload(plan, plan.default_assignments(), "nový")
    versions = alice.file_versions("P", "druhy.elt")
    assert [v.change_type for v in versions] == [dbm.ADDED] and versions[0].available


@pytest.fixture
def qapp():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_dialog_open_and_save_to_folder(qapp, env, monkeypatch):
    import os
    from PySide6.QtWidgets import QFileDialog, QMessageBox

    from spm.gui import project_tree
    from spm.gui.file_history_dialog import FileHistoryDialog
    alice, proj, tmp = env
    monkeypatch.setattr(project_tree, "STAGING_ROOT", tmp / "staging")
    opened = []
    monkeypatch.setattr(os, "startfile", lambda p: opened.append(Path(p)), raising=False)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    dlg = FileHistoryDialog(alice, "P", "deska.elt", None, None)
    assert dlg.table.rowCount() == len(dlg.versions) and "Uložit" in dlg.btn_restore.text()
    dlg.table.selectRow(1)                                              # „třetí“ (v3)
    assert dlg.btn_open.isEnabled()
    path = dlg.open_version()
    assert opened == [path] and path.read_text(encoding="utf-8") == "v3"
    assert "deska_verze_" in path.name and not os.access(path, os.W_OK)   # jen pro čtení
    dlg.table.selectRow(dlg.table.rowCount() - 1)                       # v1 – záloha smazána
    assert not dlg.btn_open.isEnabled() and "není k dispozici" in dlg.info.text()
    dlg.table.selectRow(2)                                              # „druhá“ (v2)
    out = tmp / "ulozeno"
    out.mkdir()
    write(out / "deska.elt", "jiny")
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: str(out))
    saved = dlg.restore_version()
    assert saved == out / "deska_copy.elt" and saved.read_text(encoding="utf-8") == "v2"


def test_dialog_restore_into_local_copy(qapp, env, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from spm.gui.file_history_dialog import FileHistoryDialog
    alice, proj, tmp = env
    alice.activate("P")
    local = alice.local_dir("P") / "deska.elt"
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    dlg = FileHistoryDialog(alice, "P", "deska.elt", local, None)
    assert "místní kopie" in dlg.btn_restore.text()
    dlg.table.selectRow(2)                                              # v2
    assert dlg.restore_version() == local and local.read_text(encoding="utf-8") == "v2"
    plan = alice.plan_upload("P")                                       # pozná se jako úprava
    assert [lf.path for _, lf in plan.modified] == ["deska.elt"]
