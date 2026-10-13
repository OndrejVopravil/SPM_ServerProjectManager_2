"""Předávací poznámky k projektu (text v Markdownu).

- Zveřejněné poznámky jsou v databázi projektu (tabulky notes a note_targets – zakládají se
  s první poznámkou). Mohou se týkat souborů (podle file_id – přežijí přejmenování) a složek
  (podle cesty), mohou být adresované uživateli (Windows login) a mají stav vyřízeno / otevřeno.
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
    resolved_by    TEXT
);
CREATE TABLE IF NOT EXISTS note_targets (
    note_id INTEGER NOT NULL REFERENCES notes(note_id),
    file_id TEXT,                       -- soubor (NULL u složky)
    path    TEXT NOT NULL               -- cesta při vzniku; složka končí '/'
);
CREATE INDEX IF NOT EXISTS ix_note_targets ON note_targets(note_id);
"""


@dataclass
class Draft:
    """Rozepsaná poznámka (jen v místní kopii)."""
    text: str = ""
    recipient: str = ""          # login adresáta, "" = pro všechny
    recipient_name: str = ""
    targets: list[str] = field(default_factory=list)  # soubory / složky ('/' na konci)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    created_at: str = field(default_factory=now_iso)

    @property
    def title(self) -> str:
        first = self.text.strip().split("\n", 1)[0].lstrip("#").strip()
        return first or "(prázdná poznámka)"


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

    @property
    def resolved(self) -> bool:
        return bool(self.resolved_at)

    def is_for(self, login: str) -> bool:
        return bool(self.recipient) and self.recipient == login.lower()


@dataclass
class Summary:
    open: int = 0
    recipients: dict[str, int] = field(default_factory=dict)  # login -> počet otevřených pro něj

    def for_user(self, login: str) -> int:
        return self.recipients.get(login.lower(), 0)


# --- zveřejněné poznámky (databáze projektu) ----------------------------------------------

def read_notes(server: Path) -> list[Note]:
    """Všechny poznámky projektu (nejnovější první)."""
    if not db_path(server).exists():
        return []
    with ProjectDB.for_project(server) as db:
        try:
            rows = db.conn.execute("SELECT * FROM notes ORDER BY note_id DESC").fetchall()
            targets = db.conn.execute(
                "SELECT t.note_id, t.path, f.current_path, f.deleted FROM note_targets t "
                "LEFT JOIN files f ON f.file_id = t.file_id").fetchall()
        except sqlite3.OperationalError:
            return []  # projekt zatím nemá žádné poznámky
    by_note: dict[int, list[str]] = {}
    for note_id, path, current, deleted in targets:
        by_note.setdefault(note_id, []).append(current if current and not deleted else path)
    return [Note(r["note_id"], r["created_at"], r["user"], r["pc"], r["author"] or r["user"],
                 r["recipient"] or "", r["recipient_name"] or "", r["text"], r["commit_id"],
                 r["resolved_at"], r["resolved_by"], sorted(by_note.get(r["note_id"], [])))
            for r in rows]


def insert_notes(db: ProjectDB, drafts: list[Draft], user: str, pc: str, author: str,
                 commit_id: int | None) -> list[int]:
    """Zapíše rozepsané poznámky do databáze (volá se v transakci pod zámkem zápisu)."""
    files = db.active_files(include_pending=True)
    ids = []
    for d in drafts:
        cur = db.conn.execute(
            "INSERT INTO notes(created_at, user, pc, author, recipient, recipient_name, text, commit_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (now_iso(), user, pc, author, d.recipient.lower() or None, d.recipient_name or None,
             d.text.strip(), commit_id))
        for path in d.targets:
            f = None if path.endswith("/") else files.get(path)
            db.conn.execute("INSERT INTO note_targets(note_id, file_id, path) VALUES (?, ?, ?)",
                            (cur.lastrowid, f.file_id if f else None, path))
        ids.append(cur.lastrowid)
    return ids


def ensure_tables(db: ProjectDB) -> None:
    db.conn.executescript(NOTES_SCHEMA)
    db.conn.commit()


def set_resolved(db: ProjectDB, note_id: int, resolved: bool, user: str) -> None:
    db.conn.execute("UPDATE notes SET resolved_at = ?, resolved_by = ? WHERE note_id = ?",
                    (now_iso() if resolved else None, user if resolved else None, note_id))


def write_summary(server: Path, db: ProjectDB) -> None:
    """Souhrn otevřených poznámek pro seznam projektů (.spm/notes.json)."""
    rows = db.conn.execute("SELECT recipient FROM notes WHERE resolved_at IS NULL").fetchall()
    summary = Summary(len(rows))
    for (recipient,) in rows:
        if recipient:
            summary.recipients[recipient] = summary.recipients.get(recipient, 0) + 1
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
                                          "created_at") if k in item}
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
