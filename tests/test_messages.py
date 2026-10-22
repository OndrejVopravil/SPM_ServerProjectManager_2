"""Vzkazy – poznámky pro konkrétní uživatele, nevázané na projekt (<síť>/.spm/vzkazy)."""
import pytest

from spm import messages as msgs
from spm.config import Settings
from spm.messages import MessageError, MessageStore, Person
from spm.sync import ProjectManager


@pytest.fixture
def server(tmp_path):
    root = tmp_path / "server"
    (root / "058768").mkdir(parents=True)
    (root / "Forma").mkdir()
    return root


def load(server, mid):
    return next(m for m in MessageStore(server).load() if m.id == mid)


def test_send_and_read(server):
    mid = msgs.send(server, "alice", "PC-A", "Alča", [Person("Bob", "Bobík"), ("jana", "Jana")],
                    "Zkontroluj #058768", cc=[("petr", "Petr"), ("bob", "")],
                    projects=[{"name": "058768", "project_id": ""}])
    assert (server / ".spm" / "vzkazy" / mid / "vzkaz.json").exists()
    (m,) = MessageStore(server).load()
    assert m.id == mid and m.author == "Alča" and m.text == "Zkontroluj #058768"
    assert [p.login for p in m.recipients] == ["bob", "jana"]
    assert [p.login for p in m.cc] == ["petr"]                      # adresát není zároveň na vědomí
    assert m.is_recipient("BOB") and m.is_cc("petr") and m.is_author("alice")
    assert m.involves("jana") and not m.involves("karel")
    assert not m.resolved and m.projects[0]["name"] == "058768"
    with pytest.raises(MessageError, match="adresáta"):
        msgs.send(server, "alice", "PC", "Alča", [], "nikomu")
    with pytest.raises(MessageError, match="prázdný"):
        msgs.send(server, "alice", "PC", "Alča", [("bob", "")], "  ")


def test_resolved_by_any_recipient_and_shows_who(server):
    mid = msgs.send(server, "alice", "PC", "Alča", [("bob", "Bobík"), ("jana", "Jana")], "Udělat výkres")
    m = load(server, mid)
    with pytest.raises(MessageError, match="adresát nebo autor"):
        msgs.set_resolved(m, "karel", "Karel")                      # netýká se ho
    msgs.set_resolved(m, "jana", "Jana")
    m = load(server, mid)
    assert m.resolved and [s.name for s in m.resolvers] == ["Jana"]   # stačí jeden z adresátů
    assert m.resolved_by("jana") and not m.resolved_by("bob")
    msgs.set_resolved(m, "jana", "Jana", resolved=False)
    assert not load(server, mid).resolved


def test_cc_marks_read_and_replies(server):
    mid = msgs.send(server, "alice", "PC", "Alča", [("bob", "")], "Info", cc=[("petr", "Petr")])
    m = load(server, mid)
    msgs.set_read(m, "petr", "Petr")
    with pytest.raises(MessageError, match="netýká"):
        msgs.set_read(m, "karel", "Karel")
    m = load(server, mid)
    assert m.read_by("petr") and not m.resolved                     # přečteno není vyřízeno
    rid = msgs.reply(m, "bob", "Bobík", "Hotovo zítra")
    m = load(server, mid)
    assert [r.text for r in m.replies] == ["Hotovo zítra"] and m.replies[0].author == "Bobík"
    assert m.last_change_by_others("alice") == m.replies[0].created_at   # autorovi je nová odpověď
    assert m.last_change_by_others("bob") == m.created_at            # vlastní odpověď není nová
    with pytest.raises(MessageError, match="autor"):
        msgs.edit_reply(m, rid, "alice", "cizí")
    assert msgs.edit_reply(m, rid, "bob", "Hotovo pozítří")
    m = load(server, mid)
    assert m.replies[0].text == "Hotovo pozítří" and m.replies[0].versions[0].text == "Hotovo zítra"


def test_edit_by_author_keeps_versions(server):
    mid = msgs.send(server, "alice", "PC", "Alča", [("bob", "")], "v1")
    m = load(server, mid)
    with pytest.raises(MessageError, match="autor"):
        msgs.edit(m, "bob", "cizí")
    assert not msgs.edit(m, "alice", "v1")                          # beze změny
    assert msgs.edit(m, "alice", "v2", recipients=[("bob", ""), ("jana", "")])
    m = load(server, mid)
    assert m.text == "v2" and m.edited_at and m.versions[0].text == "v1"
    assert [p.login for p in m.recipients] == ["bob", "jana"]
    with pytest.raises(MessageError, match="adresáta"):
        msgs.edit(m, "alice", "v3", recipients=[])


