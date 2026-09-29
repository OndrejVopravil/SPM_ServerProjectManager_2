"""Aktivace a deaktivace projektů – hlavní logika aplikace (bez GUI)."""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import SPM_DIR
from . import db as dbm
from .config import Settings
from .db import DbFile, ProjectDB
from .identity import current_pc, current_user
from .lock import LockInfo, acquire_lock, read_lock, release_lock, update_lock
from .matcher import RenameSuggestion, similarity, suggest_renames
from .names import describe_chars, find_invalid_names, forbidden_in, format_invalid, invalid_rel_paths
from .scanner import (
    FileInfo,
    Progress,
    copy_with_hash,
    file_hash,
    list_files,
    remove_empty_parents,
    same_stat,
    scan,
    to_abs,
    to_rel,
)


class SyncError(Exception):
    pass


# Složky, do kterých aktivace odkládá dřívější místní kopii (viz activate) – nejsou to projekty.
ORIGINAL_COPY_RE = re.compile(r"_puvodni_\d{8}_\d{6}$")


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
    if ORIGINAL_COPY_RE.search(name):
        return "Název nesmí končit příponou _puvodni_<datum> (používá ji aplikace pro zálohy)."
    return None


@dataclass
class ProjectStatus:
    name: str
    lock: LockInfo | None
    local_exists: bool
    mine: bool
    local_only: bool = False  # složka je jen v místním úložišti (lze ji publikovat)

    @property
    def active(self) -> bool:
        return self.lock is not None


@dataclass
class DeactivationPlan:
    name: str
    session_id: int
    local_files: dict[str, FileInfo]
    unchanged: list[str]
    modified: list[tuple[DbFile, FileInfo]]
    missing: list[DbFile]                   # v databázi, ale lokálně chybí
    new: list[FileInfo]                     # lokálně, ale ne v databázi
    suggestions: list[RenameSuggestion]
    conflicts: list[str] = field(default_factory=list)
    invalid_names: list[str] = field(default_factory=list)  # názvy s nepovolenými znaky

    def default_assignments(self) -> dict[str, str | None]:
        """file_id chybějícího souboru -> nová cesta (nebo None = smazán)."""
        result: dict[str, str | None] = {f.file_id: None for f in self.missing}
        for s in self.suggestions:
            result[s.file_id] = s.new_path
        return result

    @property
    def has_changes(self) -> bool:
        return bool(self.modified or self.missing or self.new)


