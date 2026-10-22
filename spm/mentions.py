"""Zmínky v textu poznámek a vzkazů.

- „@jméno“ – uživatel (login nebo přezdívka) je „na vědomí“.
- „#projekt“ – odkaz na projekt síťové složky (jen ve vzkazech); odkazem je jen název
  existujícího projektu, obyčejné „#2 bod“ zůstává textem.
"""

from __future__ import annotations

import re
from urllib.parse import quote

MENTION_RE = re.compile(r"(?:^|(?<=\s))@([\w.\-]+)")
PROJECT_RE = re.compile(r"(?:^|(?<=[\s(\[]))#([^\s#@,;()\[\]]+)")
_TRAILING = ".,:;!?\"'"


def mentions(text: str, users: list) -> list[tuple[str, str]]:
    """Uživatelé zmínění „@jméno“: [(login, zobrazované jméno)] bez opakování. Známý uživatel
    se pozná podle loginu nebo přezdívky; neznámé jméno se bere jako login (SPM ještě nespustil)."""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for word in MENTION_RE.findall(text):
        word = word.rstrip(_TRAILING)
        if not word:
            continue
        user = next((u for u in users if word.lower() in (u.login.lower(), (u.nickname or "").lower())), None)
        login, name = (user.login, user.display) if user else (word, "")
        if login.lower() not in seen:
            seen.add(login.lower())
            found.append((login.lower(), name))
    return found


def project_refs(text: str, names: list[str]) -> list[str]:
    """Projekty zmíněné „#název“ (jen existující, ve správné velikosti písmen, bez opakování)."""
    by_lower = {n.lower(): n for n in names}
    found: list[str] = []
    for word in PROJECT_RE.findall(text):
        name = by_lower.get(word.rstrip(_TRAILING).lower())
        if name and name not in found:
            found.append(name)
    return found


def link_projects(text: str, names: list[str], scheme: str = "spm-project:") -> str:
    """Markdown s odkazy na zmíněné projekty: „#058768“ -> „[#058768](spm-project:058768)“."""
    by_lower = {n.lower(): n for n in names}

    def repl(m: re.Match) -> str:
        word = m.group(1)
        stripped = word.rstrip(_TRAILING)
        name = by_lower.get(stripped.lower())
        if not name:
            return m.group(0)
        return f"[#{stripped}]({scheme}{quote(name)}){word[len(stripped):]}"
    return PROJECT_RE.sub(repl, text)
