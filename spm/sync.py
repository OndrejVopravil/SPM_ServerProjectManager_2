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
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator

from . import SPM_DIR, recycle
from . import db as dbm
from .comment import MAX_LENGTH as COMMENT_MAX_LENGTH
from .comment import ProjectComment, normalize_text, read_comment, write_comment
from .config import Settings
from .db import DbFile, LockOwner, ProjectDB, db_path
from .identity import current_pc, current_user
from .lock import LockInfo, WriteLock, active_users, add_marker, now_iso, remove_marker
from .manifest import Entry, Manifest
from .matcher import RenameSuggestion, similarity, suggest_renames
from .names import describe_chars, find_invalid_names, forbidden_in, format_invalid, invalid_rel_paths
from . import restore
from .restore import RESTORE_DIR_RE, RestoreCatalog, RestorePoint, RestoreResult
from .scanner import (
    FileInfo,
    Progress,
    copy_with_hash,
    file_hash,
    list_files,
    move_file,
    remove_empty_parents,
    rmtree,
    same_stat,
    scan,
    set_readonly,
    to_abs,
    to_rel,
)


class SyncError(Exception):
    pass


# Složky, do kterých aktivace odkládá dřívější místní kopii (viz activate) – nejsou to projekty.
ORIGINAL_COPY_RE = re.compile(r"_puvodni_\d{8}_\d{6}$")
# Dočasná složka na serveru při publikování (tečka na začátku = v seznamu projektů se nezobrazí)
PUBLISH_TMP_PREFIX = ".spm_publish_"

INVALID_NAME_CHARS = set('<>:"/\\|?*')
RESERVED_NAMES = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                  *(f"lpt{i}" for i in range(1, 10))}


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

    @property
    def active(self) -> bool:
        return bool(self.users)


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
    mirror: str | None = None    # soubor spravuje zrcadlo z jiného serveru (zdrojový projekt)

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


def mirror_lock_error(paths: list[str], sources: list[str]) -> str:
    return ("Soubory spravuje zrcadlo z jiného serveru – nelze je zamknout ani upravit zde, "
            "změny se dělají ve zdroji:\n" + "\n".join(
                f"{p} – zrcadlo z {s}" for p, s in list(zip(paths, sources))[:15]))


def new_key(path: str) -> str:
    """Klíč nového souboru ve výběru pro odevzdání (ostatní položky mají klíč = file_id)."""
    return "new:" + path


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


@dataclass
class ActivationResult:
    moved_to: Path | None = None    # kam se odložila původní místní složka
    external: list[str] = field(default_factory=list)  # změny na serveru mimo aplikaci (podle hashe)


