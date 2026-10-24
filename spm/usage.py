"""Anonymní hlášení používání a kontrola nové verze (https://spm.vopravil.cz).

Jednou denně (na pozadí, s krátkým časovým limitem; bez sítě se nic neděje) aplikace pošle:
- id: SHA-256 z pevné soli, loginu a názvu počítače – stejný uživatel na stejném PC má
  vždy stejné id, ale jméno se z něj nedá přečíst,
- číslo verze SPM.
Server odpoví aktuální verzí, odkazem ke stažení a popisem změn (upozornění na aktualizaci).

Hlášení lze vypnout v Nastavení, pro celou firmu ve spm_config.json ("usage_report": false).
Vypnuté hlášení posílá jen dotaz na verzi (bez id).
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import re
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

SERVER = "https://spm.vopravil.cz"
PING_URL = f"{SERVER}/api/ping.php"
STATS_URL = f"{SERVER}/"
SALT = "SPM-usage-v1"
TIMEOUT = 6  # s


@dataclass
class UpdateInfo:
    latest: str
    url: str
    notes: str = ""


def install_id(login: str, pc: str) -> str:
    """Anonymní identita instalace (uživatel na počítači)."""
    return hashlib.sha256(f"{SALT}|{login.strip().lower()}|{pc.strip().upper()}".encode("utf-8")).hexdigest()


def parse_version(text: str) -> tuple[int, ...] | None:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)*)\s*", text or "")
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def is_newer(latest: str, current: str) -> bool:
    a, b = parse_version(latest), parse_version(current)
    return a is not None and b is not None and a > b


def payload(enabled: bool, login: str, pc: str, version: str) -> dict:
    """Co se odešle: s hlášením id a verze, bez něj jen verze (dotaz na aktualizaci)."""
    data = {"version": version}
    if enabled:
        data["id"] = install_id(login, pc)
    return data


def ping(enabled: bool, login: str, pc: str, version: str, url: str = PING_URL,
         timeout: float = TIMEOUT) -> UpdateInfo | None:
    """Odešle hlášení (nebo jen dotaz na verzi); vrací informaci o aktuální verzi, None = server
    nedostupný nebo nesrozumitelná odpověď."""
    body = json.dumps(payload(enabled, login, pc, version)).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": f"SPM/{version}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 – pevná adresa https
            data = json.loads(resp.read(64 * 1024).decode("utf-8"))
    except Exception:  # noqa: BLE001 – bez sítě / proxy / chyba serveru: tiše nic
        return None
    if not isinstance(data, dict) or not isinstance(data.get("latest"), str):
        return None
    link = data.get("url") if isinstance(data.get("url"), str) else ""
    if not link.startswith(SERVER + "/"):
        link = STATS_URL  # odkaz ke stažení jen z vlastní domény
    return UpdateInfo(data["latest"].strip(), link, str(data.get("notes") or "")[:2000])


# --- kam rozbalit novou verzi -----------------------------------------------------------

def app_folder() -> Path | None:
    """Složka, ze které běží SPM.exe (None = spuštěno ze zdrojového kódu)."""
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).resolve().parent


def is_network_path(path: Path) -> bool:
    """Složka na síti (\\\\server\\… nebo připojený síťový disk)."""
    text = str(path)
    if text.startswith("\\\\"):
        return True
    if sys.platform == "win32" and len(text) >= 2 and text[1] == ":":
        try:
            return ctypes.windll.kernel32.GetDriveTypeW(text[:2] + "\\") == 4  # DRIVE_REMOTE
        except (AttributeError, OSError):
            return False
    return False


def install_steps(folder: Path | None, version: str) -> list[str]:
    """Postup instalace podle toho, odkud SPM běží (zip obsahuje složku SPM\\)."""
    zip_name = f"SPM-{version}.zip"
    if folder is None:
        return ["SPM běží ze zdrojového kódu – aktualizujte zdrojové soubory (např. z repozitáře)."]
    steps = [f"Stáhněte {zip_name}.", "Zavřete SPM."]
    keep = ("Stávající složku nemažte – je v ní vaše nastavení (settings.json) a místní skripty "
            "(scripts), které zip neobsahuje a zůstanou.")
    if folder.name.lower() == "spm":
        steps.append(f"Rozbalte zip do složky {folder.parent} a potvrďte přepsání souborů ve složce "
                     f"{folder.name}. {keep}")
    else:
        steps.append(f"Ze zipu zkopírujte obsah složky SPM do {folder} a potvrďte přepsání souborů. {keep}")
    steps.append("Spusťte SPM znovu.")
    if is_network_path(folder):
        steps.insert(1, f"SPM se spouští ze síťové složky {folder} – soubory nepůjde přepsat, dokud "
                        "SPM běží u kohokoli jiného. Domluvte se, aby SPM všichni zavřeli.")
    return steps
