"""Souběžná práce na projektu – kdo má projekt aktivovaný a krátký zámek pro zápis.

- Značky aktivních uživatelů: .spm/active/<uživatel>@<počítač>.json. Slouží jen pro rychlé
  zobrazení v seznamu projektů (bez otevírání databáze); rozhodující je databáze.
- Zámek zápisu: .spm/write.lock. Do databáze projektu může zapisovat víc uživatelů, ale
  SQLite na síťovém disku se na vlastní zamykání spolehnout nemůže. Každý zápis (krátká
  transakce) se proto provádí jen při držení tohoto souboru. Soubor se vytváří pomocí
  O_CREAT | O_EXCL, což je atomické i na SMB.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from . import SPM_DIR

WRITE_LOCK_TIMEOUT = 30.0   # jak dlouho čekat, než je databáze volná
WRITE_LOCK_STALE = 120.0    # starší zámek zápisu je pozůstatek spadlé aplikace


class LockError(Exception):
    pass


@dataclass
class LockInfo:
    """Aktivní uživatel projektu."""
    user: str
    pc: str
    since: str
    session_id: int | None = None

    def is_mine(self, user: str, pc: str) -> bool:
        return self.user.lower() == user.lower() and self.pc.lower() == pc.lower()

    @property
    def label(self) -> str:
        return f"{self.user} ({self.pc})"


def _safe(text: str) -> str:
    return re.sub(r"[^\w.-]", "_", text)


def active_dir(project_dir: Path) -> Path:
    return project_dir / SPM_DIR / "active"


def marker_path(project_dir: Path, user: str, pc: str) -> Path:
    return active_dir(project_dir) / f"{_safe(user)}@{_safe(pc)}.json".lower()


def active_users(project_dir: Path) -> list[LockInfo]:
    """Uživatelé, kteří mají projekt aktivovaný (podle značek)."""
    folder = active_dir(project_dir)
    try:
        entries = sorted(folder.glob("*.json"))
    except OSError:
        return []
    result = []
    for path in entries:
        try:
            result.append(LockInfo(**json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, ValueError, TypeError):
            continue  # značka se právě zapisuje / je poškozená
    return result


def add_marker(project_dir: Path, info: LockInfo) -> None:
    path = marker_path(project_dir, info.user, info.pc)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(info), ensure_ascii=False), encoding="utf-8")


def remove_marker(project_dir: Path, user: str, pc: str) -> None:
    marker_path(project_dir, user, pc).unlink(missing_ok=True)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class WriteLock:
    """Krátkodobý zámek pro zápis do databáze projektu (with WriteLock(...): ...)."""

    def __init__(self, project_dir: Path, user: str, pc: str,
                 timeout: float = WRITE_LOCK_TIMEOUT, stale: float = WRITE_LOCK_STALE):
        self.path = project_dir / SPM_DIR / "write.lock"
        self.user, self.pc = user, pc
        self.timeout, self.stale = timeout, stale

    def __enter__(self) -> "WriteLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if self._is_stale():
                    self.path.unlink(missing_ok=True)
                    continue
                if time.monotonic() > deadline:
                    raise LockError(
                        "Databáze projektu je zaneprázdněná (zapisuje do ní jiný uživatel). "
                        "Zkuste to prosím za chvíli znovu."
                    ) from None
                time.sleep(0.2)
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"user": self.user, "pc": self.pc, "since": now_iso()}, f)
            return self

    def _is_stale(self) -> bool:
        try:
            return time.time() - self.path.stat().st_mtime > self.stale
        except FileNotFoundError:
            return False

    def __exit__(self, *exc) -> None:
        self.path.unlink(missing_ok=True)

    def moved_to(self, project_dir: Path) -> None:
        """Projektová složka byla přejmenována – zámek je teď na nové cestě."""
        self.path = project_dir / SPM_DIR / "write.lock"
