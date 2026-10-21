"""Skripty v aplikaci: běh po odevzdání, nedokončený běh, okno Skripty, zrcadlo ve stromu."""
import json
import os
import stat
from pathlib import Path

import pytest

from spm import config
from spm import shared_config as sc
from spm.config import AppSettings, Settings
from spm.db import ProjectDB
from spm.scripting import store
from spm.sync import ProjectManager


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.chmod(stat.S_IWRITE | stat.S_IREAD)
    path.write_text(text, encoding="utf-8")


def files_in(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*")
                  if p.is_file() and ".spm" not in p.parts)


def run_now(parent, title, fn, *args, on_done=None, on_error=None, status_text=None, **kwargs):
    try:
        res = fn(*args, progress=None, **kwargs)
    except Exception as e:  # noqa: BLE001
        if on_error:
            on_error(str(e))
        raise
    if on_done:
        on_done(res)


@pytest.fixture
def window(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QMessageBox

    from spm.gui import main_window, scripts_dialog
    from spm.gui.main_window import MainWindow
    monkeypatch.setattr(config, "APP_DIR", tmp_path / "app")
    monkeypatch.setattr(main_window, "run_task", run_now)
    monkeypatch.setattr(scripts_dialog, "run_task", run_now)
    shown = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: shown.append(a[2]))
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a[2]))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "exec", lambda self: shown.append(
        (self.text(), self.detailedText())) or 0)

    o, w = tmp_path / "O", tmp_path / "W"
    proj = o / "058768"
    write(proj / "dily" / "nc_codes" / "E01.nc", "E01 v1")
    write(proj / "dily" / "nc_codes" / "E02.nc", "E02 v1")
    write(proj / "vykres.pdf", "pdf")
    w.mkdir()
    alice = ProjectManager(Settings(server_root=str(o), local_root=str(tmp_path / "lO")),
                           user="alice", pc="PC-alice")
    alice.activate("058768")
    plan = alice.plan_upload("058768")
    alice.deactivate(plan, plan.default_assignments(), "")

    app = AppSettings.from_settings(alice.settings, name="O")
    w_id = app.add_workspace("W")
    view = app.view(w_id)
    view.server_root, view.local_root = str(w), str(tmp_path / "lW")
    qapp = QApplication.instance() or QApplication([])
    win = MainWindow(app)
    win.pm = win.panel.pm = alice
    win.shown = shown
    win.o, win.w = o, w
    yield win
    if win._name_check:
        win._name_check.wait()
    win.close()
    qapp.processEvents()


def use_script(win, text: str, name: str = "zrcadlo.spm") -> None:
    store.save_script(name, text)
    win.settings.scripts = [name]
    win.settings.save()


def deactivate_with_change(win, edit=None):
    """Deaktivace tak, jak ji provede hlavní okno po potvrzení dialogu."""
    pm = win.pm
    pm.activate("058768")
    local = pm.local_dir("058768")
    (edit or (lambda l: write(l / "dily/nc_codes/E01.nc", "E01 v2")))(local)
    plan = pm.plan_upload("058768")
    res = pm.upload(plan, plan.default_assignments(), "zmena", None, True,
                    keep_local=bool(win.settings.scripts))
    win.run_commit_scripts("058768", "deactivate", res.script_files, "zmena", res.commit_id,
                           res.cleanup_pending, "Deaktivace", "Projekt byl deaktivován.")
    return local


def select_project(win, name="058768"):
    win.refresh(check_names=False)
    win.table.selectRow([p.name for p in win.projects].index(name))


MIRROR = 'MIRROR commit "*.nc" TO "{TAB:W}"\n'


def test_scripts_run_after_deactivation_and_clean_local(window):
    use_script(window, MIRROR)
    local = deactivate_with_change(window)
    assert files_in(window.w / "058768") == ["dily/nc_codes/E01.nc", "dily/nc_codes/E02.nc"]
    assert (window.w / "058768" / "dily/nc_codes/E01.nc").read_text(encoding="utf-8") == "E01 v2"
    assert not local.exists()                                          # uklizeno až po skriptu
    text, details = window.shown[-1]
    assert "Projekt byl deaktivován." in text and "Skripty proběhly: zrcadlo.spm" in text
    assert "MIRROR hotovo" in details
    with ProjectDB.for_project(window.o / "058768") as db:
        run = db.script_runs()[0]
        assert run["ok"] == 1 and run["action"] == "deactivate" and run["commit_id"] == db.last_commit_id()


