"""Společná konfigurace na síti (spm_config.json)."""
import json
import os
import stat

import pytest

from spm import config
from spm import shared_config as sc
from spm.config import Settings, settings_path
from spm.scanner import list_files


@pytest.fixture(autouse=True)
def app_dir(tmp_path, monkeypatch):
    # nikdy nepřepisovat skutečné nastavení uživatele
    monkeypatch.setattr(config, "APP_DIR", tmp_path / "app")


@pytest.fixture
def server(tmp_path):
    root = tmp_path / "server"
    (root / "Forma").mkdir(parents=True)
    return root


def write_config(server, data, mtime=None):
    path = server / sc.CONFIG_NAME
    if path.exists():
        path.chmod(stat.S_IWRITE | stat.S_IREAD)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    if mtime:
        os.utime(path, (mtime, mtime))
    return path


def test_no_file_means_local_settings(server):
    s = Settings(server_root=str(server), backup_count=7)
    shared = sc.SharedConfig()
    assert not shared.sync(s)
    assert shared.state.status == sc.NONE and not shared.state.active
    assert s.backup_count == 7


def test_unavailable_server(tmp_path):
    s = Settings(server_root=str(tmp_path / "neni"))
    shared = sc.SharedConfig()
    shared.sync(s)
    assert shared.state.status == sc.UNAVAILABLE and not shared.state.active


def test_values_taken_over_and_saved_locally(server):
    write_config(server, {"backup_enabled": False, "backup_count": 9,
                          "ignore_patterns": ["*.bak", " "], "check_forbidden_chars": False})
    s = Settings(server_root=str(server))
    shared = sc.SharedConfig()
    assert shared.sync(s)
    assert (s.backup_enabled, s.backup_count, s.ignore_patterns, s.check_forbidden_chars) == \
        (False, 9, ["*.bak"], False)
    # převzato i do settings.json – platí dál, i když soubor na síti zmizí
    saved = Settings.load(settings_path())
    assert (saved.backup_count, saved.ignore_patterns) == (9, ["*.bak"])
    (server / sc.CONFIG_NAME).unlink()
    assert not shared.sync(s)
    assert shared.state.status == sc.NONE and s.backup_count == 9


def test_partial_file_keeps_other_local_values(server):
    write_config(server, {"backup_count": 5})
    s = Settings(server_root=str(server), check_forbidden_chars=False, ignore_patterns=["*.x"])
    sc.SharedConfig().sync(s)
    assert (s.backup_count, s.check_forbidden_chars, s.ignore_patterns) == (5, False, ["*.x"])


def test_file_read_only_when_changed(server, monkeypatch):
    write_config(server, {"backup_count": 4}, mtime=1_000_000)
    s = Settings(server_root=str(server))
    shared = sc.SharedConfig()
    shared.sync(s)
    reads = []
    original = sc.Path.read_text
    monkeypatch.setattr(sc.Path, "read_text", lambda self, *a, **k: reads.append(self) or original(self, *a, **k))
    assert not shared.sync(s) and reads == []           # beze změny souboru se nečte
    write_config(server, {"backup_count": 6}, mtime=2_000_000)
    assert shared.sync(s) and s.backup_count == 6 and len(reads) == 1


def test_invalid_file_keeps_last_values(server):
    write_config(server, {"backup_count": 4}, mtime=1_000_000)
    s = Settings(server_root=str(server))
    shared = sc.SharedConfig()
    shared.sync(s)
    write_config(server, "{nesmysl", mtime=2_000_000)
    assert not shared.sync(s)
    assert shared.state.status == sc.ERROR and shared.state.active and "JSON" in shared.state.error
    assert s.backup_count == 4


@pytest.mark.parametrize("data, key", [
    ({"backup_count": 0}, "backup_count"),
    ({"backup_count": "3"}, "backup_count"),
    ({"backup_count": True}, "backup_count"),
    ({"backup_enabled": 1}, "backup_enabled"),
    ({"ignore_patterns": "*.tmp"}, "ignore_patterns"),
    ({"check_forbidden_chars": "ano"}, "check_forbidden_chars"),
])
def test_invalid_values_rejected(data, key):
    with pytest.raises(sc.SharedConfigError, match=key):
        sc.parse(json.dumps(data))


def test_bom_from_notepad_accepted(server):
    (server / sc.CONFIG_NAME).write_bytes(b"\xef\xbb\xbf" + json.dumps({"backup_count": 2}).encode())
    assert sc.probe(str(server)).values == {"backup_count": 2}


def test_write_and_read_back(server):
    s = Settings(server_root=str(server), backup_count=11, ignore_patterns=["*.tmp", "*.bak"])
    path = sc.write(str(server), sc.values_of(s))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data[sc.FORMAT_KEY] == sc.FORMAT
    assert set(data) == {sc.FORMAT_KEY, *sc.SHARED_FIELDS}
    assert sc.probe(str(server)).values == sc.values_of(s)
    assert not list(server.glob("*" + ".spmtmp"))


