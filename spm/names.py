"""Nepovolené znaky v názvech projektů, složek a souborů."""

from __future__ import annotations

import os
import posixpath
from pathlib import Path
from typing import Iterable

from . import SPM_DIR
from .scanner import is_ignored

CZECH_DIACRITICS = "áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ"
FORBIDDEN_CHARS = frozenset(CZECH_DIACRITICS + " ()&,.;=^")


def forbidden_in(text: str) -> list[str]:
    """Nepovolené znaky obsažené v textu (seřazené, bez opakování)."""
    return sorted({ch for ch in text if ch in FORBIDDEN_CHARS})


def describe_chars(chars: Iterable[str]) -> str:
    return " ".join("mezera" if ch == " " else ch for ch in chars)


def file_stem(name: str) -> str:
    """Název souboru bez přípony – tečka před příponou je povolená („a.dwg“ -> „a“)."""
    return posixpath.splitext(name)[0]


def invalid_rel_paths(paths: Iterable[str]) -> list[str]:
    """Z relativních cest souborů ('/' jako oddělovač) vybere ty, jejichž název souboru
    (bez přípony) nebo některá nadřazená složka obsahuje nepovolený znak.
    Vadná složka se hlásí jednou, s koncovým '/'."""
    result: set[str] = set()
    for path in paths:
        parts = path.split("/")
        for i, folder in enumerate(parts[:-1]):
            if forbidden_in(folder):
                result.add("/".join(parts[: i + 1]) + "/")
        if forbidden_in(file_stem(parts[-1])):
            result.add(path)
    return sorted(result, key=str.lower)


def find_invalid_names(root: Path, ignore: Iterable[str] = ()) -> list[str]:
    """Projde složku projektu (bez .spm a ignorovaných souborů) a vrátí vadné názvy.
    Čte jen názvy, ne obsah ani atributy souborů, takže je rychlé i přes síť."""
    ignore = list(ignore)
    result: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        prefix = "" if rel_dir == "." else rel_dir + "/"
        if not prefix and SPM_DIR in dirnames:
            dirnames.remove(SPM_DIR)
        dirnames[:] = [d for d in dirnames if not is_ignored(d, ignore)]
        for d in dirnames:
            if forbidden_in(d):
                result.append(f"{prefix}{d}/")
        for f in filenames:
            if not is_ignored(f, ignore) and forbidden_in(file_stem(f)):
                result.append(prefix + f)
    return sorted(result, key=str.lower)


def format_invalid(paths: list[str], limit: int = 15) -> str:
    """Seznam vadných názvů pro zobrazení uživateli (s vyznačením nalezených znaků)."""
    lines = []
    for p in paths[:limit]:
        name = p.rstrip("/").rsplit("/", 1)[-1]
        chars = forbidden_in(name if p.endswith("/") else file_stem(name))
        lines.append(f"{p}   [{describe_chars(chars)}]")
    if len(paths) > limit:
        lines.append(f"… a dalších {len(paths) - limit}")
    return "\n".join(lines)
