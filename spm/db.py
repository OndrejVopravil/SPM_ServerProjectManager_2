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

# Zrcadlení (v cílovém projektu): soubory spravované zrcadlem z jiného serveru. Tabulky se
# zakládají až při prvním zrcadlení – ostatní databáze zůstávají beze změny.
MIRROR_SCHEMA = """
CREATE TABLE IF NOT EXISTS mirror_files (
    file_id        TEXT PRIMARY KEY REFERENCES files(file_id),
    source         TEXT NOT NULL,       -- zdrojový projekt (jeho project_id)
    source_file_id TEXT NOT NULL        -- file_id souboru v databázi zdrojového projektu
);
CREATE INDEX IF NOT EXISTS ix_mirror_source ON mirror_files(source, source_file_id);

CREATE TABLE IF NOT EXISTS mirror_sources (
    source        TEXT PRIMARY KEY,
    synced_commit INTEGER,              -- poslední záznam historie zdroje, ke kterému je zrcadlo aktuální
    synced_at     TEXT,
    label         TEXT,                 -- cesta ke zdrojovému projektu (pro zobrazení)
    server_id     TEXT,                 -- identita zdrojové síťové složky (spm_config.json)
    server_name   TEXT
);
"""
# Zrcadlení (ve zdrojovém projektu): kam se soubory zrcadlí – pro označení ve stromu.
MIRROR_TARGETS_SCHEMA = """
CREATE TABLE IF NOT EXISTS mirror_targets (
    file_id     TEXT NOT NULL REFERENCES files(file_id),
    target      TEXT NOT NULL,      -- cílový projekt (jeho project_id)
    label       TEXT NOT NULL,      -- cesta k cílovému projektu (pro zobrazení)
    server_id   TEXT,               -- identita cílové síťové složky (spm_config.json)
    server_name TEXT,
    path        TEXT NOT NULL,      -- cesta souboru v cílovém projektu
    mirrored_at TEXT NOT NULL,
    PRIMARY KEY (file_id, target)
);
"""

# Záznamy běhu skriptů odevzdání (zakládá se až při prvním běhu skriptu)
SCRIPT_SCHEMA = """
CREATE TABLE IF NOT EXISTS script_runs (
    run_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    commit_id  INTEGER REFERENCES commits(commit_id),
    created_at TEXT NOT NULL,
    user       TEXT NOT NULL,
    pc         TEXT NOT NULL,
    action     TEXT NOT NULL,       -- submit / deactivate / publish
    scripts    TEXT NOT NULL,       -- názvy skriptů oddělené čárkou
    ok         INTEGER NOT NULL,
    log        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_script_runs_commit ON script_runs(commit_id);
"""

# sloupce mirror_sources přidané později (tabulka mohla vzniknout dřív bez nich)
MIRROR_SOURCE_COLUMNS = ("label", "server_id", "server_name")
# sloupce mirror_files přidané později: zrcadlená kopie je neaktuální (zdroj se změnil, ale
# do cíle se nezrcadlil) – kdy, kdo odevzdal a proč (changed / deleted)
MIRROR_FILE_COLUMNS = ("stale_at", "stale_by", "stale_reason")
STALE_CHANGED = "changed"
STALE_DELETED = "deleted"


@dataclass
class MirrorTarget:
    """Kam se soubor zdroje zrcadlí – pro zobrazení."""
    label: str           # cesta k cílovému projektu
    path: str            # cesta souboru v cíli
    server_id: str = ""
    server_name: str = ""
    target: str = ""     # identita cílového projektu (project_id)


@dataclass
class MirrorSource:
    """Odkud zrcadlo soubory přináší – pro zobrazení."""
    label: str           # cesta ke zdrojovému projektu, jak ji viděl ten, kdo zrcadlil
    server_id: str = ""
    server_name: str = ""


@dataclass
class MirrorStale:
    """Zrcadlená kopie v cíli je neaktuální – zdroj se změnil, ale do cíle se nezrcadlil."""
    at: str              # kdy (ISO)
    by: str              # kdo odevzdal
    reason: str          # STALE_CHANGED / STALE_DELETED

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
RESTORED = "restored"  # smazán na serveru mimo aplikaci, nahrán zpět z místní kopie

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
    RESTORED: "Obnoven z místní kopie",
}

