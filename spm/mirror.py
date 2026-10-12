"""Zrcadlení vybraných souborů z projektů jedné záložky do stejnojmenných projektů jiné záložky.

Jednosměrně: zdroj (např. O:) -> cíl (např. W:). Pravidla jsou osobní nastavení uživatele
(Settings.mirror_rules); zdroj i cíl jsou záložky aplikace.

Pravidlo vybírá soubory podle vzorů (zástupné znaky * a ?, bez ohledu na velikost písmen,
více vzorů oddělených středníkem = „nebo“; mezi podmínkami platí „a zároveň“):
- files:    vzory názvu souboru (prázdné = všechny soubory),
- folders:  soubor musí ležet (v jakékoliv hloubce) ve složce odpovídající vzoru (prázdné = kdekoliv),
- exclude:  soubor nesmí ležet ve složce odpovídající vzoru,
- projects: vzory názvu projektu (prázdné = všechny projekty).
Soubor se zrcadlí na stejnou relativní cestu ve stejnojmenném projektu cíle.

V cílovém projektu jsou zrcadlené soubory zapsané v tabulce mirror_files (zdroj a file_id
ve zdroji) – v cíli je nikdo nemůže zamknout ani upravit. Zdroj se jen čte.

Postup zrcadlení jednoho projektu (sync_project):
1. stav zdroje z jeho databáze (jen soubory v evidenci, které se právě nenahrávají),
2. plán podle stavu cíle (bez zámku),
3. nové a změněné soubory se zkopírují do dočasných souborů v cíli a ověří hashem (bez zámku –
   dlouhé kopírování neblokuje ostatní uživatele cíle),
4. pod zámkem zápisu cíle se plán znovu ověří a provede (záloha, nahrazení, přejmenování,
   smazání do koše projektu) a zapíše do historie cíle jako „Zrcadlení“.
"""

from __future__ import annotations

import fnmatch
import os
import uuid
from dataclasses import asdict, dataclass, field
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
    """Do cílové síťové složky nemám právo zapisovat."""


def split_patterns(text: str) -> list[str]:
    return [p.strip().lower() for p in (text or "").split(";") if p.strip()]


def _any_match(name: str, patterns: list[str]) -> bool:
    name = name.lower()
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


@dataclass
class MirrorRule:
    """Místní pravidlo má zdroj i cíl jako id záložek. Sdílené (ze spm_config.json zdroje) má
    zdrojem záložku, ze které se načetlo, a cíl jako identitu síťové složky (target_server) –
    uplatní ho jen uživatel, který má tuto složku jako záložku."""
    source: str                 # id záložky – zdroj
    target: str                 # id záložky – cíl (u sdíleného pravidla "" – viz target_server)
    files: str = ""
    folders: str = ""
    exclude: str = ""
    projects: str = ""
    enabled: bool = True
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    target_server: str = ""     # identita cílové síťové složky (spm_config.json)
    target_name: str = ""       # název cílové složky (pro zobrazení, když ji uživatel nemá)
    shared: bool = False        # pravidlo ze spm_config.json zdroje (platí pro všechny)

    FIELDS = ("source", "target", "files", "folders", "exclude", "projects", "enabled", "id",
              "target_server", "target_name")
    SHARED_FIELDS = ("id", "target_server", "target_name", "files", "folders", "exclude",
                     "projects", "enabled")

    @classmethod
    def from_dict(cls, data: dict) -> "MirrorRule":
        known = {k: data[k] for k in cls.FIELDS if k in data}
        known.setdefault("source", "")
        known.setdefault("target", "")
        return cls(**known)

    @classmethod
    def from_shared(cls, data: dict, source_ws: str) -> "MirrorRule":
        rule = cls.from_dict({k: data[k] for k in cls.SHARED_FIELDS if k in data})
        rule.source, rule.shared = source_ws, True
        return rule

    def to_dict(self) -> dict:
        data = asdict(self)
        data.pop("shared")
        return data

    def to_shared(self) -> dict:
        """Zápis do spm_config.json zdroje (zdroj je dán souborem, cíl identitou složky)."""
        return {k: getattr(self, k) for k in self.SHARED_FIELDS}

    def matches_project(self, name: str) -> bool:
        patterns = split_patterns(self.projects)
        return not patterns or _any_match(name, patterns)

    def matches(self, rel: str) -> bool:
        """Odpovídá soubor (relativní cesta v projektu, oddělovač '/') pravidlu?"""
        *dirs, name = rel.split("/")
        files, folders, exclude = (split_patterns(self.files), split_patterns(self.folders),
                                   split_patterns(self.exclude))
        if files and not _any_match(name, files):
            return False
        if folders and not any(_any_match(d, folders) for d in dirs):
            return False
        return not (exclude and any(_any_match(d, exclude) for d in dirs))

    def describe(self) -> str:
        parts = [f"soubory {self.files}" if self.files.strip() else "všechny soubory"]
        if self.folders.strip():
            parts.append(f"ve složkách {self.folders}")
        if self.exclude.strip():
            parts.append(f"vyjma složek {self.exclude}")
        if self.projects.strip():
            parts.append(f"jen projekty {self.projects}")
        return ", ".join(parts)

    def validate(self) -> str | None:
        if not self.source or not (self.target or self.target_server):
            return "Vyberte zdrojovou i cílovou záložku."
        if self.source == self.target:
            return "Zdroj a cíl musí být různé záložky."
        if not any(s.strip() for s in (self.files, self.folders)):
            return ("Vyplňte vzor souborů nebo složek – pravidlo bez nich by zrcadlilo "
                    "celé projekty.")
        return None


