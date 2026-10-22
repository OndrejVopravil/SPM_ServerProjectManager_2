"""Předávací poznámky k projektu (text v Markdownu).

- Zveřejněné poznámky jsou v databázi projektu (tabulky notes a note_targets – zakládají se
  s první poznámkou). Mohou se týkat souborů (podle file_id – přežijí přejmenování) a složek
  (podle cesty), mohou být adresované uživateli (Windows login) a mají stav vyřízeno / otevřeno.
  Uživatelé zmínění v textu „@jméno“ jsou „na vědomí“ (sloupec cc) – jen informace, adresát
  se vybírá zvlášť.
- Rozepsané poznámky (během práce na aktivovaném projektu) jsou v místní kopii
  (<místní kopie>/.spm/notes_draft.json) – ostatní je nevidí; zveřejní se s odevzdáním.
- Souhrn otevřených poznámek je navíc v <projekt>/.spm/notes.json, aby seznam projektů
  nemusel kvůli značce „poznámka pro vás“ otevírat databázi každého projektu.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import SPM_DIR
from .db import ProjectDB, db_path
from .lock import now_iso
from .scanner import TEMP_SUFFIX

DRAFTS_FILE = "notes_draft.json"
SUMMARY_FILE = "notes.json"

NOTES_SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    note_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at     TEXT NOT NULL,
    user           TEXT NOT NULL,       -- login autora
    pc             TEXT NOT NULL,
    author         TEXT,                -- zobrazované jméno autora (přezdívka / jméno)
    recipient      TEXT,                -- login adresáta (malými písmeny), NULL = pro všechny
    recipient_name TEXT,
    text           TEXT NOT NULL,       -- Markdown
    commit_id      INTEGER,             -- odevzdání, se kterým poznámka vznikla
    resolved_at    TEXT,
    resolved_by    TEXT,
    parent_id      INTEGER,             -- doplnění (odpověď) k poznámce; NULL = poznámka
    edited_at      TEXT,                -- poslední úprava autorem
    seq            INTEGER,             -- pořadí změny (vznik / úprava) – podle něj „NOVÁ“
    cc             TEXT                 -- na vědomí: JSON [[login, jméno], …] ze zmínek @
);
CREATE TABLE IF NOT EXISTS note_targets (
    note_id INTEGER NOT NULL REFERENCES notes(note_id),
    file_id TEXT,                       -- soubor (NULL u složky)
    path    TEXT NOT NULL               -- cesta při vzniku; složka končí '/'
);
CREATE INDEX IF NOT EXISTS ix_note_targets ON note_targets(note_id);
CREATE TABLE IF NOT EXISTS note_versions (
    note_id        INTEGER NOT NULL REFERENCES notes(note_id),
    saved_at       TEXT NOT NULL,       -- kdy bylo toto znění nahrazeno
    text           TEXT NOT NULL,
    recipient      TEXT,
    recipient_name TEXT
);
CREATE INDEX IF NOT EXISTS ix_note_versions ON note_versions(note_id);
"""
# sloupce notes přidané později (tabulka mohla vzniknout dřív bez nich)
NOTE_COLUMNS = (("parent_id", "INTEGER"), ("edited_at", "TEXT"), ("seq", "INTEGER"), ("cc", "TEXT"))


@dataclass
class Draft:
    """Rozepsaná poznámka (jen v místní kopii)."""
    text: str = ""
    recipient: str = ""          # login adresáta, "" = pro všechny
    recipient_name: str = ""
    targets: list[str] = field(default_factory=list)  # soubory / složky ('/' na konci)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    created_at: str = field(default_factory=now_iso)
    cc: list = field(default_factory=list)  # na vědomí: [[login, jméno], …]

    @property
    def title(self) -> str:
        first = self.text.strip().split("\n", 1)[0].lstrip("#").strip()
        return first or "(prázdná poznámka)"


@dataclass
class NoteVersion:
    saved_at: str
    text: str
    recipient: str
    recipient_name: str


