"""Zrcadlení souborů do projektu jiné záložky – příkaz MIRROR ve skriptu odevzdání.

Jednosměrně: zdrojový projekt (např. na O:) -> cílový projekt jiné záložky (např. na W:).
Skript předá soubory odevzdání (se stavem added / modified / renamed / deleted / unchanged)
a cestu každého z nich v cílovém projektu.

V cílovém projektu jsou zrcadlené soubory zapsané v tabulce mirror_files (zdroj = trvalá
identita zdrojového projektu, a file_id ve zdroji) – v cíli je nikdo nemůže zamknout ani
upravit. Zrcadlený soubor se v cíli pozná podle file_id ve zdroji, ne podle cesty: přejmenování
ve zdroji se v cíli provede jako přejmenování, smazání ve zdroji smaže soubor i v cíli (do koše
projektu).

Postup:
1. plán podle stavu cíle (bez zámku),
2. nové a změněné soubory se zkopírují do dočasných souborů v cíli a ověří hashem (bez zámku –
   dlouhé kopírování neblokuje ostatní uživatele cíle),
3. pod zámkem zápisu cíle se plán znovu ověří a provede (záloha, nahrazení, přejmenování,
   smazání do koše projektu) a zapíše do historie cíle jako „Zrcadlení“.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import db as dbm
from .db import DbFile, ProjectDB, db_path
from .lock import WriteLock
from .names import invalid_rel_paths
from .scanner import (
    TEMP_SUFFIX,
    Progress,
    copy_with_hash,
    file_hash,
    is_ignored,
    move_file,
    remove_empty_parents,
    to_abs,
)

STAGE_SUFFIX = ".mirror" + TEMP_SUFFIX  # dočasná kopie v cíli (scanner ji ignoruje)


class MirrorError(Exception):
    pass


class MirrorPermissionError(MirrorError):
    pass


@dataclass
class MirrorSource:
    """Zdrojový projekt zrcadlení."""
    key: str             # trvalá identita projektu (project_id) – klíč zdroje v cíli
    label: str           # cesta ke zdrojovému projektu (pro zobrazení)
    server_id: str = ""  # identita zdrojové síťové složky (název záložky u ostatních)
    server_name: str = ""
    legacy_key: str = ""  # dřívější klíč zdroje (cesta) – převede se na key


@dataclass
class MirrorItem:
    file_id: str          # soubor ve zdroji
    status: str           # added / modified / renamed / deleted / unchanged
    target_rel: str       # cesta v cílovém projektu
    sha256: str | None
    source: Path | None   # odkud kopírovat (None u smazaného)
    path: str = ""        # cesta ve zdroji (pro hlášení)


@dataclass
class MirrorResult:
    project: str
    added: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    renamed: list[str] = field(default_factory=list)      # "stará -> nová"
    deleted: list[str] = field(default_factory=list)
    adopted: list[str] = field(default_factory=list)      # v cíli už byl stejný obsah
    conflicts: list[str] = field(default_factory=list)    # nezrcadleno (s důvodem)
    created_project: bool = False
    # pro označení ve zdroji: identita cílového projektu a soubory, které jsou teď zrcadlené
    # (file_id ve zdroji -> cesta v cíli) / už nejsou (smazané)
    target_key: str = ""
    mirrored: dict[str, str] = field(default_factory=dict)
    removed: list[str] = field(default_factory=list)

    @property
    def changed(self) -> int:
        return len(self.added) + len(self.modified) + len(self.renamed) + len(self.deleted)

    @property
    def ok(self) -> bool:
        return not self.conflicts


def format_counts(res: MirrorResult) -> str:
    parts = [f"{label}: {len(items)}" for label, items in (
        ("nové", res.added), ("změněné", res.modified), ("přejmenované", res.renamed),
        ("smazané", res.deleted), ("převzaté", res.adopted)) if items]
    return ", ".join(parts) or "beze změny"


class Mirror:
    """Zrcadlení souborů jednoho zdrojového projektu do jednoho cílového projektu."""

    def __init__(self, source: MirrorSource, target_pm, project: str, comment: str = ""):
        self.source, self.target_pm, self.project = source, target_pm, project
        self.comment = comment
        # dočasné kopie v cíli mají v názvu tento kód – souběžné zrcadlení jiného uživatele
        # (nebo jiného okna) používá jiné názvy a nesrazí se s tímto
        self._stage_token = uuid.uuid4().hex[:8]

    @property
    def target(self) -> Path:
        return self.target_pm.server_dir(self.project)

    def run(self, items: list[MirrorItem], progress: Progress | None = None) -> MirrorResult:
        try:
            return self._run(items, progress)
        except PermissionError as e:
            raise MirrorPermissionError(
                "Nemáte oprávnění zapisovat do cílové síťové složky:\n"
                f"{self.target_pm.server_root}\n\n({e})") from None

    def _run(self, items: list[MirrorItem], progress: Progress | None) -> MirrorResult:
        res = MirrorResult(self.project)
        target = self.target
        if not self.target_pm.server_root.is_dir():
            raise MirrorError(f"Cílová síťová složka není dostupná:\n{self.target_pm.server_root}")
        targets: dict[str, MirrorItem] = {}
        for item in items:
            if item.status == "deleted":
                continue
            other = targets.get(item.target_rel.lower())
            if other is not None and other.file_id != item.file_id:
                raise MirrorError(f"Do cíle by se zrcadlily dva soubory na stejnou cestu "
                                  f"{item.target_rel}:\n{other.path}\n{item.path}")
            targets[item.target_rel.lower()] = item
        if not targets and not db_path(target).exists():
            return res  # nic ke zrcadlení a cíl ještě neexistuje – nezakládat prázdný projekt
        if not target.exists():
            target.mkdir()
            res.created_project = True

        # 1) plán podle stavu cíle (bez zámku); 2) kopírování do dočasných souborů
        state = self._target_state(target)
        staged: dict[str, Path] = {}  # source file_id -> dočasná kopie v cíli
        try:
            todo = [i for i in items if i.status != "deleted" and self._needs_copy(i, state, target)]
            for n, item in enumerate(todo):
                if progress:
                    progress(n, len(todo), item.target_rel)
                problem = self._target_problem(item.target_rel)
                if problem:
                    res.conflicts.append(f"{item.target_rel} – {problem}")
                    continue
                dst = to_abs(target, item.target_rel)
                stage = dst.with_name(f"{dst.name}.{self._stage_token}{STAGE_SUFFIX}")
                try:
                    sha = copy_with_hash(item.source, stage)
                except FileNotFoundError:
                    res.conflicts.append(f"{item.path} – zdrojový soubor nebyl nalezen")
                    continue
                if item.sha256 and sha != item.sha256:
                    stage.unlink(missing_ok=True)
                    res.conflicts.append(f"{item.path} – obsah se mezitím změnil (neodpovídá "
                                         "odevzdané verzi)")
                    continue
                staged[item.file_id] = stage
            # 3) provedení pod zámkem cíle
            self._apply(target, items, staged, res)
        finally:
            for stage in staged.values():
                stage.unlink(missing_ok=True)
        return res

    def _target_state(self, target: Path) -> dict:
        files: dict[str, DbFile] = {}
        mirrored: dict[str, tuple[str, str]] = {}
        if db_path(target).exists():
            with ProjectDB.for_project(target) as db:
                files = db.active_files(include_pending=True)
                mirrored = db.mirrored()
        by_id = {f.file_id: f for f in files.values()}
        return {"files": files, "by_id": by_id, "mirrored": mirrored}

    def _keys(self) -> set[str]:
        return {self.source.key} | ({self.source.legacy_key} if self.source.legacy_key else set())

    def _mirror_of(self, state: dict, source_fid: str) -> DbFile | None:
        keys = self._keys()
        for fid, (src, sfid) in state["mirrored"].items():
            if src in keys and sfid == source_fid and fid in state["by_id"]:
                return state["by_id"][fid]
        return None

    def _needs_copy(self, item: MirrorItem, state: dict, target: Path) -> bool:
        current = self._mirror_of(state, item.file_id)
        if current is None:
            existing = state["files"].get(item.target_rel)
            dst = to_abs(target, item.target_rel)
            if existing is not None and existing.sha256 == item.sha256 and dst.exists():
                return False  # v cíli už je stejný obsah – jen převzít pod správu zrcadla
            if existing is None and dst.is_file():
                return file_hash(dst) != item.sha256
            return True
        return current.sha256 != item.sha256 or not to_abs(target, current.path).exists()

    def _target_problem(self, rel: str) -> str | None:
        settings = self.target_pm.settings
        if any(is_ignored(part, settings.ignore_patterns) for part in rel.split("/")):
            return "v cíli patří mezi ignorované soubory"
        if settings.check_forbidden_chars and invalid_rel_paths([rel]):
            return "název obsahuje znaky, které cíl nepovoluje"
        return None

    def _apply(self, target: Path, items: list[MirrorItem], staged: dict[str, Path],
               res: MirrorResult) -> None:
        tpm = self.target_pm
        key = self.source.key
        with WriteLock(target, tpm.user, tpm.pc):
            with ProjectDB.for_project(target, create=True) as db:
                db.ensure_mirror_tables()
                with db.conn:
                    if self.source.legacy_key:
                        db.rekey_mirror_source(self.source.legacy_key, key)
                    db.set_mirror_source(key, self.source.label, self.source.server_id,
                                         self.source.server_name)
                    files = db.active_files(include_pending=True)
                    by_id = {f.file_id: f for f in files.values()}
                    owners = db.lock_owners()
                    mirrored = db.mirrored()
                    mine = {sfid: fid for fid, (s, sfid) in mirrored.items() if s == key}
                    comment = f"Zrcadleno z {self.source.label}"
                    if self.comment:
                        comment += f": {self.comment}"
                    commit_id = db.add_commit(None, tpm.user, tpm.pc, dbm.MIRROR, comment)
                    trash_id = f"mirror_{commit_id}"
                    res.target_key = db.ensure_project_id()

                    def blocked(fid: str | None, rel: str) -> bool:
                        """Soubor v cíli nelze přepsat: drží ho někdo, nebo patří jinému zdroji."""
                        if fid is not None and fid in owners:
                            res.conflicts.append(f"{rel} – v cíli ho drží {owners[fid].label}")
                            return True
                        if fid is not None and fid in mirrored and mirrored[fid][0] != key:
                            res.conflicts.append(f"{rel} – v cíli ho spravuje zrcadlo z "
                                                 f"{mirrored[fid][0]}")
                            return True
                        return False

                    for item in items:
                        sfid = item.file_id
                        fid = mine.get(sfid)
                        cur = by_id.get(fid) if fid else None
                        if item.status == "deleted":
                            if cur is None:
                                res.removed.append(sfid)
                                continue
                            if blocked(fid, cur.path):
                                continue
                            trash = tpm._to_trash(target, trash_id, cur.path)
                            remove_empty_parents(target, cur.path)
                            db.mark_deleted(fid)
                            db.unset_mirrored(fid)
                            db.add_change(commit_id, fid, dbm.DELETED, cur.path, None, backup_path=trash)
                            res.deleted.append(cur.path)
                            res.removed.append(sfid)
                            del by_id[fid]
                            files.pop(cur.path, None)
                            continue
                        rel = item.target_rel
                        if cur is not None:  # už zrcadlený
                            if blocked(fid, cur.path):
                                continue
                            old = cur.path
                            if old != rel:  # přejmenován / přesunut ve zdroji (nebo jiné mapování)
                                occupant = files.get(rel)
                                if occupant is not None and occupant.file_id != fid:
                                    res.conflicts.append(f"{rel} – v cíli už existuje jiný soubor")
                                    continue
                                move_file(to_abs(target, old), to_abs(target, rel))
                                remove_empty_parents(target, old)
                                res.renamed.append(f"{old} -> {rel}")
                                files.pop(old, None)
                                files[rel] = cur
                            res.mirrored[sfid] = rel
                            if sfid in staged:
                                backup = self._replace(target, staged.pop(sfid), rel)
                                res.modified.append(rel)
                                kind = dbm.RENAMED_MODIFIED if old != rel else dbm.MODIFIED
                            elif old != rel:
                                backup, kind = None, dbm.RENAMED
                            else:
                                continue
                            st = to_abs(target, rel).stat()
                            db.update_file(fid, rel, st.st_size, st.st_mtime, item.sha256)
                            db.add_change(commit_id, fid, kind, old, rel, st.st_size,
                                          st.st_mtime, item.sha256, backup)
                            continue
                        # ještě nezrcadlený: v cíli na stejné cestě nic / jiný soubor / stejný obsah
                        existing = files.get(rel)
                        if existing is not None and blocked(existing.file_id, rel):
                            continue
                        if sfid not in staged:
                            dst = to_abs(target, rel)
                            if not dst.is_file():
                                continue  # kopírování selhalo (už je v konfliktech)
                            st = dst.stat()
                            if existing is None:  # na disku stejný obsah, ale mimo evidenci
                                fid = db.add_file(rel, st.st_size, st.st_mtime, item.sha256)
                                db.add_change(commit_id, fid, dbm.IMPORTED, None, rel, st.st_size,
                                              st.st_mtime, item.sha256)
                            else:
                                fid = existing.file_id
                            db.set_mirrored(fid, key, sfid)
                            res.adopted.append(rel)
                            res.mirrored[sfid] = rel
                            continue
                        backup = self._replace(target, staged.pop(sfid), rel)
                        st = to_abs(target, rel).stat()
                        if existing is None:
                            fid = db.add_file(rel, st.st_size, st.st_mtime, item.sha256)
                            kind = dbm.ADDED
                            res.added.append(rel)
                        else:
                            fid = existing.file_id
                            db.update_file(fid, rel, st.st_size, st.st_mtime, item.sha256)
                            kind = dbm.MODIFIED
                            res.modified.append(rel)
                        db.set_mirrored(fid, key, sfid)
                        res.mirrored[sfid] = rel
                        files[rel] = DbFile(fid, rel, st.st_size, st.st_mtime, item.sha256)
                        db.add_change(commit_id, fid, kind, None if kind == dbm.ADDED else rel,
                                      rel, st.st_size, st.st_mtime, item.sha256, backup)

                    if not db.changes(commit_id):
                        db.conn.execute("DELETE FROM commits WHERE commit_id = ?", (commit_id,))

    def _replace(self, target: Path, stage: Path, rel: str) -> str | None:
        """Nasadí ověřenou dočasnou kopii; původní soubor v cíli zazálohuje."""
        dst = to_abs(target, rel)
        backup = None
        if dst.exists() and self.target_pm.settings.backup_enabled:
            backup = self.target_pm._backup(target, rel)
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.replace(stage, dst)
        return backup