def rules_from_settings(rules: list[dict]) -> list[MirrorRule]:
    return [MirrorRule.from_dict(r) for r in rules if isinstance(r, dict)]


@dataclass
class MirrorResult:
    project: str
    added: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    renamed: list[str] = field(default_factory=list)      # "stará -> nová"
    deleted: list[str] = field(default_factory=list)
    adopted: list[str] = field(default_factory=list)      # v cíli už byl stejný obsah
    released: list[str] = field(default_factory=list)     # přestal odpovídat pravidlu – v cíli ponechán
    conflicts: list[str] = field(default_factory=list)    # nezrcadleno (s důvodem)
    created_project: bool = False
    note: str = ""                                        # proč se projekt nezrcadlil vůbec

    @property
    def changed(self) -> int:
        return len(self.added) + len(self.modified) + len(self.renamed) + len(self.deleted)

    @property
    def ok(self) -> bool:
        return not self.conflicts and not self.note


@dataclass
class _Source:
    files: dict[str, DbFile]       # source file_id -> soubor (jen odpovídající pravidlům)
    all_ids: set[str]              # všechny aktivní soubory zdroje (i neodpovídající)
    last_commit: int | None
    project_id: str                # trvalá identita zdrojového projektu (klíč zdroje v cíli)


class Mirror:
    """Zrcadlení mezi dvěma záložkami podle pravidel se stejným zdrojem a cílem."""

    def __init__(self, source_pm, target_pm, rules: list[MirrorRule]):
        self.source_pm, self.target_pm = source_pm, target_pm
        self.rules = [r for r in rules if r.enabled]
        # dočasné kopie v cíli mají v názvu tento kód – souběžné zrcadlení jiného uživatele
        # (nebo jiného okna) používá jiné názvy a nesrazí se s tímto
        self._stage_token = uuid.uuid4().hex[:8]

    # --- výběr ---------------------------------------------------------------

    def applies_to(self, name: str) -> bool:
        return any(r.matches_project(name) for r in self.rules)

    def matches(self, name: str, rel: str) -> bool:
        return any(r.matches_project(name) and r.matches(rel) for r in self.rules)

    def projects(self) -> list[str]:
        root = self.source_pm.server_root
        if not root.is_dir():
            raise MirrorError(f"Zdrojová síťová složka není dostupná:\n{root}")
        return sorted(e.name for e in root.iterdir()
                      if e.is_dir() and not e.name.startswith(".") and self.applies_to(e.name))

    def _source(self, name: str) -> _Source | None:
        server = self.source_pm.server_dir(name)
        if not db_path(server).exists():
            return None
        with ProjectDB.for_project(server) as db:
            active = db.active_files()  # bez souborů, které se právě nahrávají
            last = db.last_commit_id()
            project_id = db.project_id()
        if project_id is None:  # starší databáze – identita se jednou založí
            with self.source_pm._write(server) as db:
                project_id = db.ensure_project_id()
        files = {f.file_id: f for f in active.values() if f.sha256 and self.matches(name, f.path)}
        return _Source(files, {f.file_id for f in active.values()}, last, project_id)

    def preview(self, progress: Progress | None = None) -> dict[str, list[str]]:
        """Projekt -> zachycené soubory (podle evidence zdroje). Projekty bez evidence mají
        místo souborů prázdný seznam."""
        result: dict[str, list[str]] = {}
        names = self.projects()
        for i, name in enumerate(names):
            if progress:
                progress(i, len(names), name)
            src = self._source(name)
            paths = sorted(f.path for f in src.files.values()) if src else []
            if paths or src is None:
                result[name] = paths
        if progress:
            progress(len(names), len(names), "")
        return result

    # --- zrcadlení ---------------------------------------------------------------

    def sync_all(self, progress: Progress | None = None) -> list[MirrorResult]:
        results = []
        names = self.projects()
        for i, name in enumerate(names):
            if progress:
                progress(i, len(names), name)
            results.append(self.sync_project(name))
        if progress:
            progress(len(names), len(names), "")
        return results

    def sync_project(self, name: str, progress: Progress | None = None) -> MirrorResult:
        try:
            return self._sync_project(name, progress)
        except PermissionError as e:
            raise MirrorPermissionError(
                "Nemáte oprávnění zapisovat do cílové síťové složky:\n"
                f"{self.target_pm.server_root}\n\n({e})") from None

    def _sync_project(self, name: str, progress: Progress | None) -> MirrorResult:
        res = MirrorResult(name)
        if not self.applies_to(name):
            return res
        src = self._source(name)
        if src is None:
            res.note = ("Projekt ve zdroji ještě nemá evidenci – zrcadlí se až po jeho první "
                        "aktivaci.")
            return res
        source_dir = self.source_pm.server_dir(name)
        source_key = src.project_id
        target = self.target_pm.server_dir(name)
        if not self.target_pm.server_root.is_dir():
            raise MirrorError(f"Cílová síťová složka není dostupná:\n{self.target_pm.server_root}")
        if not src.files and not db_path(target).exists():
            return res  # nic ke zrcadlení a cíl ještě neexistuje – nezakládat prázdný projekt
        if not target.exists():
            target.mkdir()
            res.created_project = True

        # 2) plán podle stavu cíle (bez zámku); 3) kopírování do dočasných souborů
        state = self._target_state(target)
        staged: dict[str, Path] = {}  # source file_id -> dočasná kopie v cíli
        try:
            todo = [f for f in src.files.values() if self._needs_copy(f, state, target, source_key)]
            for i, f in enumerate(todo):
                if progress:
                    progress(i, len(todo), f.path)
                problem = self._target_problem(f.path)
                if problem:
                    res.conflicts.append(f"{f.path} – {problem}")
                    continue
                dst = to_abs(target, f.path)
                stage = dst.with_name(f"{dst.name}.{self._stage_token}{STAGE_SUFFIX}")
                try:
                    sha = copy_with_hash(to_abs(source_dir, f.path), stage)
                except FileNotFoundError:
                    res.conflicts.append(f"{f.path} – ve zdroji mezitím zmizel")
                    continue
                if sha != f.sha256:
                    stage.unlink(missing_ok=True)
                    res.conflicts.append(f"{f.path} – obsah ve zdroji neodpovídá evidenci "
                                         "(právě se nahrává nebo změněn mimo aplikaci)")
                    continue
                staged[f.file_id] = stage
            # 4) provedení pod zámkem cíle
            self._apply(name, target, source_key, src, staged, res)
        finally:
            for stage in staged.values():
                stage.unlink(missing_ok=True)
        return res

    def _target_state(self, target: Path) -> dict:
        files: dict[str, DbFile] = {}
        mirrored: dict[str, tuple[str, str]] = {}
        owners: dict = {}
        if db_path(target).exists():
            with ProjectDB.for_project(target) as db:
                files = db.active_files(include_pending=True)
                mirrored = db.mirrored()
                owners = db.lock_owners()
        by_id = {f.file_id: f for f in files.values()}
        return {"files": files, "by_id": by_id, "mirrored": mirrored, "owners": owners}

    def _source_keys(self, name: str, source_key: str) -> set[str]:
        """Klíč zdroje a dřívější klíč (cesta ke zdrojovému projektu) – převede se při zápisu."""
        return {source_key, str(self.source_pm.server_dir(name))}

    def _mirror_of(self, state: dict, keys: set[str], source_fid: str) -> DbFile | None:
        for fid, (src, sfid) in state["mirrored"].items():
            if src in keys and sfid == source_fid and fid in state["by_id"]:
                return state["by_id"][fid]
        return None

    def _needs_copy(self, f: DbFile, state: dict, target: Path, source_key: str) -> bool:
        current = self._mirror_of(state, self._source_keys(target.name, source_key), f.file_id)
        if current is None:
            existing = state["files"].get(f.path)
            if existing is not None and existing.sha256 == f.sha256 and to_abs(target, f.path).exists():
                return False  # v cíli už je stejný obsah – jen převzít pod správu zrcadla
            if existing is None and to_abs(target, f.path).is_file():
                return file_hash(to_abs(target, f.path)) != f.sha256
            return True
        return current.sha256 != f.sha256 or not to_abs(target, current.path).exists()

    def _target_problem(self, rel: str) -> str | None:
        settings = self.target_pm.settings
        if any(is_ignored(part, settings.ignore_patterns) for part in rel.split("/")):
            return "v cíli patří mezi ignorované soubory"
        if settings.check_forbidden_chars and invalid_rel_paths([rel]):
            return "název obsahuje znaky, které cíl nepovoluje"
        return None

    def _apply(self, name: str, target: Path, source_key: str, src: _Source,
               staged: dict[str, Path], res: MirrorResult) -> None:
        tpm = self.target_pm
        with WriteLock(target, tpm.user, tpm.pc):
            with ProjectDB.for_project(target, create=True) as db:
                db.ensure_mirror_tables()
                with db.conn:
                    source_dir = self.source_pm.server_dir(name)
                    db.rekey_mirror_source(str(source_dir), source_key)  # dřívější klíč = cesta
                    db.set_mirror_source(source_key, str(source_dir),
                                         self.source_pm.settings.server_id,
                                         self.source_pm.settings.server_name)
                    files = db.active_files(include_pending=True)
                    by_id = {f.file_id: f for f in files.values()}
                    owners = db.lock_owners()
                    mirrored = db.mirrored()
                    mine = {sfid: fid for fid, (s, sfid) in mirrored.items() if s == source_key}
                    commit_id = db.add_commit(None, tpm.user, tpm.pc, dbm.MIRROR,
                                              f"Zrcadleno z {self.source_pm.server_dir(name)}")
                    trash_id = f"mirror_{commit_id}"

                    def blocked(fid: str | None, rel: str) -> bool:
                        """Soubor v cíli nelze přepsat: drží ho někdo, nebo patří jinému zdroji."""
                        if fid is not None and fid in owners:
                            res.conflicts.append(f"{rel} – v cíli ho drží {owners[fid].label}")
                            return True
                        if fid is not None and fid in mirrored and mirrored[fid][0] != source_key:
                            res.conflicts.append(f"{rel} – v cíli ho spravuje zrcadlo z "
                                                 f"{mirrored[fid][0]}")
                            return True
                        return False

                    # soubory zdroje
                    for sfid, f in src.files.items():
                        fid = mine.get(sfid)
                        cur = by_id.get(fid) if fid else None
                        if cur is not None:  # už zrcadlený
                            if blocked(fid, cur.path):
                                continue
                            if cur.path != f.path:  # přejmenován / přesunut ve zdroji
                                occupant = files.get(f.path)
                                if occupant is not None and occupant.file_id != fid:
                                    res.conflicts.append(f"{f.path} – v cíli už existuje jiný soubor")
                                    continue
                                move_file(to_abs(target, cur.path), to_abs(target, f.path))
                                remove_empty_parents(target, cur.path)
                                res.renamed.append(f"{cur.path} -> {f.path}")
                            if sfid in staged:
                                backup = self._replace(target, staged.pop(sfid), f.path)
                                res.modified.append(f.path)
                                kind = dbm.RENAMED_MODIFIED if cur.path != f.path else dbm.MODIFIED
                            elif cur.path != f.path:
                                backup, kind = None, dbm.RENAMED
                            else:
                                continue
                            st = to_abs(target, f.path).stat()
                            db.update_file(fid, f.path, st.st_size, st.st_mtime, f.sha256)
                            db.add_change(commit_id, fid, kind, cur.path, f.path, st.st_size,
                                          st.st_mtime, f.sha256, backup)
                            continue
                        # ještě nezrcadlený: v cíli na stejné cestě nic / jiný soubor / stejný obsah
                        existing = files.get(f.path)
                        if existing is not None and blocked(existing.file_id, f.path):
                            continue
                        if sfid not in staged:
                            dst = to_abs(target, f.path)
                            if not dst.is_file():
                                continue  # kopírování selhalo (už je v konfliktech)
                            st = dst.stat()
                            if existing is None:  # na disku stejný obsah, ale mimo evidenci
                                fid = db.add_file(f.path, st.st_size, st.st_mtime, f.sha256)
                                db.add_change(commit_id, fid, dbm.IMPORTED, None, f.path, st.st_size,
                                              st.st_mtime, f.sha256)
                            else:
                                fid = existing.file_id
                            db.set_mirrored(fid, source_key, sfid)
                            res.adopted.append(f.path)
                            continue
                        backup = self._replace(target, staged.pop(sfid), f.path)
                        st = to_abs(target, f.path).stat()
                        if existing is None:
                            fid = db.add_file(f.path, st.st_size, st.st_mtime, f.sha256)
                            kind = dbm.ADDED
                            res.added.append(f.path)
                        else:
                            fid = existing.file_id
                            db.update_file(fid, f.path, st.st_size, st.st_mtime, f.sha256)
                            kind = dbm.MODIFIED
                            res.modified.append(f.path)
                        db.set_mirrored(fid, source_key, sfid)
                        db.add_change(commit_id, fid, kind, None if kind == dbm.ADDED else f.path,
                                      f.path, st.st_size, st.st_mtime, f.sha256, backup)

                    # zrcadlené soubory, které už pravidlům neodpovídají nebo ve zdroji zmizely
                    for sfid, fid in mine.items():
                        if sfid in src.files or fid not in by_id:
                            continue
                        cur = by_id[fid]
                        if sfid in src.all_ids:  # ve zdroji je, jen už neodpovídá pravidlům
                            db.unset_mirrored(fid)
                            res.released.append(cur.path)
                            continue
                        if blocked(fid, cur.path):
                            continue
                        trash = tpm._to_trash(target, trash_id, cur.path)
                        remove_empty_parents(target, cur.path)
                        db.mark_deleted(fid)
                        db.unset_mirrored(fid)
                        db.add_change(commit_id, fid, dbm.DELETED, cur.path, None, backup_path=trash)
                        res.deleted.append(cur.path)

                    if not db.changes(commit_id):
                        db.conn.execute("DELETE FROM commits WHERE commit_id = ?", (commit_id,))
                    if not res.conflicts:  # s konflikty se příště zkusí znovu
                        db.set_mirror_synced(source_key, src.last_commit)

    def _replace(self, target: Path, stage: Path, rel: str) -> str | None:
        """Nasadí ověřenou dočasnou kopii; původní soubor v cíli zazálohuje."""
        dst = to_abs(target, rel)
        backup = None
        if dst.exists() and self.target_pm.settings.backup_enabled:
            backup = self.target_pm._backup(target, rel)
        os.replace(stage, dst)
        return backup