def test_read_only_file_cannot_be_changed(server):
    path = write_config(server, {"backup_count": 4})
    path.chmod(stat.S_IREAD)
    try:
        assert sc.probe(str(server)).read_only
        with pytest.raises(sc.SharedConfigError, match="jen pro čtení"):
            sc.write(str(server), sc.values_of(Settings()))
        assert json.loads(path.read_text(encoding="utf-8")) == {"backup_count": 4}
    finally:
        path.chmod(stat.S_IWRITE | stat.S_IREAD)


def test_config_file_is_not_a_project(server):
    from spm.sync import ProjectManager
    write_config(server, {"backup_count": 4})
    pm = ProjectManager(Settings(server_root=str(server), local_root=str(server.parent / "l")))
    assert [p.name for p in pm.list_projects()] == ["Forma"]


def test_spm_temp_files_always_ignored(tmp_path):
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "a.txt.spmtmp").write_text("a")
    assert [p.name for p in list_files(tmp_path, [])] == ["a.txt"]


# --- dialog Nastavení ------------------------------------------------------------------

@pytest.fixture
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def dialog(settings):
    from spm.gui.settings_dialog import SettingsDialog
    return SettingsDialog(settings)


def test_dialog_local_mode_exports(qapp, server, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    s = Settings(server_root=str(server), local_root=str(tmp_path / "l"), backup_count=3)
    dlg = dialog(s)
    assert dlg.backup_count.isEnabled() and not dlg.btn_export.isHidden()
    assert dlg.btn_edit_shared.isHidden()
    assert dlg.btn_export.isEnabled() and not dlg.shared.active
    dlg.backup_count.setValue(8)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    dlg._export()
    assert dlg.exported and dlg.shared.status == sc.OK
    assert sc.probe(str(server)).values["backup_count"] == 8
    assert not dlg.backup_count.isEnabled() and not dlg.ignore.isEnabled()   # teď řízeno ze sítě
    assert dlg.btn_export.isHidden() and not dlg.btn_edit_shared.isHidden()


def test_dialog_locked_then_edit_saves_to_network(qapp, server, tmp_path):
    write_config(server, {"backup_count": 4, "check_forbidden_chars": False})
    s = Settings(server_root=str(server), local_root=str(tmp_path / "l"), backup_count=3)
    dlg = dialog(s)
    assert dlg.backup_count.value() == 4 and not dlg.check_chars.isChecked()
    assert not dlg.backup_count.isEnabled() and not dlg.check_chars.isEnabled()
    assert dlg.btn_edit_shared.isEnabled()
    dlg._edit_shared()
    assert dlg.backup_count.isEnabled() and dlg.check_chars.isEnabled()
    dlg.backup_count.setValue(12)
    dlg._accept()
    assert dlg.result() and s.backup_count == 12
    assert sc.probe(str(server)).values["backup_count"] == 12


def test_dialog_read_only_file_cannot_be_edited(qapp, server, tmp_path):
    path = write_config(server, {"backup_count": 4})
    path.chmod(stat.S_IREAD)
    try:
        dlg = dialog(Settings(server_root=str(server), local_root=str(tmp_path / "l")))
        assert not dlg.btn_edit_shared.isEnabled() and not dlg.backup_count.isEnabled()
        assert "jen pro čtení" in dlg.shared_info.text()
        dlg._edit_shared()
        assert not dlg.editing_shared and not dlg.backup_count.isEnabled()
    finally:
        path.chmod(stat.S_IWRITE | stat.S_IREAD)


def test_main_window_takes_over_on_refresh(qapp, server, tmp_path):
    from spm.gui.main_window import MainWindow
    s = Settings(server_root=str(server), local_root=str(tmp_path / "l"))
    w = MainWindow(s)
    try:
        w.refresh()
        assert w.config_label.text() == "Nastavení: místní"
        write_config(server, {"ignore_patterns": ["*.bak"]})
        w.refresh(silent=True, check_names=False)
        assert s.ignore_patterns == ["*.bak"] and w.config_label.text() == "Nastavení: síťové"
    finally:
        if w._name_check:
            w._name_check.wait()
        w.close()
        qapp.processEvents()


def test_dialog_archivers_picked_from_users(qapp, server, tmp_path, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QDialog

    from spm import users
    from spm.gui import settings_dialog
    users.register(server, users.UserInfo("novak", "Pepa"))
    users.register(server, users.UserInfo("svoboda", full_name="Jan Svoboda"))
    s = Settings(server_root=str(server), local_root=str(tmp_path / "l"), archivers=["Novak", "host"])
    dlg = dialog(s)
    assert dlg.archivers.text.isReadOnly()                                  # nepíše se ručně
    assert dlg.archivers.text.text() == "Pepa (novak), host"               # neregistrovaný jako login

    def exec_(d):
        rows = {d.list.item(i).data(Qt.ItemDataRole.UserRole): d.list.item(i) for i in range(d.list.count())}
        assert set(rows) == {"novak", "svoboda", "host"}
        assert rows["novak"].checkState() == Qt.CheckState.Checked
        rows["host"].setCheckState(Qt.CheckState.Unchecked)
        rows["svoboda"].setCheckState(Qt.CheckState.Checked)
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(settings_dialog.UsersDialog, "exec", exec_)
    assert dlg.archivers.choose()
    assert sorted(dlg._shared_values()["archivers"]) == ["novak", "svoboda"]
    assert "Jan Svoboda (svoboda)" in dlg.archivers.text.text()
