"""Adresář uživatelů: neaktivní (SPM měsíc nespustili) jsou v nabídkách na konci a zašedlí,
záznamy uživatelů, kteří SPM rok nespustili, se samy mažou (bez nastavování)."""
import json
import os
from datetime import datetime, timedelta

import pytest

from spm import users
from spm.users import UserInfo


def put(server, login, days_ago, nick=""):
    seen = (datetime.now() - timedelta(days=days_ago)).isoformat(timespec="seconds")
    folder = users.users_dir(server)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{login}.json").write_text(json.dumps({"login": login, "nickname": nick, "seen": seen}),
                                          encoding="utf-8")


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(users, "_purged_on", {})
    put(tmp_path, "aktivni", 2, "Alena")
    put(tmp_path, "dovolena", 45, "Bohouš")
    put(tmp_path, "odesel", 400, "Cyril")
    return tmp_path


def test_inactive_order_and_purge(server):
    by = {u.login: u for u in users.list_users(server)}
    assert not by["aktivni"].inactive() and by["dovolena"].inactive() and by["odesel"].inactive()
    assert by["dovolena"].seen_label.startswith("neaktivní od ")
    assert not UserInfo("x").inactive()                              # neznámé datum = aktivní
    put(server, "zuzana", 1, "Zuzana")
    assert [u.login for u in users.list_users(server)] == ["aktivni", "zuzana", "dovolena", "odesel"]
    assert users.purge(server) == ["odesel"]                          # rok bez spuštění
    assert sorted(u.login for u in users.list_users(server)) == ["aktivni", "dovolena", "zuzana"]
    put(server, "odesel", 400)
    assert users.purge(server) == []                                  # nejvýš jednou denně
    later = datetime.now() + timedelta(days=1)
    assert users.purge(server, later) == ["odesel"]


def test_register_purges_and_returning_user_comes_back(server, tmp_path):
    from spm.config import Settings
    from spm.sync import ProjectManager
    pm = ProjectManager(Settings(server_root=str(server), local_root=str(tmp_path / "l")), user="odesel", pc="PC")
    pm.register_user()                                               # vrátil se – záznam znovu
    assert "odesel" in {u.login for u in users.list_users(server)}
    assert not {u.login: u for u in users.list_users(server)}["odesel"].inactive()


@pytest.fixture
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_menus_show_inactive_last_and_grey(qapp, server):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from spm.gui.completer_edit import CompleterEdit
    from spm.gui.notes_panel import recipient_combo
    from spm.gui.user_picker import UsersDialog
    people = users.list_users(server)
    combo = recipient_combo(people)
    texts = [combo.itemText(i) for i in range(combo.count())]
    assert texts[0] == "všem" and texts[1] == "Alena (aktivni)" and "neaktivní od" in texts[2]
    assert combo.itemData(2, Qt.ItemDataRole.ForegroundRole) is not None   # zašedlý
    # výběr uživatelů: neaktivní skryté, vybraný neaktivní vidět, zaškrtávátko je ukáže
    dlg = UsersDialog("Komu", people, ["odesel"])
    visible = [dlg.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(dlg.list.count())
               if not dlg.list.item(i).isHidden()]
    assert visible == ["aktivni", "odesel"] and not dlg.show_inactive.isHidden()
    dlg.show_inactive.setChecked(True)
    assert all(not dlg.list.item(i).isHidden() for i in range(dlg.list.count()))
    # našeptávač @: neaktivní až po napsání části jména
    ed = CompleterEdit()
    ed.users = people
    ed.show()
    QTest.keyClicks(ed, "@")
    shown = [ed.completer.completionModel().index(i, 0).data() for i in range(ed.completer.completionCount())]
    assert shown == ["Alena (aktivni)"]
    QTest.keyClicks(ed, "bo")
    shown = [ed.completer.completionModel().index(i, 0).data() for i in range(ed.completer.completionCount())]
    assert len(shown) == 1 and shown[0].startswith("Bohouš (dovolena) – neaktivní od")
    ed.close()
