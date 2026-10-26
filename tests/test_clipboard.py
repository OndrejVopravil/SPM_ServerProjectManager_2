"""Schránka ve stromu projektu: Kopírovat / Vyjmout / Vložit (a přesun move_local)."""
import os
from pathlib import Path

import pytest
from conftest import settle_panel
from test_drag_drop import env, panel, write, writable  # noqa: F401 – fixtures

from spm import recycle


@pytest.fixture
def clip(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from spm.gui import project_panel
    monkeypatch.setattr(project_panel, "CLIPBOARD_ROOT", tmp_path / "clip")
    QApplication.clipboard().clear()
    return QApplication.clipboard()


@pytest.fixture
def recycled(monkeypatch):
    """Koš Windows nahrazen trvalým smazáním (testy nemají plnit skutečný Koš)."""
    removed = []

    def fake(paths):
        for p in paths:
            removed.append(Path(p))
            os.chmod(p, 0o666)
            os.remove(p)
    monkeypatch.setattr(recycle, "to_recycle_bin", fake)
    return removed


def test_move_local_rules(env):
    alice, bob, proj, src = env
    local = alice.local_dir("Forma")
    write(local / "moje.elt", "moje")                                   # nový neodevzdaný
    write(local / "dily/obsazeny.elt", "x")
    moved, skipped = alice.move_local("Forma", [
        ("sestava/elektrody/E02.elt", "dily/E02.elt"),                  # můj zamčený
        ("moje.elt", "dily/nove/moje.elt"),                             # nový – i do nové složky
        ("sestava/elektrody/E01.elt", "dily/E01.elt"),                  # drží bob
        ("dily/deska.elt", "deska.elt"),                                # volný, nezamčený
        ("sestava/sestava.elt", "x.elt"),                               # nezamčený
    ])
    assert moved == ["dily/E02.elt", "dily/nove/moje.elt"]
    assert (local / "dily/E02.elt").exists() and not (local / "sestava/elektrody/E02.elt").exists()
    assert {rel for rel, _ in skipped} == {"sestava/elektrody/E01.elt", "dily/deska.elt", "sestava/sestava.elt"}
    moved, skipped = alice.move_local("Forma", [("dily/E02.elt", "dily/obsazeny.elt")])
    assert moved == [] and "v cíli už je" in skipped[0][1]                # nepřepisuje
    # odevzdání pozná přesun jako přejmenování (zachová historii)
    plan = alice.plan_upload("Forma")
    alice.upload(plan, plan.default_assignments(), "přesun")
    assert (proj / "dily/E02.elt").exists() and not (proj / "sestava/elektrody/E02.elt").exists()


def test_copy_paste_in_same_project(panel, env, clip):
    alice, bob, proj, src = env
    local = alice.local_dir("Forma")
    assert panel.copy_selected(["sestava/elektrody/E02.elt", "dily/deska.elt"])
    panel.paste_into("sestava/")
    settle_panel(panel)
    assert (local / "sestava/E02.elt").exists() and (local / "sestava/elektrody/E02.elt").exists()
    assert writable(local / "sestava/deska.elt")                         # kopie není jen pro čtení
    assert clip.mimeData().hasUrls()                                     # zkopírované zůstává ve schránce


def test_copy_of_readonly_goes_to_clipboard_staging(panel, env, clip, tmp_path):
    assert panel.copy_selected(["dily/deska.elt"])
    (url,) = [Path(u.toLocalFile()) for u in clip.mimeData().urls()]
    assert str(url).startswith(str(tmp_path / "clip")) and writable(url)  # i pro Průzkumník


def test_cut_paste_moves_only_own_files(panel, env, clip, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    alice, bob, proj, src = env
    local = alice.local_dir("Forma")
    shown = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a[2]))
    assert not panel.can_cut(["dily/deska.elt"]) and not panel.can_cut(["sestava/elektrody/"])
    assert not panel.copy_selected(["dily/deska.elt"], cut=True) and shown
    assert panel.can_cut(["sestava/elektrody/E02.elt"])
    assert panel.copy_selected(["sestava/elektrody/E02.elt"], cut=True)
    panel.paste_into("dily/")
    settle_panel(panel)
    assert (local / "dily/E02.elt").exists() and not (local / "sestava/elektrody/E02.elt").exists()
    assert clip.mimeData() is None or not clip.mimeData().hasUrls()                                 # po přesunu se schránka vyprázdní


def test_paste_files_from_explorer(panel, env, clip):
    from PySide6.QtCore import QMimeData, QUrl
    alice, bob, proj, src = env
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(src / "nova.elt")), QUrl.fromLocalFile(str(src / "slozka"))])
    clip.setMimeData(mime)
    assert panel.can_paste()
    panel.paste_into("dily/")
    settle_panel(panel)
    local = alice.local_dir("Forma")
    assert (local / "dily/nova.elt").exists() and (local / "dily/slozka/pod/b.elt").exists()


def test_paste_only_into_active_project(panel, env, clip, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    alice = env[0]
    panel.copy_selected(["dily/deska.elt"])
    alice.cancel_activation("Forma")
    panel.show_project(alice.project_status("Forma"))
    settle_panel(panel)
    shown = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a[2]))
    assert not panel.can_paste()
    panel.paste_into("")
    assert "aktivovaného projektu" in shown[0]


