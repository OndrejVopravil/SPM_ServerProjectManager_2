"""Společná konfigurace na síti (<síťová složka s projekty>/spm_config.json).

Pokud soubor existuje, řídí vybraná nastavení všech uživatelů (zálohy, ignorované soubory,
kontrola nepovolených znaků). Hodnoty se při každé změně souboru překopírují do místního
settings.json – když soubor zmizí nebo síť není dostupná, zůstane nastavení stejné a dá se
pak měnit místně. Nastavení, které v souboru chybí, zůstává místní.

Dále soubor nese:
- server_id / server_name: identita síťové složky – podle ní se pozná stejná složka u všech
  uživatelů bez ohledu na to, jak mají disk připojený (O:\\…, \\\\server\\…),
- mirror_rules: sdílená pravidla zrcadlení z této složky (cíl = server_id jiné složky).
"""

from __future__ import annotations

import json
import os
import stat
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings
from .scanner import TEMP_SUFFIX

CONFIG_NAME = "spm_config.json"
FORMAT_KEY = "spm_config"
FORMAT = 1
# Nastavení řízená společnou konfigurací (ostatní zůstávají vždy místní)
SHARED_FIELDS = ("backup_enabled", "backup_count", "ignore_patterns", "check_forbidden_chars")
SERVER_ID, SERVER_NAME, MIRROR_RULES = "server_id", "server_name", "mirror_rules"
RULE_TEXT_FIELDS = ("id", "target_server", "target_name", "files", "folders", "exclude", "projects")
BACKUP_COUNT_MAX = 99

# Stav společné konfigurace
NONE = "none"                # soubor neexistuje -> místní nastavení
UNAVAILABLE = "unavailable"  # síťová složka není dostupná -> místní nastavení
OK = "ok"                    # soubor platí
ERROR = "error"              # soubor existuje, ale je chybný -> platí poslední převzaté hodnoty


class SharedConfigError(Exception):
    pass


@dataclass
class SharedState:
    path: Path | None = None
    status: str = NONE
    values: dict = field(default_factory=dict)
    read_only: bool = False
    error: str = ""
    stamp: tuple | None = None  # (mtime, velikost) – soubor se znovu čte jen při změně

    @property
    def active(self) -> bool:
        """Soubor na síti existuje -> nastavení se řídí z něj (místně je nelze měnit)."""
        return self.status in (OK, ERROR)

    def describe(self) -> str:
        if self.status == OK:
            return "síťové" + (" (jen pro čtení)" if self.read_only else "")
        if self.status == ERROR:
            return "síťové – chyba v souboru"
        if self.status == UNAVAILABLE:
            return "místní (síť nedostupná)"
        return "místní"


def config_path(server_root: str) -> Path | None:
    return Path(server_root) / CONFIG_NAME if server_root else None


def parse(text: str) -> dict:
    """Načte a zkontroluje obsah souboru; vrátí jen známá nastavení, která v souboru jsou."""
    try:
        data = json.loads(text)
    except ValueError as e:
        raise SharedConfigError(f"Neplatný formát JSON: {e}") from None
    if not isinstance(data, dict):
        raise SharedConfigError("Soubor musí obsahovat objekt { … }.")
    values: dict = {}
    for key in ("backup_enabled", "check_forbidden_chars"):
        if key in data:
            if not isinstance(data[key], bool):
                raise SharedConfigError(f"{key}: očekává se true nebo false.")
            values[key] = data[key]
    if "backup_count" in data:
        count = data["backup_count"]
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= BACKUP_COUNT_MAX:
            raise SharedConfigError(f"backup_count: očekává se celé číslo 1–{BACKUP_COUNT_MAX}.")
        values["backup_count"] = count
    if "ignore_patterns" in data:
        patterns = data["ignore_patterns"]
        if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
            raise SharedConfigError("ignore_patterns: očekává se seznam textů, např. [\"*.tmp\"].")
        values["ignore_patterns"] = [p.strip() for p in patterns if p.strip()]
    for key in (SERVER_ID, SERVER_NAME):
        if key in data:
            if not isinstance(data[key], str):
                raise SharedConfigError(f"{key}: očekává se text.")
            values[key] = data[key].strip()
    if MIRROR_RULES in data:
        values[MIRROR_RULES] = _parse_rules(data[MIRROR_RULES])
    return values


def _parse_rules(rules) -> list[dict]:
    if not isinstance(rules, list) or not all(isinstance(r, dict) for r in rules):
        raise SharedConfigError("mirror_rules: očekává se seznam pravidel { … }.")
    result = []
    for i, rule in enumerate(rules, start=1):
        clean = {}
        for key in RULE_TEXT_FIELDS:
            value = rule.get(key, "")
            if not isinstance(value, str):
                raise SharedConfigError(f"mirror_rules, pravidlo {i}: {key} musí být text.")
            clean[key] = value.strip()
        enabled = rule.get("enabled", True)
        if not isinstance(enabled, bool):
            raise SharedConfigError(f"mirror_rules, pravidlo {i}: enabled musí být true nebo false.")
        clean["enabled"] = enabled
        if not clean["target_server"]:
            raise SharedConfigError(f"mirror_rules, pravidlo {i}: chybí target_server (cílová složka).")
        if not (clean["files"] or clean["folders"]):
            raise SharedConfigError(f"mirror_rules, pravidlo {i}: vyplňte files nebo folders.")
        clean["id"] = clean["id"] or uuid.uuid4().hex[:8]
        result.append(clean)
    return result


