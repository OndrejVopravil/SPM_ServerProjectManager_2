"""Uložení skriptů a stav nedokončených běhů.

- Lokální skripty: <složka aplikace>/scripts/*.spm (vedle settings.json). Které se spouštějí
  při odevzdání, si každý uživatel volí u každé záložky (Settings.scripts – pořadí běhu).
- Nedokončený běh (skript selhal): <místní složka záložky>/.spm_scripts/<projekt>.json –
  obsahuje soubory odevzdání, aby šel skript spustit znovu se stejnými daty.
- Záznam každého běhu je v databázi projektu (tabulka script_runs, viz db.py) u odevzdání.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .. import config, recycle
from ..db import now_iso
from .model import ScriptFile, files_from_dicts

SCRIPT_EXT = ".spm"
PENDING_DIR = ".spm_scripts"
INVALID_NAME_CHARS = set('<>:"/\\|?*')

# Nový skript – krátká ukázka (podrobnosti v nápovědě)
TEMPLATE = """\
# Skript odevzdání – spouští se po nahrání souborů na server.
# Množina commit = soubory tohoto odevzdání (jen ty, jiné skript nevidí).
# Nápověda: tlačítko Nápověda v okně Skripty.

FILES hlav = commit "*.h" STATUS changed
IF COUNT(hlav) > 0
    ZIP hlav TO "{WORK}/{PROJECT}_{DATE}_{TIME}.zip" KEEP 1
    LOG "Zabaleno: {COUNT(hlav)} souborů"
END
"""


NETWORK_TEMPLATE = """\
# Síťový skript – společný pro všechny uživatele záložky.
# Spouští ho místní skript příkazem: CALL SERVER "název"
# Proměnné jsou společné s místním skriptem (co tu nastavíte, místní skript vidí a naopak).

SET predani = "P:\\Predani\\{PROJECT}"
"""


def scripts_dir() -> Path:
    return config.APP_DIR / "scripts"


def network_dir(folder: Path) -> Path:
    """Síťové skripty: <síťová složka záložky>/.spm/scripts (CALL SERVER) nebo
    <projekt>/.spm/scripts (CALL PROJECT)."""
    return Path(folder) / ".spm" / "scripts"


def script_path(name: str, folder: Path | None = None) -> Path:
    """Skript ve složce (výchozí = místní skripty vedle aplikace)."""
    return (folder or scripts_dir()) / name


def list_scripts(folder: Path | None = None) -> list[str]:
    folder = folder or scripts_dir()
    try:
        items = list(folder.iterdir())
    except OSError:  # neexistuje / síť nedostupná
        return []
    return sorted((p.name for p in items if p.is_file() and p.suffix.lower() == SCRIPT_EXT), key=str.lower)


def normalize_name(name: str) -> str:
    name = name.strip()
    if name and not name.lower().endswith(SCRIPT_EXT):
        name += SCRIPT_EXT
    return name


def validate_name(name: str) -> str | None:
    if not name or name == SCRIPT_EXT:
        return "Zadejte název skriptu."
    bad = sorted(set(name) & INVALID_NAME_CHARS)
    if bad:
        return "Název nesmí obsahovat znaky " + " ".join(bad)
    if name.startswith("."):
        return "Název nesmí začínat tečkou."
    return None


def load_script(name: str, folder: Path | None = None) -> str:
    return script_path(name, folder).read_text(encoding="utf-8-sig")


def save_script(name: str, text: str, folder: Path | None = None) -> None:
    path = script_path(name, folder)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def rename_script(old: str, new: str, folder: Path | None = None) -> None:
    src, dst = script_path(old, folder), script_path(new, folder)
    if dst.exists() and dst.resolve() != src.resolve():
        raise FileExistsError(f"Skript {new} už existuje.")
    src.rename(dst)


def delete_script(name: str, folder: Path | None = None) -> None:
    """Do Koše Windows (ne trvale)."""
    recycle.to_recycle_bin([script_path(name, folder)])


# --- nedokončený běh ----------------------------------------------------------------

@dataclass
class PendingRun:
    project: str
    action: str
    comment: str
    commit_id: int | None
    files: list[ScriptFile]
    scripts: list[str]
    error: str = ""
    created_at: str = field(default_factory=now_iso)
    cleanup: bool = False   # po úspěšném běhu smazat místní kopii (deaktivace)
    vars: dict = field(default_factory=dict)  # proměnné skriptů z BEFORE (ScriptSession.export_vars)

    def to_dict(self) -> dict:
        return {"project": self.project, "action": self.action, "comment": self.comment,
                "commit_id": self.commit_id, "files": [f.to_dict() for f in self.files],
                "scripts": self.scripts, "error": self.error, "created_at": self.created_at,
                "cleanup": self.cleanup, "vars": self.vars}

    @classmethod
    def from_dict(cls, data: dict) -> "PendingRun":
        return cls(str(data["project"]), str(data.get("action", "")), str(data.get("comment", "")),
                   data.get("commit_id"), files_from_dicts(data.get("files", [])),
                   [str(s) for s in data.get("scripts", [])], str(data.get("error", "")),
                   str(data.get("created_at", "")), bool(data.get("cleanup", False)),
                   data.get("vars") if isinstance(data.get("vars"), dict) else {})


def pending_path(local_root: Path, project: str) -> Path:
    return local_root / PENDING_DIR / f"{project}.json"


def save_pending(local_root: Path, run: PendingRun) -> None:
    path = pending_path(local_root, run.project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(run.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")


def load_pending(local_root: Path, project: str) -> PendingRun | None:
    try:
        return PendingRun.from_dict(json.loads(pending_path(local_root, project).read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def clear_pending(local_root: Path, project: str) -> None:
    pending_path(local_root, project).unlink(missing_ok=True)


def pending_projects(local_root: Path) -> set[str]:
    """Názvy projektů (malými písmeny) s nedokončeným během skriptu."""
    folder = local_root / PENDING_DIR
    try:
        return {p.stem.lower() for p in folder.iterdir() if p.suffix == ".json"}
    except OSError:
        return set()