def test_cut_from_local_only_folder_into_project(panel, env, clip, recycled):
    alice, bob, proj, src = env
    mistni = alice.local_dir("Mistni")
    write(mistni / "a/x.elt", "x")
    panel.show_project(alice.project_status("Mistni"))
    settle_panel(panel)
    assert panel.can_cut(["a/"])
    assert panel.copy_selected(["a/"], cut=True)
    panel.show_project(alice.project_status("Forma"))
    settle_panel(panel)
    panel.paste_into("dily/")
    settle_panel(panel)
    assert (alice.local_dir("Forma") / "dily/a/x.elt").read_text(encoding="utf-8") == "x"
    assert not (mistni / "a/x.elt").exists() and recycled


def test_copy_names():
    from spm.folder_ops import copy_name, free_copy_name
    assert copy_name("deska.elt") == "deska_copy.elt" and copy_name("deska.elt", 2) == "deska_copy2.elt"
    assert copy_name("a.b.dwg") == "a.b_copy.dwg" and copy_name("README") == "README_copy"
    assert copy_name(".gitignore") == ".gitignore_copy" and copy_name("Vykresy.v2", is_dir=True) == "Vykresy.v2_copy"
    assert free_copy_name("x.txt", lambda n: n in {"x_copy.txt", "x_copy2.txt"}) == "x_copy3.txt"


def test_paste_in_same_place_duplicates_with_copy_suffix(panel, env, clip):
    alice, bob, proj, src = env
    local = alice.local_dir("Forma")
    assert panel.copy_selected(["sestava/elektrody/E02.elt"])          # můj soubor
    panel.paste_into("sestava/elektrody/")
    settle_panel(panel)
    assert (local / "sestava/elektrody/E02_copy.elt").read_text(encoding="utf-8") == "sestava/elektrody/E02.elt"
    panel.paste_into("sestava/elektrody/")                              # podruhé
    settle_panel(panel)
    assert (local / "sestava/elektrody/E02_copy2.elt").exists()
    assert any(e.path == "sestava/elektrody/E02_copy.elt" and e.local_new for e in panel.entries)
    # cizí (neaktivovaný) soubor jde zduplikovat taky – kopie je můj nový soubor
    assert panel.copy_selected(["dily/deska.elt"])
    panel.paste_into("dily/")
    settle_panel(panel)
    assert writable(local / "dily/deska_copy.elt")
    # složka vložená na své místo: celá složka s _copy
    assert panel.copy_selected(["sestava/elektrody/"])
    panel.paste_into("sestava/")
    settle_panel(panel)
    assert (local / "sestava/elektrody_copy/E01.elt").exists()
    assert (local / "sestava/elektrody_copy/E02_copy.elt").exists()
    assert not (local / "sestava/elektrody/E01_copy.elt").exists()


def test_no_temp_copies_until_another_program_asks(panel, env, clip, tmp_path):
    """Kopírovat / tažení: dočasné kopie vzniknou až ve chvíli, kdy si o soubory řekne jiný
    program (data text/uri-list); vložení v SPM čte soubory z původního místa."""
    from spm.gui import project_panel
    alice, bob, proj, src = env
    local = alice.local_dir("Forma")
    staging = tmp_path / "staging"
    assert panel.copy_selected(["dily/deska.elt", "sestava/"])        # jen pro čtení + složka
    mime = clip.mimeData()
    assert mime.hasUrls() and not mime.rendered()                     # nabízí soubory, ale nic nekopíruje
    assert not (tmp_path / "clip").exists()
    (local / "dily" / "deska.elt").chmod(0o666)
    write(local / "dily" / "deska.elt", "deska novější")             # obsah po Ctrl+C
    panel.paste_into("dily/")                                         # Vložit v SPM
    settle_panel(panel)
    copied = [p for p in (local / "dily").iterdir() if p.name.startswith("deska") and p.name != "deska.elt"]
    assert copied and copied[0].read_text(encoding="utf-8") == "deska novější"   # aktuální obsah
    assert not mime.rendered() and not (tmp_path / "clip").exists()    # pořád žádné dočasné kopie
    urls = [p.toLocalFile() for p in mime.urls()]                     # Průzkumník si řekne
    assert mime.rendered() and urls and all(Path(u).is_relative_to(tmp_path / "clip") for u in urls)
    # tažení: stejné – připraví se až při vyžádání
    drag = panel.drag_mime(["dily/deska.elt"])
    assert not drag.rendered() and not staging.exists()
    assert project_panel.MIME_SOURCE in drag.formats()


def test_spm_clipboard_forgotten_on_quit(panel, env, clip):
    """Při ukončení SPM se schránka se soubory z SPM vyprázdní (Windows by jinak při ukončení
    převzaly celý obsah – u odložených kopií by se teprve kopírovalo); cizí obsah zůstane."""
    from spm.gui.project_tree import forget_clipboard
    assert panel.copy_selected(["dily/deska.elt"])
    mime = clip.mimeData()
    forget_clipboard()
    assert not mime.rendered()                                        # nic se nekopírovalo
    assert clip.mimeData() is None or not clip.mimeData().hasUrls()
    clip.setText("cizí text")
    forget_clipboard()
    assert clip.text() == "cizí text"


def test_paste_from_removed_tab_shows_error(panel, env, clip, monkeypatch):
    """Zdrojová záložka mezitím zmizela: vložení skončí chybovým oknem, ne pádem."""
    from PySide6.QtWidgets import QMessageBox
    alice = env[0]
    shown = []
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: shown.append(a[2]))

    def removed(ws_id):
        raise KeyError(ws_id)
    monkeypatch.setattr(panel, "pm_for", removed)
    panel.drop_spm({"ws": "zrusena", "project": "Jiny"}, [("a.elt", "a.elt")], "dily/")
    assert shown and "Jiny" in shown[0]
    assert not (alice.local_dir("Forma") / "dily" / "a.elt").exists()