@dataclass
class Note:
    note_id: int
    created_at: str
    user: str
    pc: str
    author: str
    recipient: str
    recipient_name: str
    text: str
    commit_id: int | None
    resolved_at: str | None
    resolved_by: str | None
    targets: list[str] = field(default_factory=list)  # aktuální cesty (soubory podle file_id)
    parent_id: int | None = None
    edited_at: str | None = None
    seq: int = 0
    replies: list["Note"] = field(default_factory=list)       # doplnění (nejstarší první)
    versions: list[NoteVersion] = field(default_factory=list)  # předchozí znění (nejnovější první)
    cc: list[tuple[str, str]] = field(default_factory=list)  # na vědomí: (login, jméno)

    @property
    def resolved(self) -> bool:
        return bool(self.resolved_at)

    @property
    def last_seq(self) -> int:
        """Poslední změna poznámky včetně doplnění (pro „NOVÁ“)."""
        return max([self.seq] + [r.seq for r in self.replies])

    def is_for(self, login: str) -> bool:
        return bool(self.recipient) and self.recipient == login.lower()

    def is_cc(self, login: str) -> bool:
        """Uživatel je na vědomí (zmíněný „@jméno“)."""
        return any(c[0] == login.lower() for c in self.cc)

    def is_new(self, seen: int, me: str) -> bool:
        """Vznikla / byla upravena po posledním zobrazení – a ne mnou (vlastní změny nejsou nové)."""
        return self.seq > seen and not self.is_author(me)

    def has_news(self, seen: int, me: str) -> bool:
        """Nová / upravená poznámka nebo nové doplnění od někoho jiného."""
        return self.is_new(seen, me) or any(r.is_new(seen, me) for r in self.replies)

    def is_author(self, login: str) -> bool:
        return self.user.lower() == login.lower()

    def concerns(self, login: str) -> bool:
        """Týká se uživatele: je pro něj, nebo je jeho a někdo jiný ji doplnil."""
        return self.is_for(login) or (self.is_author(login)
                                      and any(not r.is_author(login) for r in self.replies))


@dataclass
class Summary:
    open: int = 0
    recipients: dict[str, int] = field(default_factory=dict)  # login -> počet otevřených pro něj

    def for_user(self, login: str) -> int:
        return self.recipients.get(login.lower(), 0)


class NoteError(Exception):
    pass


# --- zveřejněné poznámky (databáze projektu) ----------------------------------------------

def _columns(db: ProjectDB) -> set[str]:
    return {r[1] for r in db.conn.execute("PRAGMA table_info(notes)")}


def read_notes(server: Path) -> list[Note]:
    """Poznámky projektu (nejnovější první) i s doplněními a předchozími zněními."""
    if not db_path(server).exists():
        return []
    with ProjectDB.for_project(server) as db:
        try:
            have = _columns(db)
            if not have:
                return []  # projekt zatím nemá žádné poznámky
            extra = ", ".join(f"{c}" if c in have else f"NULL AS {c}" for c, _ in NOTE_COLUMNS)
            rows = db.conn.execute(f"SELECT *, {extra} FROM notes ORDER BY note_id DESC").fetchall() \
                if not all(c in have for c, _ in NOTE_COLUMNS) else \
                db.conn.execute("SELECT * FROM notes ORDER BY note_id DESC").fetchall()
            targets = db.conn.execute(
                "SELECT t.note_id, t.path, f.current_path, f.deleted FROM note_targets t "
                "LEFT JOIN files f ON f.file_id = t.file_id").fetchall()
            try:
                versions = db.conn.execute(
                    "SELECT * FROM note_versions ORDER BY rowid DESC").fetchall()
            except sqlite3.OperationalError:
                versions = []
        except sqlite3.OperationalError:
            return []
    by_note: dict[int, list[str]] = {}
    for note_id, path, current, deleted in targets:
        by_note.setdefault(note_id, []).append(current if current and not deleted else path)
    old: dict[int, list[NoteVersion]] = {}
    for v in versions:
        old.setdefault(v["note_id"], []).append(
            NoteVersion(v["saved_at"], v["text"], v["recipient"] or "", v["recipient_name"] or ""))
    notes = [Note(r["note_id"], r["created_at"], r["user"], r["pc"], r["author"] or r["user"],
                  r["recipient"] or "", r["recipient_name"] or "", r["text"], r["commit_id"],
                  r["resolved_at"], r["resolved_by"], sorted(by_note.get(r["note_id"], [])),
                  r["parent_id"], r["edited_at"], r["seq"] or r["note_id"], [],
                  old.get(r["note_id"], []), _parse_cc(r["cc"]))
             for r in rows]
    by_id = {n.note_id: n for n in notes}
    for n in reversed(notes):  # doplnění od nejstaršího
        if n.parent_id is not None and n.parent_id in by_id:
            by_id[n.parent_id].replies.append(n)
    return [n for n in notes if n.parent_id is None]


