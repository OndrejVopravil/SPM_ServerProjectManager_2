"""Operace se soubory v záložce „Složka“ (obyčejná složka mimo spolupráci – např. Data).

Kopírování a přesun souborů i celých složek do cílové složky; existující položky se
přepíšou jen na výslovné přání (jinak se přeskočí). Kopie nejsou nikdy jen pro čtení
(soubory z místní kopie projektu jsou často zamčené proti zápisu).
"""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

from .scanner import same_path


def _writable(path: Path) -> None:
    try:
        mode = path.stat().st_mode
        if not mode & stat.S_IWRITE:
            path.chmod(mode | stat.S_IWRITE)
    except OSError:
        pass


COPY_MARK = "_copy"


def copy_name(name: str, n: int = 1, is_dir: bool = False) -> str:
    """Název kopie: deska.elt -> deska_copy.elt, deska_copy2.elt…; složka Vykresy -> Vykresy_copy."""
    mark = COPY_MARK + ("" if n == 1 else str(n))
    stem, dot, ext = name.rpartition(".")
    if is_dir or not dot or not stem:  # složka, bez přípony, nebo „.skryty“
        return name + mark
    return f"{stem}{mark}.{ext}"


def free_copy_name(name: str, taken, is_dir: bool = False) -> str:
    """První volný název kopie; taken(název) = je obsazený."""
    n = 1
    while taken(copy_name(name, n, is_dir)):
        n += 1
    return copy_name(name, n, is_dir)


def free_name(name: str, taken, is_dir: bool = False) -> str:
    """Název, pokud je volný; jinak první volný název kopie (_copy, _copy2…)."""
    return free_copy_name(name, taken, is_dir) if taken(name) else name


def targets(sources: list[Path], dest: Path) -> list[tuple[Path, Path]]:
    """Dvojice (zdroj, cíl) – položky se vloží do dest pod svým názvem."""
    return [(src, dest / src.name) for src in sources]


def conflicts(pairs: list[tuple[Path, Path]]) -> list[Path]:
    """Cíle, které už existují (a nejde o tentýž soubor)."""
    return [dst for src, dst in pairs if dst.exists() and not same_path(src, dst)]


def keep_both(pairs: list[tuple[Path, Path]]) -> list[tuple[Path, Path]]:
    """„Ponechat obě“: cíl, který už existuje, dostane název kopie (soubor_copy.ext) – nic se
    nepřepíše ani nesloučí."""
    used: set[str] = set()
    result = []
    for src, dst in pairs:
        if dst.exists() and not same_path(src, dst):
            name = free_copy_name(dst.name, lambda n: (dst.parent / n).exists()
                                  or str(dst.parent / n).lower() in used, src.is_dir())
            dst = dst.with_name(name)
        used.add(str(dst).lower())
        result.append((src, dst))
    return result


def _into_itself(src: Path, dst: Path) -> bool:
    try:
        return src.is_dir() and dst.resolve().is_relative_to(src.resolve())
    except OSError:
        return False


def copy_items(pairs: list[tuple[Path, Path]], overwrite: bool = False) -> tuple[list[Path], list[str]]:
    """Zkopíruje soubory a složky. Vrací (zkopírované soubory v cíli, přeskočené s důvodem)."""
    done: list[Path] = []
    skipped: list[str] = []
    for src, dst in pairs:
        if same_path(src, dst):  # vložení na stejné místo = duplikát s _copy v názvu
            dst = dst.with_name(free_copy_name(dst.name, lambda n: (dst.parent / n).exists(), src.is_dir()))
        if _into_itself(src, dst):
            skipped.append(f"{src.name} – složku nelze kopírovat do sebe sama")
        elif src.is_dir():
            for f in sorted(p for p in src.rglob("*") if p.is_file()):
                target = dst / f.relative_to(src)
                _copy_file(f, target, overwrite, done, skipped)
            for d in (p for p in src.rglob("*") if p.is_dir()):  # i prázdné složky
                (dst / d.relative_to(src)).mkdir(parents=True, exist_ok=True)
            dst.mkdir(parents=True, exist_ok=True)
        elif src.is_file():
            _copy_file(src, dst, overwrite, done, skipped)
        else:
            skipped.append(f"{src.name} – neexistuje")
    return done, skipped


def _copy_file(src: Path, dst: Path, overwrite: bool, done: list[Path], skipped: list[str]) -> None:
    if dst.exists() and not overwrite:
        skipped.append(f"{dst.name} – v cíli už existuje")
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        _writable(dst)
    shutil.copy2(src, dst)
    _writable(dst)
    done.append(dst)


def move_items(pairs: list[tuple[Path, Path]], overwrite: bool = False) -> tuple[list[Path], list[str]]:
    """Přesune soubory a složky (i do existující složky – obsah se sloučí). Vrací (přesunuté
    položky v cíli, přeskočené s důvodem)."""
    done: list[Path] = []
    skipped: list[str] = []
    for src, dst in pairs:
        if same_path(src, dst):
            continue  # beze změny
        if _into_itself(src, dst):
            skipped.append(f"{src.name} – složku nelze přesunout do sebe sama")
            continue
        _move(src, dst, overwrite, done, skipped)
    return done, skipped


def _move(src: Path, dst: Path, overwrite: bool, done: list[Path], skipped: list[str]) -> None:
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        done.append(dst)
        return
    if src.is_dir() and dst.is_dir():  # sloučit obsah
        for child in list(src.iterdir()):
            _move(child, dst / child.name, overwrite, done, skipped)
        try:
            src.rmdir()
        except OSError:
            pass  # něco se přeskočilo – zdrojová složka zůstává
        return
    if not overwrite or src.is_dir() != dst.is_dir():
        skipped.append(f"{dst.name} – v cíli už existuje")
        return
    _writable(dst)
    os.replace(src, dst)
    done.append(dst)
