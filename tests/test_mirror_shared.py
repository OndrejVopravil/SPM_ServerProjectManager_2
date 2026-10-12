"""Sdílená pravidla zrcadlení (spm_config.json zdroje), identita složek a projektů, souběh."""
import json
import os
import stat
from pathlib import Path

import pytest

from spm import config
from spm import shared_config as sc
from spm.config import AppSettings, Settings
from spm.db import ProjectDB
from spm.mirror import Mirror, MirrorPermissionError, MirrorRule, mirrors_for, resolve_target
from spm.sync import ProjectManager


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.chmod(stat.S_IWRITE | stat.S_IREAD)
    path.write_text(text, encoding="utf-8")


@pytest.fixture(autouse=True)
def app_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DIR", tmp_path / "app")


@pytest.fixture
def env(tmp_path):
    o, w = tmp_path / "O", tmp_path / "W"
    proj = o / "058768"
    write(proj / "dily" / "nc_codes" / "E01.nc", "E01")
    write(proj / "dily" / "nc_codes" / "E02.nc", "E02")
    write(proj / "dily" / "deska.elt", "deska")
    w.mkdir()

    def pm(root, user, server_root=None):
        s = Settings(server_root=str(server_root or root), local_root=str(tmp_path / f"l_{root.name}_{user}"))
        return ProjectManager(s, user=user, pc=f"PC-{user}")

    alice = pm(o, "alice")
    alice.activate("058768")
    plan = alice.plan_upload("058768")
    alice.deactivate(plan, plan.default_assignments(), "")
    return alice, pm, o, w


def rule(**kw):
    return MirrorRule(source="o", target="w", **kw)


def mirrored_sources(proj: Path) -> set[str]:
    with ProjectDB.for_project(proj) as db:
        return {src for src, _ in db.mirrored().values()}


# --- spm_config.json ------------------------------------------------------------------

def test_write_merges_and_server_id_is_stable(tmp_path):
    root = tmp_path / "S"
    root.mkdir()
    sc.write(str(root), {"backup_count": 7})
    sid = sc.ensure_server_id(str(root), "Server S")
    assert sid and sc.ensure_server_id(str(root)) == sid               # podruhé stejná identita
    sc.write(str(root), {sc.MIRROR_RULES: []})
    values = sc.probe(str(root)).values
    assert values["backup_count"] == 7 and values[sc.SERVER_ID] == sid  # zápis ostatní zachová
    assert values[sc.SERVER_NAME] == "Server S" and values[sc.MIRROR_RULES] == []


@pytest.mark.parametrize("rules, message", [
    ("x", "seznam"),
    ([{"files": "*.nc"}], "target_server"),
    ([{"target_server": "a"}], "files nebo folders"),
    ([{"target_server": "a", "files": "*.nc", "enabled": "ano"}], "enabled"),
])
def test_invalid_shared_rules(rules, message):
    with pytest.raises(sc.SharedConfigError, match=message):
        sc.parse(json.dumps({"mirror_rules": rules}))


def test_apply_shared_rules_and_their_absence():
    s = Settings()
    assert sc.apply(s, {"mirror_rules": [{"id": "1", "target_server": "w", "files": "*.nc"}]})
    assert s.shared_mirror_rules[0]["files"] == "*.nc"
    assert sc.apply(s, {"backup_count": 3}) and s.shared_mirror_rules is None   # klíč chybí = místní


def test_settings_lock_only_values_present_in_file(tmp_path):
    """Soubor jen s identitou složky nezamkne v Nastavení zálohy ani ostatní hodnoty."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from spm.gui.settings_dialog import SettingsDialog
    QApplication.instance() or QApplication([])
    root = tmp_path / "S"
    root.mkdir()
    sc.ensure_server_id(str(root))
    dlg = SettingsDialog(Settings(server_root=str(root), local_root=str(tmp_path / "l")))
    assert dlg.backup_count.isEnabled() and dlg.ignore.isEnabled() and dlg.check_chars.isEnabled()


# --- identita projektu a souběh ------------------------------------------------------------

def test_source_identified_by_project_not_by_path(env, tmp_path):
    """Dva uživatelé mají stejnou síťovou složku O: připojenou různě – zrcadlo je stejné."""
    alice, pm, o, w = env
    m1 = Mirror(alice, pm(w, "alice"), [rule(files="*.nc")])
    assert sorted(m1.sync_project("058768").added) == ["dily/nc_codes/E01.nc", "dily/nc_codes/E02.nc"]
    other_path = tmp_path / "jinak" / ".." / "O"                  # jiná cesta ke stejné složce
    bob_o = pm(o, "bob", server_root=other_path)
    res = Mirror(bob_o, pm(w, "bob"), [rule(files="*.nc")]).sync_project("058768")
    assert res.ok and res.changed == 0 and not res.adopted
    with ProjectDB.for_project(o / "058768") as db:
        assert mirrored_sources(w / "058768") == {db.project_id()}


def test_new_database_has_project_id_old_gets_one(env):
    alice, pm, o, w = env
    with ProjectDB.for_project(o / "058768") as db:
        assert db.project_id()
        db.conn.execute("DELETE FROM meta WHERE key = 'project_id'")  # databáze ze starší verze
        db.conn.commit()
    Mirror(alice, pm(w, "alice"), [rule(files="*.nc")]).sync_project("058768")
    with ProjectDB.for_project(o / "058768") as db:
        assert db.project_id() and mirrored_sources(w / "058768") == {db.project_id()}


def test_old_mirror_keyed_by_path_is_rekeyed(env):
    alice, pm, o, w = env
    m = Mirror(alice, pm(w, "alice"), [rule(files="*.nc")])
    m.sync_project("058768")
    with ProjectDB.for_project(w / "058768") as db:               # stav z předchozí verze: klíč = cesta
        db.conn.execute("UPDATE mirror_files SET source = ?", (str(o / "058768"),))
        db.conn.execute("DELETE FROM mirror_sources")
        db.conn.commit()
    res = m.sync_project("058768")
    assert res.ok and res.changed == 0 and not res.adopted
    with ProjectDB.for_project(o / "058768") as db:
        assert mirrored_sources(w / "058768") == {db.project_id()}


def test_stage_names_unique_per_run(env):
    alice, pm, o, w = env
    m1, m2 = (Mirror(alice, pm(w, "alice"), [rule(files="*.nc")]) for _ in range(2))
    assert m1._stage_token != m2._stage_token
    foreign = w / "058768" / "dily" / "nc_codes" / f"E01.nc.cizi1234.mirror.spmtmp"
    write(foreign, "rozpracovaná kopie jiného uživatele")
    assert m1.sync_project("058768").ok
    assert foreign.exists()                                       # cizí dočasnou kopii nemaže


def test_permission_error_reported(env, monkeypatch):
    from spm import mirror as mirror_mod
    alice, pm, o, w = env

    def denied(src, dst):
        raise PermissionError(13, "Přístup byl odepřen")
    monkeypatch.setattr(mirror_mod, "copy_with_hash", denied)
    with pytest.raises(MirrorPermissionError, match="oprávnění"):
        Mirror(alice, pm(w, "alice"), [rule(files="*.nc")]).sync_project("058768")


def test_target_label_carries_source_server_identity(env):
    alice, pm, o, w = env
    alice.settings.server_id, alice.settings.server_name = "srv-o", "O: Nástrojárna"
    Mirror(alice, pm(w, "alice"), [rule(files="*.nc")]).sync_project("058768")
    entry = next(e for e in pm(w, "bob").file_entries("058768") if e.path.endswith("E01.nc"))
    assert (entry.mirror, entry.mirror_server_id, entry.mirror_server_name) == \
        (str(o / "058768"), "srv-o", "O: Nástrojárna")


# --- sdílená pravidla a záložky -----------------------------------------------------------

def make_app(tmp_path, o, w, with_target=True) -> AppSettings:
    app = AppSettings.from_settings(Settings(server_root=str(o), local_root=str(tmp_path / "lO")), name="O")
    if with_target:
        wid = app.add_workspace("W")
        v = app.view(wid)
        v.server_root, v.local_root = str(w), str(tmp_path / "lW")
    return app


def sync_all(app):
    for ws in app.workspaces:
        sc.SharedConfig().sync(app.view(ws["id"]))


def test_shared_rules_apply_only_with_target_tab(env, tmp_path):
    alice, pm, o, w = env
    w_id = sc.ensure_server_id(str(w), "W: Výroba")
    sc.write(str(o), {sc.MIRROR_RULES: [{"target_server": w_id, "target_name": "W: Výroba",
                                         "files": "*.nc"}]})
    app = make_app(tmp_path, o, w)
    o_tab, w_tab = (ws["id"] for ws in app.workspaces)
    app.base.mirror_rules = [MirrorRule(o_tab, w_tab, folders="jine").to_dict()]   # místní – neplatí
    sync_all(app)
    groups = mirrors_for(app)
    assert [(s, t, [r.files for r in rules]) for s, t, rules in groups] == [(o_tab, w_tab, ["*.nc"])]
    alone = make_app(tmp_path, o, w, with_target=False)                  # uživatel bez záložky W:
    sync_all(alone)
    assert mirrors_for(alone) == []


def test_target_found_by_server_id_not_path(env, tmp_path):
    alice, pm, o, w = env
    w_id = sc.ensure_server_id(str(w))
    app = make_app(tmp_path, o, w)
    o_tab, w_tab = (ws["id"] for ws in app.workspaces)
    app.view(w_tab).server_root = str(tmp_path / "x" / ".." / "W")       # jinak připojený disk
    sync_all(app)
    assert resolve_target(app, MirrorRule(o_tab, "", target_server=w_id, files="*.nc")) == w_tab


# --- GUI ------------------------------------------------------------------------------------

def run_now(parent, title, fn, *args, on_done=None, on_error=None, **kwargs):
    res = fn(*args, progress=None, **kwargs)
    if on_done:
        on_done(res)


@pytest.fixture
def window(env, tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QMessageBox

    from spm.gui import main_window, mirror_dialog
    from spm.gui.main_window import MainWindow
    monkeypatch.setattr(main_window, "run_task", run_now)
    monkeypatch.setattr(mirror_dialog, "run_task", run_now)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    shown = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: shown.append(a[2]))
    alice, pm, o, w = env
    qapp = QApplication.instance() or QApplication([])
    win = MainWindow(make_app(tmp_path, o, w))
    win.pm = win.panel.pm = alice
    win.refresh(check_names=False)
    win.shown = shown
    yield win
    if win._name_check:
        win._name_check.wait()
    win.close()
    qapp.processEvents()


def tabs(win):
    return [ws["id"] for ws in win.app_settings.workspaces]


def test_export_local_rules_to_network(window, env):
    from spm.gui.mirror_dialog import MirrorDialog
    alice, pm, o, w = env
    o_tab, w_tab = tabs(window)
    window.settings.mirror_rules = [MirrorRule(o_tab, w_tab, files="*.nc").to_dict()]
    window.settings.save()
    dlg = MirrorDialog(window)
    dlg.table.selectRow(0)
    assert dlg.btn_export.isEnabled()
    dlg.export_rules()
    w_id = sc.probe(str(w)).values[sc.SERVER_ID]                         # cíl dostal identitu
    shared = sc.probe(str(o)).values[sc.MIRROR_RULES]
    assert [(r["target_server"], r["target_name"], r["files"]) for r in shared] == [(w_id, "W", "*.nc")]
    assert window.settings.mirror_rules == []                            # teď platí ze sítě
    assert dlg.table.rowCount() == 1 and dlg.table.item(0, 4).text().startswith("síť")
    assert dlg.table.item(0, 2).text() == "W"
    # sdílené pravidlo se zrcadlí po odevzdání
    window._mirror_after("058768")
    assert (w / "058768" / "dily" / "nc_codes" / "E01.nc").exists()


def test_edit_and_toggle_shared_rule_writes_network(window, env, monkeypatch):
    from PySide6.QtCore import Qt

    from spm.gui import mirror_dialog
    from spm.gui.mirror_dialog import MirrorDialog
    alice, pm, o, w = env
    o_tab, w_tab = tabs(window)
    w_id = sc.ensure_server_id(str(w))
    sc.write(str(o), {sc.MIRROR_RULES: [{"id": "r1", "target_server": w_id, "files": "*.nc"}]})
    window._sync_shared()
    dlg = MirrorDialog(window)
    dlg.table.selectRow(0)

    class FakeRuleDialog:
        def __init__(self, workspaces, rule, parent):
            assert rule.target == w_tab                                   # cíl rozpoznán podle identity
            self.rule = rule
        def exec(self):
            return True
        def result_rule(self):
            return MirrorRule(o_tab, w_tab, files="*.nc; *.mpf", id=self.rule.id)
    monkeypatch.setattr(mirror_dialog, "RuleDialog", FakeRuleDialog)
    dlg.edit_rule()
    assert sc.probe(str(o)).values[sc.MIRROR_RULES][0]["files"] == "*.nc; *.mpf"
    dlg.table.item(0, 0).setCheckState(Qt.CheckState.Unchecked)
    assert sc.probe(str(o)).values[sc.MIRROR_RULES][0]["enabled"] is False


def test_read_only_network_rules_cannot_be_changed(window, env):
    from spm.gui.mirror_dialog import MirrorDialog
    alice, pm, o, w = env
    w_id = sc.ensure_server_id(str(w))
    path = sc.write(str(o), {sc.MIRROR_RULES: [{"id": "r1", "target_server": w_id, "files": "*.nc"}]})
    window._sync_shared()
    path.chmod(stat.S_IREAD)
    try:
        dlg = MirrorDialog(window)
        dlg.table.selectRow(0)
        dlg.remove_rule()
        assert any("jen pro čtení" in s for s in window.shown)
        assert len(sc.probe(str(o)).values[sc.MIRROR_RULES]) == 1
    finally:
        path.chmod(stat.S_IWRITE | stat.S_IREAD)


def test_deleted_network_file_keeps_rules_as_local(window, env):
    alice, pm, o, w = env
    o_tab, w_tab = tabs(window)
    w_id = sc.ensure_server_id(str(w))
    path = sc.write(str(o), {sc.MIRROR_RULES: [{"id": "r1", "target_server": w_id, "files": "*.nc"}]})
    window._sync_shared()
    assert window.app_settings.view(o_tab).shared_mirror_rules
    path.unlink()
    window._sync_shared()
    assert window.app_settings.view(o_tab).shared_mirror_rules is None
    local = [MirrorRule.from_dict(r) for r in window.settings.mirror_rules]
    assert [(r.source, r.target, r.files) for r in local] == [(o_tab, w_tab, "*.nc")]


def test_mirror_switched_off_on_this_computer(window, env):
    alice, pm, o, w = env
    o_tab, w_tab = tabs(window)
    window.settings.mirror_rules = [MirrorRule(o_tab, w_tab, files="*.nc").to_dict()]
    window.settings.mirror_enabled = False
    window._mirror_after("058768")
    assert not (w / "058768").exists()


def test_permission_error_offers_switch_off(window, env, monkeypatch):
    from spm import mirror as mirror_mod
    alice, pm, o, w = env
    o_tab, w_tab = tabs(window)
    window.settings.mirror_rules = [MirrorRule(o_tab, w_tab, files="*.nc").to_dict()]

    def denied(src, dst):
        raise PermissionError(13, "Přístup byl odepřen")
    monkeypatch.setattr(mirror_mod, "copy_with_hash", denied)
    window._mirror_after("058768")                                       # otázka -> Ano
    assert window.settings.mirror_enabled is False


def test_tree_label_uses_tab_name_by_server_identity(window, env, tmp_path):
    alice, pm, o, w = env
    o_tab, w_tab = tabs(window)
    o_id = sc.ensure_server_id(str(o), "O")
    window._sync_shared()
    source = ProjectManager(window.app_settings.view(o_tab), user="alice", pc="PC-alice")
    assert source.settings.server_id == o_id
    Mirror(source, window.pm_for(w_tab), [rule(files="*.nc")]).sync_project("058768")
    entry = next(e for e in pm(w, "bob").file_entries("058768") if e.path.endswith("E01.nc"))
    assert entry.mirror_server_id == o_id
    # záložka O: připojená úplně jinak – pozná se jen podle identity složky
    window.app_settings.view(o_tab).server_root = r"Z:\uplne\jina\cesta"
    assert window.mirror_source_label(entry) == "O"
    window.app_settings.view(o_tab).server_id = ""             # bez identity: název ze zdroje
    assert window.mirror_source_label(entry) == "O"            # server_name zapsaný při zrcadlení
    window.app_settings.view(o_tab).server_id = o_id


def test_rule_dialog_keeps_target_user_does_not_have(window):
    from spm.gui.mirror_dialog import RuleDialog
    o_tab, w_tab = tabs(window)
    rule_z = MirrorRule(o_tab, "", target_server="z-id", target_name="Z: Pobočka", files="*.pdf", id="r9")
    dlg = RuleDialog(window.app_settings.workspaces, rule_z)
    assert dlg.target.currentText() == "Z: Pobočka (nemáte jako záložku)"
    dlg.files.setText("*.pdf; *.dxf")
    new = dlg.result_rule()
    assert (new.target, new.target_server, new.target_name, new.files, new.id) == \
        ("", "z-id", "Z: Pobočka", "*.pdf; *.dxf", "r9")
    assert new.validate() is None