def probe(server_root: str, previous: SharedState | None = None) -> SharedState:
    """Zjistí stav souboru na síti. Obsah čte jen tehdy, když se soubor od `previous` změnil."""
    path = config_path(server_root)
    if path is None:
        return SharedState()
    try:
        st = path.stat()
    except OSError:
        # chybějící soubor vs. nedostupná síť (u UNC cest hlásí Windows obojí podobně)
        status = NONE if path.parent.is_dir() else UNAVAILABLE
        return SharedState(path, status)
    stamp = (st.st_mtime, st.st_size)
    read_only = not st.st_mode & stat.S_IWRITE
    if previous is not None and previous.path == path and previous.stamp == stamp:
        previous.read_only = read_only
        return previous
    try:
        text = path.read_text(encoding="utf-8-sig")  # i soubor uložený Poznámkovým blokem s BOM
    except OSError as e:
        return SharedState(path, UNAVAILABLE, error=str(e))
    try:
        return SharedState(path, OK, parse(text), read_only, stamp=stamp)
    except SharedConfigError as e:
        return SharedState(path, ERROR, read_only=read_only, error=str(e), stamp=stamp)


def values_of(settings: Settings) -> dict:
    return {key: getattr(settings, key) for key in SHARED_FIELDS}


def apply(settings: Settings, values: dict) -> bool:
    """Převezme hodnoty ze společné konfigurace do nastavení; vrátí True, pokud se něco změnilo."""
    changed = False
    for key in SHARED_FIELDS + (SERVER_ID, SERVER_NAME):
        if key in values and getattr(settings, key) != values[key]:
            setattr(settings, key, list(values[key]) if key == "ignore_patterns" else values[key])
            changed = True
    # pravidla zrcadlení: chybí v souboru = nejsou sdílená (platí místní)
    rules = values.get(MIRROR_RULES)
    if settings.shared_mirror_rules != rules:
        settings.shared_mirror_rules = [dict(r) for r in rules] if rules is not None else None
        changed = True
    return changed


def write(server_root: str, values: dict) -> Path:
    """Uloží společnou konfiguraci na síť (přes dočasný soubor, aby ji nikdo nečetl rozepsanou).
    Ukládá jen předané hodnoty; ostatní, které už v souboru jsou, zachová."""
    path = config_path(server_root)
    if path is None or not path.parent.is_dir():
        raise SharedConfigError(f"Síťová složka není dostupná:\n{server_root}")
    if path.exists() and not path.stat().st_mode & stat.S_IWRITE:
        raise SharedConfigError(f"Soubor je jen pro čtení – nastavení nelze změnit:\n{path}")
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except ValueError:
            data = None
        except OSError as e:
            raise SharedConfigError(f"Nelze přečíst {path}:\n{e}") from None
        if not isinstance(data, dict):
            data = {}  # poškozený soubor se přepíše (uživatel ho vědomě ukládá znovu)
    data.pop(FORMAT_KEY, None)
    data = {FORMAT_KEY: FORMAT, **data, **values}
    tmp = path.with_name(path.name + TEMP_SUFFIX)
    try:
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except PermissionError:
        tmp.unlink(missing_ok=True)
        raise SharedConfigError(f"Nemáte oprávnění zapisovat do:\n{path}") from None
    except OSError as e:
        tmp.unlink(missing_ok=True)
        raise SharedConfigError(f"Nelze uložit {path}:\n{e}") from None
    return path


def ensure_server_id(server_root: str, default_name: str = "") -> str:
    """Identita síťové složky ze spm_config.json; pokud ji soubor nemá (nebo neexistuje),
    vytvoří ji (ostatní obsah souboru zachová)."""
    state = probe(server_root)
    if state.status == ERROR:
        raise SharedConfigError(f"Soubor {state.path} je chybný: {state.error}")
    if state.status == UNAVAILABLE:
        raise SharedConfigError(f"Síťová složka není dostupná:\n{server_root}")
    if state.values.get(SERVER_ID):
        return state.values[SERVER_ID]
    server_id = uuid.uuid4().hex
    values = {SERVER_ID: server_id}
    if not state.values.get(SERVER_NAME):
        values[SERVER_NAME] = default_name or Path(server_root).name
    write(server_root, values)
    return server_id


class SharedConfig:
    """Sleduje soubor na síti a jeho změny přenáší do nastavení (a do settings.json)."""

    def __init__(self) -> None:
        self.state = SharedState()

    def sync(self, settings: Settings) -> bool:
        """Volá se při spuštění a při každém obnovení. Vrátí True, pokud se nastavení změnilo."""
        self.state = probe(settings.server_root, self.state)
        if self.state.status != OK or not apply(settings, self.state.values):
            return False
        try:
            settings.save()
        except OSError:
            pass
        return True
