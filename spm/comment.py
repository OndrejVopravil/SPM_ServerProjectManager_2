"""Komentář projektu – krátký popis zobrazený v hlavním seznamu, který mohou upravovat všichni.

Aktuální text je v <projekt>/.spm/comment.json (seznam projektů ho čte u každého projektu,
otevírat kvůli tomu databázi by bylo na síti pomalé). Každá změna se navíc zapíše do historie
projektu v databázi (druh záznamu PROJECT_COMMENT, komentář záznamu = nový text).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from . import SPM_DIR
from .scanner import TEMP_SUFFIX

COMMENT_FILE = "comment.json"
MAX_LENGTH = 1000


@dataclass
class ProjectComment:
    text: str
    user: str = ""
    pc: str = ""
    changed_at: str = ""  # ISO čas změny

    @property
    def first_line(self) -> str:
        return self.text.split("\n", 1)[0]

    def describe_change(self) -> str:
        """„změnil alice (PC-1), 12. 10. 2026 14:30“ – pro tooltip."""
        try:
            when = datetime.fromisoformat(self.changed_at)
            stamp = f"{when.day}. {when.month}. {when.year} {when:%H:%M}"
        except ValueError:
            stamp = self.changed_at
        who = f"{self.user} ({self.pc})" if self.pc else self.user
        return f"Změnil {who}, {stamp}" if who else stamp


def normalize_text(text: str) -> str:
    """Jednotné konce řádků, bez mezer na koncích řádků a prázdných řádků na začátku a konci."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines).strip("\n")


def comment_path(project_dir: Path) -> Path:
    return project_dir / SPM_DIR / COMMENT_FILE


def read_comment(project_dir: Path) -> ProjectComment | None:
    """Aktuální komentář projektu; None = žádný (nebo nečitelný soubor)."""
    try:
        data = json.loads(comment_path(project_dir).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("text"), str) or not data["text"]:
        return None
    return ProjectComment(data["text"], *(str(data.get(k) or "") for k in ("user", "pc", "changed_at")))


def write_comment(project_dir: Path, comment: ProjectComment) -> None:
    """Uloží komentář (přes dočasný soubor); prázdný text soubor smaže."""
    path = comment_path(project_dir)
    if not comment.text:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + TEMP_SUFFIX)
    try:
        tmp.write_text(json.dumps(asdict(comment), indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
