"""Ruční zabalení do ZIP a rozbalení – pravidla pojmenování, strom projektu, záložka Složka."""
import zipfile
from pathlib import Path

import pytest
from conftest import settle_panel
from test_drag_drop import env, panel, write  # noqa: F401 – fixtures

from spm import zip_ops


def names_in(zip_path: Path) -> list[str]:
    with zipfile.ZipFile(zip_path) as zf:
        return sorted(zf.namelist())


def test_zip_naming_rules():
    assert zip_ops.zip_name(["dily/deska.elt"], "Forma") == "dily"            # soubor -> jeho složka
    assert zip_ops.zip_name(["deska.elt"], "Forma") == "Forma"                # v kořeni -> projekt
    assert zip_ops.zip_name(["sestava/elektrody/"], "Forma") == "elektrody"   # složka -> složka
    assert zip_ops.zip_name(["a/x.txt", "a/y/"], "Forma") == "a"              # víc -> nadřazená
    assert zip_ops.zip_name(["a/x.txt", "b/y.txt"], "Forma") == "Forma"       # různé -> projekt
    assert zip_ops.zip_base(["a/b/x.txt", "a/c/"]) == "a/"
    assert zip_ops.zip_base(["sestava/elektrody/"]) == "sestava/"
    assert zip_ops.unzip_folder_name("dily/Vykresy.zip") == "Vykresy"


def test_extract_strips_single_top_folder_and_rejects_unsafe(tmp_path):
    src = tmp_path / "Vykresy"
    write(src / "a.dwg", "a")
    write(src / "pod" / "b.dwg", "b")
    z = tmp_path / "Vykresy.zip"
    zip_ops.make_zip([(src / "a.dwg", "Vykresy/a.dwg"), (src / "pod" / "b.dwg", "Vykresy/pod/b.dwg")], z)
    zip_ops.extract_zip(z, tmp_path / "out" / "Vykresy_copy")               # i do jiného názvu
    assert (tmp_path / "out" / "Vykresy_copy" / "pod" / "b.dwg").read_text() == "b"
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("../venku.txt", "x")
    with pytest.raises(zip_ops.ZipError, match="nebezpečnou"):
        zip_ops.extract_zip(bad, tmp_path / "bad")
    assert not (tmp_path / "venku.txt").exists()
    with pytest.raises(zip_ops.ZipError):
        zip_ops.make_zip([], tmp_path / "prazdny.zip")


def test_zip_and_unzip_in_project(panel, env):
    alice = env[0]
    local = alice.local_dir("Forma")
    acts = panel.context_actions(["dily/deska.elt"])
    assert acts["zip"] and not acts["unzip"]
    panel.zip_selected(["dily/deska.elt"])                                  # soubor -> dily/dily.zip
    settle_panel(panel)
    assert names_in(local / "dily" / "dily.zip") == ["deska.elt"]
    assert any(e.path == "dily/dily.zip" and e.local_new for e in panel.entries)
    panel.zip_selected(["sestava/elektrody/"])                              # složka
    settle_panel(panel)
    assert names_in(local / "sestava" / "elektrody.zip") == ["elektrody/E01.elt", "elektrody/E02.elt"]
    panel.zip_selected(["sestava/sestava.elt", "dily/deska.elt"])           # různé složky -> projekt
    panel.zip_selected(["sestava/sestava.elt", "dily/deska.elt"])           # podruhé -> _copy
    settle_panel(panel)
    assert names_in(local / "Forma.zip") == ["dily/deska.elt", "sestava/sestava.elt"]
    assert (local / "Forma_copy.zip").exists()
    # rozbalení: složka podle zipu (elektrody už existuje -> elektrody_copy), bez dvojího vnoření
    assert panel.context_actions("sestava/elektrody.zip")["unzip"]
    panel.unzip("sestava/elektrody.zip")
    settle_panel(panel)
    assert (local / "sestava" / "elektrody_copy" / "E01.elt").exists()
    panel.unzip("dily/dily.zip")
    settle_panel(panel)
    assert (local / "dily" / "dily" / "deska.elt").exists()                  # název zipu volný


def test_zip_needs_activated_project(panel, env):
    alice = env[0]
    alice.cancel_activation("Forma")
    panel.show_project(alice.project_status("Forma"))
    settle_panel(panel)
    assert not panel.context_actions(["dily/deska.elt"])["zip"]


def test_zip_and_unzip_in_folder_tab(tmp_path):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from spm.gui.folder_panel import FolderPanel
    data = tmp_path / "Data"
    write(data / "Podklady" / "a.pdf", "a")
    write(data / "Podklady" / "b.pdf", "b")
    write(data / "x.txt", "x")
    fp = FolderPanel()
    fp.set_root(str(data), "ws")
    z = fp.zip_paths([data / "Podklady"])
    assert z == data / "Podklady.zip" and names_in(z) == ["Podklady/a.pdf", "Podklady/b.pdf"]
    z2 = fp.zip_paths([data / "Podklady" / "a.pdf"])                          # soubor -> jeho složka
    assert z2 == data / "Podklady" / "Podklady.zip" and names_in(z2) == ["a.pdf"]
    z3 = fp.zip_paths([data / "x.txt", data / "Podklady"])                    # v kořeni -> složka záložky
    assert z3 == data / "Data.zip" and "x.txt" in names_in(z3)
    out = fp.unzip(z)
    assert out == data / "Podklady_copy" and (out / "a.pdf").read_text() == "a"
