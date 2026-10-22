"""Vzkazy – poznámky pro konkrétní uživatele, nevázané na projekt.

Ukládají se v síťové složce s projekty: <síťová složka>/.spm/vzkazy/<id>/ – každý vzkaz má
svou složku a každý soubor v ní zapisuje jen jeden uživatel, takže se zápisy různých lidí
nikdy nepřepíšou:

- vzkaz.json        – text, adresáti, na vědomí, zmíněné projekty (zapisuje jen autor),
- odpoved_<id>.json – odpověď (zapisuje jen její autor),
- stav_<login>.json – vyřízeno / přečteno daným uživatelem (zapisuje jen on).

Vzkaz má aspoň jednoho adresáta (může to být i sám autor – připomínka sobě); za vyřízený se
považuje, když ho vyřeší aspoň jeden z adresátů, nebo když ho autor označí jako dokončený (je
vidět kdo). Na vědomí (zmínka „@jméno“) může označit „přečteno“. Projekty zmíněné „#název“
jsou odkazy (s identitou projektu, aby odkaz přežil přejmenování).

Archiv vzkazů: vzkaz vyřízený / dokončený před více než messages_archive_days dny (a bez nové
odpovědi) přesune kterýkoli klient do <síťová složka>/.spm/vzkazy/_archiv/ – jen přejmenování
složky, nic se nemaže. Běžná kontrola vzkazů archiv nečte (zobrazí se na vyžádání); každá
změna archivovaného vzkazu (odpověď, vyřízení…) ho vrátí zpět mezi aktivní.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from . import SPM_DIR
from .lock import now_iso
from .scanner import TEMP_SUFFIX

MESSAGES_DIR = "vzkazy"
MESSAGE_FILE = "vzkaz.json"
REPLY_PREFIX = "odpoved_"
STATE_PREFIX = "stav_"
FORMAT = 1
ARCHIVE_SUBDIR = "_archiv"     # <síťová složka>/.spm/vzkazy/_archiv/<id>/


class MessageError(Exception):
    pass


@dataclass
class Person:
    login: str
    name: str = ""

    @property
    def display(self) -> str:
        return self.name or self.login


@dataclass
class Version:
    saved_at: str
    text: str
    recipients: list[Person] = field(default_factory=list)


@dataclass
class Reply:
    id: str
    created_at: str
    user: str
    author: str
    text: str
    edited_at: str | None = None
    versions: list[Version] = field(default_factory=list)

    def is_author(self, login: str) -> bool:
        return self.user.lower() == login.lower()


@dataclass
class UserState:
    login: str
    name: str = ""
    resolved_at: str | None = None
    read_at: str | None = None


@dataclass
class Message:
    id: str
    server: Path                        # síťová složka s projekty, ve které vzkaz je
    created_at: str
    user: str
    pc: str
    author: str
    recipients: list[Person]
    cc: list[Person]
    text: str
    projects: list[dict] = field(default_factory=list)   # [{"name", "project_id"}]
    edited_at: str | None = None
    versions: list[Version] = field(default_factory=list)
    replies: list[Reply] = field(default_factory=list)    # nejstarší první
    states: dict[str, UserState] = field(default_factory=dict)  # login -> stav
    archived: bool = False                # v archivu vzkazů

    @property
    def folder(self) -> Path:
        root = messages_dir(self.server)
        return (root / ARCHIVE_SUBDIR if self.archived else root) / self.id

    @property
    def closed_at(self) -> str:
        """Kdy byl vzkaz naposledy aktivní – poslední vyřízení / dokončení nebo změna."""
        return max([self.last_activity] + [s.resolved_at or "" for s in self.resolvers])

    # --- kdo -------------------------------------------------------------------------

    def is_author(self, login: str) -> bool:
        return self.user.lower() == login.lower()

    def is_recipient(self, login: str) -> bool:
        return any(p.login == login.lower() for p in self.recipients)

    def is_cc(self, login: str) -> bool:
        return any(p.login == login.lower() for p in self.cc)

    def involves(self, login: str) -> bool:
        return self.is_author(login) or self.is_recipient(login) or self.is_cc(login)

    # --- stav ------------------------------------------------------------------------

    @property
    def resolvers(self) -> list[UserState]:
        """Kdo vzkaz vyřešil – adresáti, nebo autor (dokončeno); nejdřív první."""
        done = [s for login, s in self.states.items()
                if s.resolved_at and (self.is_recipient(login) or self.is_author(login))]
        return sorted(done, key=lambda s: s.resolved_at or "")

    @property
    def resolved(self) -> bool:
        return bool(self.resolvers)

    def resolved_by(self, login: str) -> bool:
        s = self.states.get(login.lower())
        return bool(s and s.resolved_at)

    def read_by(self, login: str) -> bool:
        s = self.states.get(login.lower())
        return bool(s and s.read_at)

    @property
    def last_activity(self) -> str:
        times = [self.created_at, self.edited_at or ""]
        times += [t for r in self.replies for t in (r.created_at, r.edited_at or "")]
        return max(times)

    def last_change_by_others(self, me: str) -> str:
        """Poslední změna od někoho jiného (nový vzkaz, úprava, odpověď) – podle ní „nové“.
        Vyřízení ani přečtení se nepočítá."""
        times = []
        if not self.is_author(me):
            times += [self.created_at, self.edited_at or ""]
        for r in self.replies:
            if not r.is_author(me):
                times += [r.created_at, r.edited_at or ""]
        return max(times, default="")

    def reply(self, reply_id: str) -> Reply | None:
        return next((r for r in self.replies if r.id == reply_id), None)


def messages_dir(server_root: Path | str) -> Path:
    return Path(server_root) / SPM_DIR / MESSAGES_DIR


# --- soubory -------------------------------------------------------------------------------

def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + TEMP_SUFFIX)
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _safe(login: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in login.lower())


def _people(items) -> list[Person]:
    result = []
    for item in items or []:
        if isinstance(item, dict) and item.get("login"):
            result.append(Person(str(item["login"]).lower(), str(item.get("name") or "")))
    return result


def _versions(items) -> list[Version]:
    return [Version(str(v.get("saved_at", "")), str(v.get("text", "")), _people(v.get("recipients")))
            for v in items or [] if isinstance(v, dict)]


def _new_id() -> str:
    return f"{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"


def _clean_people(people: list) -> list[Person]:
    """Seznam osob bez opakování (login malými písmeny)."""
    result: list[Person] = []
    for p in people:
        person = p if isinstance(p, Person) else Person(str(p[0]), str(p[1]) if len(p) > 1 else "")
        person = Person(person.login.strip().lower(), person.name)
        if person.login and all(r.login != person.login for r in result):
            result.append(person)
    return result


# --- čtení ---------------------------------------------------------------------------------

class MessageStore:
    """Vzkazy jedné síťové složky. Soubory se čtou znovu jen při změně (čas a velikost) –
    obnovení seznamu vzkazů je tak levné i při každém obnovení okna."""

    def __init__(self, server_root: Path | str):
        self.server = Path(server_root)
        self._cache: dict[str, tuple[tuple[int, int], object]] = {}
        self._archived_on = ""   # den poslední archivace starých vzkazů (nejvýš jednou denně)

    def _read(self, entry: os.DirEntry):
        try:
            st = entry.stat()
        except OSError:
            return None
        stamp = (st.st_mtime_ns, st.st_size)
        cached = self._cache.get(entry.path)
        if cached and cached[0] == stamp:
            return cached[1]
        try:
            data = json.loads(Path(entry.path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        self._cache[entry.path] = (stamp, data)
        return data

    def load(self) -> list[Message]:
        """Aktivní vzkazy síťové složky (naposledy aktivní první; bez archivu).
        Nedostupná složka -> OSError."""
        root = messages_dir(self.server)
        if not root.is_dir():
            if not self.server.is_dir():
                raise OSError(f"Síťová složka není dostupná: {self.server}")
            return []
        return self._load_dir(root, archived=False)

    def load_archive(self) -> list[Message]:
        """Archivované vzkazy (jen na vyžádání – v okně Vzkazy „Zobrazit i archiv“)."""
        root = messages_dir(self.server) / ARCHIVE_SUBDIR
        if not root.is_dir():
            if not self.server.is_dir():
                raise OSError(f"Síťová složka není dostupná: {self.server}")
            return []
        return self._load_dir(root, archived=True)

    def _load_dir(self, root: Path, archived: bool) -> list[Message]:
        result = []
        with os.scandir(root) as it:
            folders = [e for e in it if e.is_dir() and not e.name.startswith((".", "_"))]
        for folder in folders:
            try:
                msg = self._load_one(folder)
            except OSError:
                continue
            if msg is not None:
                msg.archived = archived
                result.append(msg)
        return sorted(result, key=lambda m: m.last_activity, reverse=True)

    def archive_old(self, messages: list[Message], days: int, today: datetime | None = None) -> list[str]:
        """Přesune do archivu vzkazy vyřízené / dokončené před více než days dny (bez nové
        odpovědi). Nejvýš jednou denně; days 0 = nearchivovat. Vrací id přesunutých."""
        today = today or datetime.now()
        if days <= 0 or self._archived_on == today.strftime("%Y-%m-%d"):
            return []
        self._archived_on = today.strftime("%Y-%m-%d")
        limit = (today - timedelta(days=days)).isoformat(timespec="seconds")
        moved = []
        for m in messages:
            if m.archived or not m.resolved or m.closed_at >= limit:
                continue
            try:
                archive_message(m)
            except OSError:
                continue  # přesouvá ho zrovna někdo jiný / soubor je otevřený – příště
            moved.append(m.id)
        return moved

    def _load_one(self, folder: os.DirEntry) -> Message | None:
        with os.scandir(folder.path) as it:
            entries = [e for e in it if e.is_file() and e.name.endswith(".json")]
        main = next((e for e in entries if e.name == MESSAGE_FILE), None)
        data = self._read(main) if main else None
        if not isinstance(data, dict):
            return None
        msg = Message(
            id=folder.name, server=self.server, created_at=str(data.get("created_at", "")),
            user=str(data.get("user", "")), pc=str(data.get("pc", "")),
            author=str(data.get("author") or data.get("user", "")),
            recipients=_people(data.get("recipients")), cc=_people(data.get("cc")),
            text=str(data.get("text", "")),
            projects=[p for p in data.get("projects") or [] if isinstance(p, dict) and p.get("name")],
            edited_at=data.get("edited_at"), versions=_versions(data.get("versions")))
        for e in entries:
            if e.name.startswith(REPLY_PREFIX):
                r = self._read(e)
                if isinstance(r, dict) and r.get("text"):
                    msg.replies.append(Reply(e.name[len(REPLY_PREFIX):-5], str(r.get("created_at", "")),
                                             str(r.get("user", "")), str(r.get("author") or r.get("user", "")),
                                             str(r["text"]), r.get("edited_at"), _versions(r.get("versions"))))
            elif e.name.startswith(STATE_PREFIX):
                s = self._read(e)
                if isinstance(s, dict) and s.get("login"):
                    login = str(s["login"]).lower()
                    msg.states[login] = UserState(login, str(s.get("name") or ""),
                                                  s.get("resolved_at"), s.get("read_at"))
        msg.replies.sort(key=lambda r: r.created_at)
        return msg


# --- archiv vzkazů -------------------------------------------------------------------------

def archive_message(msg: Message) -> None:
    """Přesun vzkazu do archivu (přejmenování složky – nic se nemaže)."""
    target = messages_dir(msg.server) / ARCHIVE_SUBDIR / msg.id
    target.parent.mkdir(parents=True, exist_ok=True)
    msg.folder.rename(target)
    msg.archived = True


def _ensure_active(msg: Message) -> None:
    """Změna archivovaného vzkazu (odpověď, vyřízení…) ho vrátí mezi aktivní."""
    if not msg.archived:
        return
    target = messages_dir(msg.server) / msg.id
    try:
        msg.folder.rename(target)
    except FileNotFoundError:
        if not target.is_dir():  # mezitím ho vrátil někdo jiný -> v pořádku
            raise MessageError("Vzkaz už neexistuje.") from None
    msg.archived = False


# --- zápis ---------------------------------------------------------------------------------

def send(server_root: Path | str, user: str, pc: str, author: str, recipients: list, text: str,
         cc: list | None = None, projects: list[dict] | None = None) -> str:
    """Nový vzkaz. Vrací jeho id."""
    text = text.strip()
    recipients = _clean_people(recipients)
    if not text:
        raise MessageError("Vzkaz nesmí být prázdný.")
    if not recipients:
        raise MessageError("Vyberte aspoň jednoho adresáta.")
    cc = [p for p in _clean_people(cc or []) if all(r.login != p.login for r in recipients)]
    msg_id = _new_id()
    _write_json(messages_dir(server_root) / msg_id / MESSAGE_FILE, {
        "format": FORMAT, "created_at": now_iso(), "user": user, "pc": pc, "author": author,
        "recipients": [asdict(p) for p in recipients], "cc": [asdict(p) for p in cc],
        "text": text, "projects": list(projects or [])})
    return msg_id


def _main_data(msg: Message) -> dict:
    try:
        data = json.loads((msg.folder / MESSAGE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise MessageError("Vzkaz už neexistuje.") from None
    if not isinstance(data, dict):
        raise MessageError("Vzkaz je poškozený.")
    return data


def edit(msg: Message, user: str, text: str, recipients: list | None = None, cc: list | None = None,
         projects: list[dict] | None = None) -> bool:
    """Úprava vzkazu autorem (předchozí znění se uloží). None = beze změny. Vrací, zda se něco
    změnilo."""
    if not msg.is_author(user):
        raise MessageError("Upravit vzkaz může jen jeho autor.")
    text = text.strip()
    if not text:
        raise MessageError("Vzkaz nesmí být prázdný.")
    _ensure_active(msg)
    data = _main_data(msg)
    new_recipients = [asdict(p) for p in _clean_people(recipients)] if recipients is not None \
        else data.get("recipients", [])
    if not new_recipients:
        raise MessageError("Vyberte aspoň jednoho adresáta.")
    logins = {r["login"] for r in new_recipients}
    new_cc = [asdict(p) for p in _clean_people(cc) if p.login not in logins] if cc is not None \
        else data.get("cc", [])
    new_projects = list(projects) if projects is not None else data.get("projects", [])
    if (text, new_recipients, new_cc) == (data.get("text"), data.get("recipients"), data.get("cc")):
        if new_projects == data.get("projects", []):
            return False
    versions = [{"saved_at": now_iso(), "text": data.get("text", ""),
                 "recipients": data.get("recipients", [])}] + list(data.get("versions") or [])
    data.update(text=text, recipients=new_recipients, cc=new_cc, projects=new_projects,
                edited_at=now_iso(), versions=versions)
    _write_json(msg.folder / MESSAGE_FILE, data)
    return True


def reply(msg: Message, user: str, author: str, text: str) -> str:
    """Odpověď na vzkaz (autor, adresáti i na vědomí). Vrací id odpovědi."""
    text = text.strip()
    if not text:
        raise MessageError("Odpověď nesmí být prázdná.")
    _ensure_active(msg)
    if not msg.folder.is_dir():
        raise MessageError("Vzkaz už neexistuje.")
    reply_id = f"{_new_id()}_{_safe(user)}"
    _write_json(msg.folder / f"{REPLY_PREFIX}{reply_id}.json",
                {"created_at": now_iso(), "user": user, "author": author, "text": text})
    return reply_id


def edit_reply(msg: Message, reply_id: str, user: str, text: str) -> bool:
    r = msg.reply(reply_id)
    if r is None:
        raise MessageError("Odpověď už neexistuje.")
    if not r.is_author(user):
        raise MessageError("Upravit odpověď může jen její autor.")
    text = text.strip()
    if not text:
        raise MessageError("Odpověď nesmí být prázdná.")
    if text == r.text:
        return False
    _ensure_active(msg)
    path = msg.folder / f"{REPLY_PREFIX}{reply_id}.json"
    versions = [{"saved_at": now_iso(), "text": r.text}] + [asdict(v) for v in r.versions]
    _write_json(path, {"created_at": r.created_at, "user": r.user, "author": r.author, "text": text,
                       "edited_at": now_iso(), "versions": versions})
    return True


def _set_state(msg: Message, user: str, name: str, **changes) -> None:
    _ensure_active(msg)
    login = user.lower()
    current = msg.states.get(login) or UserState(login, name)
    state = {"login": login, "name": name or current.name, "resolved_at": current.resolved_at,
             "read_at": current.read_at}
    state.update(changes)
    _write_json(msg.folder / f"{STATE_PREFIX}{_safe(login)}.json", state)


def set_resolved(msg: Message, user: str, name: str, resolved: bool = True) -> None:
    """Vyřízení vzkazu adresátem, nebo dokončení autorem (vyřízený je, když ho vyřeší aspoň
    jeden z adresátů nebo ho autor dokončí)."""
    if not (msg.is_recipient(user) or msg.is_author(user)):
        raise MessageError("Vyřídit vzkaz může jen jeho adresát nebo autor.")
    _set_state(msg, user, name, resolved_at=now_iso() if resolved else None)


def set_read(msg: Message, user: str, name: str, read: bool = True) -> None:
    """Na vědomí: „přečteno“ (nepovinné)."""
    if not msg.involves(user):
        raise MessageError("Vzkaz se vás netýká.")
    _set_state(msg, user, name, read_at=now_iso() if read else None)


# --- odkazy na projekty --------------------------------------------------------------------

def project_names(server_root: Path | str) -> list[str]:
    """Názvy projektů síťové složky (jen výpis složek – pro našeptávání „#“)."""
    try:
        with os.scandir(server_root) as it:
            return sorted((e.name for e in it if e.is_dir() and not e.name.startswith(".")), key=str.lower)
    except OSError:
        return []


def _project_id(project_dir: Path) -> str:
    from .db import ProjectDB, db_path
    if not db_path(project_dir).exists():
        return ""
    try:
        with ProjectDB.for_project(project_dir) as db:
            return db.project_id() or ""
    except Exception:  # noqa: BLE001 – nečitelná databáze: odkaz jen podle názvu
        return ""


def project_info(server_root: Path | str, names: list[str]) -> list[dict]:
    """Zmíněné projekty s identitou (odkaz pak přežije přejmenování projektu)."""
    return [{"name": n, "project_id": _project_id(Path(server_root) / n)} for n in names]


def find_project(server_root: Path | str, name: str, project_id: str = "") -> str | None:
    """Aktuální název odkazovaného projektu: podle názvu, a když projekt mezitím někdo
    přejmenoval, podle identity. None = projekt v pracovní složce není (smazaný, archivovaný)."""
    root = Path(server_root)
    names = project_names(root)
    by_lower = {n.lower(): n for n in names}
    current = by_lower.get(name.lower())
    if current and (not project_id or _project_id(root / current) in ("", project_id)):
        return current
    if project_id:
        for n in names:
            if _project_id(root / n) == project_id:
                return n
    return None
