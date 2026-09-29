"""Nastavení aplikace uložené ve složce aplikace (vedle SPM.bat) v souboru settings.json."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

# Dočasné a zámkové soubory, které do projektu nepatří (nekopírují se ani nekontrolují).
# *.lck$ = zámky Cimatronu (skryté systémové soubory, zůstávají po otevřených dílech)
DEFAULT_IGNORE = ["~$*", "*.tmp", ".~lock*", "Thumbs.db", "desktop.ini", "*.spmtmp", "*.lck$"]


def _app_dir() -> Path:
    """Složka, ze které se aplikace spouští (u zabalené .exe verze složka s .exe)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


APP_DIR = _app_dir()


def settings_path() -> Path:
    return APP_DIR / "settings.json"


def legacy_settings_path() -> Path:
    """Dřívější umístění nastavení (%APPDATA%\\SPM) – použije se jen pro převzetí."""
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "SPM" / "settings.json"


@dataclass
class Settings:
    server_root: str = ""
    local_root: str = ""
    # Záloha přepisovaných souborů na serveru (.spm/backup/<cesta>.backupXY)
    backup_enabled: bool = True
    backup_count: int = 3
    # Po úspěšné deaktivaci smazat místní kopii projektu
    delete_local_after_deactivate: bool = True
    # Minimální skóre (0–1), od kterého se nabízí přiřazení přejmenovaného souboru
    rename_threshold: float = 0.6
    ignore_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_IGNORE))
    # Výchozí ignorované vzory, které už nastavení zná. Vzor přidaný do DEFAULT_IGNORE
    # v nové verzi se tak do existujícího nastavení doplní jen jednou (a uživatel ho
    # pak může v nastavení odebrat, aniž by se znovu vracel).
    known_default_ignore: list[str] = field(default_factory=lambda: list(DEFAULT_IGNORE))
    # Stav rozhraní (poloha a velikost oken, šířky sloupců, poslední projekt)
    ui: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        migrate = path is None and not settings_path().exists() and legacy_settings_path().exists()
        path = path or (legacy_settings_path() if migrate else settings_path())
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        known = {f.name for f in fields(cls)}
        data.setdefault("known_default_ignore", [])  # starší soubor nastavení tento údaj nemá
        settings = cls(**{k: v for k, v in data.items() if k in known})
        added = settings._add_new_default_ignores()
        if migrate or (added and path == settings_path()):
            try:
                settings.save()  # převzetí z %APPDATA% / doplnění nových výchozích vzorů
            except OSError:
                pass
        return settings

    def _add_new_default_ignores(self) -> bool:
        new = [p for p in DEFAULT_IGNORE if p not in self.known_default_ignore]
        for p in new:
            if p not in self.ignore_patterns:
                self.ignore_patterns.append(p)
            self.known_default_ignore.append(p)
        return bool(new)

    def save(self, path: Path | None = None) -> None:
        path = path or settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")

    def is_complete(self) -> bool:
        return bool(self.server_root and self.local_root)