def _parse_cc(value: str | None) -> list[tuple[str, str]]:
    try:
        data = json.loads(value or "[]")
        return [(str(c[0]).lower(), str(c[1] if len(c) > 1 else "")) for c in data if c and c[0]]
    except (ValueError, TypeError, IndexError):
        return []


def _cc_json(cc) -> str | None:
    items = [[str(c[0]).lower(), str(c[1] if len(c) > 1 else "")] for c in cc or [] if c and c[0]]
    return json.dumps(items, ensure_ascii=False) if items else None


def _next_seq(db: ProjectDB) -> int:
    row = db.conn.execute("SELECT MAX(MAX(COALESCE(seq, 0)), MAX(note_id)) FROM notes").fetchone()
    return (row[0] or 0) + 1


def insert_notes(db: ProjectDB, drafts: list[Draft], user: str, pc: str, author: str,
                 commit_id: int | None, parent_id: int | None = None) -> list[int]:
    """Zapíše poznámky do databáze (volá se v transakci pod zámkem zápisu)."""
    files = db.active_files(include_pending=True)
    ids = []
    for d in drafts:
        cur = db.conn.execute(
            "INSERT INTO notes(created_at, user, pc, author, recipient, recipient_name, text, "
            "commit_id, parent_id, seq, cc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now_iso(), user, pc, author, d.recipient.lower() or None, d.recipient_name or None,
             d.text.strip(), commit_id, parent_id, _next_seq(db), _cc_json(d.cc)))
        _set_targets(db, cur.lastrowid, d.targets, files)
        ids.append(cur.lastrowid)
    return ids


def _set_targets(db: ProjectDB, note_id: int, targets: list[str], files=None) -> None:
    files = files if files is not None else db.active_files(include_pending=True)
    db.conn.execute("DELETE FROM note_targets WHERE note_id = ?", (note_id,))
    for path in targets:
        f = None if path.endswith("/") else files.get(path)
        db.conn.execute("INSERT INTO note_targets(note_id, file_id, path) VALUES (?, ?, ?)",
                        (note_id, f.file_id if f else None, path))


def _row(db: ProjectDB, note_id: int):
    row = db.conn.execute("SELECT * FROM notes WHERE note_id = ?", (note_id,)).fetchone()
    if row is None:
        raise NoteError("Poznámka už neexistuje.")
    return row


