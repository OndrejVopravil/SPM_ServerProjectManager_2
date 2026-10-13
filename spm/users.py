"""Adresář uživatelů síťové složky s projekty (<síťová složka>/.spm_users/<login>.json).

Aplikace v něm při spuštění zaregistruje svého uživatele (Windows login, přezdívka z nastavení,
celé jméno z Windows, poslední počítač). Slouží k výběru adresáta poznámky – nabídnou se
všichni, kdo kdy aplikaci s touto síťovou složkou spustili, nejen ti, kdo v projektu pracovali.
Uživatel se pozná podle loginu (bez ohledu na velikost písmen), ne podle počítače.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from .lock import now_iso
from .scanner import TEMP_SUFFIX

USERS_DIR = ".spm_users"  # tečka na začátku = v seznamu projektů se nezobrazí


@dataclass
class UserInfo:
    login: str
    nickname: str = ""
    full_name: str = ""
    pc: str = ""
    seen: str = ""

    @property
    def display(self) -> str:
        """„Přezdívka (login)“ – nebo celé jméno, nebo jen login."""
        name = self.nickname or self.full_name
        return f"{name} ({self.login})" if name and name.lower() != self.login.lower() else self.login


def display_name(nickname: str, full_name: str, login: str) -> str:
    return UserInfo(login, nickname, full_name).display


def users_dir(server_root: Path) -> Path:
    return Path(server_root) / USERS_DIR


def _file(server_root: Path, login: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in login.lower())
    return users_dir(server_root) / f"{safe}.json"


def register(server_root: Path, user: UserInfo) -> bool:
    """Zapíše / aktualizuje uživatele v adresáři. Zapisuje jen při změně údajů (čas posledního
    spuštění se aktualizuje nejvýše jednou denně). Vrací True, pokud se zapisovalo."""
    path = _file(server_root, user.login)
    current = read_user(path)
    if current is not None and (current.nickname, current.full_name, current.pc) == \
            (user.nickname, user.full_name, user.pc) and current.seen[:10] == now_iso()[:10]:
        return False
    user.seen = now_iso()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + TEMP_SUFFIX)
    try:
        tmp.write_text(json.dumps(asdict(user), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return True


def read_user(path: Path) -> UserInfo | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("login"), str) or not data["login"]:
        return None
    return UserInfo(data["login"], *(str(data.get(k) or "") for k in ("nickname", "full_name", "pc", "seen")))


def list_users(server_root: Path) -> list[UserInfo]:
    """Všichni registrovaní uživatelé síťové složky (seřazení podle zobrazovaného jména)."""
    folder = users_dir(server_root)
    try:
        files = list(folder.glob("*.json"))
    except OSError:
        return []
    users = [u for u in (read_user(f) for f in files) if u is not None]
    return sorted(users, key=lambda u: u.display.lower())
