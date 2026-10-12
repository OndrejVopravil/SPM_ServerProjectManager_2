"""Zrcadlení vybraných souborů ze záložky O: do záložky W:."""
import stat
from pathlib import Path

import pytest

from spm import db as dbm
from spm.config import Settings
from spm.db import ProjectDB
from spm.mirror import Mirror, MirrorRule
from spm.sync import ProjectManager, SyncError


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.chmod(stat.S_IWRITE | stat.S_IREAD)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def env(tmp_path):
    o, w = tmp_path / "O", tmp_path / "W"
    proj = o / "058768"
    write(proj / "dily" / "nc_codes" / "E01.nc", "E01 v1")
    write(proj / "dily" / "nc_codes" / "E02.nc", "E02 v1")
    write(proj / "dily" / "nc_codes" / "popis.txt", "popis")
    write(proj / "dily" / "deska.elt", "deska")
    write(proj / "stare" / "nc_codes" / "X.nc", "stary")
    w.mkdir()

    def pm(root, user):
        return ProjectManager(Settings(server_root=str(root), local_root=str(tmp_path / f"l_{root.name}_{user}")),
                              user=user, pc=f"PC-{user}")

    alice = pm(o, "alice")
    # evidence zdroje: první aktivace a deaktivace
    alice.activate("058768")
    plan = alice.plan_upload("058768")
    alice.deactivate(plan, plan.default_assignments(), "")
    return alice, pm(w, "alice"), pm(w, "bob"), o, w


def rule(**kw):
    return MirrorRule(source="o", target="w", **kw)


def files_in(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*")
                  if p.is_file() and ".spm" not in p.parts)


def mirror_changes(proj: Path):
    with ProjectDB.for_project(proj) as db:
        commits = [c for c in db.commits() if c["kind"] == dbm.MIRROR]
        return [(ch["change_type"], ch["new_path"] or ch["old_path"])
                for c in commits for ch in db.changes(c["commit_id"])]


def test_rule_matching():
    r = rule(files="*.nc; *.MPF", folders="nc_*", exclude="stare")
    assert r.matches("dily/nc_codes/E01.nc")
    assert r.matches("a/NC_CODES/b/c/E01.mpf")            # kdekoliv ve stromu, bez ohledu na velikost
    assert not r.matches("dily/nc_codes/popis.txt")       # jiný soubor
    assert not r.matches("dily/E01.nc")                   # mimo složku
    assert not r.matches("stare/nc_codes/X.nc")           # vyjmutá složka
    assert rule(folders="nc_codes").matches("x/nc_codes/popis.txt")    # celá složka
    assert rule(files="*.nc").matches("E01.nc")
    assert rule(files="*.nc", projects="058*").matches_project("058768")
    assert not rule(files="*.nc", projects="058*").matches_project("123")
    assert rule().validate() and rule(files="*.nc").validate() is None


def test_initial_mirror_creates_project_and_locks_files(env):
    alice_o, alice_w, bob_w, o, w = env
    m = Mirror(alice_o, alice_w, [rule(files="*.nc", folders="nc_codes", exclude="stare")])
    res = m.sync_project("058768")
    assert res.created_project and res.ok
    assert sorted(res.added) == ["dily/nc_codes/E01.nc", "dily/nc_codes/E02.nc"]
    assert files_in(w / "058768") == ["dily/nc_codes/E01.nc", "dily/nc_codes/E02.nc"]
    assert (w / "058768" / "dily/nc_codes/E01.nc").read_text(encoding="utf-8") == "E01 v1"
    assert sorted(mirror_changes(w / "058768")) == [("added", "dily/nc_codes/E01.nc"),
                                                    ("added", "dily/nc_codes/E02.nc")]
    # na W: soubory spravuje zrcadlo – nikdo je nemůže zamknout
    entries = {e.path: e for e in bob_w.file_entries("058768")}
    assert entries["dily/nc_codes/E01.nc"].mirror == str(o / "058768")
    assert not entries["dily/nc_codes/E01.nc"].free
    with pytest.raises(SyncError, match="zrcadlo"):
        bob_w.activate("058768", lock_paths=["dily/nc_codes/E01.nc"])
    bob_w.activate("058768")                        # „všechny volné“ zrcadlené soubory přeskočí
    assert all(not e.mine for e in bob_w.file_entries("058768"))
    with pytest.raises(SyncError, match="zrcadlo"):
        bob_w.take("058768", ["dily/nc_codes/E02.nc"])


