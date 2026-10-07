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
    sha256: str                 # obsah souboru v místní kopii (tak, jak se stáhl / nahrál)
    # hash v databázi v okamžiku stažení, pokud se liší od obsahu (soubor změněný mimo
    # aplikaci, který drží jiný uživatel – databáze se nemění). Aktualizace stahuje znovu,
    # až když se změní databáze, ne při každém porovnání.
    db_sha: str | None = None

    @property
    def known_db_sha(self) -> str:
        return self.db_sha or self.sha256


@dataclass
class Manifest:
    project: str
    session_id: int
    files: dict[str, Entry] = field(default_factory=dict)  # relativní cesta -> položka
    # aktivováno s volbou „Nestahovat neaktivované soubory“: místní kopie obsahuje jen část
    # projektu a aktualizace ze serveru nestahuje soubory, které v ní nejsou
    partial: bool = False

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
        return cls(data["project"], data["session_id"], files, bool(data.get("partial", False)))

    def save(self, local: Path) -> None:
        path = self.path(local)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"project": self.project, "session_id": self.session_id, "partial": self.partial,
                "files": {p: {"file_id": e.file_id, "sha256": e.sha256,
                              **({"db_sha": e.db_sha} if e.db_sha else {})}
                          for p, e in self.files.items()}}
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