def edit_note(db: ProjectDB, note_id: int, user: str, text: str, recipient: str | None = None,
              recipient_name: str | None = None, targets: list[str] | None = None,
              cc: list | None = None) -> bool:
    """Úprava poznámky (nebo doplnění) jejím autorem. Předchozí znění se uloží do historie.
    recipient / targets / cc None = beze změny. Vrací False, pokud se nic nezměnilo."""
    row = _row(db, note_id)
    if row["user"].lower() != user.lower():
        raise NoteError("Upravit poznámku může jen její autor.")
    text = text.strip()
    if not text:
        raise NoteError("Poznámka nesmí být prázdná.")
    new_recipient = (recipient.lower() or None) if recipient is not None else row["recipient"]
    new_name = (recipient_name or None) if recipient is not None else row["recipient_name"]
    old_targets = sorted(r[0] for r in db.conn.execute(
        "SELECT path FROM note_targets WHERE note_id = ?", (note_id,)))
    new_cc = _cc_json(cc) if cc is not None else row["cc"]
    if (text, new_recipient, new_cc) == (row["text"], row["recipient"], row["cc"]) and \
            (targets is None or sorted(targets) == old_targets):
        return False
    db.conn.execute("INSERT INTO note_versions(note_id, saved_at, text, recipient, recipient_name) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (note_id, now_iso(), row["text"], row["recipient"], row["recipient_name"]))
    db.conn.execute("UPDATE notes SET text = ?, recipient = ?, recipient_name = ?, edited_at = ?, "
                    "seq = ?, cc = ? WHERE note_id = ?",
                    (text, new_recipient, new_name, now_iso(), _next_seq(db), new_cc, note_id))
    if targets is not None:
        _set_targets(db, note_id, targets)
    return True


def ensure_tables(db: ProjectDB) -> None:
    db.conn.executescript(NOTES_SCHEMA)
    have = _columns(db)
    for column, kind in NOTE_COLUMNS:
        if column not in have:
            db.conn.execute(f"ALTER TABLE notes ADD COLUMN {column} {kind}")
    db.conn.commit()


def set_resolved(db: ProjectDB, note_id: int, resolved: bool, user: str) -> None:
    db.conn.execute("UPDATE notes SET resolved_at = ?, resolved_by = ? WHERE note_id = ?",
                    (now_iso() if resolved else None, user if resolved else None, note_id))


def write_summary(server: Path, db: ProjectDB) -> None:
    """Souhrn otevřených poznámek pro seznam projektů (.spm/notes.json): u uživatele počet
    otevřených poznámek, které jsou pro něj, nebo které napsal a někdo jiný je doplnil."""
    rows = db.conn.execute("SELECT note_id, user, recipient FROM notes "
                           "WHERE resolved_at IS NULL AND parent_id IS NULL").fetchall()
    repliers: dict[int, set[str]] = {}
    for parent, user in db.conn.execute(
            "SELECT parent_id, user FROM notes WHERE parent_id IS NOT NULL"):
        repliers.setdefault(parent, set()).add(user.lower())
    summary = Summary(len(rows))
    for note_id, author, recipient in rows:
        concerned = set()
        if recipient:
            concerned.add(recipient)
        if repliers.get(note_id, set()) - {author.lower()}:
            concerned.add(author.lower())
        for login in concerned:
            summary.recipients[login] = summary.recipients.get(login, 0) + 1
    _write_json(server / SPM_DIR / SUMMARY_FILE, asdict(summary))


def read_summary(server: Path) -> Summary:
    try:
        data = json.loads((server / SPM_DIR / SUMMARY_FILE).read_text(encoding="utf-8"))
        recipients = {str(k): int(v) for k, v in dict(data.get("recipients") or {}).items()}
        return Summary(int(data.get("open") or 0), recipients)
    except (OSError, ValueError, TypeError, AttributeError):
        return Summary()


# --- rozepsané poznámky (místní kopie) ---------------------------------------------------

def drafts_path(local: Path) -> Path:
    return local / SPM_DIR / DRAFTS_FILE


def load_drafts(local: Path, path: Path | None = None) -> list[Draft]:
    try:
        data = json.loads((path or drafts_path(local)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    drafts = []
    for item in data if isinstance(data, list) else []:
        if isinstance(item, dict):
            known = {k: item[k] for k in ("text", "recipient", "recipient_name", "targets", "id",
                                          "created_at", "cc") if k in item}
            drafts.append(Draft(**known))
    return drafts


def save_drafts(local: Path, drafts: list[Draft]) -> None:
    path = drafts_path(local)
    if not drafts:
        path.unlink(missing_ok=True)
        return
    _write_json(path, [asdict(d) for d in drafts])


PARKED_DIR = ".spm_notes"  # v místním úložišti; tečka = nezobrazí se jako „Jen místně“


def park_drafts(local_root: Path, name: str, local: Path) -> None:
    """Před smazáním místní kopie (deaktivace) odloží nezveřejněné rozepsané poznámky stranou,
    aby se neztratily – při příští aktivaci projektu se vrátí (restore_drafts)."""
    drafts = load_drafts(local)
    if drafts:
        parked = local_root / PARKED_DIR / f"{name}.json"
        _write_json(parked, [asdict(d) for d in drafts + load_drafts_file(parked)])


def restore_drafts(local_root: Path, name: str, local: Path) -> None:
    parked = local_root / PARKED_DIR / f"{name}.json"
    if parked.exists():
        save_drafts(local, load_drafts(local) + load_drafts_file(parked))
        parked.unlink(missing_ok=True)


def load_drafts_file(path: Path) -> list[Draft]:
    return load_drafts(path.parent.parent, path) if path.exists() else []


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + TEMP_SUFFIX)
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