def test_second_run_without_changes_does_nothing(env):
    alice_o, alice_w, bob_w, o, w = env
    m = Mirror(alice_o, alice_w, [rule(files="*.nc")])
    m.sync_project("058768")
    res = m.sync_project("058768")
    assert res.changed == 0 and res.ok
    with ProjectDB.for_project(w / "058768") as db:
        assert len([c for c in db.commits() if c["kind"] == dbm.MIRROR]) == 1   # prázdný záznam se neukládá


def submit(pm, name, edit):
    """Aktivace, úprava v místní kopii a deaktivace (odevzdání)."""
    pm.activate(name)
    local = pm.local_dir(name)
    edit(local)
    plan = pm.plan_upload(name)
    pm.deactivate(plan, plan.default_assignments(), "zmena")


def test_changes_renames_and_deletions_follow_source(env):
    alice_o, alice_w, bob_w, o, w = env
    m = Mirror(alice_o, alice_w, [rule(files="*.nc", exclude="stare")])
    m.sync_project("058768")

    def edit(local):
        write(local / "dily/nc_codes/E01.nc", "E01 v2")
        (local / "dily/nc_codes/E02.nc").rename(local / "dily/nc_codes/E02b.nc")
        write(local / "dily/nc_codes/E03.nc", "E03")
        (local / "dily/deska.elt").unlink()
    submit(alice_o, "058768", edit)
    res = m.sync_project("058768")
    assert res.ok
    assert res.modified == ["dily/nc_codes/E01.nc"]
    assert res.renamed == ["dily/nc_codes/E02.nc -> dily/nc_codes/E02b.nc"]
    assert res.added == ["dily/nc_codes/E03.nc"]
    assert files_in(w / "058768") == ["dily/nc_codes/E01.nc", "dily/nc_codes/E02b.nc", "dily/nc_codes/E03.nc"]
    assert (w / "058768" / "dily/nc_codes/E01.nc").read_text(encoding="utf-8") == "E01 v2"
    assert list((w / "058768" / ".spm" / "backup").rglob("E01.nc.backup01"))   # záloha v cíli

    submit(alice_o, "058768", lambda local: (local / "dily/nc_codes/E03.nc").unlink())
    res = m.sync_project("058768")
    assert res.deleted == ["dily/nc_codes/E03.nc"]
    assert not (w / "058768" / "dily/nc_codes/E03.nc").exists()
    assert list((w / "058768" / ".spm" / "trash").rglob("E03.nc"))              # v koši projektu


def test_file_no_longer_matching_is_released_not_deleted(env):
    alice_o, alice_w, bob_w, o, w = env
    m = Mirror(alice_o, alice_w, [rule(files="*.nc")])
    m.sync_project("058768")
    submit(alice_o, "058768",
           lambda local: (local / "dily/nc_codes/E02.nc").rename(local / "dily/nc_codes/E02.bak"))
    res = m.sync_project("058768")
    assert res.released == ["dily/nc_codes/E02.nc"] and not res.deleted
    assert (w / "058768" / "dily/nc_codes/E02.nc").exists()                     # v cíli ponechán
    entries = {e.path: e for e in bob_w.file_entries("058768")}
    assert entries["dily/nc_codes/E02.nc"].free                                 # už ho nespravuje zrcadlo


