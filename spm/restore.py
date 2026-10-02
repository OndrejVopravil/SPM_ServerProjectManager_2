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

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from . import db as dbm
from .db import ProjectDB, db_path
from .scanner import Progress, copy_with_hash, same_stat, to_abs

# změny, po kterých soubor v projektu není
REMOVING = {dbm.DELETED, dbm.EXTERNAL_DELETED}
# název složky s obnovou (v seznamu projektů se nezobrazuje jako „Jen místně“)
RESTORE_DIR_RE = re.compile(r"_obnova_\d{8}_\d{6}(_\d+)?$")


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
