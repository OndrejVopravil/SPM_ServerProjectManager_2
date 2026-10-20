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
