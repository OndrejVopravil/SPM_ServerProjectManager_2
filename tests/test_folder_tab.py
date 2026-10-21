"""Záložka „Složka“: obyčejná složka mimo spolupráci – kontrola překrytí, operace se soubory,
schránka mezi složkou a projektem, přepnutí v hlavním okně."""
import os
import stat
from pathlib import Path

import pytest
from conftest import settle_panel
from test_drag_drop import env, panel, write, writable  # noqa: F401 – fixtures

from spm import config, folder_ops, recycle
from spm.config import AppSettings, Settings


@pytest.fixture
def recycled(monkeypatch):
    removed = []

    def fake(paths):
        import shutil
        for p in paths:
            p = Path(p)
            removed.append(p)
            if p.is_dir():
                shutil.rmtree(p)
            else:
                os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
                p.unlink()
    monkeypatch.setattr(recycle, "to_recycle_bin", fake)
    return removed


@pytest.fixture
def clip(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from spm.gui import project_panel
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(project_panel, "CLIPBOARD_ROOT", tmp_path / "clip")
    QApplication.clipboard().clear()
    return QApplication.clipboard()


@pytest.fixture
def folder(tmp_path):
    from spm.gui.folder_panel import FolderPanel
    data = tmp_path / "data"
    write(data / "vykres.dwg", "vykres")
    write(data / "podklady" / "a.pdf", "a")
    p = FolderPanel()
    p.set_root(str(data), "ws-data")
    return p, data


# --- nastavení ---------------------------------------------------------------------

def test_folder_tab_must_not_start_inside_collaboration(tmp_path):
    for d in ("server", "local", "data", "jinde"):
        (tmp_path / d).mkdir()
    ws = AppSettings.new_workspace("Projekty", Settings(server_root=str(tmp_path / "server"),
                                                       local_root=str(tmp_path / "local")))
    app = AppSettings(Settings(), [ws], ws["id"])
    assert app.folder_tab_conflict("", str(tmp_path / "data")) is None
    assert "uvnitř místní složky" in app.folder_tab_conflict("", str(tmp_path / "local"))
    assert "uvnitř místní složky" in app.folder_tab_conflict("", str(tmp_path / "local" / "Forma"))
    assert "uvnitř síťové složky" in app.folder_tab_conflict("", str(tmp_path / "server" / "Data"))
    # složka, která spolupráci obsahuje, je v pořádku (spolupráce v ní bude chráněná)
    assert app.folder_tab_conflict("", str(tmp_path)) is None
    assert len(app.contained_collaboration(str(tmp_path))) == 2
    # projekt SPM (servisní složka .spm), který není v žádné záložce
    (tmp_path / "jinde" / "Projekt" / ".spm").mkdir(parents=True)
    assert app.folder_tab_conflict("", str(tmp_path / "jinde")) is None                       # obsahuje
    assert "součástí projektu" in app.folder_tab_conflict("", str(tmp_path / "jinde" / "Projekt" / "x"))
    # opačně: záložka Složka nesmí skončit uvnitř složky záložky projektů
    folder_ws = AppSettings.new_workspace("Data")
    folder_ws["folder_root"] = str(tmp_path / "data")
    app.workspaces.append(folder_ws)
    assert "by ležela uvnitř" in app.folder_conflict(ws["id"], str(tmp_path / "server"), str(tmp_path))
    assert app.folder_conflict(ws["id"], str(tmp_path / "server"), str(tmp_path / "data" / "x")) is None
    assert app.view(folder_ws["id"]).is_folder and not app.view(folder_ws["id"]).is_complete()


# --- operace se soubory --------------------------------------------------------------

def test_copy_and_move_items(tmp_path):
    src = tmp_path / "src"
    write(src / "f.txt", "f")
    (src / "f.txt").chmod(stat.S_IREAD)                                  # jen pro čtení
    write(src / "dir" / "a.txt", "a")
    (src / "dir" / "prazdna").mkdir()
    dest = tmp_path / "dest"
    write(dest / "f.txt", "puvodni")
    pairs = folder_ops.targets([src / "f.txt", src / "dir"], dest)
    assert folder_ops.conflicts(pairs) == [dest / "f.txt"]
    done, skipped = folder_ops.copy_items(pairs)                         # bez přepsání
    assert (dest / "f.txt").read_text() == "puvodni" and len(skipped) == 1
    assert (dest / "dir" / "a.txt").exists() and (dest / "dir" / "prazdna").is_dir()
    done, _ = folder_ops.copy_items(pairs, overwrite=True)
    assert (dest / "f.txt").read_text() == "f" and writable(dest / "f.txt")
    # přesun: sloučení do existující složky
    write(src / "dir" / "b.txt", "b")
    done, skipped = folder_ops.move_items(folder_ops.targets([src / "dir"], dest), overwrite=False)
    assert (dest / "dir" / "b.txt").exists() and (src / "dir" / "a.txt").exists()   # a.txt přeskočen
    assert any("a.txt" in s for s in skipped)
    _, skipped = folder_ops.copy_items(folder_ops.targets([dest / "dir"], dest / "dir" / "sub"))
    assert "do sebe sama" in skipped[0]


# --- schránka mezi projektem a složkou ---------------------------------------------------

def test_copy_from_project_to_folder(panel, env, clip, folder):
    fp, data = folder
    assert panel.copy_selected(["sestava/elektrody/", "dily/deska.elt"])
    fp.paste(data / "podklady")
    assert (data / "podklady" / "elektrody" / "E01.elt").exists()
    assert writable(data / "podklady" / "deska.elt")                         # bez „jen pro čtení“
    assert (env[0].local_dir("Forma") / "dily" / "deska.elt").exists()       # originál zůstává


def test_cut_from_project_moves_to_folder(panel, env, clip, folder, recycled):
    alice = env[0]
    fp, data = folder
    fp.pm_for = lambda ws: alice
    assert panel.copy_selected(["sestava/elektrody/E02.elt"], cut=True)
    fp.paste(data)
    assert (data / "E02.elt").read_text(encoding="utf-8") == "sestava/elektrody/E02.elt"
    assert not (alice.local_dir("Forma") / "sestava/elektrody/E02.elt").exists()
    assert clip.mimeData() is None or not clip.mimeData().hasUrls()


def test_cut_from_project_skipped_file_stays(panel, env, clip, folder, recycled, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    alice = env[0]
    fp, data = folder
    fp.pm_for = lambda ws: alice
    write(data / "E02.elt", "jiny")                                         # stejný název v cíli
    monkeypatch.setattr(QMessageBox, "exec", lambda box: 0)
    monkeypatch.setattr(QMessageBox, "clickedButton",
                        lambda box: next(b for b in box.buttons() if b.text() == "Přeskočit"))
    assert panel.copy_selected(["sestava/elektrody/E02.elt"], cut=True)
    fp.paste(data)
    assert (data / "E02.elt").read_text(encoding="utf-8") == "jiny"
    assert (alice.local_dir("Forma") / "sestava/elektrody/E02.elt").exists()   # nic se nesmazalo


def test_cut_from_folder_into_project(panel, env, clip, folder, recycled):
    alice = env[0]
    fp, data = folder
    assert fp.copy_selected([data / "podklady", data / "vykres.dwg"], cut=True)
    panel.paste_into("dily/")
    settle_panel(panel)
    local = alice.local_dir("Forma")
    assert (local / "dily/podklady/a.pdf").exists() and (local / "dily/vykres.dwg").exists()
    assert not (data / "vykres.dwg").exists() and not (data / "podklady").exists()


def test_copy_and_move_inside_folder(folder, clip):
    fp, data = folder
    (data / "cil").mkdir()
    fp.copy_selected([data / "vykres.dwg"])
    fp.paste(data / "cil")
    assert (data / "cil" / "vykres.dwg").exists() and (data / "vykres.dwg").exists()
    fp.copy_selected([data / "podklady"], cut=True)
    fp.paste(data / "cil")
    assert (data / "cil" / "podklady" / "a.pdf").exists() and not (data / "podklady").exists()
    # mimo složku záložky nic
    assert fp.put([data / "vykres.dwg"], data.parent) is None


# --- hlavní okno ------------------------------------------------------------------------

def test_folder_tab_in_main_window(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QFileDialog, QInputDialog

    from spm.gui.main_window import MainWindow
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(config, "APP_DIR", tmp_path / "app")
    write(tmp_path / "server" / "Forma" / "a.txt", "a")
    write(tmp_path / "data" / "x.txt", "x")
    w = MainWindow(Settings(server_root=str(tmp_path / "server"), local_root=str(tmp_path / "local")))
    try:
        w.refresh(check_names=False)
        projects_ws = w.settings.workspace_id
        monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: str(tmp_path / "data"))
        monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("Data", True))
        w.add_folder_workspace()
        assert w.settings.is_folder and w.pages.currentWidget() is w.folder_panel
        assert w.folder_panel.root == tmp_path / "data" and "Složka:" in w.statusBar().currentMessage()
        assert [t.name for t in w.tab_infos()] == [w.app_settings.workspace(projects_ws)["name"]]
        w.refresh()                                                       # nic nenačítá ze sítě
        assert w.status_label.text().startswith("Aktualizováno")
        w.switch_workspace(projects_ws)
        assert w.pages.currentWidget() is w.projects_page and [p.name for p in w.projects] == ["Forma"]
        # překrytí se složkou projektů se odmítne
        warnings = []
        from PySide6.QtWidgets import QMessageBox
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a[2]))
        answers = iter([str(tmp_path / "local"), ""])
        monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: next(answers))
        assert w.choose_folder("") is None and "uvnitř místní složky" in warnings[0]
    finally:
        w.close()
        app.processEvents()


