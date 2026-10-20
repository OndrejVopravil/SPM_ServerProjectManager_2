"""Procházení složek, výpočet hashů a kopírování souborů."""

from __future__ import annotations

import fnmatch
import hashlib
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from . import SPM_DIR

CHUNK = 1024 * 1024
# Tolerance pro porovnání mtime (FAT/SMB mají rozlišení 2 s)
MTIME_TOLERANCE = 2.0

Progress = Callable[[int, int, str], None]  # (hotovo, celkem, text)

# Dočasné soubory SPM při kopírování – ignorují se vždy, bez ohledu na nastavení
TEMP_SUFFIX = ".spmtmp"


@dataclass
class FileInfo:
    path: str        # relativní cesta s '/' jako oddělovačem
    size: int
    mtime: float
    sha256: str = ""


class ScanError(Exception):
    pass


def to_rel(root: Path, full: Path) -> str:
    return full.relative_to(root).as_posix()


def to_abs(root: Path, rel: str) -> Path:
    return root.joinpath(*rel.split("/"))


def is_ignored(name: str, patterns: Iterable[str]) -> bool:
    lname = name.lower()
    return lname.endswith(TEMP_SUFFIX) or any(fnmatch.fnmatch(lname, p.lower()) for p in patterns)


def list_files(root: Path, ignore: Iterable[str] = ()) -> list[Path]:
    """Všechny soubory ve složce kromě servisní složky .spm a ignorovaných vzorů."""
    ignore = list(ignore)
    result: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        if Path(dirpath) == root and SPM_DIR in dirnames:
            dirnames.remove(SPM_DIR)
        dirnames[:] = [d for d in dirnames if not is_ignored(d, ignore)]
        for name in filenames:
            if not is_ignored(name, ignore):
                result.append(Path(dirpath) / name)
    result.sort()
    return result


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def stat_info(root: Path, full: Path) -> FileInfo:
    st = full.stat()
    return FileInfo(to_rel(root, full), st.st_size, st.st_mtime)


def scan(root: Path, ignore: Iterable[str] = (), progress: Progress | None = None) -> dict[str, FileInfo]:
    """Projde složku a u každého souboru spočítá SHA-256."""
    files = list_files(root, ignore)
    result: dict[str, FileInfo] = {}
    for i, full in enumerate(files):
        info = stat_info(root, full)
        if progress:
            progress(i, len(files), info.path)
        try:
            info.sha256 = file_hash(full)
        except PermissionError:
            raise ScanError(
                f"Soubor nelze přečíst – je pravděpodobně otevřen v jiné aplikaci:\n{full}"
            ) from None
        result[info.path] = info
    if progress:
        progress(len(files), len(files), "")
    return result


def copy_with_hash(src: Path, dst: Path, writable: bool = False) -> str:
    """Zkopíruje soubor včetně času změny a vrátí SHA-256 obsahu. writable = kopie nikdy
    nebude jen pro čtení (atributy se jinak přenesou ze zdroje).

    Zapisuje se nejdřív do dočasného souboru, který se pak atomicky přejmenuje,
    aby přerušené kopírování nezanechalo poškozený cílový soubor.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        set_readonly(dst, False)  # soubor jen pro čtení nejde ve Windows nahradit
    tmp = dst.with_name(dst.name + TEMP_SUFFIX)
    h = hashlib.sha256()
    try:
        with open(src, "rb") as fin, open(tmp, "wb") as fout:
            while chunk := fin.read(CHUNK):
                h.update(chunk)
                fout.write(chunk)
        shutil.copystat(src, tmp)
        if writable:
            set_readonly(tmp, False)
        os.replace(tmp, dst)
    except PermissionError:
        tmp.unlink(missing_ok=True)
        raise ScanError(f"Soubor nelze zkopírovat – je otevřen v jiné aplikaci:\n{src}") from None
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return h.hexdigest()


def set_readonly(path: Path, readonly: bool) -> None:
    """Nastaví / zruší atribut „jen pro čtení“. Soubory, které uživatel nemá zamčené,
    jsou v místní kopii jen pro čtení, aby je omylem neupravil (Cimatron je otevře jen pro čtení)."""
    try:
        mode = path.stat().st_mode
        path.chmod(mode & ~stat.S_IWRITE if readonly else mode | stat.S_IWRITE)
    except FileNotFoundError:
        pass


def _clear_readonly_and_retry(func, path, _exc) -> None:
    os.chmod(path, stat.S_IWRITE)
    func(path)


def rmtree(path: Path) -> None:
    """shutil.rmtree, které smaže i soubory jen pro čtení; ostatní chyby ignoruje."""
    try:
        shutil.rmtree(path, onexc=_clear_readonly_and_retry)
    except OSError:
        pass


def move_file(src: Path, dst: Path) -> None:
    """Přesun souboru i přes atribut jen pro čtení (atribut zůstane zachován)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dst)


def same_stat(a_size: int, a_mtime: float, b_size: int, b_mtime: float) -> bool:
    return a_size == b_size and abs(a_mtime - b_mtime) <= MTIME_TOLERANCE


def fix_case(root: Path, rel: str) -> None:
    """Disk bez rozlišení velikosti písmen (Windows): po přesunu dostanou složky i soubor cesty
    velikost písmen podle rel (přejmenování „A“ -> „a“ by jinak ponechalo starou složku „A“)."""
    current = root
    for part in rel.split("/"):
        try:
            actual = next((n for n in os.listdir(current) if n.lower() == part.lower()), None)
        except OSError:
            return
        if actual is None:
            return
        if actual != part:
            try:
                os.rename(current / actual, current / part)
            except OSError:
                return
        current = current / part


def same_path(a: Path, b: Path) -> bool:
    """Stejný soubor bez ohledu na velikost písmen (Windows)."""
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def remove_empty_parents(root: Path, rel: str) -> None:
    """Po přesunu/smazání souboru odstraní prázdné nadřazené složky (až po root)."""
    parent = to_abs(root, rel).parent
    while parent != root and root in parent.parents:
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent
