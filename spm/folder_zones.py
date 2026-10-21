"""Chráněné oblasti v záložce „Složka“: síťové a místní složky záložek projektů a projekty SPM
(poznané podle servisní složky .spm) – i když nejsou v žádné záložce.

V záložce Složka jsou vidět, ale SPM do nich nic nezapíše: nic se do nich nevloží, nic se v nich
nepřejmenuje, nesmaže ani nepřesune. Ani složka, která chráněnou oblast obsahuje, se nesmí
přejmenovat, smazat ani přesunout (s ní by se přesunula i složka spolupráce). Soubory se upravují
v záložce projektů – dvojklik na soubor v chráněné oblasti tam přepne (viz project_of).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from . import SPM_DIR

SERVER, LOCAL, PROJECT = "server", "local", "project"


@dataclass(frozen=True)
class Zone:
    root: Path
    kind: str            # server / local (složka záložky) / project (projekt mimo záložky)
    ws_id: str = ""      # záložka projektů ("" = projekt není v žádné záložce)
    ws_name: str = ""

    @property
    def label(self) -> str:
        if self.kind == SERVER:
            return f"síťová složka záložky „{self.ws_name}“"
        if self.kind == LOCAL:
            return f"místní složka záložky „{self.ws_name}“"
        return "projekt SPM (není v žádné vaší záložce)"


def _key(path: Path | str) -> str:
    """Porovnatelná cesta: skutečná (krátké názvy Windows jako ONDREJ~1 i připojené disky
    se převedou na stejný tvar), bez ohledu na velikost písmen."""
    try:
        real = os.path.realpath(str(path))
    except OSError:
        real = os.path.abspath(str(path))
    return os.path.normcase(real).rstrip("\\/")


class ProtectedZones:
    def __init__(self, zones: list[Zone]):
        self.zones = zones
        self._key_cache: dict[str, str] = {}
        self._keys = [(self._k(z.root), z) for z in zones]
        self._spm_cache: dict[str, bool] = {}

    def _k(self, path: Path | str) -> str:
        """_key s mezipamětí (strom se ptá na stejné cesty pořád dokola)."""
        text = str(path)
        if text not in self._key_cache:
            self._key_cache[text] = _key(text)
        return self._key_cache[text]

    @classmethod
    def from_workspaces(cls, workspaces: list[dict]) -> "ProtectedZones":
        zones = []
        for w in workspaces:
            if w.get("folder_root"):
                continue
            for key, kind in (("server_root", SERVER), ("local_root", LOCAL)):
                if w.get(key):
                    zones.append(Zone(Path(w[key]), kind, w["id"], w.get("name", "")))
        return cls(zones)

    # --- dotazy --------------------------------------------------------------------

    def _has_spm(self, folder: Path) -> bool:
        k = self._k(folder)
        if k not in self._spm_cache:
            try:
                self._spm_cache[k] = (folder / SPM_DIR).is_dir()
            except OSError:
                self._spm_cache[k] = False
        return self._spm_cache[k]

    def zone_of(self, path: Path, stop: Path | None = None) -> Zone | None:
        """Chráněná oblast, do které cesta patří (i kořen oblasti). stop = kořen záložky
        Složka – výš se projekty (.spm) nehledají."""
        k = self._k(path)
        for root_key, zone in self._keys:
            if k == root_key or k.startswith(root_key + os.sep):
                return zone
        stop_key = self._k(stop) if stop is not None else None
        for folder in [path, *path.parents]:
            if self._has_spm(folder):
                return Zone(folder, PROJECT)
            if stop_key is not None and self._k(folder) == stop_key:
                break
        return None

    def contains_zone(self, path: Path) -> Zone | None:
        """Leží uvnitř složky (pod ní) chráněná oblast? I projekt SPM mimo záložky – složka se
        prohledá (končí u prvního nálezu; do projektů se nevnořuje)."""
        if not path.is_dir():
            return None
        k = self._k(path)
        for root_key, zone in self._keys:
            if root_key.startswith(k + os.sep):
                return zone
        for dirpath, dirnames, _files in os.walk(path):
            if SPM_DIR in dirnames:
                return Zone(Path(dirpath), PROJECT)
        return None

    def write_blocked(self, folder: Path, stop: Path | None = None) -> Zone | None:
        """Nelze do složky zapisovat (vložit, nová složka, rozbalit zip)?"""
        return self.zone_of(folder, stop)

    def modify_blocked(self, path: Path, stop: Path | None = None) -> Zone | None:
        """Nelze položku přejmenovat, smazat ani přesunout?"""
        return self.zone_of(path, stop) or self.contains_zone(path)

    # --- kam patří soubor ------------------------------------------------------------

    @staticmethod
    def project_of(zone: Zone, path: Path) -> tuple[str, str]:
        """Projekt a cesta v něm pro soubor v chráněné oblasti záložky: (projekt, cesta) –
        ('', '') u kořene oblasti."""
        try:
            parts = path.resolve().relative_to(zone.root.resolve()).parts
        except (OSError, ValueError):
            return "", ""
        if zone.kind == PROJECT:
            return zone.root.name, "/".join(parts)
        if not parts:
            return "", ""
        return parts[0], "/".join(parts[1:])