def test_store_rereads_only_changed_files(server, monkeypatch):
    mid = msgs.send(server, "alice", "PC", "Alča", [("bob", "")], "a")
    store = MessageStore(server)
    store.load()
    reads = []
    original = msgs.Path.read_text
    monkeypatch.setattr(msgs.Path, "read_text", lambda self, *a, **k: reads.append(self.name) or original(self, *a, **k))
    store.load()
    assert reads == []                                              # nic se nezměnilo
    msgs.set_resolved(load(server, mid), "bob", "Bob")
    reads.clear()
    (m,) = store.load()
    assert m.resolved and reads == ["stav_bob.json"]


def test_unavailable_server(tmp_path):
    with pytest.raises(OSError):
        MessageStore(tmp_path / "neni").load()
    (tmp_path / "prazdny").mkdir()
    assert MessageStore(tmp_path / "prazdny").load() == []


def test_project_links_survive_rename(server, tmp_path):
    pm = ProjectManager(Settings(server_root=str(server), local_root=str(tmp_path / "l")), user="alice", pc="PC")
    pm._ensure_notes_tables(server / "Forma")                     # projekt s evidencí (identita)
    assert msgs.project_names(server) == ["058768", "Forma"]          # .spm se nenabízí
    (info,) = msgs.project_info(server, ["Forma"])
    assert info["project_id"]
    assert msgs.find_project(server, "forma", info["project_id"]) == "Forma"
    (server / "Forma").rename(server / "Forma2")
    assert msgs.find_project(server, "Forma", info["project_id"]) == "Forma2"   # přejmenovaný
    assert msgs.find_project(server, "Neni") is None


def test_author_marks_done_and_message_to_self(server):
    mid = msgs.send(server, "alice", "PC", "Alča", [("bob", ""), ("jana", "")], "Už není potřeba")
    m = load(server, mid)
    msgs.set_resolved(m, "alice", "Alča")                           # autor dokončí za všechny
    m = load(server, mid)
    assert m.resolved and [s.login for s in m.resolvers] == ["alice"]
    assert not m.resolved_by("bob")
    msgs.set_resolved(m, "alice", "Alča", resolved=False)
    assert not load(server, mid).resolved
    # vzkaz sám sobě (připomínka)
    mid = msgs.send(server, "alice", "PC", "Alča", [("alice", "Alča")], "Zavolat zákazníkovi")
    m = load(server, mid)
    assert m.is_author("alice") and m.is_recipient("alice")
    assert m.last_change_by_others("alice") == ""                    # vlastní – neohlašuje se
    msgs.set_resolved(m, "alice", "Alča")
    assert load(server, mid).resolved


def test_old_resolved_messages_move_to_archive(server):
    from datetime import datetime, timedelta
    old = msgs.send(server, "alice", "PC", "Alča", [("bob", "")], "Starý vyřízený")
    open_ = msgs.send(server, "alice", "PC", "Alča", [("bob", "")], "Starý nevyřízený")
    msgs.set_resolved(load(server, old), "bob", "Bob")
    store = MessageStore(server)
    later = datetime.now() + timedelta(days=91)
    assert store.archive_old(store.load(), 0, later) == []                 # 0 = nearchivovat
    assert store.archive_old(store.load(), 90, datetime.now()) == []        # ještě není starý
    store._archived_on = ""
    assert store.archive_old(store.load(), 90, later) == [old]              # jen vyřízený
    assert store.archive_old(store.load(), 90, later) == []                 # nejvýš jednou denně
    assert [m.id for m in store.load()] == [open_]                          # běžné načtení bez archivu
    (arch,) = store.load_archive()
    assert arch.id == old and arch.archived and arch.text == "Starý vyřízený"
    assert (server / ".spm" / "vzkazy" / "_archiv" / old / "vzkaz.json").exists()   # nic se nesmazalo
    # odpověď na archivovaný vzkaz ho vrátí mezi aktivní
    msgs.reply(arch, "alice", "Alča", "Ještě jedna věc")
    assert sorted(m.id for m in store.load()) == sorted([old, open_]) and store.load_archive() == []
    m = next(x for x in store.load() if x.id == old)
    assert [r.text for r in m.replies] == ["Ještě jedna věc"] and not m.archived
    # nedávná odpověď = vzkaz zůstává aktivní, i když je vyřízený dávno
    store._archived_on = ""
    assert store.archive_old(store.load(), 90, datetime.now() + timedelta(days=30)) == []