def test_failed_script_keeps_local_and_can_be_rerun(window):
    use_script(window, MIRROR + 'STOP "Chybí NC program"\n')
    local = deactivate_with_change(window)
    text, details = window.shown[-1]
    assert "SKRIPT SELHAL" in text and "Chybí NC program" in text and "místní kopie zůstala" in text
    assert local.exists()                                              # nesmazáno
    select_project(window)
    p = window.selected()
    assert p.script_pending and not p.mine
    assert window.act_rerun_scripts.isEnabled()
    item = window.table.item(window.table.currentRow(), 0)
    assert "Skript po odevzdání selhal" in item.toolTip()
    pending = store.load_pending(window.pm.local_root, "058768")
    assert pending.cleanup and [f.status for f in pending.files] == ["modified", "unchanged", "unchanged"]

    use_script(window, MIRROR)                                         # oprava skriptu
    window.rerun_scripts()
    assert "Skripty proběhly" in window.shown[-1][0]
    assert not local.exists() and not store.load_pending(window.pm.local_root, "058768")
    select_project(window)
    assert not window.selected().script_pending
    with ProjectDB.for_project(window.o / "058768") as db:
        assert [r["ok"] for r in db.script_runs()] == [1, 0]


def test_missing_script_file_is_failure(window):
    window.settings.scripts = ["neni.spm"]
    local = deactivate_with_change(window)
    assert "SKRIPT SELHAL" in window.shown[-1][0] and "neni.spm" in window.shown[-1][0]
    assert local.exists()


def test_discard_pending(window):
    use_script(window, 'STOP "x"')
    deactivate_with_change(window)
    select_project(window)
    window.discard_scripts()
    select_project(window)
    assert not window.selected().script_pending


def test_rerun_on_reactivated_project_keeps_local(window):
    use_script(window, 'STOP "x"')
    local = deactivate_with_change(window)
    window.pm.activate("058768")                                       # znovu aktivován
    use_script(window, 'LOG "ok"')
    select_project(window)
    window.rerun_scripts()
    assert "Skripty proběhly" in window.shown[-1][0]
    assert window.pm.local_dir("058768").exists()


def test_no_scripts_deletes_local_immediately(window):
    window.settings.scripts = []
    local = deactivate_with_change(window)
    assert not local.exists()
    assert window.shown[-1] == "Projekt byl deaktivován."


def test_submit_without_deactivation_keeps_local(window):
    use_script(window, MIRROR)
    pm = window.pm
    pm.activate("058768")
    local = pm.local_dir("058768")
    write(local / "dily/nc_codes/E01.nc", "E01 v2")
    plan = pm.plan_upload("058768")
    e01 = next(bf.file_id for bf, _ in plan.modified)
    res = pm.upload(plan, plan.default_assignments(), "jen E01", {e01}, keep_local=True)
    assert not res.ended and not res.cleanup_pending
    window.run_commit_scripts("058768", "submit", res.script_files, "jen E01", res.commit_id,
                              res.cleanup_pending, "Odevzdání", "Změny byly odevzdány.")
    assert files_in(window.w / "058768") == ["dily/nc_codes/E01.nc"]
    assert local.exists()


# --- okno Skripty -----------------------------------------------------------------------

