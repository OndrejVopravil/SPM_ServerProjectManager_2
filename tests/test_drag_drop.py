"""Kopírování přetažením: do projektu (plan_copy_in / copy_in) a ven (export_copies, panel)."""
import os
import stat
from pathlib import Path

import pytest

from spm.config import Settings
from spm.db import ProjectDB
from spm.sync import COPY_ADD, COPY_REPLACE, COPY_SKIP, ProjectManager


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.chmod(stat.S_IWRITE | stat.S_IREAD)
    path.write_text(text, encoding="utf-8")


def writable(path: Path) -> bool:
    return os.access(path, os.W_OK)


@pytest.fixture
def env(tmp_path):
    server = tmp_path / "server"
    for f in ("sestava/sestava.elt", "sestava/elektrody/E01.elt", "sestava/elektrody/E02.elt",
              "dily/deska.elt"):
        write(server / "Forma" / f, f)
    src = tmp_path / "zdroj"
    write(src / "E01.elt", "nova E01")
    write(src / "nova.elt", "nova")
    write(src / "Díl 1.elt", "spatny nazev")
    write(src / "~$docasny.elt", "lock")
    write(src / "slozka" / "a.elt", "a")
    write(src / "slozka" / "pod" / "b.elt", "b")

    def pm(user):
        return ProjectManager(Settings(server_root=str(server), local_root=str(tmp_path / f"local_{user}")),
                              user=user, pc="PC-" + user)
    alice, bob = pm("alice"), pm("bob")
    alice.activate("Forma", lock_paths=["sestava/elektrody/E02.elt"])
    bob.activate("Forma", lock_paths=["sestava/elektrody/E01.elt"])
    return alice, bob, server / "Forma", src


def plan_of(items):
    return {i.rel: (i.action, i.reason) for i in items}


def test_plan_copy_in_rules(env):
    alice, bob, proj, src = env
    write(alice.local_dir("Forma") / "moje_nova.elt", "moje")          # můj nový neodevzdaný
    plan = plan_of(alice.plan_copy_in("Forma", [
        (src / "nova.elt", "sestava/elektrody/nova.elt"),               # nový
        (src / "E01.elt", "sestava/elektrody/E02.elt"),                 # můj aktivovaný
        (src / "E01.elt", "SESTAVA/elektrody/e02.ELT"),                 # tentýž – jiná velikost písmen
        (src / "E01.elt", "sestava/elektrody/E01.elt"),                 # drží bob
        (src / "nova.elt", "dily/deska.elt"),                           # volný, neaktivovaný
        (src / "nova.elt", "moje_nova.elt"),                            # můj nový – lze nahradit
        (src / "Díl 1.elt", "Díl 1.elt"),                               # nepovolené znaky
        (src / "~$docasny.elt", "~$docasny.elt"),                       # ignorovaný
        (src / "nova.elt", ".spm/x.elt"),                               # servisní složka
    ]))
    assert plan["sestava/elektrody/nova.elt"] == (COPY_ADD, "")
    assert plan["sestava/elektrody/E02.elt"] == (COPY_REPLACE, "")
    assert plan["SESTAVA/elektrody/e02.ELT"][0] == COPY_SKIP           # stejný soubor podruhé
    assert plan["sestava/elektrody/E01.elt"][0] == COPY_SKIP
    assert plan["sestava/elektrody/E01.elt"][1].startswith("drží ho bob")
    assert plan["dily/deska.elt"] == (COPY_SKIP, "není aktivovaný")
    assert plan["moje_nova.elt"] == (COPY_REPLACE, "")
    assert plan["Díl 1.elt"] == (COPY_SKIP, "nepovolené znaky v názvu")
    assert plan["~$docasny.elt"] == (COPY_SKIP, "ignorovaný soubor")
    assert plan[".spm/x.elt"] == (COPY_SKIP, "servisní složka")


