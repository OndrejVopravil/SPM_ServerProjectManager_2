"""Místo na serveru zabrané zálohami a košem projektů a jejich úklid.

V projektu jsou tři servisní složky, které časem rostou:
- .spm/backup – předchozí verze souborů (při každém odevzdání se ponechá nastavený počet),
- .spm/trash  – soubory smazané odevzdáním (koš projektu, podle sezení / zrcadlení),
- .spm/tmp    – dočasné soubory přerušených operací.

Úklid (vždy nejdřív jako náhled – co by se smazalo a kolik místa se uvolní):
- ponechat jen posledních N záloh každého souboru (i zpětně, když se nastavení snížilo),
- vysypat koš – soubory smazané před více než X dny,
- smazat neevidované zálohy (pozůstatky přerušených odevzdání – v historii nejsou, k obnově
  se nepoužijí) a dočasné soubory.
Mazání na serveru je trvalé (zálohy a koš nejdou do Koše Windows). Historie (záznamy) zůstává;
obnova k dřívějším stavům je pak omezenější – časová osa to ukáže.
"""

from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from . import SPM_DIR
from .db import ProjectDB, db_path
from .lock import WriteLock
from .scanner import Progress, remove_empty_parents, to_abs, to_rel

BACKUP, TRASH, TMP = "backup", "trash", "tmp"


@dataclass
class StorageInfo:
    project: str
    backup_files: int = 0
    backup_bytes: int = 0
    trash_files: int = 0
    trash_bytes: int = 0
    tmp_bytes: int = 0

    @property
    def total(self) -> int:
        return self.backup_bytes + self.trash_bytes + self.tmp_bytes


@dataclass
class CleanupPlan:
    project: str
    remove: list[tuple[str, int, str]] = field(default_factory=list)  # (cesta v projektu, velikost, důvod)

    @property
    def freed(self) -> int:
        return sum(size for _, size, _ in self.remove)


def _files(folder: Path, errors: list[OSError] | None = None):
    """Soubory složky s údaji (stat). Nečitelné podsložky a soubory se přeskočí; je-li zadáno
    errors, přidají se do něj jejich chyby."""
    onerror = errors.append if errors is not None else None
    for dirpath, _dirs, names in os.walk(folder, onerror=onerror):
        for n in names:
            p = Path(dirpath) / n
            try:
                yield p, p.stat()
            except OSError as e:
                if errors is not None:
                    errors.append(e)
                continue


def project_storage(server: Path) -> StorageInfo:
    """Velikost záloh, koše a dočasných souborů projektu (bez čtení obsahu)."""
    info = StorageInfo(server.name)
    for p, st in _files(server / SPM_DIR / BACKUP):
        info.backup_files += 1
        info.backup_bytes += st.st_size
    for p, st in _files(server / SPM_DIR / TRASH):
        info.trash_files += 1
        info.trash_bytes += st.st_size
    for p, st in _files(server / SPM_DIR / TMP):
        info.tmp_bytes += st.st_size
    return info


def server_storage(server_root: Path, progress: Progress | None = None) -> list[StorageInfo]:
    """Zálohy a koš všech projektů síťové složky (největší první)."""
    try:
        projects = sorted((p for p in server_root.iterdir() if p.is_dir() and (p / SPM_DIR).is_dir()),
                          key=lambda p: p.name.lower())
    except OSError:
        return []
    result = []
    for i, p in enumerate(projects):
        if progress:
            progress(i, len(projects), p.name)
        result.append(project_storage(p))
    if progress:
        progress(len(projects), len(projects), "")
    return sorted(result, key=lambda s: -s.total)


def plan_cleanup(server: Path, keep_backups: int | None, trash_days: int | None,
                 orphans: bool) -> CleanupPlan:
    """Co by úklid smazal (nic nemaže). keep_backups = ponechat N nejnovějších záloh každého
    souboru (None = zálohy beze změny); trash_days = vysypat soubory smazané před více než
    tolika dny (None = koš beze změny); orphans = neevidované zálohy a dočasné soubory."""
    plan = CleanupPlan(server.name)
    backup_root = server / SPM_DIR / BACKUP
    by_file: dict[str, list[str]] = defaultdict(list)   # file_id -> zálohy od nejstarší
    trash_dates: dict[str, str] = {}                     # cesta v koši -> kdy smazáno
    if db_path(server).exists():
        with ProjectDB.for_project(server) as db:
            rows = db.conn.execute(
                "SELECT x.file_id, x.backup_path, x.change_type, c.created_at FROM changes x "
                "JOIN commits c USING(commit_id) WHERE x.backup_path IS NOT NULL ORDER BY x.change_id").fetchall()
        for fid, path, _kind, created in rows:
            if path.startswith(f"{SPM_DIR}/{BACKUP}/"):
                if path not in by_file[fid]:
                    by_file[fid].append(path)
            elif path.startswith(f"{SPM_DIR}/{TRASH}/"):
                trash_dates[path.lower()] = created
    known = {p.lower() for paths in by_file.values() for p in paths}

    if keep_backups is not None:
        keep = max(1, keep_backups)
        for paths in by_file.values():
            existing = [p for p in paths if to_abs(server, p).is_file()]
            for p in existing[:-keep]:
                plan.remove.append((p, to_abs(server, p).stat().st_size,
                                    f"starší záloha (ponechá se posledních {keep})"))
    if orphans:
        for p, st in _files(backup_root):
            rel = to_rel(server, p)
            if rel.lower() not in known:
                plan.remove.append((rel, st.st_size, "neevidovaná záloha (přerušené odevzdání)"))
        for p, st in _files(server / SPM_DIR / TMP):
            plan.remove.append((to_rel(server, p), st.st_size, "dočasný soubor"))
    if trash_days is not None:
        limit = datetime.now() - timedelta(days=trash_days)
        for p, st in _files(server / SPM_DIR / TRASH):
            rel = to_rel(server, p)
            created = trash_dates.get(rel.lower())
            try:
                when = datetime.fromisoformat(created) if created else datetime.fromtimestamp(st.st_mtime)
            except ValueError:
                when = datetime.fromtimestamp(st.st_mtime)
            if when < limit:
                plan.remove.append((rel, st.st_size, f"v koši od {when:%d. %m. %Y}"))
    seen: set[str] = set()
    plan.remove = [r for r in plan.remove if not (r[0].lower() in seen or seen.add(r[0].lower()))]
    return plan