def test_scripts_dialog_selection_order_and_editing(window):
    from spm.gui.scripts_dialog import ScriptsDialog
    store.save_script("a.spm", 'LOG "a"')
    store.save_script("b.spm", 'LOG "b"')
    window.settings.scripts = ["b.spm", "chybi.spm"]
    dlg = ScriptsDialog(window, project="058768")
    assert dlg.names() == ["b.spm", "a.spm"]                          # vybrané nahoře v pořadí
    assert window.settings.scripts == ["b.spm"] and "chybi.spm" in dlg.output.toPlainText()
    dlg.list.item(1).setCheckState(dlg.list.item(1).checkState().__class__.Checked)
    assert window.settings.scripts == ["b.spm", "a.spm"]
    dlg.list.setCurrentRow(1)
    dlg.move(-1)
    assert window.settings.scripts == ["a.spm", "b.spm"]
    assert AppSettings.load().view(window.settings.workspace_id).scripts == ["a.spm", "b.spm"]

    assert dlg.current == "a.spm" and dlg.editor.toPlainText() == 'LOG "a"'
    dlg.editor.selectAll()
    dlg.editor.insertPlainText('LOG "a"\nCOPY commit')                # jako psaní – změna
    assert dlg.btn_save.isEnabled()
    assert not dlg.check() and "řádek 2" in dlg.output.toPlainText()
    assert dlg.editor.textCursor().blockNumber() == 1                 # kurzor na chybném řádku
    dlg.editor.setPlainText('FILES nc = commit "*.nc"\nMIRROR nc TO "{TAB:W}"')
    dlg.test_run()
    out = dlg.output.toPlainText()
    assert "(zkouška) MIRROR: 2 soubory" in out and "nic se nezapsalo" in out
    assert not (window.w / "058768").exists()
    assert dlg.save() and store.load_script("a.spm").startswith("FILES nc")
    dlg.deleteLater()


def test_scripts_dialog_new_rename_delete(window, monkeypatch):
    from PySide6.QtWidgets import QInputDialog

    from spm import recycle
    from spm.gui.scripts_dialog import ScriptsDialog
    monkeypatch.setattr(recycle, "to_recycle_bin", lambda paths: [p.unlink() for p in paths])
    answers = iter([("novy", True), ("prejmenovany", True)])
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: next(answers))
    dlg = ScriptsDialog(window, project=None)
    assert not dlg.btn_test.isEnabled()
    dlg.new_script(None)
    assert store.list_scripts() == ["novy.spm"] and dlg.current == "novy.spm"
    assert dlg.editor.toPlainText() == store.TEMPLATE
    dlg.list.item(0).setCheckState(dlg.list.item(0).checkState().__class__.Checked)
    other = window.app_settings.workspaces[1]["id"]
    window.app_settings.view(other).scripts = ["novy.spm"]
    dlg.rename_script()
    assert store.list_scripts() == ["prejmenovany.spm"]
    assert window.settings.scripts == ["prejmenovany.spm"]
    assert window.app_settings.view(other).scripts == ["prejmenovany.spm"]   # i v jiné záložce
    dlg.delete_script()
    assert store.list_scripts() == [] and window.settings.scripts == []
    assert window.app_settings.view(other).scripts == []
    dlg.deleteLater()


def test_scripts_dialog_network_scripts(window, monkeypatch):
    from PySide6.QtWidgets import QInputDialog

    from spm import recycle
    from spm.gui.scripts_dialog import ScriptsDialog
    monkeypatch.setattr(recycle, "to_recycle_bin", lambda paths: [p.unlink() for p in paths])
    net = store.network_dir(window.o)
    store.save_script("lokalni.spm", 'CALL SERVER "spolecne"')
    store.save_script("existujici.spm", 'LOG "ze sítě"', net)
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("spolecne", True))
    dlg = ScriptsDialog(window, project="058768")
    assert dlg.names() == ["lokalni.spm"] and dlg.net_names() == ["existujici.spm"]
    dlg.new_script(dlg.net_folder)
    assert store.list_scripts(net) == ["existujici.spm", "spolecne.spm"]
    assert dlg.current == "spolecne.spm" and dlg.folder == net and dlg.where.text() == "síťový skript"
    assert dlg.list.currentRow() == -1 and not dlg.btn_rename.isEnabled()
    dlg.editor.selectAll()
    dlg.editor.insertPlainText('SET cil = "{WORK}/x"\nLOG "spolecne {PROJECT}"')
    assert dlg.save() and store.load_script("spolecne.spm", net).startswith("SET cil")
    assert store.list_scripts() == ["lokalni.spm"]                    # místní beze změny
    dlg.list.setCurrentRow(0)                                          # přepnutí na místní
    assert dlg.current == "lokalni.spm" and dlg.folder is None and dlg.net_list.currentRow() == -1
    dlg.test_run()                                                     # místní volá síťový
    assert "spolecne 058768" in dlg.output.toPlainText()
    dlg.net_list.setCurrentRow(0)
    dlg.delete_script()
    assert store.list_scripts(net) == ["spolecne.spm"] and window.settings.scripts == []
    dlg.deleteLater()