@dataclass
class DeactivationResult:
    renamed: int = 0
    modified: int = 0
    added: int = 0
    deleted: int = 0
    local_deleted: bool = False


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
            lock = read_lock(entry)
            result.append(ProjectStatus(
                name=entry.name,
                lock=lock,
                local_exists=self.local_dir(entry.name).exists(),
                mine=bool(lock and lock.is_mine(self.user, self.pc)),
            ))
        for name in self.local_only_projects(server_names):
            result.append(ProjectStatus(name=name, lock=None, local_exists=True, mine=False,
                                        local_only=True))
        result.sort(key=lambda p: p.name.lower())
        return result

    def local_only_projects(self, server_names: set[str]) -> list[str]:
        """Složky v místním úložišti, které na serveru nemají projekt (porovnání bez ohledu
        na velikost písmen, jako ve Windows)."""
        if not self.local_root.is_dir():
            return []
        return [
            e.name for e in self.local_root.iterdir()
            if e.is_dir() and not e.name.startswith(".")
            and e.name.lower() not in server_names
            and not ORIGINAL_COPY_RE.search(e.name)
        ]

    def _require_my_lock(self, name: str) -> LockInfo:
        lock = read_lock(self.server_dir(name))
        if lock is None:
            raise SyncError(f"Projekt {name} není aktivován.")
        if not lock.is_mine(self.user, self.pc):
            raise SyncError(f"Projekt {name} je aktivován uživatelem {lock.user} ({lock.pc}).")
        return lock

    # --- aktivace ----------------------------------------------------------

    def activate(self, name: str, progress: Progress | None = None) -> Path | None:
        """Zamkne projekt a zkopíruje jej do místního úložiště.

        Vrací cestu, kam byla přesunuta původní místní složka (pokud existovala).
        """
        server = self.server_dir(name)
        local = self.local_dir(name)
        if not server.is_dir():
            raise SyncError(f"Projekt neexistuje: {server}")
        self.check_names(server, name)
        self.local_root.mkdir(parents=True, exist_ok=True)

        lock = acquire_lock(server, self.user, self.pc)
        moved_to: Path | None = None
        try:
            if local.is_dir() and not any(local.iterdir()):
                local.rmdir()
            if local.exists():
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                moved_to = local.with_name(f"{name}_puvodni_{stamp}")
                local.rename(moved_to)

            files = list_files(server, self.settings.ignore_patterns)
            copied: list[FileInfo] = []
            for i, src in enumerate(files):
                rel = to_rel(server, src)
                if progress:
                    progress(i, len(files), rel)
                st = src.stat()
                sha = copy_with_hash(src, to_abs(local, rel))
                copied.append(FileInfo(rel, st.st_size, st.st_mtime, sha))
            local.mkdir(parents=True, exist_ok=True)
            if progress:
                progress(len(files), len(files), "Zápis do databáze…")

            with ProjectDB.for_project(server, create=True) as db:
                with db.conn:
                    session_id = db.start_session(self.user, self.pc)
                    self._sync_baseline(db, session_id, copied)
            lock.session_id = session_id
            update_lock(server, lock)
        except BaseException:
            shutil.rmtree(local, ignore_errors=True)
            if moved_to is not None:
                moved_to.rename(local)
                moved_to = None
            release_lock(server)
            raise
        return moved_to

    # --- publikování ----------------------------------------------------------

    def publish(self, name: str, comment: str, progress: Progress | None = None) -> bool:
        """Přesune projekt, který existuje jen v místním úložišti, na server.

        Soubory se nahrají, ověří (hash obsahu na serveru) a zapíší do nové databáze
        projektu. Teprve potom se místní složka smaže. Vrací True, pokud se ji podařilo
        smazat celou (jinak je některý soubor otevřen v jiné aplikaci).
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
        self.check_names(local, name)
        try:
            server.mkdir()  # atomické – selže, pokud projekt mezitím založil někdo jiný
        except FileExistsError:
            raise SyncError(f"Na serveru už projekt {name} existuje.") from None

        # nikdo ho nesmí aktivovat, než se publikování dokončí; pokud zámek mezitím získal
        # někdo jiný (aktivoval prázdnou složku), složku nemažeme
        acquire_lock(server, self.user, self.pc)
        try:
            files = list_files(local, self.settings.ignore_patterns)
            uploaded: list[FileInfo] = []
            for i, src in enumerate(files):
                rel = to_rel(local, src)
                if progress:
                    progress(i, len(files), rel)
                dst = to_abs(server, rel)
                sha = copy_with_hash(src, dst)
                if file_hash(dst) != sha:
                    raise SyncError(f"Kontrola nahraného souboru selhala:\n{rel}")
                st = dst.stat()
                uploaded.append(FileInfo(rel, st.st_size, st.st_mtime, sha))
            if progress:
                progress(len(files), len(files), "Zápis do databáze…")

            with ProjectDB.for_project(server, create=True) as db:
                with db.conn:
                    sid = db.start_session(self.user, self.pc)
                    for fi in uploaded:
                        fid = db.add_file(fi.path, fi.size, fi.mtime, fi.sha256)
                        db.add_change(sid, fid, dbm.PUBLISHED, None, fi.path, fi.size, fi.mtime, fi.sha256)
                    db.finish_session(sid, comment)
            release_lock(server)
        except BaseException:
            # projekt jsme právě založili a byl zamčený – nikdo jiný ho nemohl použít
            shutil.rmtree(server, ignore_errors=True)
            raise

        shutil.rmtree(local, ignore_errors=True)
        return not local.exists()

    # --- kontrola názvů -------------------------------------------------------

    def invalid_names(self, project: ProjectStatus) -> list[str]:
        """Názvy s nepovolenými znaky uvnitř projektu (u „Jen místně“ v místní složce)."""
        root = self.local_dir(project.name) if project.local_only else self.server_dir(project.name)
        try:
            return find_invalid_names(root, self.settings.ignore_patterns)
        except OSError:
            return []

    def check_names(self, root: Path, name: str) -> None:
        invalid = find_invalid_names(root, self.settings.ignore_patterns)
        if invalid:
            raise SyncError(
                f"Projekt {name} obsahuje složky nebo soubory s nepovolenými znaky "
                "(česká diakritika, mezera, ( ) & , . ; = ^). Nejdřív je přejmenujte:\n\n"
                + format_invalid(invalid)
            )

    # --- přejmenování ---------------------------------------------------------

    def rename_project(self, name: str, new_name: str) -> None:
        """Přejmenuje složku projektu na síťovém úložišti a zapíše to do databáze projektu.

        Projekt nesmí být aktivován. Po dobu přejmenování se zamkne, aby ho nikdo
        neaktivoval. Místní úložiště se nemění.
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

        lock = read_lock(server)
        if lock is not None:
            raise SyncError(f"Projekt je aktivován uživatelem {lock.user} ({lock.pc}). "
                            "Přejmenovat lze jen deaktivovaný projekt.")
        acquire_lock(server, self.user, self.pc)
        try:
            server.rename(target)
        except FileExistsError:
            release_lock(server)
            raise SyncError(f"Na serveru už projekt {new_name} existuje.") from None
        except PermissionError:
            release_lock(server)
            raise SyncError(
                "Složku projektu nelze přejmenovat – některý soubor nebo složka projektu je na "
                "serveru otevřená (např. v Průzkumníku nebo jiné aplikaci)."
            ) from None
        except BaseException:
            release_lock(server)
            raise

        try:
            with ProjectDB.for_project(target, create=True) as db:
                with db.conn:
                    sid = db.start_session(self.user, self.pc)
                    db.finish_session(sid, f"Projekt přejmenován: {name} → {new_name}",
                                      status=dbm.PROJECT_RENAMED)
        finally:
            release_lock(target)

    @staticmethod
    def _sync_baseline(db: ProjectDB, session_id: int, server_files: list[FileInfo]) -> None:
        """Srovná evidenci v databázi se skutečným stavem serveru v okamžiku aktivace."""
        baseline = db.active_files()
        on_server = {fi.path for fi in server_files}
        # Soubory, které z evidence zmizely – mohly být přejmenovány ručně přímo na serveru.
        # Stejný obsah (hash) = stejný soubor, historie se zachová pod novým názvem.
        missing_by_hash: dict[str, list[DbFile]] = {}
        for path, bf in baseline.items():
            if path not in on_server:
                missing_by_hash.setdefault(bf.sha256, []).append(bf)

        renamed: set[str] = set()
        for fi in server_files:
            bf = baseline.get(fi.path)
            if bf is not None:
                db.update_file(bf.file_id, fi.path, fi.size, fi.mtime, fi.sha256)
                if bf.sha256 != fi.sha256:
                    db.add_change(session_id, bf.file_id, dbm.EXTERNAL_MODIFIED, fi.path, fi.path,
                                  fi.size, fi.mtime, fi.sha256)
                continue
            candidates = missing_by_hash.get(fi.sha256)
            if candidates:
                # u více kandidátů se stejným obsahem vybrat ten s nejpodobnějším názvem
                old = max(candidates, key=lambda c: similarity(c, fi)[0])
                candidates.remove(old)
                renamed.add(old.path)
                db.update_file(old.file_id, fi.path, fi.size, fi.mtime, fi.sha256)
                db.add_change(session_id, old.file_id, dbm.EXTERNAL_RENAMED, old.path, fi.path,
                              fi.size, fi.mtime, fi.sha256)
            else:
                fid = db.add_file(fi.path, fi.size, fi.mtime, fi.sha256)
                db.add_change(session_id, fid, dbm.IMPORTED, None, fi.path, fi.size, fi.mtime, fi.sha256)
        for path, bf in baseline.items():
            if path not in on_server and path not in renamed:
                db.mark_deleted(bf.file_id)
                db.add_change(session_id, bf.file_id, dbm.EXTERNAL_DELETED, path, None)

    # --- deaktivace ----------------------------------------------------------

    def plan_deactivation(self, name: str, progress: Progress | None = None) -> DeactivationPlan:
        """Porovná místní kopii s evidencí a připraví návrh změn (nic nezapisuje)."""
        server = self.server_dir(name)
        local = self.local_dir(name)
        lock = self._require_my_lock(name)
        if not local.is_dir():
            raise SyncError(f"Místní kopie projektu neexistuje:\n{local}")

        with ProjectDB.for_project(server) as db:
            baseline = db.active_files()
            session_id = lock.session_id or db.open_session(self.user, self.pc)
        if session_id is None:
            raise SyncError("Aktivace nebyla dokončena. Zrušte ji a projekt aktivujte znovu.")

        local_files = scan(local, self.settings.ignore_patterns, progress)

        unchanged, modified, missing = [], [], []
        for path, bf in baseline.items():
            lf = local_files.get(path)
            if lf is None:
                missing.append(bf)
            elif lf.sha256 != bf.sha256:
                modified.append((bf, lf))
            else:
                unchanged.append(path)
        new = [lf for path, lf in local_files.items() if path not in baseline]
        suggestions = suggest_renames(missing, new, self.settings.rename_threshold)

        conflicts = []
        for path, bf in baseline.items():
            sp = to_abs(server, path)
            try:
                st = sp.stat()
            except FileNotFoundError:
                conflicts.append(f"{path} – mezitím smazán na serveru")
                continue
            if not same_stat(st.st_size, st.st_mtime, bf.size, bf.mtime):
                conflicts.append(f"{path} – mezitím změněn na serveru (bude přepsán)")
        for lf in new:
            if to_abs(server, lf.path).exists():
                conflicts.append(f"{lf.path} – na serveru mezitím vznikl soubor se stejným názvem")

        return DeactivationPlan(name, session_id, local_files, unchanged, modified, missing,
                                new, suggestions, conflicts, invalid_rel_paths(local_files))

    def deactivate(
        self,
        plan: DeactivationPlan,
        assignments: dict[str, str | None],
        comment: str,
        progress: Progress | None = None,
    ) -> DeactivationResult:
        """Nahraje změny na server, zapíše je do databáze a uvolní zámek.

        assignments: file_id chybějícího souboru -> nová cesta (přejmenování), nebo None (smazán).
        """
        comment = comment.strip()
        if not comment:
            raise SyncError("Bez komentáře nelze projekt deaktivovat.")
        if plan.invalid_names:
            raise SyncError("Projekt obsahuje názvy s nepovolenými znaky – nejdřív je opravte:\n\n"
                            + format_invalid(plan.invalid_names))
        self._require_my_lock(plan.name)

        new_by_path = {lf.path: lf for lf in plan.new}
        targets = [p for p in assignments.values() if p]
        if len(targets) != len(set(targets)):
            raise SyncError("Jeden nový soubor je přiřazen k více původním souborům.")
        if any(p not in new_by_path for p in targets):
            raise SyncError("Přiřazení obsahuje soubor, který není mezi novými soubory.")

        server = self.server_dir(plan.name)
        local = self.local_dir(plan.name)
        sid = plan.session_id
        missing_by_id = {bf.file_id: bf for bf in plan.missing}
        renames = [(missing_by_id[fid], new_by_path[p]) for fid, p in assignments.items() if p]
        deleted = [bf for fid, bf in missing_by_id.items() if not assignments.get(fid)]
        added = [lf for lf in plan.new if lf.path not in set(targets)]

        total = 2 * len(renames) + len(plan.modified) + len(added) + len(deleted)
        step = 0

        def tick(text: str) -> None:
            nonlocal step
            if progress:
                progress(step, total, text)
            step += 1

        with ProjectDB.for_project(server) as db:
            db.set_session_status(sid, "uploading")
            ops: list[tuple] = []  # záznamy pro databázi, zapisují se na konci v jedné transakci

            # 1) Přejmenování – dvoufázově přes .spm/tmp, aby fungovala i záměna názvů (a <-> b)
            tmp_dir = server / SPM_DIR / "tmp"
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
                kind = dbm.RENAMED_MODIFIED if changed else dbm.RENAMED
                ops.append((kind, bf.file_id, bf.path, lf, backup))

            # 2) Upravené soubory
            for bf, lf in plan.modified:
                tick(f"Nahrávání {lf.path}")
                backup = self._upload(server, local, lf, backup_existing=True)
                ops.append((dbm.MODIFIED, bf.file_id, bf.path, lf, backup))

            # 3) Nové soubory
            for lf in added:
                tick(f"Nahrávání {lf.path}")
                if to_abs(server, lf.path).exists() and not self._already_uploaded(server, local, lf.path, lf.sha256):
                    self._to_trash(server, sid, lf.path)
                self._upload(server, local, lf, backup_existing=False)
                ops.append((dbm.ADDED, None, None, lf, None))

            # 4) Smazané soubory – na serveru se nemažou, ale přesouvají do .spm/trash/<sezení>/
            for bf in deleted:
                tick(f"Mazání {bf.path}")
                trash = self._to_trash(server, sid, bf.path)
                remove_empty_parents(server, bf.path)
                ops.append((dbm.DELETED, bf.file_id, bf.path, None, trash))

            if progress:
                progress(total, total, "Zápis do databáze…")
            with db.conn:
                for kind, file_id, old_path, lf, backup in ops:
                    if kind == dbm.DELETED:
                        db.mark_deleted(file_id)
                        db.add_change(sid, file_id, kind, old_path, None, backup_path=backup)
                        continue
                    st = to_abs(server, lf.path).stat()
                    if kind == dbm.ADDED:
                        file_id = db.add_file(lf.path, st.st_size, st.st_mtime, lf.sha256)
                    else:
                        db.update_file(file_id, lf.path, st.st_size, st.st_mtime, lf.sha256)
                    db.add_change(sid, file_id, kind, old_path, lf.path, st.st_size,
                                  st.st_mtime, lf.sha256, backup)
                db.finish_session(sid, comment)

        release_lock(server)
        shutil.rmtree(tmp_dir, ignore_errors=True)

        result = DeactivationResult(
            renamed=len(renames), modified=len(plan.modified), added=len(added), deleted=len(deleted)
        )
        if self.settings.delete_local_after_deactivate:
            shutil.rmtree(local, ignore_errors=True)
            result.local_deleted = not local.exists()
        return result

    def cancel_activation(self, name: str) -> None:
        """Zruší vlastní aktivaci bez nahrání změn. Místní kopie zůstane beze změny."""
        server = self.server_dir(name)
        lock = self._require_my_lock(name)
        self._close_session(server, lock, "Aktivace zrušena bez nahrání změn.")
        release_lock(server)

    def force_unlock(self, name: str) -> None:
        """Odebere zámek jiného uživatele (např. když je nedostupný). Změny u něj se nenahrají."""
        server = self.server_dir(name)
        lock = read_lock(server)
        if lock is None:
            return
        self._close_session(
            server, lock, f"Zámek uživatele {lock.user} ({lock.pc}) odebrán uživatelem {self.user}."
        )
        release_lock(server)

    # --- pomocné -----------------------------------------------------------

    def _close_session(self, server: Path, lock: LockInfo, comment: str) -> None:
        try:
            db = ProjectDB.for_project(server)
        except FileNotFoundError:
            return
        with db:
            sid = lock.session_id or db.open_session(lock.user, lock.pc)
            if sid is not None:
                with db.conn:
                    db.finish_session(sid, comment, status="cancelled")

    @staticmethod
    def _already_uploaded(server: Path, local: Path, rel: str, sha256: str) -> bool:
        """Soubor už byl nahrán (např. při opakování přerušené deaktivace)."""
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
