"""Místní manifest – co je v místní kopii projektu a z jaké verze na serveru to pochází.

Uložen v <místní kopie>/.spm/manifest.json. Podle něj se pozná, které soubory uživatel
v místní kopii změnil (hash se liší od staženého) a které se mezitím změnily na serveru
(hash v databázi se liší od staženého).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import SPM_DIR


@dataclass
class Entry:
    file_id: str
    sha256: str


@dataclass
class Manifest:
    project: str
    session_id: int
    files: dict[str, Entry] = field(default_factory=dict)  # relativní cesta -> položka

    @staticmethod
    def path(local: Path) -> Path:
        return local / SPM_DIR / "manifest.json"

    @classmethod
    def load(cls, local: Path) -> "Manifest | None":
        try:
            data = json.loads(cls.path(local).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        files = {p: Entry(**e) for p, e in data.get("files", {}).items()}
        return cls(data["project"], data["session_id"], files)

    def save(self, local: Path) -> None:
        path = self.path(local)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"project": self.project, "session_id": self.session_id,
                "files": {p: {"file_id": e.file_id, "sha256": e.sha256} for p, e in self.files.items()}}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)

    def path_of(self, file_id: str) -> str | None:
        for p, e in self.files.items():
            if e.file_id == file_id:
                return p
        return None

    def by_file_id(self) -> dict[str, str]:
        return {e.file_id: p for p, e in self.files.items()}