def run_cleanup(server: Path, plan: CleanupPlan, user: str, pc: str) -> CleanupPlan:
    """Smaže soubory z plánu (trvale) pod zámkem zápisu projektu; prázdné složky zaniknou.
    Vrací, co se opravdu smazalo."""
    done = CleanupPlan(plan.project)
    spm = server / SPM_DIR
    with WriteLock(server, user, pc):
        for rel, size, reason in plan.remove:
            path = to_abs(server, rel)
            if not rel.startswith(f"{SPM_DIR}/"):
                continue  # pojistka – mimo servisní složku se nikdy nic nemaže
            try:
                path.chmod(0o666)
                path.unlink()
            except FileNotFoundError:
                continue
            except OSError:
                continue
            done.remove.append((rel, size, reason))
            remove_empty_parents(spm, to_rel(spm, path))
    return done


def fmt_bytes(n: int) -> str:
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}".replace(".", ",")
        n /= 1024
    return ""


# --- zbytky nedokončených operací v síťové složce ---------------------------------------------

# skryté složky v síťové složce s projekty, které po přerušené operaci zůstanou (tečka = v seznamu
# projektů nejsou vidět): předpona -> popis
LEFTOVER_KINDS = {
    ".spm_restoring_": "nedokončená obnova z archivu",
    ".spm_archived_": "zbytek archivovaného projektu (nešel celý smazat)",
    ".spm_publish_": "nedokončené publikování",
}
LEFTOVER_MIN_AGE = timedelta(hours=24)  # mladší složka může patřit operaci, která právě běží


@dataclass
class Leftover:
    path: Path
    kind: str          # popis (LEFTOVER_KINDS)
    size: int
    files: int
    modified: datetime


def leftovers(server_root: Path, now: datetime | None = None,
              problems: list[str] | None = None) -> list[Leftover]:
    """Zbytky nedokončených operací v síťové složce – jen starší než LEFTOVER_MIN_AGE (podle
    poslední změny čehokoliv v nich).

    Složka, kterou nejde celou přečíst (právo, síť, mezitím smazaná), se přeskočí – bez úplného
    obsahu nelze spolehlivě určit její stáří, a tedy ani nabídnout smazání. Do problems se pak
    přidá popis pro uživatele (stejně jako když nejde projít celá síťová složka)."""
    now = now or datetime.now()
    problems = problems if problems is not None else []
    result = []
    try:
        entries = list(os.scandir(server_root))
    except OSError as e:
        problems.append(f"Síťovou složku nelze projít: {e.strerror or e}")
        return []
    for e in entries:
        kind = next((label for prefix, label in LEFTOVER_KINDS.items() if e.name.startswith(prefix)), None)
        if kind is None:
            continue
        errors: list[OSError] = []
        size = files = 0
        try:
            if not e.is_dir():
                continue
            latest = e.stat().st_mtime
        except OSError as err:
            errors.append(err)
            latest = 0.0
        if not errors:
            for _p, st in _files(Path(e.path), errors):
                size += st.st_size
                files += 1
                latest = max(latest, st.st_mtime)
        if errors:
            problems.append(f"{e.name}: nelze celou přečíst ({errors[0].strerror or errors[0]}) – přeskočeno")
            continue
        modified = datetime.fromtimestamp(latest)
        if now - modified >= LEFTOVER_MIN_AGE:
            result.append(Leftover(Path(e.path), kind, size, files, modified))
    return sorted(result, key=lambda x: x.modified)


def remove_leftovers(server_root: Path, items: list[Leftover]) -> list[Path]:
    """Trvale smaže zbytky (jen skryté složky s předponou z LEFTOVER_KINDS přímo v síťové
    složce – pojistka proti smazání čehokoliv jiného). Vrací smazané."""
    from .scanner import rmtree
    root = Path(server_root).resolve()
    removed = []
    for item in items:
        path = Path(item.path)
        if path.parent.resolve() != root or not path.name.startswith(tuple(LEFTOVER_KINDS)):
            continue
        rmtree(path)
        if not path.exists():
            removed.append(path)
    return removed
