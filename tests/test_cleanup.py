"""Automatický úklid a upozornění na data, která by jinak zůstala napořád: dlouhé aktivace,
zbytky nedokončených operací v síťové složce, staré místní složky _puvodni_ / _obnova_,
nastavení zapomenutých projektů a záložek."""
import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from spm import config, storage
from spm.config import Settings
from spm.lock import marker_path
from spm.sync import ProjectManager, old_local_copy


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def age(path: Path, days: float) -> None:
    t = time.time() - days * 86400
    for p in [path, *path.rglob("*")]:
        os.utime(p, (t, t))


@pytest.fixture
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DIR", tmp_path / "app")
    server = tmp_path / "server"
    write(server / "Forma" / "a.elt", "a")
    write(server / "Alfa" / "b.elt", "b")
    return server, tmp_path


def make_window(qapp, pm):
    from spm.gui.main_window import MainWindow
    w = MainWindow(pm.settings)
    w.pm = w.panel.pm = pm
    w.refresh(check_names=False)
    return w


def close(w, qapp):
    if w._name_check:
        w._name_check.wait()
    w._messages_check.wait()
    w.close()
    qapp.processEvents()


def test_long_activation_highlighted(qapp, env):
    from spm.gui.main_window import COL_SINCE, COLOR_STALE
    server, tmp = env
    bob = ProjectManager(Settings(server_root=str(server), local_root=str(tmp / "l_bob")), user="bob", pc="PC-B")
    bob.activate("Forma", lock_paths=[])
    marker = marker_path(server / "Forma", "bob", "PC-B")
    data = json.loads(marker.read_text(encoding="utf-8"))
    data["since"] = (datetime.now() - timedelta(days=45)).isoformat(timespec="seconds")
    marker.write_text(json.dumps(data), encoding="utf-8")
    alice = ProjectManager(Settings(server_root=str(server), local_root=str(tmp / "l_alice")), user="alice", pc="PC-A")
    w = make_window(qapp, alice)
    try:
        rows = {w.table.item(r, 0).text(): r for r in range(w.table.rowCount())}
        item = w.table.item(rows["Forma"], COL_SINCE)
        assert item.text().endswith("· 45 dní") and item.foreground().color() == COLOR_STALE
        assert "Odebrat zámky uživatele" in item.toolTip()
        assert w.table.item(rows["Alfa"], COL_SINCE).text() == ""     # volný projekt beze změny
    finally:
        close(w, qapp)