def test_copy_in_writes_only_local_copy(env):
    alice, bob, proj, src = env
    local = alice.local_dir("Forma")
    plan = alice.plan_copy_in("Forma", [(src / "nova.elt", "dily/nova.elt"),
                                        (src / "E01.elt", "sestava/elektrody/E02.elt"),
                                        (src / "nova.elt", "dily/deska.elt")])
    assert alice.copy_in("Forma", plan) == ["dily/nova.elt", "sestava/elektrody/E02.elt"]
    assert (local / "dily/nova.elt").read_text(encoding="utf-8") == "nova"
    assert (local / "sestava/elektrody/E02.elt").read_text(encoding="utf-8") == "nova E01"
    assert (local / "dily/deska.elt").read_text(encoding="utf-8") == "dily/deska.elt"   # přeskočen
    assert writable(local / "dily/nova.elt")
    assert not (proj / "dily/nova.elt").exists()                     # na server až odevzdáním
    up = alice.plan_upload("Forma")
    assert [lf.path for lf in up.new] == ["dily/nova.elt"]
    assert [lf.path for _, lf in up.modified] == ["sestava/elektrody/E02.elt"]


def test_copy_into_mirrored_file_is_skipped(env, tmp_path):
    alice, bob, proj, src = env
    with ProjectDB.for_project(proj) as db:
        db.ensure_mirror_tables()
        fid = db.active_files()["sestava/elektrody/E02.elt"].file_id
        with db.conn:
            db.set_mirrored(fid, "zdroj", "x")
    plan = plan_of(alice.plan_copy_in("Forma", [(src / "E01.elt", "sestava/elektrody/E02.elt")]))
    assert plan["sestava/elektrody/E02.elt"] == (COPY_SKIP, "spravuje ho zrcadlo")


def test_local_only_folder_accepts_everything(env, tmp_path):
    alice, bob, proj, src = env
    write(alice.local_root / "Nova" / "a.elt", "puvodni")
    plan = plan_of(alice.plan_copy_in("Nova", [(src / "nova.elt", "a.elt"), (src / "nova.elt", "b.elt")]))
    assert plan == {"a.elt": (COPY_REPLACE, ""), "b.elt": (COPY_ADD, "")}


def test_export_copies_are_writable_and_include_missing_files(env, tmp_path):
    alice, bob, proj, src = env
    local = alice.local_dir("Forma")
    assert not writable(local / "dily/deska.elt")                   # neaktivovaný = jen pro čtení
    (local / "sestava" / "sestava.elt").chmod(stat.S_IWRITE | stat.S_IREAD)
    (local / "sestava" / "sestava.elt").unlink()                    # nestažený / chybí místně
    dest = tmp_path / "plocha"
    tops = alice.export_copies("Forma", [("dily/deska.elt", "deska.elt"),
                                         ("sestava/sestava.elt", "sestava/sestava.elt"),
                                         ("sestava/elektrody/E01.elt", "sestava/elektrody/E01.elt")], dest)
    assert sorted(p.name for p in tops) == ["deska.elt", "sestava"]
    assert writable(dest / "deska.elt") and (dest / "deska.elt").read_text(encoding="utf-8") == "dily/deska.elt"
    assert (dest / "sestava/sestava.elt").read_text(encoding="utf-8") == "sestava/sestava.elt"   # ze serveru
    assert writable(dest / "sestava/elektrody/E01.elt")


# --- panel ---------------------------------------------------------------------------------

