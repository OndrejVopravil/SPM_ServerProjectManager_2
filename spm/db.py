"""Databáze projektu (SQLite) uložená v .spm/project.db na síťovém úložišti.

Pravidla pro síťový disk:
- journal_mode=DELETE (WAL přes síť nefunguje a vede k poškození databáze),
- zapisuje se jen v krátkých transakcích při držení zámku zápisu (lock.WriteLock),
  protože na projektu může současně pracovat víc uživatelů.

Pojmy:
- sezení (sessions)  = jedna aktivace projektu jedním uživatelem (od aktivace po deaktivaci),
- zámek (file_locks) = soubor, který smí upravovat jen držitel (jeho sezení),
- odevzdání (commits) = jedno nahrání změn s komentářem; změny souborů (changes) patří k němu.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import SPM_DIR

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS files (
    file_id      TEXT PRIMARY KEY,      -- stabilní identita, přežije přejmenování
    current_path TEXT NOT NULL,         -- relativní cesta s '/' jako oddělovačem
    size         INTEGER,
    mtime        REAL,                  -- mtime na serveru (pro detekci cizích změn)
    sha256       TEXT,
    deleted      INTEGER NOT NULL DEFAULT 0,
    pending      INTEGER NOT NULL DEFAULT 0   -- právě se nahrává; ostatní ho zatím nestahují
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_files_path ON files(current_path) WHERE deleted = 0;

CREATE TABLE IF NOT EXISTS sessions (
    session_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    user         TEXT NOT NULL,
    pc           TEXT NOT NULL,
    activated_at TEXT NOT NULL,
    ended_at     TEXT,
    status       TEXT NOT NULL          -- active | done | cancelled
);

CREATE TABLE IF NOT EXISTS file_locks (
    file_id    TEXT PRIMARY KEY REFERENCES files(file_id),
    session_id INTEGER NOT NULL REFERENCES sessions(session_id),
    locked_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_locks_session ON file_locks(session_id);

CREATE TABLE IF NOT EXISTS commits (
    commit_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER REFERENCES sessions(session_id),
    user       TEXT NOT NULL,
    pc         TEXT NOT NULL,
    created_at TEXT NOT NULL,
    kind       TEXT NOT NULL,
    comment    TEXT
);

CREATE TABLE IF NOT EXISTS changes (
    change_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    commit_id   INTEGER NOT NULL REFERENCES commits(commit_id),
    file_id     TEXT NOT NULL REFERENCES files(file_id),
    change_type TEXT NOT NULL,
    old_path    TEXT,
    new_path    TEXT,
    size        INTEGER,
    mtime       REAL,
    sha256      TEXT,
    backup_path TEXT
);
CREATE INDEX IF NOT EXISTS ix_changes_commit ON changes(commit_id);
CREATE INDEX IF NOT EXISTS ix_changes_file ON changes(file_id);
"""

# Typy změn souborů
ADDED = "added"
MODIFIED = "modified"
RENAMED = "renamed"
RENAMED_MODIFIED = "renamed+modified"
DELETED = "deleted"
PUBLISHED = "published"  # soubor nahrán při publikování nového projektu z místního úložiště
# Změny zjištěné při aktivaci, které vznikly mimo aplikaci (přímo na serveru)
IMPORTED = "imported"
EXTERNAL_MODIFIED = "external-modified"
EXTERNAL_DELETED = "external-deleted"
EXTERNAL_RENAMED = "external-renamed"

CHANGE_LABELS = {
    ADDED: "Přidán",
    MODIFIED: "Upraven",
    RENAMED: "Přejmenován",
    RENAMED_MODIFIED: "Přejmenován a upraven",
    DELETED: "Smazán",
    PUBLISHED: "Publikován",
    IMPORTED: "Načten do evidence",
    EXTERNAL_MODIFIED: "Změněn mimo aplikaci",
    EXTERNAL_DELETED: "Smazán mimo aplikaci",
    EXTERNAL_RENAMED: "Přejmenován mimo aplikaci",
}

# Druhy odevzdání (commits)
UPLOAD = "upload"                   # odevzdání / deaktivace
PUBLISH = "publish"                 # publikování projektu z místního úložiště
SYNC = "sync"                       # změny provedené mimo aplikaci, zjištěné při aktivaci
PROJECT_RENAMED = "project-renamed"
CANCELLED = "cancelled"             # zrušení vlastní aktivace
FORCE_UNLOCK = "force-unlock"       # odebrání zámků jiného uživatele
PROJECT_COMMENT = "project-comment"  # změna komentáře projektu (komentář záznamu = nový text)

