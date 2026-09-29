"""Zámek projektu – soubor .spm/lock.json na síťovém úložišti.

Soubor se vytváří pomocí O_CREAT | O_EXCL, což je atomické i na SMB sdílení:
pokud dva uživatelé aktivují projekt současně, uspěje pouze jeden.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from . import SPM_DIR


class LockError(Exception):
    pass


@dataclass
class LockInfo:
    user: str
    pc: str
    since: str
    session_id: int | None = None

    def is_mine(self, user: str, pc: str) -> bool:
        return self.user.lower() == user.lower() and self.pc.lower() == pc.lower()


def lock_path(project_dir: Path) -> Path:
    return project_dir / SPM_DIR / "lock.json"


def read_lock(project_dir: Path) -> LockInfo | None:
    """Vrátí informace o zámku, nebo None, pokud projekt není aktivován."""
    path = lock_path(project_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return LockInfo(**data)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError):
        # Zámek existuje, ale nejde přečíst (právě se zapisuje / poškozen)
        return LockInfo(user="?", pc="?", since="")


def acquire_lock(project_dir: Path, user: str, pc: str) -> LockInfo:
    path = lock_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    info = LockInfo(user=user, pc=pc, since=datetime.now().isoformat(timespec="seconds"))
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        other = read_lock(project_dir)
        who = f"{other.user} ({other.pc})" if other else "jiný uživatel"
        raise LockError(f"Projekt je již aktivován uživatelem {who}.") from None
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(asdict(info), f, ensure_ascii=False)
    return info


def update_lock(project_dir: Path, info: LockInfo) -> None:
    """Přepíše obsah existujícího zámku (např. doplnění session_id)."""
    lock_path(project_dir).write_text(json.dumps(asdict(info), ensure_ascii=False), encoding="utf-8")


def release_lock(project_dir: Path) -> None:
    try:
        lock_path(project_dir).unlink()
    except FileNotFoundError:
        pass
