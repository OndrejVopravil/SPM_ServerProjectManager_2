"""Databáze projektu (SQLite) uložená v .spm/project.db na síťovém úložišti.

Pravidla pro síťový disk:
- journal_mode=DELETE (WAL přes síť nefunguje a vede k poškození databáze),
- zapisuje pouze držitel zámku projektu, ostatní instance pouze čtou.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import SPM_DIR

SCHEMA_VERSION = 1

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
    deleted      INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_files_path ON files(current_path) WHERE deleted = 0;

CREATE TABLE IF NOT EXISTS sessions (
    session_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    user           TEXT NOT NULL,
    pc             TEXT NOT NULL,
    activated_at   TEXT NOT NULL,
    deactivated_at TEXT,
    status         TEXT NOT NULL,       -- active | uploading | done | cancelled
    comment        TEXT
);

CREATE TABLE IF NOT EXISTS changes (
    change_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES sessions(session_id),
    file_id     TEXT NOT NULL REFERENCES files(file_id),
    change_type TEXT NOT NULL,
    old_path    TEXT,
    new_path    TEXT,
    size        INTEGER,
    mtime       REAL,
    sha256      TEXT,
    backup_path TEXT,
    changed_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_changes_session ON changes(session_id);
CREATE INDEX IF NOT EXISTS ix_changes_file ON changes(file_id);
"""

# Typy změn
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

# Stav záznamu v tabulce sessions, který není sezením práce, ale přejmenováním celého projektu
PROJECT_RENAMED = "project-renamed"

STATUS_LABELS = {
    PROJECT_RENAMED: "Přejmenování projektu",
    "active": "Aktivní",
    "uploading": "Nahrávání přerušeno",
    "done": "Dokončeno",
    "cancelled": "Zrušeno",
}


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def db_path(project_dir: Path) -> Path:
    return project_dir / SPM_DIR / "project.db"


@dataclass
class DbFile:
    file_id: str
    path: str
    size: int
    mtime: float
    sha256: str


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
        if create:
            with self.conn:
                self.conn.executescript(SCHEMA)
                self.conn.execute(
                    "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
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

    def active_files(self) -> dict[str, DbFile]:
        rows = self.conn.execute(
            "SELECT file_id, current_path, size, mtime, sha256 FROM files WHERE deleted = 0"
        )
        return {r["current_path"]: DbFile(r[0], r[1], r[2], r[3], r[4]) for r in rows}

    def add_file(self, path: str, size: int, mtime: float, sha256: str) -> str:
        file_id = uuid.uuid4().hex
        self.conn.execute(
            "INSERT INTO files(file_id, current_path, size, mtime, sha256) VALUES (?, ?, ?, ?, ?)",
            (file_id, path, size, mtime, sha256),
        )
        return file_id

    def update_file(self, file_id: str, path: str, size: int, mtime: float, sha256: str) -> None:
        self.conn.execute(
            "UPDATE files SET current_path = ?, size = ?, mtime = ?, sha256 = ? WHERE file_id = ?",
            (path, size, mtime, sha256, file_id),
        )

    def mark_deleted(self, file_id: str) -> None:
        self.conn.execute("UPDATE files SET deleted = 1 WHERE file_id = ?", (file_id,))

    # --- sezení --------------------------------------------------------

    def start_session(self, user: str, pc: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO sessions(user, pc, activated_at, status) VALUES (?, ?, ?, 'active')",
            (user, pc, now_iso()),
        )
        return cur.lastrowid

    def set_session_status(self, session_id: int, status: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE sessions SET status = ? WHERE session_id = ?", (status, session_id)
            )

    def finish_session(self, session_id: int, comment: str, status: str = "done") -> None:
        self.conn.execute(
            "UPDATE sessions SET status = ?, comment = ?, deactivated_at = ? WHERE session_id = ?",
            (status, comment, now_iso(), session_id),
        )

    def open_session(self, user: str, pc: str) -> int | None:
        """Poslední neukončené sezení daného uživatele (active/uploading)."""
        row = self.conn.execute(
            "SELECT session_id FROM sessions WHERE user = ? AND pc = ? "
            "AND status IN ('active', 'uploading') ORDER BY session_id DESC LIMIT 1",
            (user, pc),
        ).fetchone()
        return row[0] if row else None

    def add_change(
        self,
        session_id: int,
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
            "INSERT INTO changes(session_id, file_id, change_type, old_path, new_path, size, "
            "mtime, sha256, backup_path, changed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, file_id, change_type, old_path, new_path, size, mtime, sha256,
             backup_path, now_iso()),
        )

    # --- historie ------------------------------------------------------

    def sessions(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT s.*, (SELECT COUNT(*) FROM changes c WHERE c.session_id = s.session_id) AS n_changes "
            "FROM sessions s ORDER BY s.session_id DESC"
        ).fetchall()

    def changes(self, session_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM changes WHERE session_id = ? ORDER BY change_id", (session_id,)
        ).fetchall()

    def file_history(self, file_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT c.*, s.user, s.comment FROM changes c JOIN sessions s USING(session_id) "
            "WHERE c.file_id = ? ORDER BY c.change_id DESC",
            (file_id,),
        ).fetchall()