COMMIT_LABELS = {
    UPLOAD: "Odevzdání",
    PUBLISH: "Publikování",
    SYNC: "Změny mimo aplikaci",
    PROJECT_RENAMED: "Přejmenování projektu",
    CANCELLED: "Zrušení aktivace",
    FORCE_UNLOCK: "Odebrání zámků",
    PROJECT_COMMENT: "Komentář projektu",
}


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def db_path(project_dir: Path) -> Path:
    return project_dir / SPM_DIR / "project.db"


class DbVersionError(Exception):
    pass


@dataclass
class DbFile:
    file_id: str
    path: str
    size: int
    mtime: float
    sha256: str
    pending: bool = False


@dataclass
class LockOwner:
    session_id: int
    user: str
    pc: str

    @property
    def label(self) -> str:
        return f"{self.user} ({self.pc})"

    def is_me(self, user: str, pc: str) -> bool:
        return self.user.lower() == user.lower() and self.pc.lower() == pc.lower()


class ProjectDB:
    def __init__(self, path: Path, create: bool = False):
        if not create and not path.exists():
            raise FileNotFoundError(f"Databáze projektu neexistuje: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(str(path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=DELETE")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._check_version()
        if create:
            with self.conn:
                self.conn.executescript(SCHEMA)
                self.conn.execute(
                    "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )

    def _check_version(self) -> None:
        try:
            row = self.conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        except sqlite3.OperationalError:
            return  # nová (prázdná) databáze
        if row and row[0] != str(SCHEMA_VERSION):
            self.conn.close()
            raise DbVersionError(
                f"Databáze projektu je ze starší verze aplikace (verze {row[0]}, potřebná "
                f"{SCHEMA_VERSION}) a nelze ji použít:\n{self.path}\n\n"
                "Pokud historii projektu nepotřebujete, smažte složku .spm v projektu na serveru – "
                "při příští aktivaci se založí nová."
            )

    @classmethod
    def for_project(cls, project_dir: Path, create: bool = False) -> "ProjectDB":
        return cls(db_path(project_dir), create=create)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "ProjectDB":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- soubory -------------------------------------------------------

    def active_files(self, include_pending: bool = False) -> dict[str, DbFile]:
        sql = "SELECT file_id, current_path, size, mtime, sha256, pending FROM files WHERE deleted = 0"
        if not include_pending:
            sql += " AND pending = 0"
        return {r[1]: DbFile(r[0], r[1], r[2], r[3], r[4], bool(r[5])) for r in self.conn.execute(sql)}

    def add_file(self, path: str, size: int | None, mtime: float | None, sha256: str | None,
                 pending: bool = False) -> str:
        file_id = uuid.uuid4().hex
        self.conn.execute(
            "INSERT INTO files(file_id, current_path, size, mtime, sha256, pending) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (file_id, path, size, mtime, sha256, int(pending)),
        )
        return file_id

    def update_file(self, file_id: str, path: str, size: int, mtime: float, sha256: str) -> None:
        self.conn.execute(
            "UPDATE files SET current_path = ?, size = ?, mtime = ?, sha256 = ?, pending = 0 "
            "WHERE file_id = ?",
            (path, size, mtime, sha256, file_id),
        )

    def set_pending(self, file_ids: list[str], pending: bool = True) -> None:
        self.conn.executemany("UPDATE files SET pending = ? WHERE file_id = ?",
                              [(int(pending), f) for f in file_ids])

    def mark_deleted(self, file_id: str) -> None:
        self.conn.execute("UPDATE files SET deleted = 1, pending = 0 WHERE file_id = ?", (file_id,))
        self.conn.execute("DELETE FROM file_locks WHERE file_id = ?", (file_id,))

    def drop_placeholder(self, file_id: str) -> None:
        """Smaže rezervaci nového souboru (bez obsahu a historie)."""
        self.conn.execute("DELETE FROM file_locks WHERE file_id = ?", (file_id,))
        self.conn.execute("DELETE FROM files WHERE file_id = ? AND sha256 IS NULL AND NOT EXISTS "
                          "(SELECT 1 FROM changes WHERE file_id = ?)", (file_id, file_id))

    def discard_pending_new(self, session_id: int) -> None:
        """Smaže rezervace nových souborů sezení, které se nikdy nedokončily (bez obsahu)."""
        ids = [r[0] for r in self.conn.execute(
            "SELECT f.file_id FROM files f JOIN file_locks l USING(file_id) "
            "WHERE l.session_id = ? AND f.pending = 1 AND f.sha256 IS NULL", (session_id,))]
        for fid in ids:
            self.conn.execute("DELETE FROM file_locks WHERE file_id = ?", (fid,))
            self.conn.execute("DELETE FROM files WHERE file_id = ? AND NOT EXISTS "
                              "(SELECT 1 FROM changes WHERE file_id = ?)", (fid, fid))
        self.conn.execute(
            "UPDATE files SET pending = 0 WHERE file_id IN "
            "(SELECT file_id FROM file_locks WHERE session_id = ?)", (session_id,))

    # --- zámky souborů -------------------------------------------------

    def lock_owners(self) -> dict[str, LockOwner]:
        """file_id -> kdo soubor drží."""
        rows = self.conn.execute(
            "SELECT l.file_id, s.session_id, s.user, s.pc FROM file_locks l "
            "JOIN sessions s USING(session_id)")
        return {r[0]: LockOwner(r[1], r[2], r[3]) for r in rows}

    def locked_by(self, session_id: int) -> set[str]:
        return {r[0] for r in self.conn.execute(
            "SELECT file_id FROM file_locks WHERE session_id = ?", (session_id,))}

    def lock(self, session_id: int, file_ids: list[str]) -> list[str]:
        """Zamkne volné soubory; vrací file_id souborů, které už drží někdo jiný."""
        owners = self.lock_owners()
        taken = [f for f in file_ids if f in owners and owners[f].session_id != session_id]
        if not taken:
            self.conn.executemany(
                "INSERT OR IGNORE INTO file_locks(file_id, session_id, locked_at) VALUES (?, ?, ?)",
                [(f, session_id, now_iso()) for f in file_ids])
        return taken

    def release(self, session_id: int, file_ids: list[str] | None = None) -> None:
        if file_ids is None:
            self.conn.execute("DELETE FROM file_locks WHERE session_id = ?", (session_id,))
        else:
            self.conn.executemany("DELETE FROM file_locks WHERE session_id = ? AND file_id = ?",
                                  [(session_id, f) for f in file_ids])

    # --- sezení --------------------------------------------------------

    def start_session(self, user: str, pc: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO sessions(user, pc, activated_at, status) VALUES (?, ?, ?, 'active')",
            (user, pc, now_iso()),
        )
        return cur.lastrowid

    def active_session(self, user: str, pc: str) -> int | None:
        row = self.conn.execute(
            "SELECT session_id FROM sessions WHERE lower(user) = lower(?) AND lower(pc) = lower(?) "
            "AND status = 'active' ORDER BY session_id DESC LIMIT 1", (user, pc)).fetchone()
        return row[0] if row else None

    def active_sessions(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM sessions WHERE status = 'active' ORDER BY session_id").fetchall()

    def end_session(self, session_id: int, status: str = "done") -> None:
        self.discard_pending_new(session_id)
        self.release(session_id)
        self.conn.execute("UPDATE sessions SET status = ?, ended_at = ? WHERE session_id = ?",
                          (status, now_iso(), session_id))

    # --- odevzdání a změny ---------------------------------------------

    def add_commit(self, session_id: int | None, user: str, pc: str, kind: str, comment: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO commits(session_id, user, pc, created_at, kind, comment) "
            "VALUES (?, ?, ?, ?, ?, ?)", (session_id, user, pc, now_iso(), kind, comment))
        return cur.lastrowid

    def add_change(
        self,
        commit_id: int,
        file_id: str,
        change_type: str,
        old_path: str | None,
        new_path: str | None,
        size: int | None = None,
        mtime: float | None = None,
        sha256: str | None = None,
        backup_path: str | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO changes(commit_id, file_id, change_type, old_path, new_path, size, "
            "mtime, sha256, backup_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (commit_id, file_id, change_type, old_path, new_path, size, mtime, sha256, backup_path),
        )

    # --- historie ------------------------------------------------------

    def commits(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT c.*, (SELECT COUNT(*) FROM changes x WHERE x.commit_id = c.commit_id) AS n_changes "
            "FROM commits c ORDER BY c.commit_id DESC").fetchall()

    def changes(self, commit_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM changes WHERE commit_id = ? ORDER BY change_id", (commit_id,)).fetchall()

    def all_changes(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM changes ORDER BY change_id").fetchall()

    def file_history(self, file_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT x.*, c.user, c.comment, c.created_at FROM changes x JOIN commits c USING(commit_id) "
            "WHERE x.file_id = ? ORDER BY x.change_id DESC", (file_id,)).fetchall()