@pytest.fixture
def panel(env, tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from conftest import settle_panel
    from spm import config
    from spm.gui import project_tree
    from spm.gui.project_panel import ProjectPanel
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(config, "APP_DIR", tmp_path / "app")
    monkeypatch.setattr(project_tree, "STAGING_ROOT", tmp_path / "staging")
    alice = env[0]
    p = ProjectPanel(alice)
    p.show_project(alice.project_status("Forma"))
    settle_panel(p)
    yield p
    p.deleteLater()


def test_drag_items_names(panel):
    items = dict(panel.drag_items(["sestava/elektrody/", "dily/deska.elt", "sestava/elektrody/E01.elt"]))
    assert items == {"sestava/elektrody/E01.elt": "elektrody/E01.elt",
                     "sestava/elektrody/E02.elt": "elektrody/E02.elt",
                     "dily/deska.elt": "deska.elt"}
    write(panel.pm.local_dir("Forma") / "deska.elt", "jina deska")     # stejný název jinde
    from conftest import settle_panel
    panel.reload()
    settle_panel(panel)
    items = dict(panel.drag_items(["dily/deska.elt", "deska.elt"]))
    assert items == {"dily/deska.elt": "deska.elt", "deska.elt": "deska (2).elt"}


def test_drag_mime_direct_or_staged(panel, tmp_path):
    from spm.gui.project_tree import MIME_FILES, mime_items
    local = panel.pm.local_dir("Forma")
    mime = panel.drag_mime(["sestava/elektrody/E02.elt"])            # můj zapisovatelný soubor
    assert [Path(u.toLocalFile()) for u in mime.urls()] == [local / "sestava/elektrody/E02.elt"]
    assert mime_items(mime) == [("sestava/elektrody/E02.elt", "E02.elt")]
    mime = panel.drag_mime(["dily/deska.elt", "sestava/elektrody/E02.elt"])   # deska jen pro čtení
    urls = [Path(u.toLocalFile()) for u in mime.urls()]
    assert all(str(u).startswith(str(tmp_path / "staging")) for u in urls)
    assert all(writable(u) for u in urls) and sorted(u.name for u in urls) == ["E02.elt", "deska.elt"]
    mime = panel.drag_mime(["sestava/"])                             # složka – vždy kopie
    (top,) = [Path(u.toLocalFile()) for u in mime.urls()]
    assert top.name == "sestava" and sorted(p.relative_to(top).as_posix() for p in top.rglob("*")
                                            if p.is_file()) == ["elektrody/E01.elt", "elektrody/E02.elt",
                                                                "sestava.elt"]
    assert mime.hasFormat(MIME_FILES)


def test_drop_from_explorer_and_between_folders(panel, env, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from conftest import settle_panel
    alice, bob, proj, src = env
    shown = []
    monkeypatch.setattr(QMessageBox, "exec", lambda self: shown.append(self.text())
                        or QMessageBox.StandardButton.Yes)
    local = alice.local_dir("Forma")
    panel.drop_files([src / "nova.elt", src / "slozka"], None, "dily/")      # jen nové – bez dotazu
    settle_panel(panel)
    assert shown == []
    assert (local / "dily/nova.elt").exists() and (local / "dily/slozka/pod/b.elt").exists()
    assert any(e.path == "dily/slozka/pod/b.elt" and e.local_new for e in panel.entries)
    # mezi složkami stromu: elektrody -> dily (E02 je můj, nahradí se v dily? ne – nový soubor)
    panel.drop_files([], panel.drag_items(["sestava/elektrody/"]), "dily/")
    settle_panel(panel)
    assert (local / "dily/elektrody/E01.elt").read_text(encoding="utf-8") == "sestava/elektrody/E01.elt"
    # nahrazení vlastního a přeskočení cizího – zeptá se
    panel.drop_files([src / "E01.elt"], None, "sestava/elektrody/")
    settle_panel(panel)
    assert "Nic se nezkopírovalo" in shown[-1]                    # E01 drží bob
    assert (local / "sestava/elektrody/E01.elt").read_text(encoding="utf-8") == "sestava/elektrody/E01.elt"
    write(src / "E02.elt", "nova E02")
    panel.drop_files([src / "E02.elt"], None, "sestava/elektrody/")
    settle_panel(panel)
    assert "nahradí se (máte je aktivované): 1" in shown[-1]
    assert (local / "sestava/elektrody/E02.elt").read_text(encoding="utf-8") == "nova E02"
    # odmítnutí dotazu = nic
    monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.StandardButton.No)
    write(src / "E02.elt", "jeste novejsi")
    panel.drop_files([src / "E02.elt"], None, "sestava/elektrody/")
    assert (local / "sestava/elektrody/E02.elt").read_text(encoding="utf-8") == "nova E02"


def test_drop_only_into_active_project(panel, env):
    alice = env[0]
    assert panel.tree.can_drop()
    alice.cancel_activation("Forma")
    panel.show_project(alice.project_status("Forma"))
    assert not panel.tree.can_drop()


def test_drop_target_folder(panel):
    from conftest import settle_panel
    from tests_helpers_tree import find_item
    settle_panel(panel)
    tree = panel.tree
    tree.expandAll()
    for path, expected in (("sestava/elektrody/E01.elt", "sestava/elektrody/"), ("sestava", "sestava/"),
                           ("dily/deska.elt", "dily/")):
        item = find_item(tree, path)
        pos = tree.visualItemRect(item).center()
        assert tree.drop_folder(pos)[0] == expected
    assert tree.drop_folder(tree.viewport().rect().bottomLeft())[0] == ""


# --- nová složka --------------------------------------------------------------------------

def test_create_and_list_empty_folders(env):
    from spm.sync import SyncError
    alice, bob, proj, src = env
    local = alice.local_dir("Forma")
    assert alice.create_local_folder("Forma", "", "Nova") == "Nova"
    assert alice.create_local_folder("Forma", "sestava/", "pod") == "sestava/pod"
    assert alice.create_local_folder("Forma", "Nova/", "hluboka") == "Nova/hluboka"
    write(local / "sestava" / "pod2" / "Thumbs.db", "x")             # jen ignorovaný soubor = prázdná
    assert alice.local_empty_folders("Forma") == ["Nova/hluboka", "sestava/pod", "sestava/pod2"]
    with pytest.raises(SyncError, match="už existuje"):
        alice.create_local_folder("Forma", "", "dily")
    with pytest.raises(SyncError, match="nepovolené znaky"):
        alice.create_local_folder("Forma", "", "Nová")
    alice.settings.check_forbidden_chars = False                    # i bez kontroly znaků
    with pytest.raises(SyncError, match="servisní složky .spm"):
        alice.create_local_folder("Forma", "", ".spm")
    alice.settings.check_forbidden_chars = True
    (local / "dily" / "deska.elt").chmod(stat.S_IWRITE | stat.S_IREAD)
    import shutil
    shutil.rmtree(local / "dily")                                    # místně není, na serveru ano
    with pytest.raises(SyncError, match="Na serveru už je"):
        alice.create_local_folder("Forma", "", "DILY")
    alice.remove_empty_local_folder("Forma", "Nova/")
    assert not (local / "Nova").exists()
    write(local / "sestava" / "pod" / "a.elt", "a")
    with pytest.raises(SyncError, match="není prázdná"):
        alice.remove_empty_local_folder("Forma", "sestava/pod")
    with pytest.raises(SyncError, match="aktivován"):
        bob.cancel_activation("Forma") or bob.create_local_folder("Forma", "", "X")


def test_panel_new_folder_rename_drop_delete(panel, env, monkeypatch):
    from PySide6.QtWidgets import QInputDialog, QMessageBox

    from conftest import settle_panel
    from tests_helpers_tree import find_item
    alice, bob, proj, src = env
    local = alice.local_dir("Forma")
    answers = iter([("Nová", True), ("Nova", True), ("podslozka", True), ("Prejmenovana", True)])
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: next(answers))
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a[2]))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    assert panel.context_actions([])["new_folder"]
    panel.create_folder("")                                          # prázdné místo stromu
    settle_panel(panel)
    assert warnings and "nepovolené znaky" in warnings[0]
    item = find_item(panel.tree, "Nova")
    assert item is not None and item.text(1) == "prázdná složka" and "na server" in item.toolTip(0)
    panel.create_folder("Nova/")                                     # podsložka
    settle_panel(panel)
    assert find_item(panel.tree, "Nova/podslozka").text(1) == "prázdná složka"
    acts = panel.context_actions(["Nova/"])
    assert acts["rename"] and acts["delete"]
    panel._rename("Nova/")                                           # přejmenovat prázdnou složku
    settle_panel(panel)
    assert (local / "Prejmenovana" / "podslozka").is_dir() and not (local / "Nova").exists()
    panel.drop_files([src / "nova.elt"], None, "Prejmenovana/podslozka/")   # puštění do prázdné
    settle_panel(panel)
    assert (local / "Prejmenovana/podslozka/nova.elt").exists()
    item = find_item(panel.tree, "Prejmenovana/podslozka")
    assert item.text(1) == ""                                         # už není prázdná
    assert panel.context_actions(["Prejmenovana/"])["delete"]        # obsahuje můj nový soubor
    # mazání prázdné složky
    (local / "Prazdna").mkdir()
    panel.reload()
    settle_panel(panel)
    assert panel.context_actions(["Prazdna/"])["delete"]
    panel._delete(["Prazdna/"])
    settle_panel(panel)
    assert not (local / "Prazdna").exists() and panel.tree.find_folder("Prazdna/") is None


def test_new_folder_only_in_active_project(panel, env):
    alice = env[0]
    alice.cancel_activation("Forma")
    panel.show_project(alice.project_status("Forma"))
    assert not panel.context_actions([])["new_folder"]
