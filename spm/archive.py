"""Archivace dokončených projektů a jejich obnova z archivu.

Archivace (archive_project):
1. kontrola – oprávnění (spm_config.json: archivers), nikdo nemá projekt
   aktivovaný, nemá neproběhlý skript ani nevyřízenou poznámku,
2. projekt se označí „archivuje se“ (.spm/archiving.json – nelze ho aktivovat) a do historie se
   zapíše „Archivace“ (je pak i v archivu),
3. do zipu se zabalí soubory projektu a jeho .spm (historie, poznámky) – bez koše, dočasných
   souborů a starších záloh (ponechá se archive_keep_backups posledních záloh každého souboru);
   zapisuje se do dočasného souboru, ověří se (kontrolní součty, počet souborů) a teprve pak
   dostane konečný název,
4. až potom se projekt z pracovní síťové složky smaže.
Archivní složka je pevně <síťová složka s projekty>/.spm/archiv (tečka = v seznamu projektů
se nezobrazí; s projekty se přesouvá i zálohuje spolu).
V zipu je popis spm_archive.json (kdo, kdy, komentář…), takže seznam archivu zip nerozbaluje.

Obnova (restore_archive): rozbalení do skryté dočasné složky v pracovní síťové složce, ověření,
přejmenování na název projektu (objeví se celý najednou) a záznam „Obnova z archivu“. Zip
v archivu zůstává (záznam o obnovách je v <zip>.restores.json vedle něj).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath

from . import SPM_DIR
from . import db as dbm
from . import storage
from .comment import read_comment
from .db import ProjectDB, db_path
from .lock import WriteLock, active_users, now_iso
from .scanner import TEMP_SUFFIX, Progress, rmtree, to_rel

ARCHIVE_SUBDIR = "archiv"                  # <síťová složka>/.spm/archiv
ARCHIVE_INFO = "spm_archive.json"          # popis archivu v zipu
MARKER = "archiving.json"                  # .spm/archiving.json – projekt se právě archivuje
RESTORES_SUFFIX = ".restores.json"         # vedle zipu: kdy a kdo projekt obnovil
# do archivu se nedává: koš, dočasné soubory, zámek zápisu, značky aktivace, značka archivace
SKIP_SPM = ("trash/", "tmp/", "active/", "write.lock", MARKER)


class ArchiveError(Exception):
    pass


@dataclass
class ArchiveEntry:
    path: Path                     # zip v archivu
    project: str
    archived_at: str = ""
    archived_by: str = ""
    comment: str = ""
    project_comment: str = ""
    files: int = 0
    size: int = 0
    restores: list[dict] = field(default_factory=list)


@dataclass
class ArchiveResult:
    zip_path: Path
    files: int
    size: int
    leftover: bool = False         # projekt se z pracovní složky nepodařilo celý smazat


# --- nastavení a kontroly ------------------------------------------------------------------

def archive_dir(settings) -> Path | None:
    """Archivní složka záložky: <síťová složka s projekty>/.spm/archiv; None = síťová složka
    není nastavená."""
    root = (settings.server_root or "").strip()
    if not root:
        return None
    return Path(os.path.normpath(Path(root) / SPM_DIR / ARCHIVE_SUBDIR))


def may_archive(settings, user: str) -> bool:
    return user.lower() in {a.lower() for a in settings.archivers}


def permission_problem(settings, user: str) -> str | None:
    if archive_dir(settings) is None:
        return "Síťová složka s projekty není nastavená."
    if not may_archive(settings, user):
        who = ", ".join(settings.archivers)
        return ("Archivovat a vracet z archivu smí jen: " + who if who else
                "Archivovat zatím nesmí nikdo – oprávnění se nastavuje ve společné konfiguraci "
                "(Nastavení → Společné nastavení → Archivovat smí).")
    if not Path(settings.server_root).is_dir():
        return f"Síťová složka s projekty není dostupná:\n{settings.server_root}"
    return None


def cleanup_problem(settings, user: str) -> str | None:
    """Proč uživatel nesmí uklízet zálohy a koš (trvalé mazání) – smí jen ti, kdo smí archivovat."""
    if may_archive(settings, user):
        return None
    who = ", ".join(settings.archivers)
    return ("Úklid záloh a koše smí jen: " + who if who else
            "Úklid záloh a koše zatím nesmí nikdo – oprávnění má ten, kdo smí archivovat "
            "(Nastavení → Společné nastavení → Archivovat smí).")


def archive_problems(pm, name: str) -> list[str]:
    """Proč projekt nejde archivovat (prázdné = jde)."""
    problem = permission_problem(pm.settings, pm.user)
    if problem:
        return [problem]
    server = pm.server_dir(name)
    if not server.is_dir():
        return [f"Projekt není na serveru: {server}"]
    problems = []
    users = active_users(server)
    if db_path(server).exists():
        with ProjectDB.for_project(server) as db:
            sessions = db.active_sessions()
        if sessions:
            users = users or sessions
    if users:
        problems.append("Projekt má někdo aktivovaný – archivovat lze jen projekt, který nikdo nemá aktivovaný.")
    status = pm.project_status(name)
    if status is not None and status.script_pending:
        problems.append("Projekt má neproběhlý skript po odevzdání – nejdřív ho spusťte znovu nebo zahoďte.")
    try:
        open_notes = [n for n in pm.notes(name) if not n.resolved]
    except Exception:  # noqa: BLE001 – poškozené poznámky nebrání kontrole ostatního
        open_notes = []
    if open_notes:
        problems.append(f"Projekt má nevyřízené poznámky ({len(open_notes)}) – vyřiďte je v Poznámkách.")
    if (server / SPM_DIR / MARKER).exists():
        problems.append("Projekt se právě archivuje (nebo se předchozí archivace nedokončila).")
    return problems


def is_archived(settings, name: str) -> bool:
    """Je v archivní složce záložky zip s tímto projektem? (Zrcadlení ho pak nezaloží znovu.)"""
    folder = archive_dir(settings)
    if folder is None or not folder.is_dir():
        return False
    pattern = re.compile(re.escape(name) + r"(_\d{8}_\d{6})?\.zip$", re.IGNORECASE)
    try:
        return any(pattern.fullmatch(p.name) for p in folder.iterdir())
    except OSError:
        return False


def _zip_path(folder: Path, name: str) -> Path:
    path = folder / f"{name}.zip"
    if not path.exists():
        return path
    return folder / f"{name}_{datetime.now():%Y%m%d_%H%M%S}.zip"


# --- archivace -----------------------------------------------------------------------------

def _members(server: Path, keep_backups: int) -> list[tuple[Path, str]]:
    """Soubory projektu do zipu: (cesta, název v zipu)."""
    drop: set[str] = set()
    backups = server / SPM_DIR / "backup"
    if keep_backups <= 0:
        drop = {to_rel(server, p).lower() for p in backups.rglob("*") if p.is_file()} if backups.is_dir() else set()
    else:
        plan = storage.plan_cleanup(server, keep_backups, trash_days=None, orphans=True)
        drop = {rel.lower() for rel, _, _ in plan.remove}
    members = []
    for dirpath, _dirs, names in os.walk(server):
        for n in names:
            path = Path(dirpath) / n
            rel = to_rel(server, path)
            low = rel.lower()
            if low.endswith(TEMP_SUFFIX) or low in drop:
                continue
            if low.startswith(f"{SPM_DIR}/"):
                inner = low[len(SPM_DIR) + 1:]
                if inner.startswith(SKIP_SPM) or inner.startswith("clock."):
                    continue
            members.append((path, rel))
    return sorted(members, key=lambda m: m[1].lower())


def _verify(zip_path: Path, expected: int) -> None:
    try:
        with zipfile.ZipFile(zip_path) as zf:
            bad = zf.testzip()
            count = len([i for i in zf.infolist() if not i.is_dir()])
    except (zipfile.BadZipFile, OSError) as e:
        raise ArchiveError(f"Zip se nepodařilo ověřit:\n{e}") from None
    if bad is not None:
        raise ArchiveError(f"Zip je poškozený (soubor {bad}) – archivace se zrušila.")
    if count != expected:
        raise ArchiveError(f"Zip neobsahuje všechny soubory ({count} z {expected}) – archivace se zrušila.")


def archive_project(pm, name: str, comment: str = "", progress: Progress | None = None) -> ArchiveResult:
    """Zabalí projekt do zipu v archivní složce a z pracovní složky ho smaže (viz výše)."""
    problems = archive_problems(pm, name)
    if problems:
        raise ArchiveError("\n".join(problems))
    server = pm.server_dir(name)
    folder = archive_dir(pm.settings)
    folder.mkdir(parents=True, exist_ok=True)
    marker = server / SPM_DIR / MARKER
    marked = False                 # značku zapsala tahle archivace (jen tu po chybě smazat)
    commit_id = None
    target = _zip_path(folder, name)
    tmp = target.with_name(target.name + TEMP_SUFFIX)
    # od zápisu značky je všechno v jednom bloku: při jakékoli chybě se značka i záznam
    # „Archivace“ vrátí, aby projekt nezůstal zablokovaný
    try:
        with WriteLock(server, pm.user, pm.pc):
            with ProjectDB.for_project(server, create=True) as db:
                if db.active_sessions():
                    raise ArchiveError("Projekt mezitím někdo aktivoval.")
                if marker.exists():
                    raise ArchiveError("Projekt mezitím začal archivovat někdo jiný.")
                project_comment = read_comment(server)   # čteno v zámku – přímo ze souboru
                marker.write_text(json.dumps({"user": pm.user, "pc": pm.pc, "since": now_iso()}),
                                  encoding="utf-8")
                marked = True
                with db.conn:
                    commit_id = db.add_commit(None, pm.user, pm.pc, dbm.ARCHIVED,
                                              f"Archivováno do {folder}" + (f": {comment}" if comment else ""))
        members = _members(server, pm.settings.archive_keep_backups)
        files = sum(1 for _, rel in members if not rel.lower().startswith(f"{SPM_DIR}/"))
        info = {"format": 1, "project": name, "archived_at": now_iso(), "archived_by": pm.user,
                "display_name": pm.display_name, "pc": pm.pc, "comment": comment,
                "project_comment": project_comment.text if project_comment else "",
                "files": files, "source": str(server),
                "kept_backups": pm.settings.archive_keep_backups}
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            zf.writestr(ARCHIVE_INFO, json.dumps(info, ensure_ascii=False, indent=1))
            for n, (path, rel) in enumerate(members):
                if progress:
                    progress(n, len(members), rel)
                zf.write(path, rel)
        if progress:
            progress(len(members), len(members), "Ověřování archivu…")
        _verify(tmp, len(members) + 1)
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        if marked:
            marker.unlink(missing_ok=True)
        if commit_id is not None:  # nepovedená archivace nemá zůstat v historii
            try:
                with WriteLock(server, pm.user, pm.pc), ProjectDB.for_project(server) as db, db.conn:
                    db.conn.execute("DELETE FROM commits WHERE commit_id = ?", (commit_id,))
            except Exception:  # noqa: BLE001 – původní chyba je důležitější
                pass
        raise
    # zip je ověřený – projekt z pracovní složky pryč (nejdřív přejmenovat: zmizí ze seznamu
    # celý najednou, i když se mazání nějakého souboru nepovede)
    trash = server.parent / f".spm_archived_{name}_{uuid.uuid4().hex[:6]}"
    try:
        server.rename(trash)
    except OSError:
        trash = server
    rmtree(trash)
    return ArchiveResult(target, files, target.stat().st_size, leftover=trash.exists())


# --- seznam archivu ------------------------------------------------------------------------

def _restores(zip_path: Path) -> list[dict]:
    try:
        data = json.loads(zip_path.with_name(zip_path.name + RESTORES_SUFFIX).read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def list_archives(folder: Path) -> list[ArchiveEntry]:
    """Archivované projekty (nejnověji archivované první)."""
    entries = []
    try:
        zips = [p for p in folder.iterdir() if p.suffix.lower() == ".zip" and p.is_file()]
    except OSError:
        return []
    for z in zips:
        entry = ArchiveEntry(z, z.stem, size=z.stat().st_size, restores=_restores(z))
        try:
            with zipfile.ZipFile(z) as zf:
                info = json.loads(zf.read(ARCHIVE_INFO).decode("utf-8"))
            entry.project = info.get("project") or z.stem
            entry.archived_at = info.get("archived_at", "")
            entry.archived_by = info.get("display_name") or info.get("archived_by", "")
            entry.comment = info.get("comment", "")
            entry.project_comment = info.get("project_comment", "")
            entry.files = int(info.get("files") or 0)
        except (KeyError, ValueError, zipfile.BadZipFile, OSError):
            pass  # zip bez popisu (vytvořený jinak) – aspoň název a velikost
        entries.append(entry)
    return sorted(entries, key=lambda e: e.archived_at, reverse=True)


def archive_files(zip_path: Path) -> list[str]:
    """Soubory projektu v zipu (bez servisní složky a popisu) – náhled obsahu."""
    with zipfile.ZipFile(zip_path) as zf:
        return sorted((i.filename for i in zf.infolist()
                       if not i.is_dir() and i.filename != ARCHIVE_INFO
                       and not i.filename.lower().startswith(f"{SPM_DIR}/")), key=str.lower)


# --- obnova --------------------------------------------------------------------------------

def _zip_identity(zip_path: Path) -> str | None:
    """Identita projektu (project_id) uložená v zipu; None = zip bez evidence nebo starý projekt."""
    with zipfile.ZipFile(zip_path) as zf:
        try:
            data = zf.read(f"{SPM_DIR}/project.db")
        except KeyError:
            return None
    with tempfile.TemporaryDirectory(prefix="spm_archive_") as tmp:
        db_file = db_path(Path(tmp))
        db_file.parent.mkdir(parents=True)
        db_file.write_bytes(data)
        with ProjectDB.for_project(Path(tmp)) as db:
            return db.project_id()


def existing_identity(pm, zip_path: Path) -> str | None:
    """Název projektu v pracovní složce, který je tentýž projekt jako v zipu (stejná identita –
    obnovený už je, nebo se jen přejmenoval); None = žádný."""
    pid = _zip_identity(zip_path)
    if not pid:
        return None
    try:
        dirs = [d for d in pm.server_root.iterdir() if d.is_dir() and not d.name.startswith(".")]
    except OSError:
        return None
    for d in dirs:
        if not db_path(d).exists():
            continue
        try:
            with ProjectDB.for_project(d) as db:
                if db.project_id() == pid:
                    return d.name
        except Exception:  # noqa: BLE001 – nečitelná databáze jiného projektu
            continue
    return None


def identity_message(project: str, existing: str) -> str:
    return (f"Projekt {project} z archivu už v pracovní složce je"
            + (f" (jako {existing})" if existing.lower() != project.lower() else "")
            + " – obnovit ho znovu nelze, vznikly by dva projekty se stejnou identitou "
            "(historie a zrcadlení by se pletly).")

def restore_archive(pm, zip_path: Path, name: str | None = None, progress: Progress | None = None) -> Path:
    """Obnoví projekt ze zipu do pracovní síťové složky záložky pod názvem name (výchozí =
    původní). Vrací cestu k obnovenému projektu."""
    from .sync import validate_project_name
    problem = permission_problem(pm.settings, pm.user)
    if problem:
        raise ArchiveError(problem)
    entry = next((e for e in list_archives(zip_path.parent) if e.path == zip_path), None)
    name = (name or (entry.project if entry else zip_path.stem)).strip()
    error = validate_project_name(name)
    if error:
        raise ArchiveError(error)
    root = pm.server_root
    same = existing_identity(pm, zip_path)
    if same:
        raise ArchiveError(identity_message(entry.project if entry else zip_path.stem, same))
    if any(p.name.lower() == name.lower() for p in root.iterdir()):
        raise ArchiveError(f"V pracovní složce už projekt {name} existuje.")
    tmp = root / f".spm_restoring_{name}_{uuid.uuid4().hex[:6]}"
    try:
        with zipfile.ZipFile(zip_path) as zf:
            bad = zf.testzip()
            if bad is not None:
                raise ArchiveError(f"Zip je poškozený (soubor {bad}) – obnovit nelze.")
            items = [i for i in zf.infolist() if not i.is_dir() and i.filename != ARCHIVE_INFO]
            for n, info in enumerate(items):
                rel = PurePosixPath(info.filename.replace("\\", "/"))
                if rel.is_absolute() or ".." in rel.parts:
                    raise ArchiveError(f"Zip obsahuje nebezpečnou cestu:\n{info.filename}")
                if progress:
                    progress(n, len(items), str(rel))
                dst = tmp.joinpath(*rel.parts)
                dst.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(dst, "wb") as out:
                    while chunk := src.read(1024 * 1024):
                        out.write(chunk)
                os.utime(dst, (datetime(*info.date_time).timestamp(),) * 2)
    except BaseException:
        rmtree(tmp)
        raise
    target = root / name
    tmp.rename(target)
    with WriteLock(target, pm.user, pm.pc):
        with ProjectDB.for_project(target, create=True) as db, db.conn:
            db.add_commit(None, pm.user, pm.pc, dbm.UNARCHIVED,
                          f"Obnoveno z archivu {zip_path.name}" + (f" jako {name}" if entry and name != entry.project else ""))
    restores = _restores(zip_path) + [{"at": now_iso(), "by": pm.display_name, "as": name}]
    try:
        zip_path.with_name(zip_path.name + RESTORES_SUFFIX).write_text(
            json.dumps(restores, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass  # záznam o obnově je jen informativní
    return target
