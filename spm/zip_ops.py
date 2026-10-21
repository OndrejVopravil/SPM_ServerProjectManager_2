"""Ruční zabalení souborů do ZIP a rozbalení ZIP (strom projektu i záložka „Složka“).

Název zipu:
- jeden soubor  -> podle složky, ve které je (v kořeni podle projektu / složky záložky),
- jedna složka  -> podle složky,
- víc položek   -> podle společné nadřazené složky (v kořeni podle projektu / složky záložky).
Zip vznikne ve společné nadřazené složce položek. Rozbalení vytvoří vedle zipu složku se
stejným názvem jako zip (bez přípony).

Cesty jsou relativní s '/' jako oddělovačem; složka končí '/' ('' = kořen).
"""

from __future__ import annotations

import os
import zipfile
from pathlib import Path, PurePosixPath

from .scanner import TEMP_SUFFIX

ZIP_EXT = ".zip"
FALLBACK_NAME = "archiv"


class ZipError(Exception):
    pass


def is_zip(path: str) -> bool:
    return path.lower().endswith(ZIP_EXT) and not path.endswith("/")


def _parent(scope: str) -> str:
    """Nadřazená složka položky ('' = kořen, jinak 'a/b/')."""
    parent = scope.rstrip("/").rpartition("/")[0]
    return parent + "/" if parent else ""


def zip_base(scopes: list[str]) -> str:
    """Společná nadřazená složka vybraných položek – tam zip vznikne."""
    parents = [_parent(s).rstrip("/").split("/") if _parent(s) else [] for s in scopes]
    common: list[str] = []
    for parts in zip(*parents):
        if all(p.lower() == parts[0].lower() for p in parts):
            common.append(parts[0])
        else:
            break
    return "/".join(common) + "/" if common else ""


def zip_name(scopes: list[str], root_name: str) -> str:
    """Název zipu (bez přípony) podle pravidel výše; root_name = projekt / složka záložky."""
    if len(scopes) == 1 and scopes[0].endswith("/"):
        name = scopes[0].rstrip("/").rpartition("/")[2]
    else:
        base = zip_base(scopes)
        name = base.rstrip("/").rpartition("/")[2] if base else root_name
    return name or root_name or FALLBACK_NAME


def unzip_folder_name(zip_rel: str) -> str:
    """Složka pro rozbalení: název zipu bez přípony."""
    name = zip_rel.rpartition("/")[2]
    return name[:-len(ZIP_EXT)] if is_zip(name) else name


def make_zip(members: list[tuple[Path, str]], dest: Path) -> int:
    """Zabalí soubory (zdroj, název v zipu) do dest. Zapisuje se do dočasného souboru,
    přerušené balení nezanechá poškozený zip. Vrací počet zabalených souborů."""
    if not members:
        raise ZipError("Ve výběru nejsou žádné soubory k zabalení.")
    tmp = dest.with_name(dest.name + TEMP_SUFFIX)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            for src, arcname in members:
                zf.write(src, arcname)
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return len(members)


def _safe_names(zf: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, PurePosixPath]]:
    """Položky zipu s bezpečnou relativní cestou (bez absolutních cest a '..')."""
    result = []
    for info in zf.infolist():
        name = info.filename.replace("\\", "/")
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or (path.parts and ":" in path.parts[0]):
            raise ZipError(f"Zip obsahuje nebezpečnou cestu, nerozbalí se:\n{info.filename}")
        if path.parts:
            result.append((info, path))
    return result


def extract_zip(zip_path: Path, dest: Path) -> list[Path]:
    """Rozbalí zip do složky dest. Má-li zip jedinou hlavní složku se stejným názvem jako
    zip (typicky zabalená složka), nevnoří se dvakrát (Vykresy/Vykresy/…). Vrací soubory."""
    stem = unzip_folder_name(zip_path.name).lower()
    try:
        zf = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError) as e:
        raise ZipError(f"Soubor není platný zip:\n{zip_path.name}\n({e})") from None
    with zf:
        items = _safe_names(zf)
        tops = {p.parts[0].lower() for _, p in items}
        strip = (len(tops) == 1 and next(iter(tops)) == stem
                 and all(len(p.parts) > 1 or info.is_dir() for info, p in items))
        files: list[Path] = []
        dest.mkdir(parents=True, exist_ok=True)
        for info, path in items:
            parts = path.parts[1:] if strip else path.parts
            if not parts:
                continue
            target = dest.joinpath(*parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                while chunk := src.read(1024 * 1024):
                    out.write(chunk)
            files.append(target)
    return files