def test_script_dialogs_shown_from_worker_thread(window, monkeypatch):
    """Skript běží ve vlákně – dialog se ukáže v hlavním vlákně a vlákno počká na odpověď."""
    import threading

    from PySide6.QtWidgets import QApplication, QInputDialog
    monkeypatch.setattr(QInputDialog, "getItem", lambda *a, **k: ("DMU80", True))
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("", False))
    result = {}

    def work():
        ui = window.script_ui
        result["thread"] = threading.current_thread() is not threading.main_thread()
        result["choose"] = ui.choose("t", "Stroj", ["DMU50", "DMU80"], 0)
        result["ask"] = ui.ask("t", "Pokračovat?")              # question je v testu vždy Ano
        result["input"] = ui.input("t", "Zakázka", "x")          # Storno
    t = threading.Thread(target=work)
    t.start()
    while t.is_alive():
        QApplication.processEvents()
        t.join(0.01)
    assert result == {"thread": True, "choose": 1, "ask": True, "input": None}


def test_progress_hidden_while_script_dialog_open(window, monkeypatch):
    """Okno průběhu (odevzdání) se během dialogu skriptu neukáže, ani když dialog trvá déle
    než jeho minimumDuration; potom se zase ukáže a po skončení úlohy zavře."""
    import time

    from PySide6.QtWidgets import QApplication, QMessageBox

    from spm.gui import worker
    seen = {}

    def wait(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            QApplication.processEvents()
            time.sleep(0.01)

    def question(*a, **k):
        seen["before"] = seen.get("shown_before")
        wait(0.6)                                          # uživatel přemýšlí
        seen["during"] = [d.isVisible() for d in worker._active_progress]
        return QMessageBox.StandardButton.Yes
    monkeypatch.setattr(QMessageBox, "question", question)
    result = {}

    def work(progress=None):
        progress(0, 2, "nahrávání")                        # průběh spustí časovač okna
        time.sleep(0.5)                                    # okno průběhu se mezitím ukáže
        result["ask"] = window.script_ui.ask("t", "Zrcadlit?")
        time.sleep(0.5)                                    # a po dialogu znovu
        return "hotovo"
    worker.run_task(window, "Deaktivace", work, on_done=lambda r: result.setdefault("done", r))
    dlg = worker._active_progress[0]
    shown = {"before": False, "after": False}
    end = time.monotonic() + 10
    while "done" not in result and time.monotonic() < end:
        QApplication.processEvents()
        if dlg.isVisible():
            shown["after" if "ask" in result else "before"] = True
        time.sleep(0.01)
    assert result == {"ask": True, "done": "hotovo"}
    assert seen["during"] == [False]
    assert shown == {"before": True, "after": True}
    assert worker._active_progress == []


def test_form_dialog_values(window):
    from spm.gui.script_ui import FormDialog
    dlg = FormDialog(window, "Skript", "Předání", [
        {"kind": "INPUT", "name": "zak", "label": "Zakázka", "default": "123", "options": []},
        {"kind": "CHOOSE", "name": "stroj", "label": "Stroj", "default": 1, "options": ["A", "B"]},
        {"kind": "ASK", "name": "mail", "label": "E-mail", "default": True, "options": []}])
    assert dlg.values() == {"zak": "123", "stroj": 1, "mail": True}
    dlg.widgets["zak"].setText("456")
    dlg.widgets["stroj"].setCurrentIndex(0)
    dlg.widgets["mail"].setChecked(False)
    assert dlg.values() == {"zak": "456", "stroj": 0, "mail": False}
    dlg.deleteLater()


def test_scripts_after_submit_use_dialogs(window, monkeypatch):
    from PySide6.QtWidgets import QInputDialog
    monkeypatch.setattr(QInputDialog, "getItem", lambda *a, **k: ("NC", True))
    use_script(window, 'SET sl = CHOOSE("Složka", "NC", "CAM")\n'
                       'MIRROR commit "*.nc" TO "{TAB:W}/{PROJECT}/{sl}"\n')
    deactivate_with_change(window)
    assert files_in(window.w / "058768") == ["NC/dily/nc_codes/E01.nc", "NC/dily/nc_codes/E02.nc"]


def test_toolbar_has_scripts_not_mirror(window):
    texts = [a.text() for a in window.findChildren(type(window.act_scripts))]
    assert "Skripty" in texts and "Zrcadlení" not in texts


def test_history_shows_script_log(window):
    from spm.gui.timeline_dialog import ROLE, ROW_COMMIT, TimelineDialog
    use_script(window, 'LOG "ahoj ze skriptu"')
    deactivate_with_change(window)
    with ProjectDB.for_project(window.o / "058768") as db:
        dlg = TimelineDialog(db, "058768")
        cards = [r for r in range(dlg.timeline.count())
                 if dlg.timeline.item(r).data(ROLE)["row"] == ROW_COMMIT]
        dlg.timeline.setCurrentRow(cards[0])                            # odevzdání se skriptem
        assert not dlg.btn_scripts.isHidden()
        assert dlg.btn_scripts.text() == "Skripty: proběhly…"
        dlg.show_script_log()
        assert "ahoj ze skriptu" in window.shown[-1][1]
        dlg.timeline.setCurrentRow(cards[1])                            # aktivace – bez skriptů
        assert dlg.btn_scripts.isHidden()
        dlg.deleteLater()


# --- zrcadlo ve stromu cíle -----------------------------------------------------------------

def test_target_tree_shows_mirror_source(window):
    from conftest import settle_panel
    from tests_helpers_tree import find_item
    use_script(window, MIRROR)
    deactivate_with_change(window)
    window.tabs.setCurrentIndex(1)
    window.pm.activate("058768")                                       # „všechny volné“
    write(window.w / "058768" / "vykres.pdf", "pdf")
    select_project(window)
    settle_panel(window.panel)
    tree = window.panel.tree
    assert not tree.isColumnHidden(2)
    nc = find_item(tree, "dily/nc_codes/E01.nc")
    assert (nc.text(1), nc.text(2)) == ("", "← O")
    assert find_item(tree, "dily").text(2) == "← O (2)"


def test_target_tree_shows_stale_mirror_and_releases_it(window, monkeypatch):
    from conftest import settle_panel
    from PySide6.QtWidgets import QMessageBox
    from tests_helpers_tree import find_item
    use_script(window, MIRROR)
    deactivate_with_change(window)                                     # E01, E02 zrcadleny
    use_script(window, 'MIRROR commit "*.nc;!*E01.nc" TO "{TAB:W}"\n')
    deactivate_with_change(window, lambda l: write(l / "dily/nc_codes/E01.nc", "E01 v3"))
    window.tabs.setCurrentIndex(1)
    select_project(window)
    settle_panel(window.panel)
    tree = window.panel.tree
    e01 = find_item(tree, "dily/nc_codes/E01.nc")
    assert e01.text(2) == "← O · neaktuální"
    assert "nezrcadlil" in e01.toolTip(2) and "nezrcadlil" in e01.toolTip(0)
    assert find_item(tree, "dily/nc_codes/E02.nc").text(2) == "← O"
    assert "neaktuální: <b>1</b>" in window.panel.summary.text()
    actions = window.panel.context_actions
    assert actions("dily/nc_codes/E01.nc")["release_mirror"]
    assert not actions("dily/nc_codes/E02.nc")["release_mirror"]
    assert actions("dily/")["release_mirror"]
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    window.panel.release_mirror(["dily/"])
    settle_panel(window.panel)
    assert find_item(tree, "dily/nc_codes/E01.nc").text(2) == ""
    assert find_item(tree, "dily/nc_codes/E02.nc").text(2) == "← O"


def test_source_tree_shows_mirror_target(window):
    from conftest import settle_panel
    from tests_helpers_tree import find_item
    use_script(window, MIRROR + 'MIRROR commit "**/E01.nc" TO "{TAB:W}/CAM" FLAT\n')
    deactivate_with_change(window)
    select_project(window)
    settle_panel(window.panel)
    tree = window.panel.tree
    assert find_item(tree, "dily/nc_codes/E01.nc").text(2) == "→ W, → W/CAM"
    assert find_item(tree, "dily/nc_codes/E02.nc").text(2) == "→ W"
    assert find_item(tree, "vykres.pdf").text(2) == ""
    assert find_item(tree, "dily").text(2) == "→ W (2), → W/CAM (1)"
    assert f"Zrcadleno do {window.w / 'CAM'} – E01.nc" in find_item(tree, "dily/nc_codes/E01.nc").toolTip(2)
    assert "zrcadleno: <b>2</b> → W, <b>1</b> → W/CAM" in window.panel.summary.text()


def test_source_label_by_server_identity(window):
    from spm.mirror import Mirror, MirrorItem, MirrorSource
    o_tab, w_tab = (ws["id"] for ws in window.app_settings.workspaces)
    window.app_settings.view(o_tab).server_id = "srv-o"
    target = window.pm_for(w_tab)
    src = window.o / "058768" / "dily/nc_codes/E01.nc"
    Mirror(MirrorSource("pid", str(window.o / "058768"), "srv-o", "O: Nástrojárna"), target,
           "058768").run([MirrorItem("f1", "added", "E01.nc", None, src)])
    entry = next(e for e in target.file_entries("058768"))
    window.app_settings.workspace(o_tab)["server_root"] = r"Z:\uplne\jina\cesta"
    assert window.mirror_source_label(entry) == "O"                   # podle identity složky
    window.app_settings.view(o_tab).server_id = ""
    assert window.mirror_source_label(entry) == "O: Nástrojárna"      # název ze zdroje


# --- spm_config.json a nastavení -------------------------------------------------------------

# --- BEFORE v odevzdání přes hlavní okno -------------------------------------------------------

BEFORE_ZIP = '''BEFORE
    SET archiv = "Export/{PROJECT}.zip"
    ZIP commit "*.nc" STATUS changed TO "{LOCAL_DIR}/{archiv}" KEEP 1
END
MIRROR commit "**/{archiv}" TO "{TAB:W}"
'''


def confirm_upload(win, monkeypatch, end_session=True, edit=None):
    """Odevzdání / deaktivace přes hlavní okno (dialog odevzdání se potvrdí s komentářem)."""
    pm = win.pm
    pm.activate("058768")
    (edit or (lambda l: write(l / "dily/nc_codes/E01.nc", "E01 v2")))(pm.local_dir("058768"))

    def accept(dlg, key):
        if hasattr(dlg, "comment"):
            dlg.comment.setPlainText("zmena")
        return True
    monkeypatch.setattr(win, "_exec_dialog", accept)
    win._confirm_upload(pm.plan_upload("058768"), end_session=end_session)
    return pm.local_dir("058768")


def test_before_file_is_submitted_with_upload(window, monkeypatch):
    use_script(window, BEFORE_ZIP)
    local = confirm_upload(window, monkeypatch)
    text, details = window.shown[-1]
    assert "Skript (BEFORE) přidal k odevzdání:\n  Export/058768.zip" in text
    assert "Skripty proběhly" in text and "== zrcadlo.spm – BEFORE ==" in details
    assert (window.o / "058768" / "Export" / "058768.zip").exists()
    assert files_in(window.w / "058768") == ["Export/058768.zip"]
    assert not local.exists()
    with ProjectDB.for_project(window.o / "058768") as db:
        assert len([c for c in db.commits() if c["kind"] == "upload"]) == 1   # jedno odevzdání


def test_before_failure_uploads_nothing(window, monkeypatch):
    use_script(window, 'BEFORE\n  ZIP commit "*.nc" TO "{LOCAL_DIR}/x.zip"\n  STOP "chybí výkres"\nEND\n')
    local = confirm_upload(window, monkeypatch)
    text, details = window.shown[-1]
    assert "SKRIPT SELHAL PŘED ODEVZDÁNÍM" in text and "chybí výkres" in text
    assert (local / "x.zip").exists()                                  # zůstal v místní kopii
    assert (window.o / "058768" / "dily/nc_codes/E01.nc").read_text(encoding="utf-8") == "E01 v1"
    select_project(window)
    p = window.selected()
    assert p.mine and not p.script_pending                              # pořád aktivovaný


def test_syntax_error_or_missing_script_uploads_nothing(window, monkeypatch):
    use_script(window, "NESMYSL")
    confirm_upload(window, monkeypatch)
    assert "Skript obsahuje chybu – nic se neodevzdalo" in window.shown[-1]
    window.settings.scripts = ["neni.spm"]
    window._confirm_upload(window.pm.plan_upload("058768"), end_session=True)
    assert "nelze načíst" in window.shown[-1] and "Nic se neodevzdalo" in window.shown[-1]
    assert (window.o / "058768" / "dily/nc_codes/E01.nc").read_text(encoding="utf-8") == "E01 v1"


def test_rerun_after_failure_keeps_before_variables(window, monkeypatch):
    use_script(window, BEFORE_ZIP + 'STOP "{archiv}"\n')
    local = confirm_upload(window, monkeypatch)
    assert "STOP: Export/058768.zip" in window.shown[-1][0]
    assert local.exists() and (window.o / "058768" / "Export" / "058768.zip").exists()
    use_script(window, BEFORE_ZIP)                                     # oprava – BEFORE se znovu nespustí
    select_project(window)
    window.rerun_scripts()
    assert "Skripty proběhly" in window.shown[-1][0]
    assert files_in(window.w / "058768") == ["Export/058768.zip"] and not local.exists()


def test_publish_with_before(window, monkeypatch):
    local = window.pm.local_root / "NOVY"
    write(local / "a.nc", "a")
    use_script(window, 'BEFORE\n  WRITE "{LOCAL_DIR}/info.txt"\n  {PROJECT}\n  END\nEND\n'
                       'MIRROR commit TO "{TAB:W}"\n')
    monkeypatch.setattr(window, "_exec_dialog", lambda dlg, key: dlg.comment.setPlainText("prvni") or True)
    select_project(window, "NOVY")
    window.publish()
    text, _ = window.shown[-1]
    assert "Skript (BEFORE) přidal k publikování:\n  info.txt" in text
    assert files_in(window.o / "NOVY") == ["a.nc", "info.txt"]
    assert files_in(window.w / "NOVY") == ["a.nc", "info.txt"]


def test_secrets_dialog_and_settings(window, monkeypatch):
    from PySide6.QtWidgets import QLineEdit

    from spm.gui import settings_dialog
    from spm.gui.settings_dialog import SecretsDialog, SettingsDialog
    dlg = SecretsDialog({"cam": "abc"})
    assert dlg.table.cellWidget(0, 1).echoMode() == QLineEdit.EchoMode.Password
    dlg.show_values.setChecked(True)
    assert dlg.table.cellWidget(0, 1).echoMode() == QLineEdit.EchoMode.Normal
    dlg._add_row("api", "xyz")
    assert dlg.values() == {"cam": "abc", "api": "xyz"}
    dlg._add_row("CAM", "dup")
    shown = []
    monkeypatch.setattr(settings_dialog.QMessageBox, "warning", lambda *a, **k: shown.append(a[2]))
    dlg._accept()
    assert shown and "opakovat" in shown[0] and not dlg.result()
    dlg.deleteLater()

    class Accept(SecretsDialog):
        def exec(self):
            self.table.removeRow(0)
            self._add_row("cam", "tajne")
            return True
    monkeypatch.setattr(settings_dialog, "SecretsDialog", Accept)
    sd = SettingsDialog(window.settings)
    sd._edit_secrets()
    assert sd.btn_secrets.text() == "Upravit… (1)"
    sd._accept()
    assert window.settings.secrets == {"cam": "tajne"}
    assert AppSettings.load().view(window.settings.workspace_id).secrets == {"cam": "tajne"}
    assert window.script_env("058768", "test", []).secrets == {"cam": "tajne"}
    sd.deleteLater()


def test_old_mirror_settings_are_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DIR", tmp_path / "app")
    root = tmp_path / "S"
    root.mkdir()
    (root / sc.CONFIG_NAME).write_text(json.dumps(
        {"backup_count": 4, "mirror_rules": [{"target_server": "x", "files": "*.nc"}]}), encoding="utf-8")
    assert sc.probe(str(root)).values == {"backup_count": 4}           # pravidla se ignorují
    sc.write(str(root), {"backup_count": 5})
    data = json.loads((root / sc.CONFIG_NAME).read_text(encoding="utf-8"))
    assert "mirror_rules" not in data and data["backup_count"] == 5
    settings = config.settings_path()
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"mirror_rules": [{"a": 1}], "mirror_enabled": False, "workspaces": [
        {"id": "w1", "name": "O", "server_root": str(root), "shared_mirror_rules": [], "scripts": ["a.spm"]}]}),
        encoding="utf-8")
    app = AppSettings.load()
    assert "shared_mirror_rules" not in app.workspaces[0] and app.view("w1").scripts == ["a.spm"]
    app.save()
    saved = json.loads(settings.read_text(encoding="utf-8"))
    assert "mirror_rules" not in saved and "mirror_enabled" not in saved
