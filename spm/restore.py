"""Obnovení projektu ze záloh k zvolenému odevzdání (do nové místní složky).

Z historie (tabulka changes) je pro každý okamžik známo, které soubory projekt měl, pod jakou
cestou a s jakým obsahem (SHA-256). Obsah starších verzí se hledá ve „zdrojích“:
- aktuální soubor na serveru (pokud odpovídá evidenci),
- záloha .spm/backup/…/soubor.backupNN – obsahuje verzi před změnou, která ji vytvořila,
- koš projektu .spm/trash/… – obsahuje verzi před smazáním.
Stav lze obnovit kompletně, pokud pro každý jeho soubor existuje zdroj se stejným obsahem.
Při obnově se každý soubor ověří hashem; server se jen čte.
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from . import db as dbm
from .db import ProjectDB, db_path
from .scanner import TEMP_SUFFIX, Progress, copy_with_hash, same_stat, set_readonly, to_abs

# změny, po kterých soubor v projektu není
REMOVING = {dbm.DELETED, dbm.EXTERNAL_DELETED}
# název složky s obnovou (v seznamu projektů se nezobrazuje jako „Jen místně“)
RESTORE_DIR_RE = re.compile(r"_obnova_(\d{8}_\d{6})(_\d+)?$")  # skupina 1 = datum


class RestoreError(Exception):
    pass


@dataclass
class Blob:
    """Místo na serveru, kde leží určitý obsah souboru."""
    path: str                    # relativně k projektu (může být v .spm/backup nebo .spm/trash)
    size: int | None
    mtime: float | None = None   # jen u aktuálních souborů (kontrola, že odpovídají evidenci)
    current: bool = False


@dataclass
class RestorePoint:
    commit_id: int
    created_at: str
    user: str
    pc: str
    kind: str
    comment: str
    files: dict[str, tuple[str, str]] = field(default_factory=dict)  # file_id -> (cesta, sha256)
    missing: list[str] = field(default_factory=list)                 # cesty bez dostupného zdroje

    @property
    def complete(self) -> bool:
        return not self.missing


@dataclass
class RestoreCatalog:
    name: str
    points: list[RestorePoint]             # od nejnovějšího
    blobs: dict[str, list[Blob]]           # sha256 -> kde ten obsah leží

    @property
    def oldest_complete(self) -> RestorePoint | None:
        """Nejstarší stav, ke kterému lze obnovit kompletně (a všechny novější také)."""
        result = None
        for p in self.points:
            if not p.complete:
                break
            result = p
        return result


@dataclass
class RestoreResult:
    folder: Path
    restored: int = 0
    failed: list[str] = field(default_factory=list)


def restore_points(server: Path, name: str, progress: Progress | None = None) -> RestoreCatalog:
    """Spočítá, ke kterým odevzdáním lze projekt obnovit (jen čte server a databázi)."""
    if not db_path(server).exists():
        raise RestoreError(f"Projekt {name} nemá historii – není z čeho obnovovat.")
    with ProjectDB.for_project(server) as db:
        commits = list(reversed(db.commits()))   # od nejstaršího
        changes = db.all_changes()               # v pořadí, jak vznikly
        current = db.active_files(include_pending=True)

    # 1) Kde leží jaký obsah
    blobs: dict[str, list[Blob]] = defaultdict(list)
    for f in current.values():
        if f.sha256 and not f.pending:
            blobs[f.sha256].append(Blob(f.path, f.size, f.mtime, current=True))
    previous: dict[str, tuple[str, int | None]] = {}  # file_id -> (sha, velikost) poslední verze
    for ch in changes:
        fid = ch["file_id"]
        if ch["backup_path"] and fid in previous:
            # záloha / koš obsahuje verzi souboru před touto změnou
            sha, size = previous[fid]
            blobs[sha].append(Blob(ch["backup_path"], size))
        if ch["change_type"] in REMOVING:
            previous.pop(fid, None)
        elif ch["sha256"]:
            previous[fid] = (ch["sha256"], ch["size"])

    # 2) Který obsah je skutečně k dispozici (jen existence a velikost – bez čtení obsahu)
    available: set[str] = set()
    all_blobs = [(sha, b) for sha, bs in blobs.items() for b in bs]
    for i, (sha, b) in enumerate(all_blobs):
        if progress:
            progress(i, len(all_blobs), b.path)
        if sha in available:
            continue
        try:
            st = to_abs(server, b.path).stat()
        except OSError:
            continue
        if b.current:
            ok = same_stat(st.st_size, st.st_mtime, b.size, b.mtime)
        else:
            ok = b.size is None or st.st_size == b.size
        if ok:
            available.add(sha)

    # 3) Stav projektu po každém odevzdání a jeho úplnost
    by_commit: dict[int, list] = defaultdict(list)
    for ch in changes:
        by_commit[ch["commit_id"]].append(ch)
    state: dict[str, tuple[str, str]] = {}
    points: list[RestorePoint] = []
    for c in commits:
        chs = by_commit.get(c["commit_id"], [])
        for ch in chs:
            if ch["change_type"] in REMOVING:
                state.pop(ch["file_id"], None)
            elif ch["sha256"]:
                state[ch["file_id"]] = (ch["new_path"], ch["sha256"])
        if not chs:
            continue  # bez změn souborů (přejmenování projektu, zrušení aktivace…)
        missing = sorted(path for path, sha in state.values() if sha not in available)
        points.append(RestorePoint(c["commit_id"], c["created_at"], c["user"], c["pc"], c["kind"],
                                   c["comment"] or "", dict(state), missing))
    if progress:
        progress(len(all_blobs), len(all_blobs), "")
    points.reverse()
    return RestoreCatalog(name, points, blobs)


# --- jeden soubor ---------------------------------------------------------------------------

@dataclass
class FileVersion:
    """Jeden záznam historie souboru (verze po změně) a kde leží její obsah."""
    change_id: int
    commit_id: int
    created_at: str
    user: str
    comment: str
    change_type: str
    path: str                     # cesta souboru po změně (u smazání před ní)
    sha256: str | None            # obsah po změně (None u smazání)
    size: int | None
    sources: list[Blob] = field(default_factory=list)  # kde obsah leží (ověří se při obnově)
    reason: str = ""              # proč verze není k dispozici

    @property
    def removed(self) -> bool:
        return self.change_type in REMOVING

    @property
    def available(self) -> bool:
        return bool(self.sources)


def file_versions(server: Path, file_id: str, backup_count: int | None = None) -> list[FileVersion]:
    """Historie jednoho souboru (nejnovější první): každá změna, obsah po ní a jeho dostupnost
    – aktuální soubor na serveru, záloha (obsahuje verzi před změnou, která ji vytvořila) nebo
    koš projektu (verze před smazáním). Server se jen čte; existence se ověřuje bez čtení obsahu."""
    if not db_path(server).exists():
        return []
    with ProjectDB.for_project(server) as db:
        rows = list(reversed(db.file_history(file_id)))  # od nejstarší
        current = db.active_files(include_pending=True)
    cur = next((f for f in current.values() if f.file_id == file_id and not f.pending), None)
    versions = [FileVersion(r["change_id"], r["commit_id"], r["created_at"], r["user"],
                            r["comment"] or "", r["change_type"],
                            r["new_path"] or r["old_path"] or "", r["sha256"], r["size"])
                for r in rows]
    # obsah poslední verze před změnou s uloženou zálohou / koš = tato záloha
    pending_reason: dict[int, str] = {}
    last: FileVersion | None = None
    for v, r in zip(versions, rows):
        if last is not None and not last.removed and last.sha256:
            if r["backup_path"]:
                last.sources.append(Blob(r["backup_path"], last.size))
            elif v.sha256 != last.sha256 and r["change_type"] not in REMOVING:
                pending_reason[last.change_id] = "nebyla zálohována (zálohování bylo vypnuté)"
        if v.removed:
            v.sha256 = None
        last = v if not v.removed else None
    if cur is not None and last is not None and last.sha256 == cur.sha256:
        last.sources.append(Blob(cur.path, cur.size, cur.mtime, current=True))
    # stejný obsah = stejné zdroje (přejmenování bez změny, vrácení k dřívější verzi)
    by_sha: dict[str, list[Blob]] = defaultdict(list)
    for v in versions:
        if v.sha256:
            by_sha[v.sha256].extend(v.sources)
    for v in versions:
        if not v.sha256:
            continue
        v.sources = [b for b in dict.fromkeys(
            (b.path, b.size, b.mtime, b.current) for b in by_sha[v.sha256])]
        v.sources = [Blob(*b) for b in v.sources if _blob_exists(server, Blob(*b))]
        if not v.sources:
            if v.change_id in pending_reason:
                v.reason = pending_reason[v.change_id]
            elif v is last and cur is not None:
                v.reason = "soubor na serveru byl změněn mimo aplikaci"
            else:
                v.reason = ("záloha už byla smazána"
                            + (f" (ponechává se posledních {backup_count} záloh)" if backup_count else ""))
    return list(reversed(versions))


def _blob_exists(server: Path, b: Blob) -> bool:
    try:
        st = to_abs(server, b.path).stat()
    except OSError:
        return False
    if b.current:
        return same_stat(st.st_size, st.st_mtime, b.size, b.mtime)
    return b.size is None or st.st_size == b.size


def restore_file_version(server: Path, version: FileVersion, dest: Path) -> bool:
    """Zkopíruje verzi souboru do dest a ověří ji hashem (zkusí všechny zdroje). Nejdřív do
    dočasného souboru – dest (např. soubor v místní kopii) se nahradí až ověřenou verzí.
    Kopie není jen pro čtení. Vrací, zda se to podařilo."""
    tmp = dest.with_name(dest.name + ".restore" + TEMP_SUFFIX)
    for blob in sorted(version.sources, key=lambda b: not b.current):
        try:
            if copy_with_hash(to_abs(server, blob.path), tmp, writable=True) == version.sha256:
                if dest.exists():
                    set_readonly(dest, False)
                os.replace(tmp, dest)
                return True
        except Exception:  # noqa: BLE001 – nečitelný zdroj = zkusit další
            pass
        tmp.unlink(missing_ok=True)
    return False


def restore_folder_name(name: str, point: RestorePoint, local_root: Path) -> Path:
    stamp = point.created_at.replace("-", "").replace(":", "").replace("T", "_")[:15]
    folder = local_root / f"{name}_obnova_{stamp}"
    n = 2
    while folder.exists():
        folder = local_root / f"{name}_obnova_{stamp}_{n}"
        n += 1
    return folder


def restore(server: Path, local_root: Path, catalog: RestoreCatalog, point: RestorePoint,
            progress: Progress | None = None) -> RestoreResult:
    """Obnoví projekt ke stavu `point` do nové místní složky. Každý soubor ověří hashem;
    pokud zdroj nesedí, zkusí další. Soubory, které se obnovit nepodařilo, vrátí ve `failed`."""
    folder = restore_folder_name(catalog.name, point, local_root)
    folder.mkdir(parents=True)
    result = RestoreResult(folder)
    items = sorted(point.files.values())
    for i, (path, sha) in enumerate(items):
        if progress:
            progress(i, len(items), path)
        dst = to_abs(folder, path)
        # aktuální soubory mají přednost (bývají nejblíž), pak zálohy a koš
        for blob in sorted(catalog.blobs.get(sha, []), key=lambda b: not b.current):
            src = to_abs(server, blob.path)
            try:
                if copy_with_hash(src, dst) == sha:
                    result.restored += 1
                    break
            except Exception:  # noqa: BLE001 – nečitelný zdroj = zkusit další
                pass
            dst.unlink(missing_ok=True)
        else:
            result.failed.append(path)
    if progress:
        progress(len(items), len(items), "")
    return result