def test_copy_from_folder_into_project(panel, env, clip, folder):
    alice = env[0]
    fp, data = folder
    assert fp.copy_selected([data / "vykres.dwg"])
    panel.paste_into("dily/")
    settle_panel(panel)
    assert (alice.local_dir("Forma") / "dily/vykres.dwg").exists() and (data / "vykres.dwg").exists()


def test_paste_in_same_place_duplicates_in_folder(folder, clip):
    fp, data = folder
    fp.copy_selected([data / "vykres.dwg", data / "podklady"])
    fp.paste(data)
    assert (data / "vykres_copy.dwg").read_text() == "vykres" and (data / "podklady_copy" / "a.pdf").exists()
    fp.paste(data)
    assert (data / "vykres_copy2.dwg").exists() and (data / "podklady_copy2" / "a.pdf").exists()


def test_keep_both_on_name_clash(folder, clip, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    fp, data = folder
    other = tmp_path / "jinde"
    write(other / "vykres.dwg", "novy")
    write(other / "podklady" / "a.pdf", "novy a")
    monkeypatch.setattr(QMessageBox, "exec", lambda box: 0)
    monkeypatch.setattr(QMessageBox, "clickedButton",
                        lambda box: next(b for b in box.buttons() if b.text() == "Ponechat obě"))
    done = fp.put([other / "vykres.dwg", other / "podklady"], data)
    assert (data / "vykres.dwg").read_text() == "vykres"                  # původní beze změny
    assert (data / "vykres_copy.dwg").read_text() == "novy"
    assert (data / "podklady" / "a.pdf").read_text() == "a"               # složka se nesloučila
    assert (data / "podklady_copy" / "a.pdf").read_text() == "novy a"
    assert done