def test_existing_target_files_adopted_or_overwritten_with_backup(env):
    alice_o, alice_w, bob_w, o, w = env
    write(w / "058768" / "dily/nc_codes/E01.nc", "E01 v1")          # stejný obsah, mimo evidenci
    write(w / "058768" / "dily/nc_codes/E02.nc", "jina verze")      # jiný obsah
    write(w / "058768" / "jen_na_W.txt", "w")                       # jen na W: – zůstane
    m = Mirror(alice_o, alice_w, [rule(files="*.nc", exclude="stare")])
    res = m.sync_project("058768")
    assert res.adopted == ["dily/nc_codes/E01.nc"]
    assert res.added == ["dily/nc_codes/E02.nc"]
    assert (w / "058768" / "dily/nc_codes/E02.nc").read_text(encoding="utf-8") == "E02 v1"
    assert list((w / "058768" / ".spm" / "backup").rglob("E02.nc.backup01"))
    assert (w / "058768" / "jen_na_W.txt").exists()


def test_file_held_on_target_is_conflict(env):
    alice_o, alice_w, bob_w, o, w = env
    write(w / "058768" / "dily/nc_codes/E01.nc", "verze z W")
    bob_w.activate("058768")                      # bob na W: drží E01.nc (zatím nezrcadlený)
    res = Mirror(alice_o, alice_w, [rule(files="*.nc")]).sync_project("058768")
    assert any("E01.nc" in c and "bob" in c for c in res.conflicts)
    assert (w / "058768" / "dily/nc_codes/E01.nc").read_text(encoding="utf-8") == "verze z W"
    with ProjectDB.for_project(w / "058768") as db:
        assert db.mirror_synced(str(o / "058768")) is None      # s konfliktem se příště zkusí znovu


def test_forbidden_name_on_target_not_mirrored(env):
    """Zdroj má kontrolu znaků vypnutou, cíl zapnutou – vadný název se do cíle nezrcadlí."""
    alice_o, alice_w, bob_w, o, w = env
    alice_o.settings.check_forbidden_chars = False
    submit(alice_o, "058768", lambda local: write(local / "dily/nc_codes/Výkres 1.nc", "x"))
    m = Mirror(alice_o, alice_w, [rule(files="*.nc", exclude="stare")])
    res = m.sync_project("058768")
    assert any("Výkres 1.nc" in c and "znaky" in c for c in res.conflicts)
    assert sorted(res.added) == ["dily/nc_codes/E01.nc", "dily/nc_codes/E02.nc"]
    assert not (w / "058768" / "dily/nc_codes/Výkres 1.nc").exists()


def test_project_without_source_evidence_is_skipped(env):
    alice_o, alice_w, bob_w, o, w = env
    write(o / "NOVY" / "nc_codes" / "A.nc", "a")
    res = Mirror(alice_o, alice_w, [rule(files="*.nc")]).sync_project("NOVY")
    assert res.note and not (w / "NOVY").exists()


def test_preview_and_sync_all(env):
    alice_o, alice_w, bob_w, o, w = env
    write(o / "NOVY" / "nc_codes" / "A.nc", "a")
    m = Mirror(alice_o, alice_w, [rule(files="*.nc", folders="nc_codes", exclude="stare")])
    preview = m.preview()
    assert preview == {"058768": ["dily/nc_codes/E01.nc", "dily/nc_codes/E02.nc"], "NOVY": []}
    results = {r.project: r for r in m.sync_all()}
    assert len(results["058768"].added) == 2 and results["NOVY"].note


def test_staging_files_are_cleaned_and_ignored(env):
    alice_o, alice_w, bob_w, o, w = env
    Mirror(alice_o, alice_w, [rule(files="*.nc")]).sync_project("058768")
    assert not list((w / "058768").rglob("*.spmtmp"))


# --- GUI -------------------------------------------------------------------------------

def run_now(parent, title, fn, *args, on_done=None, on_error=None, **kwargs):
    res = fn(*args, progress=None, **kwargs)
    if on_done:
        on_done(res)