@dataclass
class PublishResult:
    published: int = 0
    local_deleted: bool = False      # celá místní složka odstraněna (publikováno vše)
    leftover: Path | None = None     # kam se odložily nepublikované soubory


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
        if not self.server_root.is_dir():
            raise SyncError(f"Síťová složka není dostupná:\n{self.server_root}")
        result = []
        server_names: set[str] = set()
        for entry in self.server_root.iterdir():
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            server_names.add(entry.name.lower())
            result.append(self._server_status(entry.name))
        for name in self.local_only_projects(server_names):
            result.append(ProjectStatus(name=name, users=[], local_exists=True, mine=False,
                                        local_only=True))
        # projekty, které mám aktivované já, vždy nahoře; jinak abecedně
        result.sort(key=lambda p: (not p.mine, p.name.lower()))
        return result

    def _server_status(self, name: str) -> ProjectStatus:
        server = self.server_dir(name)
        users = active_users(server)
        others = [u for u in users if not u.is_mine(self.user, self.pc)]
        return ProjectStatus(name=name, users=users, local_exists=self.local_dir(name).exists(),
                             mine=len(others) < len(users), others=others,
                             comment=read_comment(server))

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
        if db_path(server).exists():
            with ProjectDB.for_project(server) as db:
                files = db.active_files(include_pending=True)
                owners = db.lock_owners()
                mirrored = db.mirrored()
        result = []
        for path in paths:
            f = files.get(path)
            owner = owners.get(f.file_id) if f else None
            source = mirrored.get(f.file_id) if f else None
            result.append(FileEntry(path, owner, bool(owner and owner.is_me(self.user, self.pc)),
                                    mirror=source[0] if source else None))
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
                        raise SyncError(mirror_lock_error([p], [mirrored[f.file_id][0]]))
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
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                moved_to = local.with_name(f"{name}_puvodni_{stamp}")
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
                if changed_locally or to_abs(local, f.path).exists():
                    result.conflicts.append(f"{lpath} – na serveru přejmenován na {f.path}, "
                                            "ale místní kopie je změněná")
                    continue
                move_file(lp, to_abs(local, f.path))
                remove_empty_parents(local, lpath)
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
                    blocked, [mirrored[files[p].file_id][0] for p in blocked]))
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
                        conflicts.append(f"{path} – smazán na serveru mimo aplikaci")
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
            if f is not None and not (f.file_id in mine and f.pending):
                conflicts.append(f"{path} – na serveru už je soubor se stejným názvem (nelze nahrát)")
                unsavable.append(path)
                continue
            new.append(lf)

        suggestions = suggest_renames(missing, new, self.settings.rename_threshold)
        invalid = self._invalid_paths([lf.path for lf in new] + [lf.path for _, lf in modified])
        return UploadPlan(name, sid, local_files, unchanged, modified, missing, new,
                          locked_unchanged, suggestions, conflicts, invalid, unsavable)

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
    ) -> UploadResult:
        """Odevzdá vybrané změny na server a uvolní odevzdané soubory.

        assignments: file_id chybějícího souboru -> nová cesta (přejmenování) / None (smazán).
        selected:    klíče položek k odevzdání (file_id, u nových souborů new_key(cesta));
                     None = vše. Při end_session se vždy odevzdá vše a uvolní všechny zámky.
        """
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
        has_changes = bool(renames or deleted or modified or added)

        if has_changes and not comment:
            raise SyncError("Bez komentáře nelze změny odevzdat.")
        invalid = self._invalid_paths([lf.path for _, lf in renames + modified] + [lf.path for lf in added])
        if invalid:
            raise SyncError("Soubory mají názvy s nepovolenými znaky – nejdřív je opravte:\n\n"
                            + format_invalid(invalid))
        if not (has_changes or release_only or end_session):
            raise SyncError("Není vybráno nic k odevzdání.")

        server = self.server_dir(plan.name)
        local = self.local_dir(plan.name)
        sid = plan.session_id

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
            db.set_pending([bf.file_id for bf, _ in renames + modified] + [bf.file_id for bf in deleted])

        # 2) Soubory na serveru
        total = 2 * len(renames) + len(modified) + len(added) + len(deleted)
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
            for bf, lf in renames:
                tick(f"Přejmenování → {lf.path}")
                tmp = tmp_dir / bf.file_id
                dst = to_abs(server, lf.path)
                if tmp.exists():
                    if dst.exists():
                        self._to_trash(server, sid, lf.path)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(tmp, dst)
                remove_empty_parents(server, bf.path)
                changed = bf.sha256 != lf.sha256
                backup = None
                if changed or not dst.exists():
                    backup = self._upload(server, local, lf, backup_existing=changed)
                ops.append((dbm.RENAMED_MODIFIED if changed else dbm.RENAMED, bf.file_id, bf.path, lf, backup))

        for bf, lf in modified:
            tick(f"Nahrávání {lf.path}")
            backup = self._upload(server, local, lf, backup_existing=True)
            ops.append((dbm.MODIFIED, bf.file_id, bf.path, lf, backup))

        for lf in added:
            tick(f"Nahrávání {lf.path}")
            self._upload(server, local, lf, backup_existing=False)
            ops.append((dbm.ADDED, new_ids[lf.path], None, lf, None))

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
        with self._write(server) as db:
            self._my_session(db, sid)
            commit_id = db.add_commit(sid, self.user, self.pc, dbm.UPLOAD, comment) if has_changes else None
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
                              auto_deactivated=auto_end, note=auto_note)
        if end_session or auto_end:
            remove_marker(server, self.user, self.pc)
            if self.settings.delete_local_after_deactivate:
                rmtree(local)
                result.local_deleted = not local.exists()
            else:
                manifest.save(local)
        else:
            manifest.save(local)
        return result

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
                    for fi in uploaded:
                        fid = db.add_file(fi.path, fi.size, fi.mtime, fi.sha256)
                        db.add_change(cid, fid, dbm.PUBLISHED, None, fi.path, fi.size, fi.mtime, fi.sha256)
            try:
                tmp.rename(server)
            except FileExistsError:
                raise SyncError(f"Na serveru už projekt {name} existuje.") from None
        except BaseException:
            rmtree(tmp)
            raise

        result = PublishResult(published=len(selected))
        if len(selected) == len(all_files):
            rmtree(local)
            result.local_deleted = not local.exists()
            return result
        # část: publikované soubory jsou na serveru (ověřené) -> z místní složky pryč,
        # zbytek odložit, aby se nepletl s místní kopií nového serverového projektu
        for rel in selected:
            to_abs(local, rel).unlink(missing_ok=True)
            remove_empty_parents(local, rel)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        leftover = local.with_name(f"{name}_puvodni_{stamp}")
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

    def _upload(self, server: Path, local: Path, lf: FileInfo, backup_existing: bool) -> str | None:
        """Nahraje soubor na server; původní verzi případně zazálohuje. Vrací cestu zálohy."""
        rel = lf.path
        if self._already_uploaded(server, local, rel, lf.sha256):
            return None
        dst = to_abs(server, rel)
        backup = None
        if backup_existing and dst.exists() and self.settings.backup_enabled:
            backup = self._backup(server, rel)
        copy_with_hash(to_abs(local, rel), dst)
        return backup

    def _backup(self, server: Path, rel: str) -> str:
        """Přesune soubor do .spm/backup/<cesta>.backupXY a ponechá jen posledních N záloh."""
        src = to_abs(server, rel)
        backup_dir = to_abs(server / SPM_DIR / "backup", rel).parent
        backup_dir.mkdir(parents=True, exist_ok=True)
        pattern = re.compile(re.escape(src.name) + r"\.backup(\d+)$", re.IGNORECASE)
        existing = sorted(
            (int(m.group(1)), p) for p in backup_dir.iterdir() if (m := pattern.match(p.name))
        )
        number = existing[-1][0] + 1 if existing else 1
        target = backup_dir / f"{src.name}.backup{number:02d}"
        os.replace(src, target)
        keep = max(1, self.settings.backup_count)
        for _, old in (existing + [(number, target)])[:-keep]:
            old.unlink(missing_ok=True)
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
