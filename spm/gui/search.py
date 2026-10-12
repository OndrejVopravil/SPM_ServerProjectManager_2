"""Vyhledávání „obsahuje“ bez ohledu na velikost písmen a diakritiku."""

from __future__ import annotations

import re
import unicodedata
from typing import Callable


def normalize(text: str) -> str:
    """Malá písmena a bez diakritiky („Ústí“ -> „usti“)."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def contains(haystack: str, needle_normalized: str) -> bool:
    return not needle_normalized or needle_normalized in normalize(haystack)


def wildcard_matcher(pattern: str) -> Callable[[str], bool] | None:
    """„Obsahuje“ se zástupnými znaky: * = cokoliv (i nic), ? = jeden znak; bez ohledu na
    velikost písmen a diakritiku. „*xyz“ najde „abcdxyz“, „e*01“ najde „deska_E01.elt“.
    Prázdný vzor (nebo jen hvězdičky) -> None = nehledá se."""
    needle = normalize(pattern.strip())
    if not needle.replace("*", ""):
        return None
    regex = re.compile("".join(".*" if ch == "*" else "." if ch == "?" else re.escape(ch)
                               for ch in needle))
    return lambda text: regex.search(normalize(text)) is not None