@pytest.fixture
def window(env, tmp_path, monkeypatch):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QMessageBox

    from spm import config
    from spm.config import AppSettings
    from spm.gui import main_window, mirror_dialog
    from spm.gui.main_window import MainWindow
    monkeypatch.setattr(config, "APP_DIR", tmp_path / "app")
    monkeypatch.setattr(main_window, "run_task", run_now)
    monkeypatch.setattr(mirror_dialog, "run_task", run_now)
    shown = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: shown.append(a[2]))
    monkeypatch.setattr(QMessageBox, "exec", lambda self: shown.append(self.text()) or 0)
    alice_o, alice_w, bob_w, o, w = env
    app = AppSettings.from_settings(alice_o.settings, name="O")
    w_id = app.add_workspace("W")
    view = app.view(w_id)
    view.server_root, view.local_root = alice_w.settings.server_root, alice_w.settings.local_root
    qapp = QApplication.instance() or QApplication([])
    win = MainWindow(app)
    win.pm = win.panel.pm = alice_o
    win.shown = shown
    yield win
    if win._name_check:
        win._name_check.wait()
    win.close()
    qapp.processEvents()


def add_rule(win, **kw):
    o_id, w_id = (ws["id"] for ws in win.app_settings.workspaces)
    win.settings.mirror_rules = [MirrorRule(source=o_id, target=w_id, **kw).to_dict()]
    win.settings.save()


def test_mirror_runs_after_submit(window, env):
    alice_o, alice_w, bob_w, o, w = env
    add_rule(window, files="*.nc", exclude="stare")
    window._mirror_after("058768")
    assert files_in(w / "058768") == ["dily/nc_codes/E01.nc", "dily/nc_codes/E02.nc"]
    assert "v pořádku" in window.mirror_label.text()
    assert "nové: 2" in window.mirror_label.toolTip()


def test_mirror_problems_reported(window, env):
    alice_o, alice_w, bob_w, o, w = env
    add_rule(window, files="*.nc", exclude="stare")
    write(w / "058768" / "dily/nc_codes/E01.nc", "verze z W")
    bob_w.activate("058768")
    window._mirror_after("058768")
    assert "s problémy" in window.mirror_label.text()
    assert any("bob" in s for s in window.shown)


def test_no_rule_no_mirror(window, env):
    alice_o, alice_w, bob_w, o, w = env
    add_rule(window, files="*.nc", projects="999*")
    window._mirror_after("058768")
    assert not (w / "058768").exists() and not hasattr(window, "mirror_label")


def test_rules_are_personal_and_persisted(window):
    from spm.config import AppSettings
    add_rule(window, files="*.nc")
    loaded = AppSettings.load()
    for ws in loaded.workspaces:                              # platí ve všech záložkách
        assert loaded.view(ws["id"]).mirror_rules[0]["files"] == "*.nc"


def test_dialog_add_rule_preview_and_sync(window, env, monkeypatch):
    from spm.gui import mirror_dialog
    from spm.gui.mirror_dialog import MirrorDialog
    alice_o, alice_w, bob_w, o, w = env
    o_id, w_id = (ws["id"] for ws in window.app_settings.workspaces)

    class FakeRuleDialog:
        def __init__(self, workspaces, rule, parent):
            assert rule.source == o_id                      # zdroj předvyplněn aktuální záložkou
        def exec(self):
            return True
        def result_rule(self):
            return MirrorRule(source=o_id, target=w_id, files="*.nc", folders="nc_codes",
                              exclude="stare")
    monkeypatch.setattr(mirror_dialog, "RuleDialog", FakeRuleDialog)
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    dlg = MirrorDialog(window)
    dlg.add_rule()
    assert dlg.table.rowCount() == 1 and dlg.table.item(0, 2).text() == "W"
    assert window.settings.mirror_rules[0]["folders"] == "nc_codes"
    dlg.preview()
    assert any("2 souborů v 1 projektech" in s for s in window.shown)
    dlg.sync_now()
    assert files_in(w / "058768") == ["dily/nc_codes/E01.nc", "dily/nc_codes/E02.nc"]
    # vypnutí pravidla zaškrtávátkem
    from PySide6.QtCore import Qt
    dlg.table.item(0, 0).setCheckState(Qt.CheckState.Unchecked)
    assert window.settings.mirror_rules[0]["enabled"] is False
    assert not dlg.btn_sync.isEnabled()