# Druhy odevzdání (commits)
UPLOAD = "upload"                   # odevzdání / deaktivace
PUBLISH = "publish"                 # publikování projektu z místního úložiště
SYNC = "sync"                       # změny provedené mimo aplikaci, zjištěné při aktivaci
PROJECT_RENAMED = "project-renamed"
CANCELLED = "cancelled"             # zrušení vlastní aktivace
FORCE_UNLOCK = "force-unlock"       # odebrání zámků jiného uživatele
PROJECT_COMMENT = "project-comment"  # změna komentáře projektu (komentář záznamu = nový text)
MIRROR = "mirror"                   # soubory zrcadlené z jiného serveru (v cílovém projektu)

COMMIT_LABELS = {
    UPLOAD: "Odevzdání",
    PUBLISH: "Publikování",
    SYNC: "Změny mimo aplikaci",
    PROJECT_RENAMED: "Přejmenování projektu",
    CANCELLED: "Zrušení aktivace",
    FORCE_UNLOCK: "Odebrání zámků",
    PROJECT_COMMENT: "Komentář projektu",
    MIRROR: "Zrcadlení",
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
                self.conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('project_id', ?)",
                                  (uuid.uuid4().hex,))

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

    # --- zrcadlení (cílový projekt) --------------------------------------

    def ensure_mirror_tables(self) -> None:
        self.conn.executescript(MIRROR_SCHEMA)
        have = {r[1] for r in self.conn.execute("PRAGMA table_info(mirror_sources)")}
        for column in MIRROR_SOURCE_COLUMNS:
            if column not in have:
                self.conn.execute(f"ALTER TABLE mirror_sources ADD COLUMN {column} TEXT")
        have = {r[1] for r in self.conn.execute("PRAGMA table_info(mirror_files)")}
        for column in MIRROR_FILE_COLUMNS:
            if column not in have:
                self.conn.execute(f"ALTER TABLE mirror_files ADD COLUMN {column} TEXT")
        self.conn.commit()

    def mirror_sources(self) -> dict[str, MirrorSource]:
        """source -> popis zdroje (cesta, identita a název síťové složky)."""
        try:
            rows = self.conn.execute("SELECT * FROM mirror_sources").fetchall()
        except sqlite3.OperationalError:
            return {}
        result = {}
        for r in rows:
            keys = r.keys()
            result[r["source"]] = MirrorSource(
                (r["label"] if "label" in keys else None) or r["source"],
                (r["server_id"] if "server_id" in keys else None) or "",
                (r["server_name"] if "server_name" in keys else None) or "")
        return result

    def set_mirror_source(self, source: str, label: str, server_id: str, server_name: str) -> None:
        self.conn.execute(
            "INSERT INTO mirror_sources(source, label, server_id, server_name) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(source) DO UPDATE SET label = excluded.label, server_id = excluded.server_id, "
            "server_name = excluded.server_name", (source, label, server_id, server_name))

    def rekey_mirror_source(self, old: str, new: str) -> None:
        """Dřívější klíč zdroje (cesta) -> nový (project_id)."""
        self.conn.execute("UPDATE mirror_files SET source = ? WHERE source = ?", (new, old))
        self.conn.execute("DELETE FROM mirror_sources WHERE source = ?", (old,))

    # --- zrcadlení (zdrojový projekt) -------------------------------------

    def ensure_mirror_target_tables(self) -> None:
        self.conn.executescript(MIRROR_TARGETS_SCHEMA)
        self.conn.commit()

    def set_mirror_target(self, file_id: str, target: str, label: str, server_id: str,
                          server_name: str, path: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO mirror_targets(file_id, target, label, server_id, server_name, path, "
            "mirrored_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (file_id, target, label, server_id, server_name, path, now_iso()))

    def remove_mirror_target(self, file_id: str, target: str) -> None:
        self.conn.execute("DELETE FROM mirror_targets WHERE file_id = ? AND target = ?", (file_id, target))

    def mirror_targets(self) -> dict[str, list[MirrorTarget]]:
        """file_id -> kam se soubor zrcadlí; prázdné, pokud se z projektu nezrcadlilo."""
        try:
            rows = self.conn.execute("SELECT * FROM mirror_targets ORDER BY label").fetchall()
        except sqlite3.OperationalError:
            return {}
        result: dict[str, list[MirrorTarget]] = {}
        for r in rows:
            result.setdefault(r["file_id"], []).append(
                MirrorTarget(r["label"], r["path"], r["server_id"] or "", r["server_name"] or "",
                             r["target"]))
        return result

    # --- identita projektu -----------------------------------------------

    def project_id(self) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = 'project_id'").fetchone()
        return row[0] if row else None

    def ensure_project_id(self) -> str:
        """Trvalá identita projektu (přežije přejmenování i jiné připojení disku)."""
        existing = self.project_id()
        if existing:
            return existing
        new = uuid.uuid4().hex
        self.conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('project_id', ?)", (new,))
        return self.project_id() or new

    def mirrored(self) -> dict[str, tuple[str, str]]:
        """file_id -> (zdroj, file_id ve zdroji) u souborů spravovaných zrcadlem."""
        try:
            rows = self.conn.execute(
                "SELECT m.file_id, m.source, m.source_file_id FROM mirror_files m "
                "JOIN files f USING(file_id) WHERE f.deleted = 0").fetchall()
        except sqlite3.OperationalError:
            return {}  # projekt nikdy nebyl cílem zrcadlení
        return {r[0]: (r[1], r[2]) for r in rows}

    def set_mirrored(self, file_id: str, source: str, source_file_id: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO mirror_files(file_id, source, source_file_id) "
                          "VALUES (?, ?, ?)", (file_id, source, source_file_id))

    def unset_mirrored(self, file_id: str) -> None:
        self.conn.execute("DELETE FROM mirror_files WHERE file_id = ?", (file_id,))

    def set_mirror_stale(self, file_id: str, by: str, reason: str) -> None:
        """Zrcadlená kopie je neaktuální (po ensure_mirror_tables)."""
        self.conn.execute("UPDATE mirror_files SET stale_at = ?, stale_by = ?, stale_reason = ? "
                          "WHERE file_id = ?", (now_iso(), by, reason, file_id))

    def clear_mirror_stale(self, file_id: str) -> None:
        """Kopie je znovu aktuální (zrcadlo ji právě ověřilo / nahrálo)."""
        try:
            self.conn.execute("UPDATE mirror_files SET stale_at = NULL, stale_by = NULL, "
                              "stale_reason = NULL WHERE file_id = ?", (file_id,))
        except sqlite3.OperationalError:
            pass  # starší tabulka bez sloupců – nic neaktuálního v ní není

    def mirror_stale(self) -> dict[str, MirrorStale]:
        """file_id -> neaktuální zrcadlená kopie (soubory, které zdroj nezrcadlil)."""
        try:
            rows = self.conn.execute(
                "SELECT m.file_id, m.stale_at, m.stale_by, m.stale_reason FROM mirror_files m "
                "JOIN files f USING(file_id) WHERE f.deleted = 0 AND m.stale_at IS NOT NULL").fetchall()
        except sqlite3.OperationalError:
            return {}
        return {r[0]: MirrorStale(r[1], r[2] or "", r[3] or STALE_CHANGED) for r in rows}

    def last_commit_id(self) -> int | None:
        return self.conn.execute("SELECT MAX(commit_id) FROM commits").fetchone()[0]

    # --- skripty -------------------------------------------------------

    def ensure_script_tables(self) -> None:
        self.conn.executescript(SCRIPT_SCHEMA)
        self.conn.commit()

    def has_script_runs(self) -> bool:
        return self.conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' "
                                 "AND name = 'script_runs'").fetchone() is not None

    def add_script_run(self, commit_id: int | None, user: str, pc: str, action: str,
                       scripts: list[str], ok: bool, log: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO script_runs(commit_id, created_at, user, pc, action, scripts, ok, log) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (commit_id, now_iso(), user, pc, action, ", ".join(scripts), int(ok), log))
        return cur.lastrowid

    def script_runs(self) -> list[sqlite3.Row]:
        """Všechny běhy skriptů (nejnovější první); prázdné, pokud tabulka neexistuje."""
        if not self.has_script_runs():
            return []
        return self.conn.execute("SELECT * FROM script_runs ORDER BY run_id DESC").fetchall()

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
