"""Vyhledávání „obsahuje“ bez ohledu na velikost písmen a diakritiku."""

from __future__ import annotations

import unicodedata


def normalize(text: str) -> str:
    """Malá písmena a bez diakritiky („Ústí“ -> „usti“)."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def contains(haystack: str, needle_normalized: str) -> bool:
    return not needle_normalized or needle_normalized in normalize(haystack)