def test_leftovers_in_server_folder(qapp, env, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from spm.gui.storage_dialog import StorageDialog
    server, tmp = env
    old = server / ".spm_restoring_Forma_abc123"
    write(old / "a.elt", "x" * 100)
    age(old, 3)
    fresh = server / ".spm_publish_Novy_def456"                       # může právě běžet
    write(fresh / "b.elt", "y")
    write(server / ".spm_users" / "bob.json", "{}")                 # není zbytek
    found = storage.leftovers(server)
    assert [f.path.name for f in found] == [old.name] and found[0].size == 100
    assert "obnova z archivu" in found[0].kind
    dlg = StorageDialog(server, storage.server_storage(server), "alice", "PC", 2, leftovers=found,
                        clean_problem="Úklid záloh a koše smí jen: novak")
    assert not dlg.leftover_box.isHidden() and not dlg.btn_leftovers.isEnabled()   # bez oprávnění
    dlg = StorageDialog(server, storage.server_storage(server), "alice", "PC", 2, leftovers=found)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    assert dlg.remove_leftovers() == 1
    assert not old.exists() and fresh.exists() and (server / ".spm_users").exists()
    assert dlg.leftovers == [] and dlg.leftover_list.count() == 0
    # pojistka: nic mimo síťovou složku a bez předpony se nesmaže
    outside = tmp / ".spm_restoring_jinde"
    write(outside / "c.elt", "c")
    assert storage.remove_leftovers(server, [storage.Leftover(outside, "x", 1, 1, datetime.now())]) == []
    assert outside.exists()
    assert StorageDialog(server, [], "alice", "PC", 2).leftover_box.isHidden()   # bez zbytků skryté


def test_old_local_copies_to_recycle_bin(qapp, env, monkeypatch):
    from spm.gui import main_window
    server, tmp = env
    old_name = f"Forma_puvodni_{(datetime.now() - timedelta(days=40)):%Y%m%d_%H%M%S}"
    new_name = f"Forma_obnova_{datetime.now():%Y%m%d_%H%M%S}"
    write(tmp / "l" / old_name / "a.elt", "a")
    write(tmp / "l" / new_name / "a.elt", "a")
    assert old_local_copy(old_name)[0] == "odsunutá při aktivaci" and old_local_copy(new_name) is None
    pm = ProjectManager(Settings(server_root=str(server), local_root=str(tmp / "l")), user="alice", pc="PC")
    w = make_window(qapp, pm)
    try:
        assert [x[0] for x in w.old_copies()] == [old_name]
        assert not w.btn_old_copies.isHidden() and "(1)" in w.btn_old_copies.text()
        recycled = []
        monkeypatch.setattr(main_window.recycle, "to_recycle_bin", lambda paths: recycled.extend(paths)
                            or [__import__("shutil").rmtree(p) for p in paths])
        monkeypatch.setattr(w, "_exec_dialog", lambda dlg, key: True)
        assert w.clean_old_copies() == 1
        assert recycled == [tmp / "l" / old_name] and (tmp / "l" / new_name).exists()
        assert w.btn_old_copies.isHidden()
    finally:
        close(w, qapp)


def test_settings_forget_missing_projects_and_workspaces(qapp, env):
    server, tmp = env
    pm = ProjectManager(Settings(server_root=str(server), local_root=str(tmp / "l")), user="alice", pc="PC")
    from spm.gui.main_window import MainWindow
    w = MainWindow(pm.settings)
    w.pm = w.panel.pm = pm
    try:
        ws = w.settings.workspace_id
        ui = w.settings.ui
        ui[f"notes_seen.{ws}.Forma"] = "3"
        ui[f"notes_seen.{ws}.Smazany"] = "5"
        ui[f"favorites.{ws}"] = "Forma\nSmazany"
        ui[f"recent.{ws}"] = "Smazany\nAlfa"
        ui["notes_seen.zaniklazalozka.Forma"] = "1"
        ui["favorites.zaniklazalozka"] = "Forma"
        ui["main.geometry"] = "x"
        w.refresh(check_names=False)
        assert f"notes_seen.{ws}.Smazany" not in ui and ui[f"notes_seen.{ws}.Forma"] == "3"
        assert ui[f"favorites.{ws}"] == "Forma" and ui[f"recent.{ws}"] == "Alfa"
        w._prune_removed_workspaces()
        assert "notes_seen.zaniklazalozka.Forma" not in ui and "favorites.zaniklazalozka" not in ui
        assert ui["main.geometry"] == "x" and f"favorites.{ws}" in ui    # ostatní zůstává
        # nedostupná síť / prázdný seznam: nic se nemaže
        w.projects = []
        ui[f"favorites.{ws}"] = "Forma\nCokoliv"
        w._prune_ui()
        assert ui[f"favorites.{ws}"] == "Forma\nCokoliv"
    finally:
        close(w, qapp)


def test_old_local_copy_names():
    """Druh a datum z názvu – stejné vzory jako při vzniku složek (ORIGINAL_COPY_RE, RESTORE_DIR_RE)."""
    now = datetime(2026, 10, 26)
    assert old_local_copy("Forma_puvodni_20260801_101500", now) == ("odsunutá při aktivaci", datetime(2026, 8, 1, 10, 15))
    assert old_local_copy("Forma_obnova_20260801_101500_2", now)[0] == "obnova ze zálohy"
    assert old_local_copy("Forma_obnova_20261020_101500", now) is None        # mladší než 30 dní
    assert old_local_copy("Forma_puvodni_20261399_101500", now) is None       # neplatné datum
    assert old_local_copy("Forma", now) is None


def test_leftovers_unreadable_folder_reported(qapp, env, monkeypatch):
    """Zbytek, který nejde celý přečíst, se přeskočí (stáří nelze spolehlivě určit) a uživatel
    dostane upozornění; ostatní zbytky se zpracují. Nedostupná síťová složka = jen upozornění."""
    from spm.gui.storage_dialog import StorageDialog
    server, tmp = env
    good = server / ".spm_restoring_Forma_abc123"
    bad = server / ".spm_archived_Stary_def456"
    write(good / "a.elt", "a")
    write(bad / "pod" / "b.elt", "b")
    age(good, 3)
    age(bad, 3)
    real_walk = os.walk

    def walk(top, onerror=None, **kw):
        if Path(top) == bad and onerror:
            onerror(PermissionError(13, "Přístup byl odepřen", str(bad / "pod")))
        return real_walk(top, onerror=onerror, **kw)
    monkeypatch.setattr(storage.os, "walk", walk)
    problems = []
    found = storage.leftovers(server, problems=problems)
    assert [f.path.name for f in found] == [good.name]
    assert len(problems) == 1 and bad.name in problems[0] and "přeskočeno" in problems[0]
    dlg = StorageDialog(server, storage.server_storage(server), "alice", "PC", 2,
                        leftovers=found, leftover_problems=problems)
    assert not dlg.leftover_box.isHidden() and not dlg.leftover_warning.isHidden()
    assert bad.name in dlg.leftover_warning.text()
    # celá síťová složka nedostupná
    problems = []
    assert storage.leftovers(tmp / "neexistuje", problems=problems) == [] and "nelze projít" in problems[0]
    dlg = StorageDialog(server, storage.server_storage(server), "alice", "PC", 2, leftovers=[],
                        leftover_problems=problems)
    assert not dlg.leftover_box.isHidden() and dlg.leftover_list.isHidden()   # jen upozornění
