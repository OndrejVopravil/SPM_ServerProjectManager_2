"""Nastavení aplikace uložené ve složce aplikace (vedle SPM.bat) v souboru settings.json.

Aplikace má pracovní prostory (záložky) – každý má vlastní síťovou a místní složku a vlastní
společná nastavení (zálohy, ignorované soubory, kontrola znaků; viz shared_config). Ostatní
nastavení jsou osobní a platí pro všechny záložky.

`Settings` = platné nastavení jedné záložky (s osobními hodnotami) – s ním pracuje ProjectManager.
`AppSettings` = celý soubor settings.json: osobní nastavení, stav rozhraní a seznam záložek.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from dataclasses import dataclass, field, fields
from pathlib import Path

# Dočasné a zámkové soubory, které do projektu nepatří (nekopírují se ani nekontrolují).
# *.lck$ = zámky Cimatronu (skryté systémové soubory, zůstávají po otevřených dílech)
DEFAULT_IGNORE = ["~$*", "*.tmp", ".~lock*", "Thumbs.db", "desktop.ini", "*.spmtmp", "*.lck$"]


AUTO_REFRESH_OFF = -1
AUTO_REFRESH_MIN = 60


def normalize_refresh(seconds) -> int:
    """-1 (nebo jakákoliv záporná / neplatná hodnota) = vypnuto, jinak nejméně 60 s."""
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return AUTO_REFRESH_OFF
    if seconds < 0:
        return AUTO_REFRESH_OFF
    return max(seconds, AUTO_REFRESH_MIN)


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
    # záložka „Složka“: obyčejná složka mimo spolupráci (např. Data); prázdné = záložka projektů
    folder_root: str = ""
    # Záloha přepisovaných souborů na serveru (.spm/backup/<cesta>.backupXY)
    backup_enabled: bool = True
    backup_count: int = 3
    # Po úspěšné deaktivaci smazat místní kopii projektu
    delete_local_after_deactivate: bool = True
    # Kontrola nepovolených znaků (diakritika, mezera, ( ) & , . ; = ^) v názvech souborů a složek
    check_forbidden_chars: bool = True
    # Výchozí stav volby „Nestahovat neaktivované soubory“ v dialogu aktivace
    skip_unlocked_download: bool = False
    # Automatické obnovení seznamu projektů v sekundách; -1 = vypnuto, jinak nejméně 60
    auto_refresh_seconds: int = -1
    # Minimální skóre (0–1), od kterého se nabízí přiřazení přejmenovaného souboru
    rename_threshold: float = 0.6
    ignore_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_IGNORE))
    # Výchozí ignorované vzory, které už nastavení zná. Vzor přidaný do DEFAULT_IGNORE
    # v nové verzi se tak do existujícího nastavení doplní jen jednou (a uživatel ho
    # pak může v nastavení odebrat, aniž by se znovu vracel).
    known_default_ignore: list[str] = field(default_factory=lambda: list(DEFAULT_IGNORE))
    # Přezdívka – jak uživatele uvidí ostatní (u poznámek, výběr adresáta); "" = jméno z Windows
    nickname: str = ""
    # Složky ve stromu projektu (panel pod seznamem) ve výchozím stavu rozbalené
    tree_expanded: bool = True
    # Při ukončení aplikace upozornit na projekty, které mám stále aktivované
    warn_active_on_exit: bool = True
    # Tajné hodnoty pro skripty ({SECRET:název}) – např. API klíče; jen místně, nikdy na síti
    secrets: dict[str, str] = field(default_factory=dict)
    # Barva záložky (klíč z gui.tab_bar.TAB_COLORS; "" = bez barvy)
    tab_color: str = ""
    # Skripty spouštěné po odevzdání z této záložky (názvy souborů ve složce scripts, v pořadí běhu)
    scripts: list[str] = field(default_factory=list)
    # Archivace dokončených projektů (společná konfigurace spm_config.json; archiv je vždy
    # v <síťová složka>/.spm/archiv): kolik záloh každého souboru se do archivu ponechá a kdo
    # smí archivovat, vracet z archivu a uklízet zálohy (loginy; prázdné = nikdo)
    archive_keep_backups: int = 2
    archivers: list[str] = field(default_factory=list)
    # Vzkazy vyřízené / dokončené před více dny (a bez nové odpovědi) se přesunou do archivu
    # vzkazů (<síťová složka>/.spm/vzkazy/_archiv) – nic se nemaže; 0 = nearchivovat
    messages_archive_days: int = 90
    # Anonymní statistika používání (jednou denně hash loginu a PC + verze na spm.vopravil.cz,
    # viz usage.py); vypnutá u kterékoli záložky = neodesílá se vůbec (jen dotaz na novou verzi)
    usage_report: bool = True
    # --- převzato ze spm_config.json síťové složky záložky (viz shared_config) ---
    # identita síťové složky – stejná pro všechny uživatele bez ohledu na připojení disku
    server_id: str = ""
    server_name: str = ""
    # Stav rozhraní (poloha a velikost oken, šířky sloupců, poslední projekt)
    ui: dict[str, str] = field(default_factory=dict)
    # Záložka, ke které nastavení patří, a celé nastavení aplikace (neukládá se)
    workspace_id: str = field(default="", repr=False, compare=False)
    _app: "AppSettings | None" = field(default=None, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        known = {f.name for f in fields(cls) if f.name not in INTERNAL_FIELDS}
        data = dict(data)
        data.setdefault("known_default_ignore", [])  # starší soubor nastavení tento údaj nemá
        settings = cls(**{k: v for k, v in data.items() if k in known})
        settings.auto_refresh_seconds = normalize_refresh(settings.auto_refresh_seconds)
        return settings

    def to_dict(self) -> dict:
        # ne asdict() – ten by hluboce kopíroval i odkaz na AppSettings
        return {f.name: getattr(self, f.name) for f in fields(self) if f.name not in INTERNAL_FIELDS}

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        """Načte nastavení v jednoduchém formátu (jedna síťová složka) – dřívější verze
        settings.json; aplikace používá AppSettings.load()."""
        migrate = path is None and not settings_path().exists() and legacy_settings_path().exists()
        path = path or (legacy_settings_path() if migrate else settings_path())
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        settings = cls.from_dict(data)
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
        if self._app is not None and path is None:  # nastavení záložky -> uložit celou aplikaci
            self._app.store(self)
            self._app.save()
            return
        path = path or settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")

    def is_complete(self) -> bool:
        """Záložka projektů má nastavenou síťovou i místní složku."""
        return bool(self.server_root and self.local_root) and not self.is_folder

    @property
    def is_folder(self) -> bool:
        """Záložka „Složka“ (obyčejná složka, ne projekty)."""
        return bool(self.folder_root)


INTERNAL_FIELDS = ("workspace_id", "_app")
# Nastavení, která má každá záložka vlastní; ostatní (kromě ui) jsou osobní – pro všechny záložky
WORKSPACE_FIELDS = ("server_root", "local_root", "folder_root", "backup_enabled", "backup_count",
                    "check_forbidden_chars", "ignore_patterns", "known_default_ignore",
                    "server_id", "server_name", "tab_color", "scripts",
                    "archive_keep_backups", "archivers", "messages_archive_days", "usage_report")
PERSONAL_FIELDS = tuple(f.name for f in fields(Settings)
                        if f.name not in WORKSPACE_FIELDS + INTERNAL_FIELDS + ("ui",))
DEFAULT_WORKSPACE_NAME = "Projekty"


def _same_dir(a: str, b: str) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return a.lower() == b.lower()


def _inside(child: str, parent: str) -> bool:
    try:
        return Path(child).resolve().is_relative_to(Path(parent).resolve())
    except OSError:
        return False


def project_folder_inside(folder: str) -> str | None:
    """Je složka projektem SPM nebo leží v projektu (servisní složka .spm)? Zachytí i projekty,
    které nejsou v žádné záložce tohoto počítače. (Složka, která projekty obsahuje, je v pořádku
    – projekty v ní budou chráněné.)"""
    from . import SPM_DIR
    path = Path(folder)
    try:
        for p in [path, *path.parents]:
            if (p / SPM_DIR).is_dir():
                return f"Složka je součástí projektu SPM ({p}) – vyberte složku mimo projekty."
    except OSError:
        return None
    return None


class AppSettings:
    """Celé nastavení aplikace: osobní hodnoty, stav rozhraní a záložky (pracovní prostory).

    Každá záložka je slovník {"id", "name", <WORKSPACE_FIELDS>}. view(id) vrací platné
    nastavení záložky jako `Settings`; jeho save() uloží celý soubor."""

    def __init__(self, base: Settings, workspaces: list[dict], active: str = ""):
        self.base = base            # osobní nastavení a ui (hodnoty záložky v něm neplatí)
        self.workspaces = workspaces
        self.active = active if any(w["id"] == active for w in workspaces) else workspaces[0]["id"]
        self._views: dict[str, Settings] = {}

    # --- záložky ---------------------------------------------------------------

    @staticmethod
    def new_workspace(name: str, values: Settings | None = None) -> dict:
        values = values or Settings()
        ws = {"id": uuid.uuid4().hex[:8], "name": name}
        ws.update({k: getattr(values, k) for k in WORKSPACE_FIELDS})
        ws["ignore_patterns"] = list(ws["ignore_patterns"])
        ws["known_default_ignore"] = list(ws["known_default_ignore"])
        ws["scripts"] = list(ws["scripts"])
        ws["archivers"] = list(ws["archivers"])
        return ws

    def workspace(self, ws_id: str) -> dict:
        return next(w for w in self.workspaces if w["id"] == ws_id)

    def view(self, ws_id: str) -> Settings:
        """Platné nastavení záložky (stále stejný objekt – změny se ukládají přes save())."""
        if ws_id not in self._views:
            ws = self.workspace(ws_id)
            data = {**{k: getattr(self.base, k) for k in PERSONAL_FIELDS},
                    **{k: ws[k] for k in WORKSPACE_FIELDS if k in ws}}
            s = Settings.from_dict(data)
            s.ui = self.base.ui  # stav rozhraní je společný
            s.workspace_id, s._app = ws_id, self
            if s._add_new_default_ignores():
                self.store(s)
            self._views[ws_id] = s
        return self._views[ws_id]

    def store(self, view: Settings) -> None:
        """Převezme změny z nastavení záložky: její hodnoty do záložky, osobní do všech záložek."""
        ws = self.workspace(view.workspace_id)
        for k in WORKSPACE_FIELDS:
            value = getattr(view, k)
            ws[k] = list(value) if isinstance(value, list) else value
        for k in PERSONAL_FIELDS:
            value = getattr(view, k)
            setattr(self.base, k, value)
            for other in self._views.values():
                setattr(other, k, value)

    def add_workspace(self, name: str) -> str:
        """Nová záložka s výchozími hodnotami (složky se nastaví v dialogu Nastavení)."""
        ws = self.new_workspace(name)
        self.workspaces.append(ws)
        return ws["id"]

    def remove_workspace(self, ws_id: str) -> None:
        if len(self.workspaces) <= 1:
            raise ValueError("Poslední záložku nelze odebrat.")
        self.workspaces = [w for w in self.workspaces if w["id"] != ws_id]
        self._views.pop(ws_id, None)
        for key in [k for k in self.base.ui if k.endswith(f".{ws_id}")]:
            del self.base.ui[key]
        if self.active == ws_id:
            self.active = self.workspaces[0]["id"]

    def rename_workspace(self, ws_id: str, name: str) -> None:
        self.workspace(ws_id)["name"] = name

    def folder_conflict(self, ws_id: str, server_root: str, local_root: str) -> str | None:
        """Kolize složek s jinou záložkou (stejný server, stejná nebo vnořená místní složka;
        záložka „Složka“ uvnitř této složky). Složka pro spolupráci uvnitř záložky „Složka“ je
        v pořádku – tam je chráněná (jen pro čtení, viz folder_zones)."""
        for w in self.workspaces:
            if w["id"] == ws_id:
                continue
            folder = w.get("folder_root")
            if folder:
                for root, label in ((server_root, "síťové"), (local_root, "místní")):
                    if root and (_same_dir(folder, root) or _inside(folder, root)):
                        return (f"Záložka „{w['name']}“ typu Složka ({folder}) by ležela uvnitř "
                                f"{label} složky této záložky.\nObyčejná složka nesmí být uvnitř "
                                "složky pro spolupráci – nejdřív záložku Složka odeberte nebo změňte.")
                continue
            if w.get("server_root") and server_root and _same_dir(w["server_root"], server_root):
                return f"Síťovou složku už používá záložka „{w['name']}“."
            other = w.get("local_root")
            if other and local_root and (_same_dir(other, local_root) or _inside(local_root, other)
                                         or _inside(other, local_root)):
                return (f"Místní složka se kryje s místní složkou záložky „{w['name']}“ ({other}).\n"
                        "Každá záložka musí mít vlastní místní složku (projekty se stejným názvem "
                        "na různých serverech by se jinak srazily).")
        return None

    def folder_tab_conflict(self, ws_id: str, folder: str) -> str | None:
        """Záložka „Složka“ nesmí začínat uvnitř síťové ani místní složky záložky projektů ani
        uvnitř projektu SPM. Obsahovat je smí – v záložce Složka jsou pak chráněné (jen pro
        čtení, viz folder_zones)."""
        for w in self.workspaces:
            if w["id"] == ws_id or w.get("folder_root"):
                continue
            for key, label in (("server_root", "síťové"), ("local_root", "místní")):
                root = w.get(key)
                if root and (_same_dir(folder, root) or _inside(folder, root)):
                    return (f"Složka leží uvnitř {label} složky záložky „{w['name']}“ ({root}).\n"
                            "Záložka Složka nesmí začínat uvnitř složky pro spolupráci – soubory "
                            "projektů se upravují v záložce projektů.")
        return project_folder_inside(folder)

    def contained_collaboration(self, folder: str) -> list[str]:
        """Popisy složek pro spolupráci uvnitř složky (pro upozornění při přidání záložky)."""
        found = []
        for w in self.workspaces:
            if w.get("folder_root"):
                continue
            for key, label in (("server_root", "síťová"), ("local_root", "místní")):
                root = w.get(key)
                if root and _inside(root, folder) and not _same_dir(root, folder):
                    found.append(f"{root} – {label} složka záložky „{w['name']}“")
        return found

    # --- načtení a uložení ------------------------------------------------------

    @classmethod
    def from_settings(cls, settings: Settings, name: str = "") -> "AppSettings":
        """Jedna záložka z nastavení v jednoduchém formátu (převod starého settings.json);
        předaný objekt se rovnou stane nastavením této záložky."""
        ws = cls.new_workspace(name or Path(settings.server_root).name or DEFAULT_WORKSPACE_NAME, settings)
        app = cls(settings, [ws], ws["id"])
        settings.workspace_id, settings._app = ws["id"], app
        app._views[ws["id"]] = settings
        return app

    @classmethod
    def load(cls) -> "AppSettings":
        path = settings_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if not isinstance(data, dict) or not isinstance(data.get("workspaces"), list) \
                or not data["workspaces"]:
            # starší formát (jedna síťová složka) – včetně převzetí z %APPDATA%
            app = cls.from_settings(Settings.load())
            if path.exists() or legacy_settings_path().exists():
                try:
                    app.save()
                except OSError:
                    pass
            return app
        base = Settings.from_dict({k: v for k, v in data.items() if k != "workspaces"})
        workspaces = []
        for w in data["workspaces"]:
            if isinstance(w, dict) and w.get("id"):
                defaults = cls.new_workspace(DEFAULT_WORKSPACE_NAME)
                # jen známé údaje (dřívější, např. pravidla zrcadlení, se zahodí)
                workspaces.append({**defaults, **{k: v for k, v in w.items() if k in defaults}})
        if not workspaces:
            workspaces = [cls.new_workspace(DEFAULT_WORKSPACE_NAME)]
        return cls(base, workspaces, str(data.get("active_workspace") or ""))

    def save(self) -> None:
        for view in self._views.values():
            ws = self.workspace(view.workspace_id) if any(
                w["id"] == view.workspace_id for w in self.workspaces) else None
            if ws is not None:
                for k in WORKSPACE_FIELDS:
                    value = getattr(view, k)
                    ws[k] = list(value) if isinstance(value, list) else value
        data = {k: getattr(self.base, k) for k in PERSONAL_FIELDS}
        data["ui"] = self.base.ui
        data["workspaces"] = self.workspaces
        data["active_workspace"] = self.active
        path = settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