def effective_rules(app_settings) -> list[MirrorRule]:
    """Platná pravidla všech záložek: má-li zdrojová složka pravidla ve svém spm_config.json,
    platí ta (sdílená); jinak místní pravidla uživatele s touto záložkou jako zdrojem."""
    local = rules_from_settings(app_settings.base.mirror_rules)
    result: list[MirrorRule] = []
    for ws in app_settings.workspaces:
        shared = app_settings.view(ws["id"]).shared_mirror_rules
        if shared is not None:
            result += [MirrorRule.from_shared(d, ws["id"]) for d in shared]
        else:
            result += [r for r in local if r.source == ws["id"]]
    return result


def resolve_target(app_settings, rule: MirrorRule) -> str | None:
    """Záložka cíle pravidla – u sdíleného podle identity síťové složky. None = uživatel
    cílovou složku jako záložku nemá (pravidlo se ho netýká)."""
    ids = [w["id"] for w in app_settings.workspaces]
    if rule.target in ids:
        return rule.target
    if rule.target_server:
        for ws_id in ids:
            if app_settings.view(ws_id).server_id == rule.target_server:
                return ws_id
    return None


def mirrors_for(app_settings, source_ws: str | None = None) -> list[tuple[str, str, list[MirrorRule]]]:
    """Zapnutá pravidla seskupená podle (zdrojová záložka, cílová záložka); jen pravidla,
    jejichž zdroj i cíl má uživatel jako záložky."""
    groups: dict[tuple[str, str], list[MirrorRule]] = {}
    for rule in effective_rules(app_settings):
        if not rule.enabled or (source_ws is not None and rule.source != source_ws):
            continue
        target = resolve_target(app_settings, rule)
        if target is None or target == rule.source:
            continue
        groups.setdefault((rule.source, target), []).append(rule)
    return [(s, t, rules) for (s, t), rules in groups.items()]