def test_removing_workspace_removes_its_rules(window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    add_rule(window, files="*.nc")
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    w_id = window.app_settings.workspaces[1]["id"]
    window.remove_workspace(w_id)
    assert window.settings.mirror_rules == []


def test_mirrored_files_in_tree_of_target(window, env):
    from spm.gui.project_panel import ProjectPanel
    alice_o, alice_w, bob_w, o, w = env
    Mirror(alice_o, alice_w, [rule(files="*.nc", exclude="stare")]).sync_project("058768")
    entries = {e.path: e for e in bob_w.file_entries("058768")}
    e = entries["dily/nc_codes/E01.nc"]
    assert e.holder == "" and e.mirror == str(o / "058768")       # Drží = jen zámky


def test_tree_marks_files_matching_rules_in_source(window, env):
    from conftest import settle_panel
    from tests_helpers_tree import find_item
    alice_o, alice_w, bob_w, o, w = env
    add_rule(window, files="*.nc", exclude="stare")
    window.refresh(check_names=False)
    window.table.selectRow([p.name for p in window.projects].index("058768"))
    settle_panel(window.panel)
    tree = window.panel.tree
    assert not tree.isColumnHidden(2)
    assert find_item(tree, "dily/nc_codes/E01.nc").text(2) == "→ W"
    assert find_item(tree, "dily/nc_codes/popis.txt").text(2) == ""
    assert find_item(tree, "stare/nc_codes/X.nc").text(2) == ""         # vyjmutá složka
    assert find_item(tree, "dily").text(2) == "→ W (2)"
    assert find_item(tree, "dily/nc_codes").text(2) == "→ W (2)"
    assert find_item(tree, "stare").text(2) == ""
    assert "zrcadlí se: <b>2</b> → W" in window.panel.summary.text()
    # v cílové záložce: zdroj zrcadla ve sloupci Zrcadlo, Drží zůstává pro zámky
    window._mirror_after("058768")
    window.tabs.setCurrentIndex(1)
    window.table.selectRow([p.name for p in window.projects].index("058768"))
    settle_panel(window.panel)
    assert window.panel.entries and not tree.isColumnHidden(2)            # sloupec vždy
    item = find_item(tree, "dily/nc_codes/E01.nc")
    assert item.text(1) == "" and item.text(2) == "← O"     # Drží prázdné, zdroj = záložka O
    assert find_item(tree, "dily").text(2) == "← O (2)"


def test_tree_mirror_column_shown_without_rules(window):
    from conftest import settle_panel
    window.refresh(check_names=False)
    window.table.selectRow(0)
    settle_panel(window.panel)
    assert not window.panel.tree.isColumnHidden(2)
    assert all(i.text(2) == "" for i in window.panel.tree.leaf_items())


def test_activated_target_project_shows_mirror_not_holder(window, env):
    """Aktivace projektu na W: – zrcadlené soubory nikdo nedrží, ve sloupci Zrcadlo je zdroj."""
    from conftest import settle_panel
    from tests_helpers_tree import find_item
    alice_o, alice_w, bob_w, o, w = env
    add_rule(window, files="*.nc", exclude="stare")
    write(w / "058768" / "vykres.pdf", "pdf")                    # vlastní soubor projektu na W:
    window._mirror_after("058768")
    window.tabs.setCurrentIndex(1)
    window.pm.activate("058768")                                  # „všechny volné“
    window.refresh(check_names=False)
    window.table.selectRow([p.name for p in window.projects].index("058768"))
    settle_panel(window.panel)
    tree = window.panel.tree
    nc = find_item(tree, "dily/nc_codes/E01.nc")
    assert (nc.text(1), nc.text(2)) == ("", "← O")
    pdf = find_item(tree, "vykres.pdf")
    assert (pdf.text(1), pdf.text(2)) == ("já", "")
