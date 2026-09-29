"""Detekce přejmenovaných / přesunutých souborů.

Porovnávají se pouze soubory, které v místní kopii chybí (jsou v databázi, ale
lokálně ne), se soubory, které jsou nové (lokálně ano, v databázi ne).

- Shodný obsah (SHA-256)  -> jistota 100 %, jen přejmenování/přesun.
- Rozdílný obsah          -> skóre podobnosti z názvu, přípony, složky a velikosti.
                             Nabídne se, pokud skóre překročí nastavený práh.
Párování probíhá od nejlepší shody, každý soubor lze přiřadit jen jednou.
Výsledek je pouze návrh – uživatel jej v dialogu potvrdí nebo upraví.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass
from difflib import SequenceMatcher

from .db import DbFile
from .scanner import FileInfo

W_NAME = 0.45
W_EXT = 0.20
W_DIR = 0.15
W_SIZE = 0.20


@dataclass
class RenameSuggestion:
    file_id: str
    old_path: str
    new_path: str
    score: float           # 0–1
    same_content: bool
    reason: str


def _split(path: str) -> tuple[str, str, str]:
    directory, name = posixpath.split(path.lower())
    stem, ext = posixpath.splitext(name)
    return directory, stem, ext


def similarity(old: DbFile, new: FileInfo) -> tuple[float, str]:
    """Skóre podobnosti dvou souborů s rozdílným obsahem + textové zdůvodnění."""
    o_dir, o_stem, o_ext = _split(old.path)
    n_dir, n_stem, n_ext = _split(new.path)

    name = SequenceMatcher(None, o_stem, n_stem).ratio()
    ext = 1.0 if o_ext == n_ext else 0.0
    folder = 1.0 if o_dir == n_dir else SequenceMatcher(None, o_dir, n_dir).ratio() * 0.5
    big, small = max(old.size, new.size), min(old.size, new.size)
    size = 1.0 if big == 0 else small / big

    score = W_NAME * name + W_EXT * ext + W_DIR * folder + W_SIZE * size
    if not ext:
        score *= 0.5  # jiná přípona je silný signál, že jde o jiný soubor

    reason = f"název {name:.0%}, velikost {size:.0%}"
    reason += ", stejná složka" if o_dir == n_dir else ", jiná složka"
    if not ext:
        reason += ", jiná přípona"
    return score, reason


def suggest_renames(
    missing: list[DbFile], new: list[FileInfo], threshold: float = 0.6
) -> list[RenameSuggestion]:
    candidates: list[tuple[float, float, RenameSuggestion]] = []
    for old in missing:
        for nf in new:
            heur, reason = similarity(old, nf)
            if old.sha256 and old.sha256 == nf.sha256:
                sugg = RenameSuggestion(old.file_id, old.path, nf.path, 1.0, True, "stejný obsah")
                # shoda obsahu má vždy přednost, heuristika jen rozhoduje mezi duplikáty
                candidates.append((2.0, heur, sugg))
            elif heur >= threshold:
                sugg = RenameSuggestion(old.file_id, old.path, nf.path, heur, False,
                                        "upraveno – " + reason)
                candidates.append((heur, heur, sugg))

    candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
    used_old: set[str] = set()
    used_new: set[str] = set()
    result: list[RenameSuggestion] = []
    for _, _, sugg in candidates:
        if sugg.file_id in used_old or sugg.new_path in used_new:
            continue
        used_old.add(sugg.file_id)
        used_new.add(sugg.new_path)
        result.append(sugg)
    return result
