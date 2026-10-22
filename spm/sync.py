"""Aktivace, odevzdání, aktualizace a deaktivace projektů – hlavní logika aplikace (bez GUI).

Model spolupráce: projekt může mít aktivovaný víc uživatelů současně. Každý má v místním
úložišti celou kopii projektu, ale upravovat smí jen soubory, které má zamčené – ostatní
jsou v místní kopii jen pro čtení. Nové soubory může vytvářet kdokoliv; patří tomu, kdo je
vytvořil. Odevzdané soubory se nahrají na server a uvolní, ostatní si je stáhnou
aktualizací ze serveru.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable, Iterator

from . import SPM_DIR, recycle
from . import db as dbm
from .comment import MAX_LENGTH as COMMENT_MAX_LENGTH
from .comment import COMMENT_FILE, ProjectComment, normalize_text, read_comment, write_comment
from . import notes as notes_mod
from . import users
from .notes import Draft, Note
from .config import Settings
from .db import DbFile, LockOwner, ProjectDB, db_path
from .identity import current_full_name, current_pc, current_user
from .lock import LockInfo, WriteLock, active_users, add_marker, now_iso, read_marker, remove_marker
from .manifest import Entry, Manifest
from .folder_ops import free_copy_name
from .mirror import Mirror
from .matcher import RenameSuggestion, similarity, suggest_renames
from .names import describe_chars, find_invalid_names, forbidden_in, format_invalid, invalid_rel_paths
from . import file_locks, restore
from .restore import RESTORE_DIR_RE, RestoreCatalog, RestorePoint, RestoreResult
from .scripting import store as script_store
from .scripting.model import ADDED, DELETED, MODIFIED, RENAMED, UNCHANGED, ScriptFile
from .scanner import (
    TEMP_SUFFIX,
    FileInfo,
    Progress,
    copy_with_hash,
    file_hash,
    fix_case,
    list_files,
    move_file,
    is_ignored,
    remove_empty_parents,
    rmtree,
    same_path,
    same_stat,
    scan,
    set_readonly,
    to_abs,
    to_rel,
)


class SyncError(Exception):
    pass


# Kolik projektů zjišťovat najednou (dotazy na síť čekají hlavně na odezvu serveru)
STATUS_WORKERS = 16


# Složky, do kterých aktivace odkládá dřívější místní kopii (viz activate) – nejsou to projekty.
ORIGINAL_COPY_RE = re.compile(r"_puvodni_\d{8}_\d{6}$")
# Dočasná složka na serveru při publikování (tečka na začátku = v seznamu projektů se nezobrazí)
PUBLISH_TMP_PREFIX = ".spm_publish_"

INVALID_NAME_CHARS = set('<>:"/\\|?*')
RESERVED_NAMES = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                  *(f"lpt{i}" for i in range(1, 10))}


def original_copy_path(local: Path, name: str) -> Path:
    """Volná složka <projekt>_puvodni_<datum_čas> vedle místní kopie. Při kolizi (dvě odložení
    během jedné sekundy) se čas posune – název musí končit datem (ORIGINAL_COPY_RE)."""
    when = datetime.now()
    while True:
        target = local.with_name(f"{name}_puvodni_{when:%Y%m%d_%H%M%S}")
        if not target.exists():
            return target
        when += timedelta(seconds=1)


def validate_project_name(name: str) -> str | None:
    """Vrátí popis chyby, pokud název nelze použít jako název složky projektu, jinak None."""
    if not name:
        return "Název projektu nesmí být prázdný."
    forbidden = forbidden_in(name)
    if forbidden:
        return ("Název obsahuje nepovolené znaky: " + describe_chars(forbidden) +
                "\n(Nepovolená je česká diakritika, mezera a znaky ( ) & , . ; = ^)")
    bad = sorted({ch for ch in name if ch in INVALID_NAME_CHARS or ord(ch) < 32})
    if bad:
        return "Název obsahuje nepovolené znaky: " + " ".join(bad)
    if name != name.strip() or name.endswith("."):
        return "Název nesmí začínat ani končit mezerou a nesmí končit tečkou."
    if name.startswith("."):
        return "Název nesmí začínat tečkou."
    if name.split(".")[0].lower() in RESERVED_NAMES:
        return f"„{name}“ je ve Windows rezervovaný název."
    if ORIGINAL_COPY_RE.search(name) or RESTORE_DIR_RE.search(name):
        return ("Název nesmí končit příponou _puvodni_<datum> ani _obnova_<datum> "
                "(používá je aplikace pro své složky).")
    return None


def validate_folder_name(name: str, check_forbidden: bool = True) -> str | None:
    """Vrátí popis chyby, pokud nový název složky nelze použít, jinak None."""
    if not name.strip():
        return "Název složky nesmí být prázdný."
    error = validate_file_name(name, check_forbidden=False)
    if error:
        return error.replace("souboru", "složky")
    if check_forbidden and forbidden_in(name):
        return ("Název obsahuje nepovolené znaky: " + describe_chars(forbidden_in(name))
                + "\n(Nepovolená je česká diakritika, mezera a znaky ( ) & , . ; = ^)")
    return None


def validate_file_name(name: str, check_forbidden: bool = True) -> str | None:
    """Vrátí popis chyby, pokud nový název souboru nelze použít, jinak None."""
    if not name.strip():
        return "Název souboru nesmí být prázdný."
    bad = sorted({ch for ch in name if ch in INVALID_NAME_CHARS or ord(ch) < 32})
    if bad:
        return "Název obsahuje znaky, které Windows nedovoluje: " + " ".join(bad)
    if name != name.strip() or name.endswith("."):
        return "Název nesmí začínat ani končit mezerou a nesmí končit tečkou."
    if name.split(".")[0].lower() in RESERVED_NAMES:
        return f"„{name}“ je ve Windows rezervovaný název."
    if check_forbidden and invalid_rel_paths([name]):
        return ("Název obsahuje nepovolené znaky: " + describe_chars(forbidden_in(name.rsplit(".", 1)[0]))
                + "\n(Nepovolená je česká diakritika, mezera a znaky ( ) & , . ; = ^)")
    return None


@dataclass
class ProjectStatus:
    name: str
    users: list[LockInfo]     # kdo má projekt aktivovaný
    local_exists: bool
    mine: bool                # mám projekt aktivovaný já
    local_only: bool = False  # složka je jen v místním úložišti (lze ji publikovat)
    others: list[LockInfo] = field(default_factory=list)  # aktivní uživatelé kromě mě
    comment: ProjectComment | None = None  # komentář projektu (jen projekty na serveru)
    notes_for_me: int = 0         # otevřené poznámky adresované mně
    script_pending: bool = False  # skript po mém odevzdání selhal – lze ho spustit znovu

    @property
    def active(self) -> bool:
        return bool(self.users)


def project_rank(p: ProjectStatus) -> int:
    """Skupina projektu v seznamu (uvnitř skupiny abecedně):
    0 aktivované mnou, 1 s upozorněním pro mě (poznámka pro mě, neproběhlý skript),
    2 jen v místním úložišti (neodevzdané), 3 aktivované jinými, 4 ostatní."""
    if p.mine:
        return 0
    if p.notes_for_me or p.script_pending:
        return 1
    if p.local_only:
        return 2
    if p.active:
        return 3
    return 4


@dataclass
class FileEntry:
    """Soubor projektu pro výběr v dialogu (zamknutí při aktivaci / převzetí)."""
    path: str
    owner: LockOwner | None
    mine: bool
    local_new: bool = False      # nový soubor jen v mé místní kopii (ještě neodevzdaný)
    local_deleted: bool = False  # můj zamčený soubor, v místní kopii smazaný (do Koše), neodevzdaný
    in_local: bool = False       # soubor je v mé místní kopii (lze ho otevřít)
    renamed_from: str | None = None  # můj soubor přejmenovaný v místní kopii (původní cesta)
    mirror: str | None = None    # soubor spravuje zrcadlo z jiného serveru (cesta zdrojového projektu)
    mirror_server_id: str = ""   # identita zdrojové síťové složky (pro název záložky)
    mirror_server_name: str = ""
    mirror_targets: list = field(default_factory=list)  # kam se soubor zrcadlí (db.MirrorTarget)
    mirror_stale: dbm.MirrorStale | None = None  # zrcadlená kopie je neaktuální (zdroj se změnil)

    @property
    def free(self) -> bool:
        """Volný soubor na serveru (lze převzít)."""
        return self.owner is None and not self.local_new and self.mirror is None

    @property
    def holder(self) -> str:
        """Kdo soubor drží (zamkl) – pro sloupec „Drží“; zrcadlo se ukazuje ve sloupci „Zrcadlo“."""
        if self.mine:
            return "já"
        if self.owner is not None:
            return self.owner.label
        return ""


@dataclass
class UploadPlan:
    """Porovnání místní kopie se serverem – podklad pro odevzdání i deaktivaci."""
    name: str
    session_id: int
    local_files: dict[str, FileInfo]
    unchanged: list[str]
    modified: list[tuple[DbFile, FileInfo]]    # vlastní (zamčené) soubory se změnou
    missing: list[DbFile]                      # vlastní soubory, které v místní kopii chybí
    new: list[FileInfo]                        # nové soubory (nejsou v evidenci)
    locked_unchanged: list[DbFile]             # vlastní soubory beze změny (lze je jen uvolnit)
    suggestions: list[RenameSuggestion]
    conflicts: list[str] = field(default_factory=list)
    invalid_names: list[str] = field(default_factory=list)
    # místní práce, kterou nelze nahrát (změněné cizí soubory, nové soubory s obsazeným
    # názvem) – po smazání místní kopie by se ztratila
    unsavable: list[str] = field(default_factory=list)
    # moje zamčené soubory, které někdo smazal přímo na serveru (mimo aplikaci) – při odevzdání
    # se nahrají zpět z místní kopie
    gone_on_server: set[str] = field(default_factory=set)

    @property
    def held(self) -> list[str]:
        """file_id souborů ze serveru, které držím (zamčené mnou)."""
        return ([bf.file_id for bf, _ in self.modified] + [bf.file_id for bf in self.missing]
                + [bf.file_id for bf in self.locked_unchanged])

    def default_assignments(self) -> dict[str, str | None]:
        """file_id chybějícího souboru -> nová cesta (nebo None = smazán)."""
        result: dict[str, str | None] = {f.file_id: None for f in self.missing}
        for s in self.suggestions:
            result[s.file_id] = s.new_path
        return result

    @property
    def has_changes(self) -> bool:
        return bool(self.modified or self.missing or self.new)


def mirror_lock_error(paths: list[str], sources: list[str], labels: dict) -> str:
    """labels: zdroj -> MirrorSource (čitelný popis místo identifikátoru projektu)."""
    def label(source: str) -> str:
        info = labels.get(source)
        return info.label if info else source
    return ("Soubory spravuje zrcadlo z jiného serveru – nelze je zamknout ani upravit zde, "
            "změny se dělají ve zdroji:\n" + "\n".join(
                f"{p} – zrcadlo z {label(s)}" for p, s in list(zip(paths, sources))[:15]))


def new_key(path: str) -> str:
    """Klíč nového souboru ve výběru pro odevzdání (ostatní položky mají klíč = file_id)."""
    return "new:" + path


def selection_files(plan: UploadPlan, assignments: dict[str, str | None],
                    selected: set[str] | None) -> list[ScriptFile]:
    """Soubory, které se odevzdají (výběr v dialogu) – pro blok BEFORE a zkušební běh skriptu.
    Nové soubory ještě nemají file_id."""
    def sel(key: str) -> bool:
        return selected is None or key in selected
    new_by_path = {lf.path: lf for lf in plan.new}
    targets = {p for p in assignments.values() if p}
    files = [ScriptFile(lf.path, MODIFIED, bf.file_id, None, lf.sha256, lf.size)
             for bf, lf in plan.modified if sel(bf.file_id)]
    for bf in plan.missing:
        if not sel(bf.file_id):
            continue
        new = assignments.get(bf.file_id)
        if new and new in new_by_path:
            lf = new_by_path[new]
            files.append(ScriptFile(new, RENAMED, bf.file_id, bf.path, lf.sha256, lf.size))
        else:
            files.append(ScriptFile(bf.path, DELETED, bf.file_id, sha256=bf.sha256))
    files += [ScriptFile(lf.path, ADDED, "", None, lf.sha256, lf.size)
              for lf in plan.new if lf.path not in targets and sel(new_key(lf.path))]
    files += [ScriptFile(bf.path, UNCHANGED, bf.file_id, sha256=bf.sha256, size=bf.size)
              for bf in plan.locked_unchanged if sel(bf.file_id)]
    return sorted(files, key=lambda f: f.path.lower())


def has_script_changes(files: list[ScriptFile]) -> bool:
    """Skripty se spouštějí jen při odevzdání se změnami."""
    return any(f.status != UNCHANGED for f in files)


def auto_deactivation(plan: UploadPlan, assignments: dict[str, str | None],
                      selected: set[str] | None) -> tuple[bool, str]:
    """Deaktivuje se projekt po odevzdání? Vrací (ano/ne, důvod, proč ne – nebo "").

    Ano, pokud odevzdáním uvolním poslední soubor ze serveru, který držím.
    Ne, pokud jsem žádný soubor ze serveru nedržel (jen přidávám nové soubory – např.
    NC programy k cizím elektrodám; pracuji dál), nebo pokud by se ztratila neodevzdaná
    místní práce (deaktivace může smazat místní kopii)."""
    held = plan.held
    if not held or (selected is not None and any(f not in selected for f in held)):
        return False, ""
    targets = {p for p in assignments.values() if p}
    leftover = [lf.path for lf in plan.new if lf.path not in targets
                and selected is not None and new_key(lf.path) not in selected]
    if leftover:
        return False, (f"Odevzdáváte poslední držené soubory, ale v místní kopii zůstává "
                       f"{len(leftover)} neodevzdaných nových souborů – projekt zůstane aktivovaný.")
    if plan.unsavable:
        return False, ("Odevzdáváte poslední držené soubory, ale místní kopie obsahuje změny, které "
                       "nelze nahrát (viz Upozornění) – projekt zůstane aktivovaný.")
    return True, ""


@dataclass
class UploadResult:
    renamed: int = 0
    modified: int = 0
    added: int = 0
    deleted: int = 0
    released: int = 0
    local_deleted: bool = False
    auto_deactivated: bool = False   # odevzdán poslední držený soubor -> projekt deaktivován
    note: str = ""                   # proč se automaticky nedeaktivoval (je-li co říct)
    notes_published: int = 0         # zveřejněné rozepsané poznámky
    restored: list[str] = field(default_factory=list)       # smazané na serveru, nahrané zpět
    lost_versions: list[str] = field(default_factory=list)  # jejich poslední verze ze serveru se nezachovala
    commit_id: int | None = None
    # soubory odevzdání pro skripty; None = odevzdání bez změn (skripty se nespouštějí)
    script_files: list[ScriptFile] | None = None
    ended: bool = False              # aktivace skončila (deaktivace / automatická deaktivace)
    cleanup_pending: bool = False    # místní kopie se smaže až po skriptech (finish_local)


@dataclass
class CopyItem:
    """Jeden soubor kopírovaný do projektu (přetažením)."""
    rel: str                 # cílová cesta v projektu
    source: Path
    action: str              # ADD (nový) / REPLACE (nahradí můj soubor) / TAKE / SKIP
    reason: str = ""         # proč se přeskočí
    copy_rel: str = ""       # přeskočený: volný název kopie (soubor_copy.ext), lze vložit místo něj


# TAKE = existující volný soubor: převezme se (zamkne) a nahradí
COPY_ADD, COPY_REPLACE, COPY_TAKE, COPY_SKIP = "add", "replace", "take", "skip"


def as_copy(item: CopyItem) -> CopyItem:
    """Přeskočený soubor vložit pod názvem kopie (nový soubor)."""
    return CopyItem(item.copy_rel, item.source, COPY_ADD)

# dočasná kopie při nahrávání na server (vedle cíle; scanner ji ignoruje – končí TEMP_SUFFIX)
UPLOAD_SUFFIX = ".upload" + TEMP_SUFFIX


def changed_during_upload(rel: str) -> str:
    return (f"Soubor {rel} se změnil po porovnání se serverem (pravděpodobně je otevřený a "
            "ukládá se). Nic z něj se nenahrálo – uložte a zavřete ho a odevzdejte znovu.")


@dataclass
class ActivationResult:
    moved_to: Path | None = None    # kam se odložila původní místní složka
    external: list[str] = field(default_factory=list)  # změny na serveru mimo aplikaci (podle hashe)


@dataclass
class PublishResult:
    published: int = 0
    local_deleted: bool = False      # celá místní složka odstraněna (publikováno vše)
    leftover: Path | None = None     # kam se odložily nepublikované soubory
    commit_id: int | None = None
    script_files: list[ScriptFile] = field(default_factory=list)


@dataclass
class RefreshResult:
    downloaded: list[str] = field(default_factory=list)
    moved: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    external: list[str] = field(default_factory=list)  # změny na serveru mimo aplikaci (podle hashe)

    @property
    def total(self) -> int:
        return len(self.downloaded) + len(self.moved) + len(self.removed)


class ProjectManager:
    def __init__(self, settings: Settings, user: str | None = None, pc: str | None = None):
        self.settings = settings
        self.user = user or current_user()
        self.pc = pc or current_pc()

    # --- cesty -----------------------------------------------------------

    @property
    def server_root(self) -> Path:
        return Path(self.settings.server_root)

    @property
    def local_root(self) -> Path:
        return Path(self.settings.local_root)

    def server_dir(self, name: str) -> Path:
        return self.server_root / name

    def local_dir(self, name: str) -> Path:
        return self.local_root / name

    # --- přehled -----------------------------------------------------------

    def list_projects(self) -> list[ProjectStatus]:
        """Projekty na serveru a složky jen v místním úložišti.

        Rychlé i při stovkách projektů na síti: stav projektů se zjišťuje souběžně a u každého
        stačí výpis jeho složky .spm – soubory (značky aktivace, komentář, souhrn poznámek)
        se čtou jen tehdy, když se od minulého obnovení změnily (viz _cached)."""
        try:
            with os.scandir(self.server_root) as it:  # typ položky bez dalšího dotazu na síť
                names = [e.name for e in it if not e.name.startswith(".") and e.is_dir()]
        except OSError:
            raise SyncError(f"Síťová složka není dostupná:\n{self.server_root}") from None
        server_names = {n.lower() for n in names}
        if names:
            with ThreadPoolExecutor(max_workers=min(STATUS_WORKERS, len(names))) as pool:
                result = list(pool.map(self._server_status, names))
        else:
            result = []
        for name in self.local_only_projects(server_names):
            result.append(ProjectStatus(name=name, users=[], local_exists=True, mine=False,
                                        local_only=True))
        result.sort(key=lambda p: (project_rank(p), p.name.lower()))
        return result

    def _server_status(self, name: str) -> ProjectStatus:
        server = self.server_dir(name)
        # jeden výpis .spm: které soubory existují a kdy se změnily (DirEntry.stat() ve Windows
        # nepotřebuje další dotaz na síť)
        entries: dict[str, os.DirEntry] = {}
        try:
            with os.scandir(server / SPM_DIR) as it:
                entries = {e.name: e for e in it}
        except OSError:
            pass  # projekt bez evidence
        users: list[LockInfo] = []
        if "active" in entries:
            try:
                with os.scandir(entries["active"].path) as it:
                    markers = sorted((e for e in it if e.name.endswith(".json")), key=lambda e: e.name)
                    users = [u for u in (self._cached(e, read_marker) for e in markers) if u]
            except OSError:
                pass
        comment = self._cached(entries.get(COMMENT_FILE), lambda _path: read_comment(server))
        summary = self._cached(entries.get(notes_mod.SUMMARY_FILE),
                               lambda _path: notes_mod.read_summary(server)) or notes_mod.Summary()
        others = [u for u in users if not u.is_mine(self.user, self.pc)]
        return ProjectStatus(name=name, users=users, local_exists=self.local_dir(name).exists(),
                             mine=len(others) < len(users), others=others,
                             comment=comment, notes_for_me=summary.for_user(self.user),
                             script_pending=script_store.pending_path(self.local_root, name).exists())

    def _cached(self, entry: os.DirEntry | None, read):
        """Obsah malého souboru ze serveru; znovu se čte jen při změně (čas a velikost)."""
        if entry is None:
            return None
        try:
            st = entry.stat()
        except OSError:
            return None
        stamp = (st.st_mtime_ns, st.st_size)
        cache = self.__dict__.setdefault("_status_cache", {})
        hit = cache.get(entry.path)
        if hit is not None and hit[0] == stamp:
            return hit[1]
        value = read(Path(entry.path))
        cache[entry.path] = (stamp, value)
        return value

    # --- poznámky ---------------------------------------------------------------

    @property
    def display_name(self) -> str:
        """Jak uživatele vidí ostatní: přezdívka z nastavení, jinak jméno z Windows, jinak login."""
        if self.settings.nickname.strip():
            return self.settings.nickname.strip()
        if not hasattr(self, "_full_name"):
            self._full_name = current_full_name()
        return self._full_name or self.user

    def register_user(self) -> None:
        """Zapíše uživatele do adresáře síťové složky (výběr adresáta poznámek)."""
        if not hasattr(self, "_full_name"):
            self._full_name = current_full_name()
        users.register(self.server_root, users.UserInfo(self.user, self.settings.nickname.strip(),
                                                        self._full_name, self.pc))

    def users(self) -> list[users.UserInfo]:
        return users.list_users(self.server_root)

    def notes(self, name: str) -> list[Note]:
        return notes_mod.read_notes(self.server_dir(name))

    def _ensure_notes_tables(self, server: Path) -> None:
        with WriteLock(server, self.user, self.pc):
            with ProjectDB.for_project(server, create=True) as db:
                notes_mod.ensure_tables(db)

    def add_notes(self, name: str, drafts: list[Draft], commit_id: int | None = None) -> int:
        """Zveřejní poznámky hned (bez odevzdání – např. u projektu, který nemám aktivovaný)."""
        drafts = [d for d in drafts if d.text.strip()]
        server = self.server_dir(name)
        if not drafts:
            return 0
        if not server.is_dir():
            raise SyncError(f"Projekt neexistuje: {server}")
        self._ensure_notes_tables(server)
        with self._write(server, create=True) as db:
            notes_mod.insert_notes(db, drafts, self.user, self.pc, self.display_name, commit_id)
            notes_mod.write_summary(server, db)
        return len(drafts)

    def resolve_note(self, name: str, note_id: int, resolved: bool = True) -> None:
        server = self.server_dir(name)
        self._ensure_notes_tables(server)
        with self._write(server) as db:
            notes_mod.set_resolved(db, note_id, resolved, self.display_name)
            notes_mod.write_summary(server, db)

    def edit_note(self, name: str, note_id: int, text: str, recipient: str | None = None,
                  recipient_name: str | None = None) -> bool:
        """Úprava zveřejněné poznámky (nebo doplnění) – jen jejím autorem; předchozí znění se
        uloží. Upravená poznámka se ostatním znovu označí jako nová."""
        server = self.server_dir(name)
        self._ensure_notes_tables(server)
        try:
            with self._write(server) as db:
                changed = notes_mod.edit_note(db, note_id, self.user, text, recipient, recipient_name)
                notes_mod.write_summary(server, db)
        except notes_mod.NoteError as e:
            raise SyncError(str(e)) from None
        return changed

    def reply_note(self, name: str, parent_id: int, text: str) -> int:
        """Doplnění (odpověď) k poznámce – může kdokoliv; původní text se nemění."""
        if not text.strip():
            raise SyncError("Doplnění nesmí být prázdné.")
        server = self.server_dir(name)
        self._ensure_notes_tables(server)
        with self._write(server) as db:
            parent = db.conn.execute("SELECT parent_id FROM notes WHERE note_id = ?", (parent_id,)).fetchone()
            if parent is None:
                raise SyncError("Poznámka už neexistuje.")
            parent_id = parent[0] or parent_id  # doplnění k doplnění patří k původní poznámce
            note_id = notes_mod.insert_notes(db, [Draft(text)], self.user, self.pc, self.display_name,
                                             None, parent_id=parent_id)[0]
            notes_mod.write_summary(server, db)
        return note_id

    def drafts(self, name: str) -> list[Draft]:
        """Rozepsané poznámky v mé místní kopii projektu."""
        local = self.local_dir(name)
        return notes_mod.load_drafts(local) if local.is_dir() else []

    def save_drafts(self, name: str, drafts: list[Draft]) -> None:
        local = self.local_dir(name)
        if local.is_dir():
            notes_mod.save_drafts(local, drafts)

    def set_comment(self, name: str, text: str) -> bool:
        """Změní komentář projektu (prázdný text = smazat) a zapíše změnu do historie.
        Lze kdykoliv, i když má projekt aktivovaný někdo jiný. Vrací False, pokud se nic nezměnilo."""
        text = normalize_text(text)
        if len(text) > COMMENT_MAX_LENGTH:
            raise SyncError(f"Komentář je příliš dlouhý ({len(text)} znaků, nejvýše {COMMENT_MAX_LENGTH}).")
        server = self.server_dir(name)
        if not server.is_dir():
            raise SyncError(f"Projekt neexistuje: {server}")
        with WriteLock(server, self.user, self.pc):
            current = read_comment(server)
            if text == (current.text if current else ""):
                return False
            # nejdřív historie – když se zápis do databáze nepovede, komentář se nezmění
            with ProjectDB.for_project(server, create=True) as db:
                with db.conn:
                    db.add_commit(None, self.user, self.pc, dbm.PROJECT_COMMENT, text)
            write_comment(server, ProjectComment(text, self.user, self.pc, now_iso()))
        return True

    def project_status(self, name: str) -> ProjectStatus | None:
        """Aktuální stav jednoho projektu (levné – jen výpis .spm/active), None = už neexistuje."""
        if self.server_dir(name).is_dir():
            return self._server_status(name)
        if self.local_dir(name).is_dir():
            return ProjectStatus(name=name, users=[], local_exists=True, mine=False, local_only=True)
        return None

    def restore_points(self, name: str, progress: Progress | None = None) -> RestoreCatalog:
        """Ke kterým odevzdáním lze projekt obnovit ze záloh (viz restore.py)."""
        return restore.restore_points(self.server_dir(name), name, progress)

    def restore(self, catalog: RestoreCatalog, point: RestorePoint,
                progress: Progress | None = None) -> RestoreResult:
        """Obnoví projekt ke stavu `point` do nové místní složky <projekt>_obnova_<datum>."""
        self.local_root.mkdir(parents=True, exist_ok=True)
        return restore.restore(self.server_dir(catalog.name), self.local_root, catalog, point, progress)

    def file_versions(self, name: str, path: str) -> list[restore.FileVersion]:
        """Historie jednoho souboru projektu (verze a jejich dostupnost) – viz restore.py."""
        server = self.server_dir(name)
        if not db_path(server).exists():
            return []
        with ProjectDB.for_project(server) as db:
            f = db.active_files(include_pending=True).get(path)
        if f is None:
            raise SyncError(f"Soubor není v evidenci projektu:\n{path}")
        keep = self.settings.backup_count if self.settings.backup_enabled else None
        return restore.file_versions(server, f.file_id, keep)

    def restore_file_version(self, name: str, version: restore.FileVersion, dest: Path) -> bool:
        """Uloží verzi souboru do dest (ověřeno hashem)."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        return restore.restore_file_version(self.server_dir(name), version, dest)

    def create_local_project(self, name: str) -> Path:
        """Nový projekt jen v místním úložišti – prázdná složka (jako ručně vytvořená);
        na síť se dostane publikováním. Vrací cestu ke složce."""
        name = name.strip()
        error = validate_project_name(name)
        if error:
            raise SyncError(error)
        lower = name.lower()
        if self.local_root.is_dir() and any(e.name.lower() == lower for e in self.local_root.iterdir()):
            raise SyncError(f"V místním úložišti už složka {name} existuje:\n{self.local_root}")
        try:
            on_server = self.server_root.is_dir() and any(
                e.name.lower() == lower for e in self.server_root.iterdir())
        except OSError:
            on_server = False  # síť nedostupná – shoda se ověří při publikování
        if on_server:
            raise SyncError(f"Na serveru už projekt {name} existuje – aktivujte ho, místo "
                            "vytváření nového.")
        path = self.local_dir(name)
        path.mkdir(parents=True)
        return path

    def rename_local_project(self, name: str, new_name: str) -> Path:
        """Přejmenuje složku projektu, který je jen v místním úložišti (ještě neodevzdaný).
        Vrací novou cestu ke složce."""
        new_name = new_name.strip()
        src = self.local_dir(name)
        if new_name == name:
            return src
        error = validate_project_name(new_name)
        if error:
            raise SyncError(error)
        if not src.is_dir():
            raise SyncError(f"Složka projektu neexistuje: {src}")
        lower = new_name.lower()
        case_only = lower == name.lower()
        try:
            on_server = self.server_root.is_dir() and any(
                e.name.lower() == lower for e in self.server_root.iterdir())
        except OSError:
            on_server = False  # síť nedostupná – shoda se ověří při publikování
        if on_server:
            raise SyncError(f"Na serveru už projekt {new_name} existuje – zvolte jiný název.")
        if not case_only and any(e.name.lower() == lower for e in self.local_root.iterdir()):
            raise SyncError(f"V místním úložišti už složka {new_name} existuje:\n{self.local_root}")
        dst = self.local_dir(new_name)
        try:
            src.rename(dst)
        except PermissionError:
            raise SyncError(
                "Složku projektu nelze přejmenovat – některý soubor nebo složka projektu je "
                "otevřená (např. v Průzkumníku nebo jiné aplikaci)."
            ) from None
        return dst

    def local_only_projects(self, server_names: set[str]) -> list[str]:
        """Složky v místním úložišti, které na serveru nemají projekt (porovnání bez ohledu
        na velikost písmen, jako ve Windows)."""
        if not self.local_root.is_dir():
            return []
        return [
            e.name for e in self.local_root.iterdir()
            if e.is_dir() and not e.name.startswith(".")
            and e.name.lower() not in server_names
        ]

    def file_entries(self, name: str) -> list[FileEntry]:
        """Soubory projektu na serveru a kdo je drží – pro výběr souborů k zamknutí."""
        server = self.server_dir(name)
        paths = [to_rel(server, p) for p in list_files(server, self.settings.ignore_patterns)]
        files: dict[str, DbFile] = {}
        owners: dict[str, LockOwner] = {}
        mirrored: dict[str, tuple[str, str]] = {}
        sources: dict = {}
        stale: dict = {}
        targets: dict = {}
        if db_path(server).exists():
            with ProjectDB.for_project(server) as db:
                files = db.active_files(include_pending=True)
                owners = db.lock_owners()
                mirrored = db.mirrored()
                sources = db.mirror_sources() if mirrored else {}
                stale = db.mirror_stale() if mirrored else {}
                targets = db.mirror_targets()
        result = []
        for path in paths:
            f = files.get(path)
            owner = owners.get(f.file_id) if f else None
            entry = FileEntry(path, owner, bool(owner and owner.is_me(self.user, self.pc)))
            if f is not None and f.file_id in mirrored:
                src = sources.get(mirrored[f.file_id][0])
                entry.mirror = src.label if src else mirrored[f.file_id][0]
                entry.mirror_server_id = src.server_id if src else ""
                entry.mirror_server_name = src.server_name if src else ""
                entry.mirror_stale = stale.get(f.file_id)
            if f is not None:
                entry.mirror_targets = targets.get(f.file_id, [])
            result.append(entry)
        return result

    def local_new_files(self, name: str) -> list[str]:
        """Nové soubory v mé místní kopii, které ještě nejsou na serveru (neodevzdané).
        Jen výpis místní složky – bez čtení obsahu a bez přístupu k serveru."""
        local = self.local_dir(name)
        manifest = Manifest.load(local) if local.is_dir() else None
        if manifest is None:
            return []
        known = {p.lower() for p in manifest.files}
        return [rel for rel in (to_rel(local, f) for f in list_files(local, self.settings.ignore_patterns))
                if rel.lower() not in known]

    def delete_local(self, name: str, paths: Iterable[str]) -> list[str]:
        """Přesune soubory z mé místní kopie do Koše Windows (ne trvalé smazání).

        Smí se jen moje soubory: zamčené mnou, nebo nové neodevzdané. Na serveru se smazání
        projeví až při odevzdání (a i tam se soubor jen přesune do koše projektu).
        Vrací smazané cesty."""
        server = self.server_dir(name)
        local, manifest = self._manifest(name)
        with ProjectDB.for_project(server) as db:
            mine = db.locked_by(self._my_session(db, manifest.session_id))
        paths = [p for p in dict.fromkeys(paths)]
        not_allowed = [p for p in paths if p in manifest.files and manifest.files[p].file_id not in mine]
        if not_allowed:
            raise SyncError("Tyto soubory nemáte zamčené, nelze je smazat:\n" + "\n".join(not_allowed[:15]))
        existing = [p for p in paths if to_abs(local, p).is_file()]
        recycle.to_recycle_bin([to_abs(local, p) for p in existing])
        for p in existing:
            remove_empty_parents(local, p)
        return existing

    def rename_local(self, name: str, path: str, new_name: str) -> str:
        """Přejmenuje soubor v mé místní kopii (ve stejné složce). Vrací novou cestu.

        Smí se jen moje soubory: zamčené mnou, nové neodevzdané, nebo celá složka „Jen místně“.
        Na serveru se přejmenování projeví až při odevzdání (rozpozná se podle obsahu)."""
        error = validate_file_name(new_name, self.settings.check_forbidden_chars)
        if error:
            raise SyncError(error)
        local = self.local_dir(name)
        folder, _, old_name = path.rpartition("/")
        new_path = f"{folder}/{new_name}" if folder else new_name
        if new_path == path:
            return path
        src, dst = to_abs(local, path), to_abs(local, new_path)
        if not src.is_file():
            raise SyncError(f"Soubor v místní kopii neexistuje:\n{src}")
        case_only = new_path.lower() == path.lower()
        if dst.exists() and not case_only:
            raise SyncError(f"Soubor s tímto názvem už ve složce je:\n{new_path}")

        server = self.server_dir(name)
        if server.exists():  # projekt na serveru – musí to být můj soubor a název nesmí být obsazený
            _, manifest = self._manifest(name)
            with ProjectDB.for_project(server) as db:
                mine = db.locked_by(self._my_session(db, manifest.session_id))
                taken = {p.lower(): f for p, f in db.active_files(include_pending=True).items()}
            entry = manifest.files.get(path)
            if entry is not None and entry.file_id not in mine:
                raise SyncError("Soubor nemáte zamčený – přejmenovat ho nelze. Nejdřív ho převezměte.")
            other = taken.get(new_path.lower())
            if other is not None and not case_only and (entry is None or other.file_id != entry.file_id):
                raise SyncError(f"Na serveru už je soubor s tímto názvem:\n{new_path}")
        os.rename(src, dst)
        return new_path

    def create_local_folder(self, name: str, parent: str, new_name: str) -> str:
        """Vytvoří složku v mé místní kopii (parent = '' nebo 'a/b/'). Vrací její cestu.
        Na server se dostane až se souborem, který v ní bude při odevzdání."""
        error = validate_folder_name(new_name, self.settings.check_forbidden_chars)
        if error:
            raise SyncError(error)
        parent = parent.strip("/")
        rel = f"{parent}/{new_name}" if parent else new_name
        if rel.split("/")[0].lower() == SPM_DIR:
            raise SyncError("Do servisní složky .spm nelze nic přidávat.")
        local = self.local_dir(name)
        path = to_abs(local, rel)
        if path.exists():
            raise SyncError(f"Složka nebo soubor s tímto názvem už existuje:\n{rel}")
        server = self.server_dir(name)
        if server.exists():  # projekt na serveru – musí být aktivovaný mnou
            _, manifest = self._manifest(name)
            with ProjectDB.for_project(server) as db:
                self._my_session(db, manifest.session_id)
                files = db.active_files(include_pending=True)
            prefix = rel.lower() + "/"
            if any(p.lower().startswith(prefix) or p.lower() == rel.lower() for p in files):
                raise SyncError(f"Na serveru už je složka nebo soubor s tímto názvem:\n{rel}")
        path.mkdir(parents=True)
        return rel

    def local_empty_folders(self, name: str) -> list[str]:
        """Prázdné složky v místní kopii (bez souborů a bez podsložek; ignorované soubory se
        nepočítají) – strom je ukáže, i když v nich zatím nic není."""
        local = self.local_dir(name)
        if not local.is_dir():
            return []
        result: list[str] = []
        ignore = self.settings.ignore_patterns
        for dirpath, dirnames, filenames in os.walk(local):
            rel_dir = Path(dirpath).relative_to(local).as_posix()
            if rel_dir == ".":
                dirnames[:] = [d for d in dirnames if d.lower() != SPM_DIR and not is_ignored(d, ignore)]
                continue
            dirnames[:] = [d for d in dirnames if not is_ignored(d, ignore)]
            if not dirnames and not any(not is_ignored(f, ignore) for f in filenames):
                result.append(rel_dir)
        return sorted(result, key=str.lower)

    def remove_empty_local_folder(self, name: str, folder: str) -> None:
        """Smaže prázdnou složku z místní kopie (i prázdné podsložky; soubor v ní = chyba)."""
        path = to_abs(self.local_dir(name), folder.strip("/"))
        if not path.is_dir():
            return
        if any(p.is_file() for p in path.rglob("*")):
            raise SyncError(f"Složka není prázdná:\n{folder}")
        for sub in sorted((p for p in path.rglob("*") if p.is_dir()), key=lambda p: len(p.parts),
                          reverse=True):
            sub.rmdir()
        path.rmdir()

    def rename_local_folder(self, name: str, folder: str, new_name: str) -> str:
        """Přejmenuje složku v mé místní kopii (ve stejné nadřazené složce). Vrací novou cestu.

        Jen když mám aktivované (zamčené) všechny soubory ve složce i ve všech podsložkách
        (nebo jde o mé nové soubory, případně o celou složku „Jen místně“). Na serveru se
        přejmenování projeví až při odevzdání – soubory se rozpoznají jako přejmenované podle
        obsahu (zachová se historie i zálohy)."""
        error = validate_folder_name(new_name, self.settings.check_forbidden_chars)
        if error:
            raise SyncError(error)
        folder = folder.strip("/")
        parent, _, _old = folder.rpartition("/")
        new_folder = f"{parent}/{new_name}" if parent else new_name
        if new_folder == folder:
            return folder
        local = self.local_dir(name)
        src, dst = to_abs(local, folder), to_abs(local, new_folder)
        if not src.is_dir():
            raise SyncError(f"Složka v místní kopii neexistuje:\n{src}")
        case_only = new_folder.lower() == folder.lower()
        if dst.exists() and not case_only:
            raise SyncError(f"Složka s tímto názvem už existuje:\n{new_folder}")

        server = self.server_dir(name)
        if server.exists():
            _, manifest = self._manifest(name)
            with ProjectDB.for_project(server) as db:
                mine = db.locked_by(self._my_session(db, manifest.session_id))
                files = db.active_files(include_pending=True)
            prefix = folder.lower() + "/"
            foreign = sorted(p for p, f in files.items()
                             if p.lower().startswith(prefix) and f.file_id not in mine)
            if foreign:
                raise SyncError("Složku lze přejmenovat, jen když máte aktivované všechny soubory "
                                "v ní i ve všech podsložkách. Neaktivované:\n"
                                + "\n".join(foreign[:15]) + ("\n…" if len(foreign) > 15 else ""))
            new_prefix = new_folder.lower() + "/"
            if not case_only and any(p.lower().startswith(new_prefix) for p in files):
                raise SyncError(f"Na serveru už je složka s tímto názvem:\n{new_folder}")
        os.rename(src, dst)
        return new_folder

    # --- kopírování přetažením ---------------------------------------------------

    def plan_copy_in(self, name: str, sources: list[tuple[Path, str]]) -> list[CopyItem]:
        """Co se stane při kopírování souborů do místní kopie projektu (přetažení do stromu).
        sources: (zdrojový soubor, cílová cesta v projektu). Nový soubor se přidá, existující
        se nahradí jen tehdy, když ho mám aktivovaný (nebo je to můj nový soubor); ostatní se
        přeskočí s důvodem. U složky „Jen místně“ lze vše."""
        local = self.local_dir(name)
        server = self.server_dir(name)
        on_server = server.exists()
        files: dict[str, DbFile] = {}
        mine: set[str] = set()
        owners: dict = {}
        mirrored: dict = {}
        if on_server:
            _, manifest = self._manifest(name)
            with ProjectDB.for_project(server) as db:
                mine = db.locked_by(self._my_session(db, manifest.session_id))
                files = {p.lower(): f for p, f in db.active_files(include_pending=True).items()}
                owners = db.lock_owners()
                mirrored = db.mirrored()
        items: list[CopyItem] = []
        seen: set[str] = set()
        for src, rel in sources:
            rel = rel.strip("/")
            parts = rel.split("/")

            def skip(reason: str) -> None:
                items.append(CopyItem(rel, src, COPY_SKIP, reason))
            if not rel or parts[0].lower() == SPM_DIR:
                skip("servisní složka")
                continue
            if rel.lower() in seen:
                skip("stejný název je mezi kopírovanými soubory víckrát")
                continue
            seen.add(rel.lower())
            if any(is_ignored(part, self.settings.ignore_patterns) for part in parts):
                skip("ignorovaný soubor")
                continue
            if self._invalid_paths([rel]):
                skip("nepovolené znaky v názvu")
                continue
            dst = to_abs(local, rel)
            if dst.exists() and same_path(dst, src):
                # vložení na stejné místo (Ctrl+C, Ctrl+V) = duplikát s _copy v názvu
                folder, _, base = rel.rpartition("/")

                def taken(candidate: str) -> bool:
                    path = f"{folder}/{candidate}" if folder else candidate
                    return (path.lower() in seen or path.lower() in files
                            or to_abs(local, path).exists())
                base = free_copy_name(base, taken)
                rel = f"{folder}/{base}" if folder else base
                seen.add(rel.lower())
                items.append(CopyItem(rel, src, COPY_ADD))
                continue
            f = files.get(rel.lower())
            if f is None:
                if dst.is_dir():
                    skip("v místní kopii je složka se stejným názvem")
                else:
                    items.append(CopyItem(rel, src, COPY_REPLACE if dst.exists() else COPY_ADD))
                continue
            rel = f.path  # existující soubor – jeho cesta v projektu (velikost písmen)
            if f.file_id in mine and f.file_id not in mirrored:
                items.append(CopyItem(rel, src, COPY_REPLACE))
            elif f.file_id in mirrored or f.file_id in owners:
                # nahradit nelze – nabídne se vložení pod názvem kopie
                folder, _, base = rel.rpartition("/")

                def taken(candidate: str) -> bool:
                    path = f"{folder}/{candidate}" if folder else candidate
                    return (path.lower() in seen or path.lower() in files
                            or to_abs(local, path).exists())
                copy_name = free_copy_name(base, taken)
                copy_rel = f"{folder}/{copy_name}" if folder else copy_name
                seen.add(copy_rel.lower())
                reason = ("spravuje ho zrcadlo" if f.file_id in mirrored
                          else f"drží ho {owners[f.file_id].label}")
                items.append(CopyItem(rel, src, COPY_SKIP, reason, copy_rel))
            else:
                items.append(CopyItem(rel, src, COPY_TAKE))  # volný – převezme se a nahradí
        return items

    def copy_in(self, name: str, items: list[CopyItem], progress: Progress | None = None) -> list[str]:
        """Provede kopírování naplánované plan_copy_in (jen do místní kopie – na server až
        odevzdáním). Volné soubory k nahrazení (TAKE) se nejdřív převezmou – zamknou se a stáhne
        se jejich aktuální verze. Vrací cesty zkopírovaných souborů; chyba = SyncError."""
        local = self.local_dir(name)
        takes = [i.rel for i in items if i.action == COPY_TAKE]
        if takes:
            self.take(name, takes)  # mezitím je zamkl někdo jiný -> SyncError, nic se nekopíruje
        todo = [i for i in items if i.action != COPY_SKIP]
        done: list[str] = []
        for n, item in enumerate(todo):
            if progress:
                progress(n, len(todo), item.rel)
            try:
                copy_with_hash(item.source, to_abs(local, item.rel), writable=True)
            except OSError as e:
                raise SyncError(f"Soubor {item.rel} se nepodařilo zkopírovat:\n{e}"
                                + (f"\n\nZkopírováno předtím: {len(done)}" if done else "")) from None
            done.append(item.rel)
        return done

    def move_local(self, name: str, moves: list[tuple[str, str]]) -> tuple[list[str], list[tuple[str, str]]]:
        """Přesune soubory v mé místní kopii (Vyjmout + Vložit): moves = (cesta, nová cesta).
        Jen moje soubory – zamčené mnou nebo nové (u složky „Jen místně“ všechny). Na serveru
        se přesun projeví až odevzdáním (rozpozná se podle obsahu jako přejmenování).
        Existující cíl se nepřepisuje. Vrací (přesunuté nové cesty, přeskočené (cesta, důvod))."""
        local = self.local_dir(name)
        server = self.server_dir(name)
        manifest = None
        mine: set[str] = set()
        taken: dict = {}
        if server.exists():
            _, manifest = self._manifest(name)
            with ProjectDB.for_project(server) as db:
                mine = db.locked_by(self._my_session(db, manifest.session_id))
                taken = {p.lower(): f for p, f in db.active_files(include_pending=True).items()}
        moved: list[str] = []
        skipped: list[tuple[str, str]] = []
        for rel, new_rel in moves:
            new_rel = new_rel.strip("/")
            src, dst = to_abs(local, rel), to_abs(local, new_rel)
            entry = manifest.files.get(rel) if manifest else None
            if new_rel == rel:
                continue
            if not src.is_file():
                skipped.append((rel, "v místní kopii není"))
            elif entry is not None and entry.file_id not in mine:
                skipped.append((rel, "nemáte ho zamčený"))
            elif new_rel.split("/")[0].lower() == SPM_DIR:
                skipped.append((rel, "servisní složka"))
            elif any(is_ignored(part, self.settings.ignore_patterns) for part in new_rel.split("/")):
                skipped.append((rel, "ignorovaný název"))
            elif self._invalid_paths([new_rel]):
                skipped.append((rel, "nepovolené znaky v cílové cestě"))
            elif dst.exists() and not same_path(src, dst):
                skipped.append((rel, f"v cíli už je {new_rel}"))
            elif (other := taken.get(new_rel.lower())) is not None and (
                    entry is None or other.file_id != entry.file_id):
                skipped.append((rel, f"na serveru už je {new_rel}"))
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.rename(src, dst)
                except OSError as e:
                    skipped.append((rel, f"nelze přesunout ({e.strerror or e})"))
                    continue
                remove_empty_parents(local, rel)
                moved.append(new_rel)
        return moved, skipped

    def file_source(self, name: str, rel: str) -> Path:
        """Odkud soubor projektu číst: místní kopie, jinak server (nestažený soubor)."""
        local = to_abs(self.local_dir(name), rel)
        return local if local.is_file() else to_abs(self.server_dir(name), rel)

    def export_copies(self, name: str, items: list[tuple[str, str]], dest: Path,
                      progress: Progress | None = None) -> list[Path]:
        """Kopie souborů projektu pro přetažení ven (např. na plochu): items = (cesta v projektu,
        cesta v cíli). Kopie jsou vždy zapisovatelné (bez „jen pro čtení“) a nestažené soubory
        se vezmou ze serveru. Vrací položky nejvyšší úrovně v dest (soubory a složky)."""
        tops: dict[str, Path] = {}
        for n, (rel, out) in enumerate(items):
            if progress:
                progress(n, len(items), rel)
            target = to_abs(dest, out)
            copy_with_hash(self.file_source(name, rel), target, writable=True)
            top = out.split("/")[0]
            tops.setdefault(top.lower(), dest / top)
        return list(tops.values())

    def local_renames(self, name: str) -> dict[str, str]:
        """Moje zamčené soubory přejmenované v místní kopii, ještě neodevzdané: stará cesta ->
        nová. Pozná se jako při odevzdání – chybějící soubor a nový soubor se stejným obsahem."""
        local = self.local_dir(name)
        manifest = Manifest.load(local) if local.is_dir() else None
        if manifest is None:
            return {}
        missing = {manifest.files[p].sha256: p for p in self.local_deleted_files(name)}
        result: dict[str, str] = {}
        for new in self.local_new_files(name):
            if not missing:
                break
            try:
                sha = file_hash(to_abs(local, new))
            except OSError:
                continue
            old = missing.pop(sha, None)
            if old is not None:
                result[old] = new
        return result

    def local_deleted_files(self, name: str) -> list[str]:
        """Moje zamčené soubory, které v místní kopii chybí (smazané, ještě neodevzdané)."""
        local = self.local_dir(name)
        manifest = Manifest.load(local) if local.is_dir() else None
        if manifest is None:
            return []
        with ProjectDB.for_project(self.server_dir(name)) as db:
            mine = db.locked_by(manifest.session_id)
        return [p for p, e in manifest.files.items() if e.file_id in mine and not to_abs(local, p).exists()]

    # --- kontrola názvů -------------------------------------------------------

    # Kontrolu lze v nastavení vypnout (check_forbidden_chars); název projektu se kontroluje vždy.

    def _find_invalid(self, root: Path) -> list[str]:
        if not self.settings.check_forbidden_chars:
            return []
        return find_invalid_names(root, self.settings.ignore_patterns)

    def _invalid_paths(self, paths: list[str]) -> list[str]:
        return invalid_rel_paths(paths) if self.settings.check_forbidden_chars else []

    def invalid_file_paths(self, paths: list[str]) -> list[str]:
        """Z daných souborů ty, které mají v názvu (nebo v cestě) nepovolený znak."""
        if not self.settings.check_forbidden_chars:
            return []
        bad_dirs = [p for p in invalid_rel_paths(paths) if p.endswith("/")]
        bad_files = set(invalid_rel_paths(paths))
        return [p for p in paths if p in bad_files or any(p.startswith(d) for d in bad_dirs)]

    def invalid_names(self, project: ProjectStatus) -> list[str]:
        """Názvy s nepovolenými znaky uvnitř projektu (u „Jen místně“ v místní složce)."""
        root = self.local_dir(project.name) if project.local_only else self.server_dir(project.name)
        try:
            return self._find_invalid(root)
        except OSError:
            return []

    def check_names(self, root: Path, name: str) -> None:
        invalid = self._find_invalid(root)
        if invalid:
            raise SyncError(
                f"Projekt {name} obsahuje složky nebo soubory s nepovolenými znaky "
                "(česká diakritika, mezera, ( ) & , . ; = ^). Nejdřív je přejmenujte:\n\n"
                + format_invalid(invalid)
            )

    # --- pomocné: databáze a sezení -------------------------------------------

    @contextmanager
    def _write(self, server: Path, create: bool = False) -> Iterator[ProjectDB]:
        """Zápis do databáze projektu: zámek zápisu + jedna transakce."""
        with WriteLock(server, self.user, self.pc):
            with ProjectDB.for_project(server, create=create) as db:
                with db.conn:
                    yield db

    def _my_session(self, db: ProjectDB, expected: int | None = None) -> int:
        sid = db.active_session(self.user, self.pc)
        if sid is None or (expected is not None and sid != expected):
            raise SyncError("Projekt u vás není aktivován (aktivace byla zrušena nebo vám "
                            "byly zámky odebrány).")
        return sid

    def _manifest(self, name: str) -> tuple[Path, Manifest]:
        local = self.local_dir(name)
        manifest = Manifest.load(local) if local.is_dir() else None
        if manifest is None:
            raise SyncError(f"Místní kopie projektu neexistuje nebo je poškozená:\n{local}")
        return local, manifest

    # --- aktivace ----------------------------------------------------------

    def activate(self, name: str, lock_paths: Iterable[str] | None = None,
                 progress: Progress | None = None, only_locked: bool = False) -> ActivationResult:
        """Stáhne projekt do místního úložiště a zamkne vybrané soubory.

        lock_paths:  soubory, které chce uživatel upravovat (None = všechny volné).
                     Ostatní soubory budou v místní kopii jen pro čtení.
        only_locked: „Nestahovat neaktivované soubory“ – stáhnou se jen zamčené soubory
                     (rychlejší u velkých projektů; místní kopie pak obsahuje jen část projektu).
        Vrací, kam byla přesunuta původní místní složka (pokud existovala), a změny na serveru
        provedené mimo aplikaci, zjištěné podle hashe při kopírování.
        """
        server = self.server_dir(name)
        local = self.local_dir(name)
        if not server.is_dir():
            raise SyncError(f"Projekt neexistuje: {server}")
        if (server / SPM_DIR / "archiving.json").exists():
            raise SyncError(f"Projekt {name} se právě archivuje – aktivovat ho nelze.")
        self.check_names(server, name)
        if db_path(server).exists():
            with ProjectDB.for_project(server) as db:
                if db.active_session(self.user, self.pc):
                    raise SyncError(f"Projekt {name} už máte aktivovaný.")
        self.local_root.mkdir(parents=True, exist_ok=True)

        moved_to: Path | None = None
        sid: int | None = None
        try:
            # 1) Nejdřív zámky – ještě před kopírováním. Od této chvíle mi nikdo vybrané soubory
            #    nezamkne a ostatní vidí, že projekt aktivuji.
            if progress:
                progress(0, 0, "Zamykání souborů…")
            server_paths = [to_rel(server, p) for p in list_files(server, self.settings.ignore_patterns)]
            with self._write(server, create=True) as db:
                if db.active_session(self.user, self.pc):
                    raise SyncError(f"Projekt {name} už máte aktivovaný.")
                sid = db.start_session(self.user, self.pc)
                all_files = db.active_files(include_pending=True)
                owners = db.lock_owners()
                mirrored = db.mirrored()
                on_server = set(server_paths)
                want: list[str] = []
                want_paths: set[str] = set()
                for p in (server_paths if lock_paths is None else list(lock_paths)):
                    f = all_files.get(p)
                    if f is not None and f.file_id in mirrored:
                        if lock_paths is None:
                            continue  # „všechny volné“ – soubory spravované zrcadlem nejsou volné
                        raise SyncError(mirror_lock_error([p], [mirrored[f.file_id][0]],
                                                          db.mirror_sources()))
                    if f is None:
                        if p in on_server:
                            # soubor zatím není v evidenci (první aktivace / přidán mimo aplikaci):
                            # rezervace, údaje doplní srovnání evidence po zkopírování
                            want.append(db.add_file(p, None, None, None, pending=True))
                            want_paths.add(p)
                    elif not (lock_paths is None and f.file_id in owners):  # None = jen volné
                        want.append(f.file_id)
                        want_paths.add(p)
                taken = db.lock(sid, want)
                if taken:
                    by_id = {f.file_id: f for f in all_files.values()}
                    raise SyncError("Některé soubory mezitím zamkl jiný uživatel:\n" + "\n".join(
                        f"{by_id[f].path} – {owners[f].label}" for f in taken[:15]))
            add_marker(server, LockInfo(self.user, self.pc, now_iso(), sid))

            # 2) Kopírování
            if local.is_dir() and not any(local.iterdir()):
                local.rmdir()
            if local.exists():
                moved_to = original_copy_path(local, name)
                local.rename(moved_to)
            copied: dict[str, FileInfo] = {}
            to_copy = [p for p in server_paths if p in want_paths] if only_locked else server_paths
            for i, rel in enumerate(to_copy):
                if progress:
                    progress(i, len(to_copy), rel)
                src = to_abs(server, rel)
                try:
                    st = src.stat()
                    sha = copy_with_hash(src, to_abs(local, rel))
                except FileNotFoundError:
                    continue  # mezitím smazán / přejmenován jiným uživatelem
                copied[rel] = FileInfo(rel, st.st_size, st.st_mtime, sha)
            local.mkdir(parents=True, exist_ok=True)
            if progress:
                progress(len(to_copy), len(to_copy), "Zápis do databáze…")

            # 3) Srovnání evidence se zkopírovaným stavem
            with self._write(server) as db:
                self._my_session(db, sid)
                stale, gone, external = self._sync_baseline(db, server, sid, list(copied.values()))
                db.discard_pending_new(sid)  # rezervace souborů, které mezitím zmizely
                final = db.active_files()
                locked = db.locked_by(sid)

            # 4) Soubory, které během kopírování odevzdal jiný uživatel: stáhnout aktuální verzi
            for rel in stale:
                copied[rel].sha256 = copy_with_hash(to_abs(server, rel), to_abs(local, rel))
            for rel in gone:
                to_abs(local, rel).unlink(missing_ok=True)
                remove_empty_parents(local, rel)
                copied.pop(rel, None)

            manifest = Manifest(name, sid, partial=only_locked)
            for rel, fi in copied.items():
                f = final.get(rel)
                if f is None:
                    continue
                manifest.files[rel] = Entry(f.file_id, fi.sha256,
                                            db_sha=f.sha256 if f.sha256 != fi.sha256 else None)
                if f.file_id not in locked:
                    set_readonly(to_abs(local, rel), True)
            manifest.save(local)
            notes_mod.restore_drafts(self.local_root, name, local)  # odložené při deaktivaci
        except BaseException:
            if sid is not None:
                try:
                    with self._write(server) as db:
                        db.end_session(sid, "cancelled")
                except Exception:  # noqa: BLE001 – původní chyba je důležitější
                    pass
                remove_marker(server, self.user, self.pc)
            rmtree(local)
            if moved_to is not None:
                moved_to.rename(local)
                moved_to = None
            raise
        return ActivationResult(moved_to, external)

    def _sync_baseline(self, db: ProjectDB, server: Path, session_id: int,
                       server_files: list[FileInfo]) -> tuple[list[str], list[str], list[str]]:
        """Srovná evidenci v databázi se zkopírovaným stavem serveru (při aktivaci).

        - Soubory, které drží jiní uživatelé nebo se právě nahrávají: stav v databázi platí.
        - Moje rezervace (soubory dosud mimo evidenci): doplní se údaje ze zkopírovaného souboru.
        - Volný soubor s jiným obsahem: pokud server pořád odpovídá databázi, změnil ho během
          kopírování jiný uživatel (odevzdal) – kopie je zastaralá -> vrací se v `stale`
          ke stažení znovu. Jinak jde o změnu mimo aplikaci a zapíše se.
        - Soubor zkopírovaný, ale už neexistující na serveru -> vrací se v `gone`.
        Změny mimo aplikaci se zapíší jako odevzdání „Změny mimo aplikaci“ a vrací se
        v `external` (popisy pro upozornění uživatele)."""
        baseline = db.active_files(include_pending=True)
        owners = db.lock_owners()
        mine = {f for f, o in owners.items() if o.session_id == session_id}
        protected = ({f for f in owners if f not in mine}
                     | {f.file_id for f in baseline.values() if f.pending and f.file_id not in mine})
        on_server = {fi.path for fi in server_files}
        stale: list[str] = []
        gone: list[str] = []
        external: list[str] = []
        # projekt už měl evidenci -> soubor, který v ní není, přibyl mimo aplikaci
        # (při úplně první aktivaci jde jen o načtení do evidence)
        known = any(b.sha256 is not None for b in baseline.values())
        commit_id: int | None = None

        def server_matches_db(bf: DbFile) -> bool:
            try:
                st = to_abs(server, bf.path).stat()
            except FileNotFoundError:
                return False
            return bf.size is not None and same_stat(st.st_size, st.st_mtime, bf.size, bf.mtime)

        def commit() -> int:
            nonlocal commit_id
            if commit_id is None:
                commit_id = db.add_commit(session_id, self.user, self.pc, dbm.SYNC,
                                          "Změny provedené mimo aplikaci, zjištěné při aktivaci.")
            return commit_id

        # Soubory, které z evidence zmizely – mohly být přejmenovány ručně přímo na serveru.
        # Stejný obsah (hash) = stejný soubor, historie se zachová pod novým názvem.
        # Jen soubory, které na serveru opravdu nejsou (ne ty, které jen přibyly po výpisu).
        missing = {path: bf for path, bf in baseline.items()
                   if path not in on_server and bf.file_id not in protected and bf.sha256 is not None
                   and not to_abs(server, path).exists()}
        missing_by_hash: dict[str, list[DbFile]] = {}
        for bf in missing.values():
            missing_by_hash.setdefault(bf.sha256, []).append(bf)

        renamed: set[str] = set()
        for fi in server_files:
            bf = baseline.get(fi.path)
            if bf is not None:
                if bf.file_id in protected:
                    if not bf.pending and bf.sha256 and bf.sha256 != fi.sha256:
                        who = owners.get(bf.file_id)
                        external.append(f"{fi.path} – změněn mimo aplikaci"
                                        + (f" (soubor drží {who.label})" if who else ""))
                    continue
                if bf.sha256 is None:  # moje rezervace -> poprvé do evidence
                    candidates = missing_by_hash.get(fi.sha256)
                    if candidates:
                        # ve skutečnosti ruční přejmenování na serveru: rezervaci nahradí
                        # původní soubor (historie pokračuje), zámek přejde na něj
                        old = max(candidates, key=lambda c: similarity(c, fi)[0])
                        candidates.remove(old)
                        renamed.add(old.path)
                        db.drop_placeholder(bf.file_id)
                        db.update_file(old.file_id, fi.path, fi.size, fi.mtime, fi.sha256)
                        db.lock(session_id, [old.file_id])
                        db.add_change(commit(), old.file_id, dbm.EXTERNAL_RENAMED, old.path, fi.path,
                                      fi.size, fi.mtime, fi.sha256)
                        external.append(f"{old.path} – přejmenován mimo aplikaci na {fi.path}")
                        continue
                    db.update_file(bf.file_id, fi.path, fi.size, fi.mtime, fi.sha256)
                    db.add_change(commit(), bf.file_id, dbm.IMPORTED, None, fi.path,
                                  fi.size, fi.mtime, fi.sha256)
                    if known:
                        external.append(f"{fi.path} – přidán mimo aplikaci")
                    continue
                if bf.sha256 != fi.sha256 and bf.file_id not in mine and server_matches_db(bf):
                    stale.append(fi.path)  # odevzdal ho jiný uživatel během kopírování
                    continue
                db.update_file(bf.file_id, fi.path, fi.size, fi.mtime, fi.sha256)
                if bf.sha256 != fi.sha256:
                    db.add_change(commit(), bf.file_id, dbm.EXTERNAL_MODIFIED, fi.path, fi.path,
                                  fi.size, fi.mtime, fi.sha256)
                    external.append(f"{fi.path} – změněn mimo aplikaci")
                continue
            if not to_abs(server, fi.path).exists():
                gone.append(fi.path)  # během kopírování smazán / přejmenován jiným uživatelem
                continue
            candidates = missing_by_hash.get(fi.sha256)
            if candidates:
                # u více kandidátů se stejným obsahem vybrat ten s nejpodobnějším názvem
                old = max(candidates, key=lambda c: similarity(c, fi)[0])
                candidates.remove(old)
                renamed.add(old.path)
                db.update_file(old.file_id, fi.path, fi.size, fi.mtime, fi.sha256)
                db.add_change(commit(), old.file_id, dbm.EXTERNAL_RENAMED, old.path, fi.path,
                              fi.size, fi.mtime, fi.sha256)
                external.append(f"{old.path} – přejmenován mimo aplikaci na {fi.path}")
            else:
                fid = db.add_file(fi.path, fi.size, fi.mtime, fi.sha256)
                db.add_change(commit(), fid, dbm.IMPORTED, None, fi.path, fi.size, fi.mtime, fi.sha256)
                if known:
                    external.append(f"{fi.path} – přidán mimo aplikaci")
        for path, bf in missing.items():
            if path not in renamed:
                db.mark_deleted(bf.file_id)
                db.add_change(commit(), bf.file_id, dbm.EXTERNAL_DELETED, path, None)
                external.append(f"{path} – smazán mimo aplikaci")
        return stale, gone, external

    # --- aktualizace ze serveru a převzetí ------------------------------------

    def refresh(self, name: str, progress: Progress | None = None) -> RefreshResult:
        """Stáhne ze serveru nové a změněné soubory, které nemám zamčené.

        Místně změněné cizí soubory se nepřepisují – nahlásí se jako konflikt."""
        server = self.server_dir(name)
        local, manifest = self._manifest(name)
        with ProjectDB.for_project(server) as db:
            self._my_session(db, manifest.session_id)
            files = db.active_files(include_pending=True)
            mine = db.locked_by(manifest.session_id)
        result = RefreshResult()
        local_by_id = manifest.by_file_id()
        server_ids = {f.file_id for f in files.values()}
        found: list[tuple[str, str, str]] = []  # (file_id, cesta, hash staženého) – nesedí s DB

        def download(f: DbFile, dst: Path) -> Entry:
            sha = copy_with_hash(to_abs(server, f.path), dst)
            set_readonly(dst, True)
            if sha != f.sha256:
                found.append((f.file_id, f.path, sha))  # obsah na serveru neodpovídá evidenci
            return Entry(f.file_id, sha, db_sha=f.sha256 if sha != f.sha256 else None)

        todo = [f for f in files.values() if f.file_id not in mine and not f.pending]
        for i, f in enumerate(todo):
            if progress:
                progress(i, len(todo), f.path)
            lpath = local_by_id.get(f.file_id)
            if lpath is None:  # nový soubor na serveru (nebo nestažený – viz partial)
                if manifest.partial:
                    continue  # částečná kopie: stahovat jen soubory, které v ní už jsou
                dst = to_abs(local, f.path)
                if dst.exists() or f.path in manifest.files:
                    result.conflicts.append(f"{f.path} – v místní kopii je jiný soubor se stejným názvem")
                    continue
                manifest.files[f.path] = download(f, dst)
                result.downloaded.append(f.path)
                continue

            entry = manifest.files[lpath]
            lp = to_abs(local, lpath)
            changed_locally = not lp.exists() or file_hash(lp) != entry.sha256
            if lpath != f.path:  # přejmenován jiným uživatelem
                target = to_abs(local, f.path)
                if changed_locally or (target.exists() and not same_path(target, lp)):
                    result.conflicts.append(f"{lpath} – na serveru přejmenován na {f.path}, "
                                            "ale místní kopie je změněná")
                    continue
                move_file(lp, to_abs(local, f.path))
                remove_empty_parents(local, lpath)
                fix_case(local, f.path)
                manifest.files[f.path] = manifest.files.pop(lpath)
                lp = to_abs(local, f.path)
                result.moved.append(f"{lpath} → {f.path}")
            if f.sha256 != entry.known_db_sha:  # v evidenci nová verze (někdo odevzdal)
                if changed_locally:
                    result.conflicts.append(f"{f.path} – změněn na serveru i v místní kopii")
                    continue
                manifest.files[f.path] = download(f, lp)
                result.downloaded.append(f.path)

        for lpath, entry in list(manifest.files.items()):
            if entry.file_id in server_ids or entry.file_id in mine:
                continue
            lp = to_abs(local, lpath)  # smazán na serveru jiným uživatelem
            if lp.exists():
                if file_hash(lp) != entry.sha256:
                    result.conflicts.append(f"{lpath} – smazán na serveru, ale místní kopie je změněná")
                    continue
                set_readonly(lp, False)
                lp.unlink()
                remove_empty_parents(local, lpath)
            del manifest.files[lpath]
            result.removed.append(lpath)

        result.external, recorded = self._confirm_external(server, manifest.session_id, found, "aktualizaci")
        for e in manifest.files.values():
            if e.file_id in recorded:
                e.db_sha = None  # evidence teď odpovídá obsahu
        manifest.save(local)
        if progress:
            progress(len(todo), len(todo), "")
        return result

    def _confirm_external(self, server: Path, sid: int, found: list[tuple[str, str, str]],
                          when: str) -> tuple[list[str], set[str]]:
        """Ověří soubory, jejichž stažený obsah nesedí s hashem v evidenci (změna mimo aplikaci).

        Znovu načte evidenci (soubor mohl mezitím legitimně odevzdat jiný uživatel – pak nejde
        o změnu mimo aplikaci). Volné a mé soubory zapíše do historie jako „Změněn mimo
        aplikaci“; u souborů, které drží jiný uživatel, jen upozorní (evidenci nemění, aby
        mu zůstalo varování při jeho odevzdání). Vrací (popisy pro uživatele, zapsané file_id)."""
        messages: list[str] = []
        recorded: set[str] = set()
        if not found:
            return messages, recorded
        with self._write(server) as db:
            files = {f.file_id: f for f in db.active_files(include_pending=True).values()}
            owners = db.lock_owners()
            commit_id: int | None = None
            for fid, path, sha in found:
                f = files.get(fid)
                if f is None or f.pending or f.sha256 == sha:
                    continue
                owner = owners.get(fid)
                if owner is not None and owner.session_id != sid:
                    messages.append(f"{path} – změněn mimo aplikaci (soubor drží {owner.label})")
                    continue
                try:
                    st = to_abs(server, f.path).stat()
                except OSError:
                    continue
                if commit_id is None:
                    commit_id = db.add_commit(sid, self.user, self.pc, dbm.SYNC,
                                              f"Změny provedené mimo aplikaci, zjištěné při {when}.")
                db.update_file(fid, f.path, st.st_size, st.st_mtime, sha)
                db.add_change(commit_id, fid, dbm.EXTERNAL_MODIFIED, f.path, f.path,
                              st.st_size, st.st_mtime, sha)
                recorded.add(fid)
                messages.append(f"{path} – změněn mimo aplikaci")
        return messages, recorded

    def take(self, name: str, paths: Iterable[str], progress: Progress | None = None) -> RefreshResult:
        """Převezme (zamkne) další soubory k úpravě a stáhne jejich aktuální verzi."""
        server = self.server_dir(name)
        local, manifest = self._manifest(name)
        paths = list(paths)
        with self._write(server) as db:
            sid = self._my_session(db, manifest.session_id)
            files = db.active_files(include_pending=True)
            unknown = [p for p in paths if p not in files]
            if unknown:
                raise SyncError("Soubory nejsou v evidenci projektu (zkuste Aktualizovat):\n"
                                + "\n".join(unknown[:15]))
            mirrored = db.mirrored()
            blocked = [p for p in paths if files[p].file_id in mirrored]
            if blocked:
                raise SyncError(mirror_lock_error(
                    blocked, [mirrored[files[p].file_id][0] for p in blocked], db.mirror_sources()))
            owners = db.lock_owners()
            taken = db.lock(sid, [files[p].file_id for p in paths])
            if taken:
                by_id = {f.file_id: f for f in files.values()}
                raise SyncError("Soubory drží jiný uživatel:\n" + "\n".join(
                    f"{by_id[f].path} – {owners[f].label}" for f in taken[:15]))

        result = RefreshResult()
        local_by_id = manifest.by_file_id()
        found: list[tuple[str, str, str]] = []  # obsah na serveru nesedí s evidencí
        for i, p in enumerate(paths):
            if progress:
                progress(i, len(paths), p)
            f = files[p]
            src = to_abs(server, f.path)
            lpath = local_by_id.get(f.file_id)
            lp = to_abs(local, lpath or f.path)
            if lpath is not None and lpath != f.path and lp.exists():
                # přejmenovaný na serveru – nejdřív přesunout místní kopii
                move_file(lp, to_abs(local, f.path))
                remove_empty_parents(local, lpath)
                fix_case(local, f.path)
                manifest.files[f.path] = manifest.files.pop(lpath)
            lp = to_abs(local, f.path)
            entry = manifest.files.get(f.path)
            if entry is None or not lp.exists():  # ještě nestažený
                entry = manifest.files[f.path] = Entry(f.file_id, copy_with_hash(src, lp))
                server_sha = entry.sha256
                result.downloaded.append(f.path)
            elif file_hash(lp) != entry.sha256:  # místně upravený (cizí soubor) – nepřepisovat
                if f.sha256 != entry.known_db_sha:
                    result.conflicts.append(f"{f.path} – změněn na serveru i ve vaší místní kopii; "
                                            "vaše místní verze zůstala")
                server_sha = file_hash(src)
            else:
                # vždy čerstvá kopie: soubor budu upravovat a odevzdávat – nesmí se potichu
                # přepsat případná změna na serveru (ověří se hashem)
                server_sha = copy_with_hash(src, lp)
                if server_sha != entry.sha256:
                    result.downloaded.append(f.path)
                entry.sha256 = server_sha
            entry.db_sha = None
            if server_sha != f.sha256:
                found.append((f.file_id, f.path, server_sha))
            set_readonly(lp, False)
        result.external, _ = self._confirm_external(server, sid, found, "převzetí")
        manifest.save(local)
        return result

    # --- odevzdání a deaktivace ----------------------------------------------

    def files_open_elsewhere(self, plan: UploadPlan, end_session: bool = False) -> dict[str, list[str]]:
        """Soubory k odevzdání, které má otevřené jiný program (CAD, právě se ukládá) -> programy
        (Restart Manager Windows, viz file_locks). Při deaktivaci i ostatní soubory místní kopie
        (místní kopie se pak maže a otevřený soubor by v ní zůstal)."""
        local = self.local_dir(plan.name)
        paths = [lf.path for _, lf in plan.modified] + [lf.path for lf in plan.new]
        if end_session:
            paths += [p for p in plan.local_files if p not in paths]
        found = file_locks.open_by([to_abs(local, p) for p in paths])
        by_path = {os.path.normcase(str(k)): v for k, v in found.items()}
        return {p: by_path[os.path.normcase(str(to_abs(local, p)))] for p in paths
                if os.path.normcase(str(to_abs(local, p))) in by_path}

    def plan_upload(self, name: str, progress: Progress | None = None) -> UploadPlan:
        """Porovná místní kopii s evidencí a připraví návrh změn (nic nezapisuje)."""
        server = self.server_dir(name)
        local, manifest = self._manifest(name)
        with ProjectDB.for_project(server) as db:
            sid = self._my_session(db, manifest.session_id)
            files_all = db.active_files(include_pending=True)
            mine = db.locked_by(sid)
            owners = db.lock_owners()
        by_id = {f.file_id: f for f in files_all.values()}

        local_files = scan(local, self.settings.ignore_patterns, progress)
        unchanged: list[str] = []
        modified: list[tuple[DbFile, FileInfo]] = []
        missing: list[DbFile] = []
        locked_unchanged: list[DbFile] = []
        conflicts: list[str] = []
        unsavable: list[str] = []
        gone: set[str] = set()
        # můj soubor přejmenovaný jen ve velikosti písmen (A/x -> a/x): chybí a je „nový“ –
        # nesmí se brát jako kolize se sebou samým (jinak by se odevzdal jako smazaný)
        local_lower = {p.lower() for p in local_files}
        case_renamed = {p.lower() for p, e in manifest.files.items()
                        if p not in local_files and p.lower() in local_lower and e.file_id in mine}
        for path, entry in manifest.files.items():
            lf = local_files.get(path)
            dbf = by_id.get(entry.file_id)
            if entry.file_id in mine and dbf is not None:
                base = DbFile(entry.file_id, path, dbf.size, dbf.mtime, entry.sha256)
                if lf is None:
                    missing.append(base)
                elif lf.sha256 != entry.sha256:
                    modified.append((base, lf))
                else:
                    locked_unchanged.append(base)
                    unchanged.append(path)
                if not dbf.pending and lf is not None:
                    try:
                        st = to_abs(server, dbf.path).stat()
                        if not same_stat(st.st_size, st.st_mtime, dbf.size, dbf.mtime):
                            conflicts.append(f"{path} – změněn na serveru mimo aplikaci (bude přepsán)")
                    except FileNotFoundError:
                        gone.add(entry.file_id)
                        conflicts.append(f"{path} – smazán na serveru mimo aplikaci – obnoví se "
                                         "z vaší místní kopie")
            elif lf is None:
                conflicts.append(f"{path} – smazán v místní kopii, ale nemáte ho zamčený "
                                 "(na serveru zůstane)")
            elif lf.sha256 != entry.sha256:
                who = owners.get(entry.file_id)
                conflicts.append(f"{path} – změněn v místní kopii, ale nemáte ho zamčený"
                                 + (f" (drží {who.label})" if who else "") + " – změna se nenahraje")
                unsavable.append(path)
            else:
                unchanged.append(path)

        by_lower = {p.lower(): f for p, f in files_all.items()}
        new: list[FileInfo] = []
        for path, lf in local_files.items():
            if path in manifest.files:
                continue
            f = by_lower.get(path.lower())
            if f is not None and not (f.file_id in mine and f.pending) and path.lower() not in case_renamed:
                conflicts.append(f"{path} – na serveru už je soubor se stejným názvem (nelze nahrát)")
                unsavable.append(path)
                continue
            new.append(lf)

        suggestions = suggest_renames(missing, new, self.settings.rename_threshold)
        invalid = self._invalid_paths([lf.path for lf in new] + [lf.path for _, lf in modified])
        return UploadPlan(name, sid, local_files, unchanged, modified, missing, new,
                          locked_unchanged, suggestions, conflicts, invalid, unsavable, gone)

    # zpětně čitelné názvy pro deaktivaci
    plan_deactivation = plan_upload

    def deactivate(self, plan: UploadPlan, assignments: dict[str, str | None], comment: str,
                   progress: Progress | None = None) -> UploadResult:
        """Nahraje všechny vlastní změny, uvolní všechny zámky a ukončí aktivaci."""
        return self.upload(plan, assignments, comment, None, end_session=True, progress=progress)

    def upload(
        self,
        plan: UploadPlan,
        assignments: dict[str, str | None],
        comment: str,
        selected: set[str] | None = None,
        end_session: bool = False,
        progress: Progress | None = None,
        notes: list[Draft] | None = None,
        keep_local: bool = False,
    ) -> UploadResult:
        """Odevzdá vybrané změny na server a uvolní odevzdané soubory.

        assignments: file_id chybějícího souboru -> nová cesta (přejmenování) / None (smazán).
        selected:    klíče položek k odevzdání (file_id, u nových souborů new_key(cesta));
                     None = vše. Při end_session se vždy odevzdá vše a uvolní všechny zámky.
        notes:       rozepsané poznámky, které se zveřejní spolu s odevzdáním (a z rozepsaných
                     zmizí).
        keep_local:  po deaktivaci místní kopii zatím nemazat (poběží skripty) – smaže ji
                     finish_local(); result.cleanup_pending říká, zda je to potřeba.
        """
        notes = [d for d in (notes or []) if d.text.strip()]
        if end_session:
            selected = None
        comment = comment.strip()
        # odevzdání posledního drženého souboru = deaktivace (viz auto_deactivation)
        auto_end, auto_note = (False, "") if end_session else auto_deactivation(plan, assignments, selected)

        def sel(key: str) -> bool:
            return selected is None or key in selected

        new_by_path = {lf.path: lf for lf in plan.new}
        targets = [p for p in assignments.values() if p]
        if len(targets) != len(set(targets)):
            raise SyncError("Jeden nový soubor je přiřazen k více původním souborům.")
        if any(p not in new_by_path for p in targets):
            raise SyncError("Přiřazení obsahuje soubor, který není mezi novými soubory.")
        missing_by_id = {bf.file_id: bf for bf in plan.missing}
        renames = [(missing_by_id[fid], new_by_path[p]) for fid, p in assignments.items()
                   if p and sel(fid)]
        deleted = [bf for bf in plan.missing if not assignments.get(bf.file_id) and sel(bf.file_id)]
        modified = [(bf, lf) for bf, lf in plan.modified if sel(bf.file_id)]
        added = [lf for lf in plan.new if lf.path not in set(targets) and sel(new_key(lf.path))]
        release_only = [bf for bf in plan.locked_unchanged if sel(bf.file_id)]
        # zamčené beze změny, ale na serveru smazané mimo aplikaci -> nahrát zpět z místní kopie
        restored = [(bf, plan.local_files[bf.path]) for bf in release_only
                    if bf.file_id in plan.gone_on_server and bf.path in plan.local_files]
        release_only = [bf for bf in release_only if bf.file_id not in {r.file_id for r, _ in restored}]
        # upravené a zároveň na serveru smazané: nahrají se jako upravené + záznam o smazání
        modified_gone = [bf for bf, _ in modified if bf.file_id in plan.gone_on_server]
        has_changes = bool(renames or deleted or modified or added)

        if has_changes and not comment:
            raise SyncError("Bez komentáře nelze změny odevzdat.")
        invalid = self._invalid_paths([lf.path for _, lf in renames + modified] + [lf.path for lf in added])
        if invalid:
            raise SyncError("Soubory mají názvy s nepovolenými znaky – nejdřív je opravte:\n\n"
                            + format_invalid(invalid))
        if restored and not comment:  # jen obnova smazaných – komentář není potřeba psát
            comment = "Obnoveno z místní kopie (smazáno na serveru mimo aplikaci)."
        if not (has_changes or restored or release_only or end_session):
            raise SyncError("Není vybráno nic k odevzdání.")

        server = self.server_dir(plan.name)
        local = self.local_dir(plan.name)
        sid = plan.session_id
        # soubor změněný po porovnání (velikost / čas změny) – zjistit dřív, než se na serveru
        # cokoliv změní; obsah se při nahrání ještě ověří hashem (viz _upload)
        for lf in [lf for _, lf in renames + modified + restored] + added:
            try:
                st = to_abs(local, lf.path).stat()
            except OSError:
                raise SyncError(f"Soubor {lf.path} v místní kopii chybí nebo ho nelze přečíst.") from None
            if st.st_size != lf.size or st.st_mtime != lf.mtime:
                raise SyncError(changed_during_upload(lf.path))

        # 1) Rezervace: nové názvy a soubory, které se budou měnit, se označí jako „nahrávají se“
        #    (ostatní uživatelé je zatím nestahují a nikdo jiný si stejný název nezabere).
        new_ids: dict[str, str] = {}
        with self._write(server) as db:
            self._my_session(db, sid)
            mine = db.locked_by(sid)
            by_lower = {p.lower(): f for p, f in db.active_files(include_pending=True).items()}
            for lf in added:
                f = by_lower.get(lf.path.lower())
                if f is not None and f.file_id in mine and f.pending:
                    new_ids[lf.path] = f.file_id  # rezervace z přerušeného pokusu
                    continue
                if f is not None or to_abs(server, lf.path).exists():
                    raise SyncError(f"Na serveru už je soubor se stejným názvem:\n{lf.path}")
                fid = db.add_file(lf.path, None, None, None, pending=True)
                db.lock(sid, [fid])
                new_ids[lf.path] = fid
            for bf, lf in renames:
                f = by_lower.get(lf.path.lower())
                if f is not None and f.file_id != bf.file_id:
                    raise SyncError(f"Na serveru už je soubor se stejným názvem:\n{lf.path}")
            db.set_pending([bf.file_id for bf, _ in renames + modified + restored]
                           + [bf.file_id for bf in deleted])

        # 2) Soubory na serveru
        total = 2 * len(renames) + len(modified) + len(added) + len(deleted) + len(restored)
        step = 0

        def tick(text: str) -> None:
            nonlocal step
            if progress:
                progress(step, total, text)
            step += 1

        ops: list[tuple] = []
        tmp_dir = server / SPM_DIR / "tmp"
        if renames:
            # dvoufázově přes .spm/tmp, aby fungovala i záměna názvů (a <-> b)
            tmp_dir.mkdir(parents=True, exist_ok=True)
            for bf, _ in renames:
                tick(f"Přejmenování {bf.path}")
                src = to_abs(server, bf.path)
                if src.exists():
                    os.replace(src, tmp_dir / bf.file_id)
            for bf, _ in renames:  # vyprázdněné složky pryč (i kvůli změně velikosti písmen)
                remove_empty_parents(server, bf.path)
            for bf, lf in renames:
                tick(f"Přejmenování → {lf.path}")
                tmp = tmp_dir / bf.file_id
                dst = to_abs(server, lf.path)
                if tmp.exists():
                    if dst.exists():
                        self._to_trash(server, sid, lf.path)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(tmp, dst)
                    fix_case(server, lf.path)
                remove_empty_parents(server, bf.path)
                changed = bf.sha256 != lf.sha256
                backup = None
                if changed or not dst.exists():
                    backup = self._upload(server, local, lf, backup_existing=changed, file_id=bf.file_id)
                ops.append((dbm.RENAMED_MODIFIED if changed else dbm.RENAMED, bf.file_id, bf.path, lf, backup))

        for bf, lf in modified:
            tick(f"Nahrávání {lf.path}")
            backup = self._upload(server, local, lf, backup_existing=True, file_id=bf.file_id)
            ops.append((dbm.MODIFIED, bf.file_id, bf.path, lf, backup))

        for lf in added:
            tick(f"Nahrávání {lf.path}")
            self._upload(server, local, lf, backup_existing=False)
            ops.append((dbm.ADDED, new_ids[lf.path], None, lf, None))

        for bf, lf in restored:
            tick(f"Obnova {lf.path}")
            self._upload(server, local, lf, backup_existing=False)
            ops.append((dbm.RESTORED, bf.file_id, bf.path, lf, None))

        # upravené soubory smazané na serveru: zachovala se jejich poslední verze ze serveru?
        lost_versions: list[str] = []
        if modified_gone:
            with ProjectDB.for_project(server) as db:
                db_sha = {f.file_id: f.sha256 for f in db.active_files(include_pending=True).values()}
            for bf in modified_gone:
                if not self._version_in_backups(server, bf.file_id, db_sha.get(bf.file_id)):
                    lost_versions.append(bf.path)

        for bf in deleted:
            # na serveru se nemažou, ale přesouvají do .spm/trash/<sezení>/
            tick(f"Mazání {bf.path}")
            trash = self._to_trash(server, sid, bf.path)
            remove_empty_parents(server, bf.path)
            ops.append((dbm.DELETED, bf.file_id, bf.path, None, trash))

        # 3) Zápis do databáze a uvolnění zámků
        if progress:
            progress(total, total, "Zápis do databáze…")
        released: list[str] = []
        if notes:
            self._ensure_notes_tables(server)
        with self._write(server) as db:
            self._my_session(db, sid)
            commit_id = (db.add_commit(sid, self.user, self.pc, dbm.UPLOAD, comment)
                         if has_changes or restored else None)
            for bf in modified_gone:  # v historii je vidět, že soubor někdo smazal mimo aplikaci
                db.add_change(commit_id, bf.file_id, dbm.EXTERNAL_DELETED, bf.path, None)
            if notes:
                notes_mod.insert_notes(db, notes, self.user, self.pc, self.display_name, commit_id)
                notes_mod.write_summary(server, db)
            for kind, file_id, old_path, lf, backup in ops:
                if kind == dbm.DELETED:
                    db.mark_deleted(file_id)
                    db.add_change(commit_id, file_id, kind, old_path, None, backup_path=backup)
                    continue
                st = to_abs(server, lf.path).stat()
                db.update_file(file_id, lf.path, st.st_size, st.st_mtime, lf.sha256)
                db.add_change(commit_id, file_id, kind, old_path, lf.path, st.st_size,
                              st.st_mtime, lf.sha256, backup)
                released.append(file_id)
            released += [bf.file_id for bf in release_only]
            if not end_session:
                db.release(sid, released)
                # pojistka: deaktivovat jen tehdy, když opravdu nic dalšího nedržím
                auto_end = auto_end and not db.locked_by(sid)
            if end_session or auto_end:
                db.end_session(sid, "done")
        if renames:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        # 4) Místní kopie: manifest a atribut „jen pro čtení“ u odevzdaných souborů
        _, manifest = self._manifest(plan.name)
        for kind, file_id, old_path, lf, _ in ops:
            if kind == dbm.DELETED:
                manifest.files.pop(old_path, None)
                continue
            if old_path and old_path != lf.path:
                manifest.files.pop(old_path, None)
            manifest.files[lf.path] = Entry(file_id, lf.sha256)
            set_readonly(to_abs(local, lf.path), True)
        for bf in release_only:
            set_readonly(to_abs(local, bf.path), True)

        result = UploadResult(renamed=len(renames), modified=len(modified), added=len(added),
                              deleted=len(deleted), released=len(released),
                              auto_deactivated=auto_end, note=auto_note, notes_published=len(notes),
                              restored=[lf.path for _, lf in restored] + [bf.path for bf in modified_gone],
                              lost_versions=lost_versions, commit_id=commit_id,
                              ended=end_session or auto_end)
        if has_changes:
            result.script_files = self._script_files(ops, missing_by_id, release_only)
        if notes:  # zveřejněné už nejsou rozepsané
            published = {d.id for d in notes}
            notes_mod.save_drafts(local, [d for d in notes_mod.load_drafts(local) if d.id not in published])
        manifest.save(local)
        if result.ended:
            remove_marker(server, self.user, self.pc)
            if self.settings.delete_local_after_deactivate:
                if keep_local:
                    result.cleanup_pending = True
                else:
                    result.local_deleted = self.finish_local(plan.name)
        return result

    @staticmethod
    def _script_files(ops: list[tuple], missing_by_id: dict[str, DbFile],
                      release_only: list[DbFile]) -> list[ScriptFile]:
        """Soubory odevzdání pro skripty – všechny odevzdávané soubory se stavem."""
        kinds = {dbm.ADDED: ADDED, dbm.MODIFIED: MODIFIED, dbm.RENAMED: RENAMED,
                 dbm.RENAMED_MODIFIED: RENAMED, dbm.RESTORED: UNCHANGED}
        files: list[ScriptFile] = []
        for kind, file_id, old_path, lf, _ in ops:
            if kind == dbm.DELETED:
                bf = missing_by_id.get(file_id)
                files.append(ScriptFile(old_path, DELETED, file_id, sha256=bf.sha256 if bf else None))
                continue
            files.append(ScriptFile(lf.path, kinds[kind], file_id,
                                    old_path if kinds[kind] == RENAMED else None, lf.sha256, lf.size))
        for bf in release_only:
            files.append(ScriptFile(bf.path, UNCHANGED, bf.file_id, sha256=bf.sha256, size=bf.size))
        return sorted(files, key=lambda f: f.path.lower())

    def finish_local(self, name: str) -> bool:
        """Po deaktivaci (a doběhnutých skriptech) smaže místní kopii. Vrací, zda zmizela celá."""
        local = self.local_dir(name)
        if not local.exists():
            return True
        # nezveřejněné rozepsané poznámky se se složkou nesmažou – vrátí se při aktivaci
        notes_mod.park_drafts(self.local_root, name, local)
        rmtree(local)
        return not local.exists()

    def preview_files(self, name: str) -> list[ScriptFile]:
        """Soubory pro zkušební běh skriptu: u mého aktivovaného projektu to, co by se
        odevzdalo při deaktivaci; jinak všechny soubory projektu jako beze změny."""
        status = self.project_status(name)
        if status is not None and status.mine:
            plan = self.plan_upload(name)
            return selection_files(plan, plan.default_assignments(), None)
        with ProjectDB.for_project(self.server_dir(name)) as db:
            files = [ScriptFile(f.path, UNCHANGED, f.file_id, sha256=f.sha256, size=f.size)
                     for f in db.active_files().values()]
        return sorted(files, key=lambda f: f.path.lower())

    def script_file_state(self, name: str, rel: str) -> tuple[str, ScriptFile | None, str]:
        """Stav souboru projektu pro skript (ACTIVATE, zápis v BEFORE):
        ("missing", None, "") – v projektu není (skript ho může vytvořit),
        ("mine", soubor, "")  – mám ho zamčený,
        ("free", soubor, "")  – existuje, nikdo ho nedrží (lze aktivovat),
        ("other", soubor, kdo) – drží ho jiný, ("mirror", soubor, odkud) – spravuje ho zrcadlo."""
        server = self.server_dir(name)
        if not db_path(server).exists():
            return "missing", None, ""
        _, manifest = self._manifest(name)
        with ProjectDB.for_project(server) as db:
            sid = self._my_session(db, manifest.session_id)
            f = next((x for p, x in db.active_files(include_pending=True).items()
                      if p.lower() == rel.lower()), None)
            if f is None:
                return "missing", None, ""
            sf = ScriptFile(f.path, UNCHANGED, f.file_id, sha256=f.sha256, size=f.size)
            mirrored = db.mirrored()
            if f.file_id in mirrored:
                src = db.mirror_sources().get(mirrored[f.file_id][0])
                return "mirror", sf, src.label if src else mirrored[f.file_id][0]
            if f.file_id in db.locked_by(sid):
                return "mine", sf, ""
            owner = db.lock_owners().get(f.file_id)
            if owner is not None:
                return "other", sf, owner.label
        return "free", sf, ""

    def script_activate(self, name: str, rel: str, progress: Progress | None = None) -> ScriptFile:
        """ACTIVATE ve skriptu: zamkne volný soubor projektu a stáhne jeho aktuální verzi
        (místně změněnou verzi nepřepíše – viz take)."""
        state, sf, who = self.script_file_state(name, rel)
        if state == "mine":
            return sf
        if state != "free":
            raise SyncError(f"Soubor {rel} nelze aktivovat"
                            + (f" – drží ho {who}." if state == "other" else
                               f" – spravuje ho zrcadlo z {who}." if state == "mirror" else
                               " – v projektu není."))
        self.take(name, [sf.path], progress)
        _, manifest = self._manifest(name)
        entry = manifest.files.get(sf.path)
        return ScriptFile(sf.path, UNCHANGED, sf.file_id, sha256=entry.sha256 if entry else sf.sha256,
                          size=sf.size)

    def adopt_script_outputs(self, name: str, before: UploadPlan, selected: set[str] | None,
                             progress: Progress | None = None, activated: Iterable[str] = ()
                             ) -> tuple[UploadPlan, set[str] | None, list[str]]:
        """Po bloku BEFORE: soubory, které skript v místní kopii vytvořil nebo přepsal, a soubory,
        které aktivoval (activated – ACTIVATE / potvrzený zápis), se přidají k odevzdání.
        Změněný soubor, který nemám zamčený (např. ho změnil program přes RUN), se neodevzdá –
        odevzdání se nespustí (SyncError).
        Vrací (nový plán, výběr k odevzdání, cesty souborů od skriptu)."""
        plan = self.plan_upload(name, progress)
        old = before.local_files
        touched = sorted(p for p, lf in plan.local_files.items()
                         if p not in old or old[p].sha256 != lf.sha256)
        new_paths = {lf.path for lf in plan.new}
        modified = {lf.path: bf.file_id for bf, lf in plan.modified}
        same = {bf.path: bf.file_id for bf in plan.locked_unchanged}  # zamčené beze změny
        stuck = [p for p in touched if p not in new_paths and p not in modified and p not in same]
        if stuck:
            details = [next((c for c in plan.conflicts if c.startswith(p + " – ")), p) for p in stuck]
            raise SyncError("Skript (BEFORE) změnil soubory, které nemáte aktivované – nic se "
                            "neodevzdalo:\n" + "\n".join(details[:15])
                            + "\n\nSoubor nejdřív aktivujte ve skriptu příkazem ACTIVATE.")
        activated = [p for p in activated if p in same or p in modified]
        if selected is not None:
            selected = set(selected) | {new_key(p) for p in touched if p in new_paths} \
                | {modified[p] for p in touched if p in modified} \
                | {modified.get(p) or same[p] for p in activated}
        return plan, selected, sorted(set(touched) | set(activated))

    def record_mirror_targets(self, name: str, res, label: str, server_id: str,
                              server_name: str) -> None:
        """Po zrcadlení (MIRROR) označí soubory zdrojového projektu, kam se zrcadlí."""
        if not res.target_key or not (res.mirrored or res.removed):
            return
        server = self.server_dir(name)
        with WriteLock(server, self.user, self.pc):
            with ProjectDB.for_project(server) as db:
                db.ensure_mirror_target_tables()
                with db.conn:
                    for file_id, path in res.mirrored.items():
                        db.set_mirror_target(file_id, res.target_key, label, server_id, server_name, path)
                    for file_id in res.removed:
                        db.remove_mirror_target(file_id, res.target_key)

    def clear_stale_mirror_marks(self, name: str, files: list[ScriptFile],
                                 keep: set[tuple[str, str]],
                                 resolve: Callable[[dbm.MirrorTarget], "ProjectManager | None"] | None = None,
                                 warnings: list[str] | None = None) -> int:
        """Po skriptech: změněné soubory odevzdání, které se do cíle tentokrát nezrcadlily
        (skript je vynechal, zastaven, MIRROR přeskočen nebo selhal), ztratí označení
        „zrcadleno do“ a jejich kopie v cíli dostanou příznak „neaktuální“.
        keep = (file_id, cíl) zrcadlené v tomto běhu; resolve = ProjectManager záložky cíle
        (None = cíl není mezi záložkami – jen se odebere označení); nepovedený zápis do cíle
        se přidá do warnings. Vrací počet odebraných označení."""
        by_id = {f.file_id: f for f in files if f.status != UNCHANGED and f.file_id}
        server = self.server_dir(name)
        if not by_id or not db_path(server).exists():
            return 0
        with ProjectDB.for_project(server) as db:
            marks = db.mirror_targets()
            source_keys = {db.project_id() or "", str(server)} - {""}
        stale = [(fid, t) for fid in by_id for t in marks.get(fid, [])
                 if (fid, t.target) not in keep]
        if not stale:
            return 0
        # příznak „neaktuální“ v cílech (každý cíl zvlášť; nedostupný cíl nebrání úklidu zdroje)
        targets: dict[str, tuple[dbm.MirrorTarget, dict[str, str]]] = {}
        for fid, t in stale:
            reason = dbm.STALE_DELETED if by_id[fid].status == DELETED else dbm.STALE_CHANGED
            targets.setdefault(t.target, (t, {}))[1][fid] = reason
        for t, reasons in targets.values():
            try:
                target_pm = resolve(t) if resolve else None
                if target_pm is not None:
                    Mirror.mark_stale(target_pm, Path(t.label).name, t.target, source_keys,
                                      reasons, self.user)
                elif warnings is not None:
                    warnings.append(f"Cíl zrcadla {t.label} není mezi vašimi záložkami – kopie "
                                    "v něm nedostaly příznak „neaktuální“.")
            except (OSError, sqlite3.Error) as e:
                if warnings is not None:
                    warnings.append(f"Příznak „neaktuální“ v cíli {t.label} se nepodařilo zapsat: {e}")
        with WriteLock(server, self.user, self.pc):
            with ProjectDB.for_project(server) as db:
                with db.conn:
                    for fid, t in stale:
                        db.remove_mirror_target(fid, t.target)
        return len(stale)

    def release_mirrored(self, name: str, paths: Iterable[str]) -> int:
        """Uvolní neaktuální zrcadlené soubory v tomto (cílovém) projektu: zrcadlo je dál
        nespravuje, jsou to běžné soubory projektu (lze je převzít a upravit). Aktuální
        zrcadlené soubory se neuvolňují. Vrací počet uvolněných."""
        server = self.server_dir(name)
        if not db_path(server).exists():
            return 0
        wanted = {p.lower() for p in paths}
        with self._write(server) as db:
            files = db.active_files(include_pending=True)
            stale = db.mirror_stale()
            released = sorted((f for p, f in files.items() if p.lower() in wanted and f.file_id in stale),
                              key=lambda f: f.path.lower())
            if not released:
                return 0
            for f in released:
                db.unset_mirrored(f.file_id)
            names = ", ".join(f.path for f in released[:10])
            more = f" a další ({len(released) - 10})" if len(released) > 10 else ""
            db.add_commit(None, self.user, self.pc, dbm.MIRROR,
                          f"Uvolněno ze zrcadla (neaktuální kopie): {names}{more}")
        return len(released)

    def record_script_run(self, name: str, commit_id: int | None, action: str, scripts: list[str],
                          ok: bool, log: str) -> None:
        """Záznam běhu skriptů do historie projektu (u odevzdání)."""
        server = self.server_dir(name)
        with WriteLock(server, self.user, self.pc):
            with ProjectDB.for_project(server) as db:
                db.ensure_script_tables()
                with db.conn:
                    db.add_script_run(commit_id, self.user, self.pc, action, scripts, ok, log)

    def cancel_activation(self, name: str) -> None:
        """Zruší vlastní aktivaci bez nahrání změn a uvolní všechny zámky.
        Místní kopie zůstane beze změny."""
        server = self.server_dir(name)
        with self._write(server) as db:
            sid = self._my_session(db)
            db.add_commit(sid, self.user, self.pc, dbm.CANCELLED,
                          "Aktivace zrušena bez nahrání změn.")
            db.end_session(sid, "cancelled")
        remove_marker(server, self.user, self.pc)

    def force_unlock(self, name: str, user: str, pc: str) -> None:
        """Odebere zámky jiného uživatele (např. když je nedostupný). Jeho neodevzdané změny
        se už nenahrají."""
        server = self.server_dir(name)
        if db_path(server).exists():
            with self._write(server) as db:
                sid = db.active_session(user, pc)
                if sid is not None:
                    db.add_commit(sid, self.user, self.pc, dbm.FORCE_UNLOCK,
                                  f"Zámky uživatele {user} ({pc}) odebrány uživatelem {self.user}.")
                    db.end_session(sid, "cancelled")
        remove_marker(server, user, pc)

    # --- publikování ----------------------------------------------------------

    def local_files(self, name: str) -> list[str]:
        """Soubory v místní složce (relativní cesty, bez ignorovaných)."""
        local = self.local_dir(name)
        return [to_rel(local, p) for p in list_files(local, self.settings.ignore_patterns)] \
            if local.is_dir() else []

    def publish(self, name: str, comment: str, paths: Iterable[str] | None = None,
                progress: Progress | None = None) -> PublishResult:
        """Přesune projekt, který existuje jen v místním úložišti, na server.

        paths: publikovat jen tyto soubory (None = celou složku).
        Soubory se nahrají do skryté dočasné složky, ověří (hash obsahu na serveru)
        a zapíší do nové databáze projektu. Pak se složka atomicky přejmenuje na název
        projektu (selže, pokud mezitím projekt stejného názvu založil někdo jiný).
        Teprve potom se publikované soubory z místní složky odstraní (přesun). Nepublikované
        soubory se odloží do složky <projekt>_puvodni_<datum> (zůstane „Jen místně“).
        """
        comment = comment.strip()
        if not comment:
            raise SyncError("Bez komentáře nelze projekt publikovat.")
        local = self.local_dir(name)
        server = self.server_dir(name)
        if not local.is_dir():
            raise SyncError(f"Místní složka neexistuje:\n{local}")
        if not self.server_root.is_dir():
            raise SyncError(f"Síťová složka není dostupná:\n{self.server_root}")
        all_files = self.local_files(name)
        if paths is None:
            selected = all_files
        else:
            wanted = set(paths)
            selected = [p for p in all_files if p in wanted]
        if not selected:
            raise SyncError("Není vybrán žádný soubor k publikování.")
        invalid = self._invalid_paths(selected)
        if invalid:
            raise SyncError(f"Projekt {name} obsahuje složky nebo soubory s nepovolenými znaky "
                            "(česká diakritika, mezera, ( ) & , . ; = ^). Nejdřív je přejmenujte:\n\n"
                            + format_invalid(invalid))
        if server.exists():
            raise SyncError(f"Na serveru už projekt {name} existuje.")

        tmp = self.server_root / f"{PUBLISH_TMP_PREFIX}{uuid.uuid4().hex}"
        try:
            tmp.mkdir()
            files = [to_abs(local, p) for p in selected]
            uploaded: list[FileInfo] = []
            for i, src in enumerate(files):
                rel = to_rel(local, src)
                if progress:
                    progress(i, len(files), rel)
                dst = to_abs(tmp, rel)
                sha = copy_with_hash(src, dst)
                if file_hash(dst) != sha:
                    raise SyncError(f"Kontrola nahraného souboru selhala:\n{rel}")
                st = dst.stat()
                uploaded.append(FileInfo(rel, st.st_size, st.st_mtime, sha))
            if progress:
                progress(len(files), len(files), "Zápis do databáze…")

            with ProjectDB.for_project(tmp, create=True) as db:
                with db.conn:
                    cid = db.add_commit(None, self.user, self.pc, dbm.PUBLISH, comment)
                    file_ids = []
                    for fi in uploaded:
                        fid = db.add_file(fi.path, fi.size, fi.mtime, fi.sha256)
                        file_ids.append(fid)
                        db.add_change(cid, fid, dbm.PUBLISHED, None, fi.path, fi.size, fi.mtime, fi.sha256)
            try:
                tmp.rename(server)
            except FileExistsError:
                raise SyncError(f"Na serveru už projekt {name} existuje.") from None
        except BaseException:
            rmtree(tmp)
            raise

        result = PublishResult(published=len(selected), commit_id=cid,
                               script_files=[ScriptFile(fi.path, ADDED, fid, None, fi.sha256, fi.size)
                                             for fi, fid in zip(uploaded, file_ids)])
        if len(selected) == len(all_files):
            rmtree(local)
            result.local_deleted = not local.exists()
            return result
        # část: publikované soubory jsou na serveru (ověřené) -> z místní složky pryč,
        # zbytek odložit, aby se nepletl s místní kopií nového serverového projektu
        for rel in selected:
            to_abs(local, rel).unlink(missing_ok=True)
            remove_empty_parents(local, rel)
        leftover = original_copy_path(local, name)
        try:
            local.rename(leftover)
            result.leftover = leftover
        except OSError:
            result.leftover = local  # nejde přejmenovat (otevřený soubor) – zůstává pod původním názvem
        return result

    def delete_local_only(self, name: str, paths: Iterable[str]) -> list[str]:
        """Přesune soubory ze složky „Jen místně“ do Koše Windows (ne trvalé smazání)."""
        if self.server_dir(name).exists():
            raise SyncError(f"Projekt {name} je na serveru – použijte mazání v aktivovaném projektu.")
        local = self.local_dir(name)
        existing = [p for p in dict.fromkeys(paths) if to_abs(local, p).is_file()]
        recycle.to_recycle_bin([to_abs(local, p) for p in existing])
        for p in existing:
            remove_empty_parents(local, p)
        return existing

    # --- přejmenování projektu -------------------------------------------------

    def rename_project(self, name: str, new_name: str) -> None:
        """Přejmenuje složku projektu na síťovém úložišti a zapíše to do databáze projektu.

        Projekt nesmí mít nikdo aktivovaný. Místní úložiště se nemění.
        """
        new_name = new_name.strip()
        if new_name == name:
            return
        error = validate_project_name(new_name)
        if error:
            raise SyncError(error)
        server = self.server_dir(name)
        target = self.server_dir(new_name)
        if not server.is_dir():
            raise SyncError(f"Projekt neexistuje: {server}")
        case_only = new_name.lower() == name.lower()
        if not case_only and target.exists():
            raise SyncError(f"Na serveru už projekt {new_name} existuje.")
        if not case_only and self.local_dir(new_name).exists():
            raise SyncError(
                f"V místním úložišti už existuje složka {new_name}.\n"
                "Zvolte jiný název, nebo tuto místní složku nejdřív přejmenujte či odstraňte."
            )

        with WriteLock(server, self.user, self.pc) as wl:
            users = active_users(server)
            if db_path(server).exists():
                with ProjectDB.for_project(server) as db:
                    users += [LockInfo(r["user"], r["pc"], r["activated_at"]) for r in db.active_sessions()]
            if users:
                raise SyncError(f"Projekt má aktivovaný {users[0].label}. "
                                "Přejmenovat lze jen projekt, který nikdo nemá aktivovaný.")
            try:
                server.rename(target)
            except FileExistsError:
                raise SyncError(f"Na serveru už projekt {new_name} existuje.") from None
            except PermissionError:
                raise SyncError(
                    "Složku projektu nelze přejmenovat – některý soubor nebo složka projektu je na "
                    "serveru otevřená (např. v Průzkumníku nebo jiné aplikaci)."
                ) from None
            wl.moved_to(target)
            with ProjectDB.for_project(target, create=True) as db:
                with db.conn:
                    db.add_commit(None, self.user, self.pc, dbm.PROJECT_RENAMED,
                                  f"Projekt přejmenován: {name} → {new_name}")

    # --- pomocné -----------------------------------------------------------

    @staticmethod
    def _already_uploaded(server: Path, local: Path, rel: str, sha256: str) -> bool:
        """Soubor už byl nahrán (např. při opakování přerušeného odevzdání)."""
        dst = to_abs(server, rel)
        try:
            s, l = dst.stat(), to_abs(local, rel).stat()
        except FileNotFoundError:
            return False
        # rychlé vyloučení podle velikosti/času, rozhoduje až obsah
        return same_stat(s.st_size, s.st_mtime, l.st_size, l.st_mtime) and file_hash(dst) == sha256

    def _upload(self, server: Path, local: Path, lf: FileInfo, backup_existing: bool,
                file_id: str | None = None) -> str | None:
        """Nahraje soubor na server; původní verzi případně zazálohuje. Vrací cestu zálohy.

        Nejdřív do dočasného souboru vedle cíle a ověří hash proti porovnání (plánu): soubor,
        který se mezitím změnil, se nenahraje (evidence by neodpovídala obsahu). Záloha
        a nahrazení až potom – při výpadku sítě během kopírování zůstane na serveru původní
        soubor."""
        rel = lf.path
        if self._already_uploaded(server, local, rel, lf.sha256):
            return None
        dst = to_abs(server, rel)
        stage = dst.with_name(dst.name + UPLOAD_SUFFIX)
        sha = copy_with_hash(to_abs(local, rel), stage)
        if sha != lf.sha256:
            stage.unlink(missing_ok=True)
            raise SyncError(changed_during_upload(rel))
        backup = None
        try:
            if backup_existing and dst.exists() and self.settings.backup_enabled:
                backup = self._backup(server, rel, file_id)
            if dst.exists():
                set_readonly(dst, False)  # soubor jen pro čtení nejde ve Windows nahradit
            os.replace(stage, dst)
        finally:
            stage.unlink(missing_ok=True)
        return backup

    @staticmethod
    def _backups_of(server: Path, file_id: str, db: ProjectDB | None = None) -> list[Path]:
        """Zálohy souboru podle jeho identity (z historie), od nejstarší – i ty pod dřívější
        cestou (před přejmenováním souboru nebo složky)."""
        def query(conn_db: ProjectDB) -> list[str]:
            rows = conn_db.conn.execute(
                "SELECT backup_path FROM changes WHERE file_id = ? AND backup_path IS NOT NULL "
                "ORDER BY change_id", (file_id,)).fetchall()
            return [r[0] for r in rows]
        if db is not None:
            paths = query(db)
        elif db_path(server).exists():
            with ProjectDB.for_project(server) as own:
                paths = query(own)
        else:
            paths = []
        prefix = f"{SPM_DIR}/backup/"
        return [to_abs(server, p) for p in dict.fromkeys(paths) if p.startswith(prefix)]

    @classmethod
    def _version_in_backups(cls, server: Path, file_id: str, sha256: str | None) -> bool:
        """Je verze souboru (podle hashe) mezi jeho zálohami?"""
        if not sha256:
            return False
        for path in cls._backups_of(server, file_id):
            try:
                if file_hash(path) == sha256:
                    return True
            except OSError:
                continue
        return False

    def _backup(self, server: Path, rel: str, file_id: str | None = None,
                db: ProjectDB | None = None) -> str:
        """Přesune soubor do .spm/backup/<cesta>.backupXY a ponechá jen posledních N záloh.

        Zálohy patří souboru (file_id), ne cestě: po přejmenování souboru nebo složky jdou nové
        zálohy do složky podle nového názvu a starší zůstávají pod původní cestou; počet N platí
        pro všechny dohromady – nejstarší se mažou i z původní cesty a prázdné složky záloh
        zaniknou. (Bez file_id se počítají zálohy se stejným názvem ve stejné složce.)"""
        root = server / SPM_DIR / "backup"
        src = to_abs(server, rel)
        backup_dir = to_abs(root, rel).parent
        backup_dir.mkdir(parents=True, exist_ok=True)
        pattern = re.compile(re.escape(src.name) + r"\.backup(\d+)$", re.IGNORECASE)
        in_dir = sorted(
            (int(m.group(1)), p) for p in backup_dir.iterdir() if (m := pattern.match(p.name))
        )
        number = in_dir[-1][0] + 1 if in_dir else 1  # číslo navazuje – nic se nepřepíše
        target = backup_dir / f"{src.name}.backup{number:02d}"
        os.replace(src, target)
        if file_id is not None:
            previous = [p for p in self._backups_of(server, file_id, db) if p.exists()]
        else:
            previous = [p for _, p in in_dir]
        keep = max(1, self.settings.backup_count)
        for old in (previous + [target])[:-keep]:
            old.unlink(missing_ok=True)
            remove_empty_parents(root, to_rel(root, old))
        return to_rel(server, target)

    @staticmethod
    def _to_trash(server: Path, session_id: int, rel: str) -> str | None:
        src = to_abs(server, rel)
        if not src.exists():
            return None
        target = to_abs(server / SPM_DIR / "trash" / str(session_id), rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, target)
        return to_rel(server, target)
