"""Provádění skriptů SPM nad soubory odevzdání.

Skript vidí jen soubory odevzdání (množina `commit`) a smí je jen číst. Zapisovat smí mimo
složky záložek SPM (síťové i místní) – do projektů jiných záložek jen příkazem MIRROR, který
vede evidenci a zamyká zrcadlené soubory. Relativní cesty v příkazech patří do dočasné
pracovní složky {WORK}, která se po běhu smaže.

Zkušební běh (dry) nic nezapisuje – jen vypíše, co by se stalo.
"""

from __future__ import annotations

import codecs
import fnmatch
import json
import os
import posixpath
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Callable

from .. import __version__, recycle
from ..db import ProjectDB, db_path
from ..mirror import Mirror, MirrorError, MirrorItem, MirrorSource, format_counts
from .. import SPM_DIR
from ..scanner import Progress, copy_with_hash, set_readonly
from . import lang
from .lang import ScriptSyntaxError
from . import store
from .model import ADDED, DELETED, RENAMED, RunReport, ScriptFile, ScriptUI, TabInfo

COMMIT_SET = "commit"
RUN_TIMEOUT = 300      # s – RUN bez WAIT (program, který se zasekne, nesmí zastavit odevzdání navždy)
POST_TIMEOUT = 30      # s – POST bez TIMEOUT
LOG_OUTPUT_LINES = 30  # kolik posledních řádků výstupu RUN / odpovědi POST jde do záznamu
SECRET_MASK = "•••"
FORMAT_KEY = "#format:"  # mezi proměnnými: formát proměnné (# v názvu proměnné být nemůže)


class Record:
    """Výsledek RUN (code, output) nebo POST (status, body, json(…)) uložený přes -> proměnná."""

    def __init__(self, kind: str, **fields):
        self.kind, self.fields = kind, fields

    def to_dict(self) -> dict:
        return {"record": self.kind, "fields": self.fields}


def _decode(data: bytes) -> str:
    """Výstup programu: UTF-8, jinak kódování konzole Windows (OEM), jinak ANSI."""
    for enc in ("utf-8", "oem", "cp1250"):
        try:
            codecs.lookup(enc)
            return data.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("cp1250", errors="replace")


def _tail(text: str, lines: int = LOG_OUTPUT_LINES) -> list[str]:
    rows = text.strip().splitlines()
    return (["   …"] if len(rows) > lines else []) + ["   " + r for r in rows[-lines:]]


DECIMALS_SHOWN = 6  # desetinná čísla bez FORMAT: nejvýš tolik míst (bez koncových nul)
_FORMAT_RE = re.compile(r"(0+)(?:([.,])(0+))?")


def number_text(value: float) -> str:
    """Desetinné číslo jako text: desetinná čárka, bez zbytečných nul (2.50 -> 2,5)."""
    text = f"{value:.{DECIMALS_SHOWN}f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text.replace(".", ",")


def format_number(value: int | float, mask: str) -> str:
    """FORMAT: maska z nul – před oddělovačem nejmenší počet cifer (doplní se nulami),
    za ním přesný počet desetinných míst (zaokrouhlí se, 5 nahoru). Oddělovač masky
    (tečka / čárka) se použije i ve výsledku. "000" -> 007, "0.00" -> 3.14, "00,0" -> 03,1."""
    m = _FORMAT_RE.fullmatch(mask.strip())
    if m is None:
        raise ValueError(f"FORMAT: maska „{mask}“ – použijte nuly a oddělovač, např. \"000\", "
                         "\"0.00\", \"000,0\".")
    digits, sep, decimals = len(m.group(1)), m.group(2) or "", len(m.group(3) or "")
    number = Decimal(str(value)).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    sign = "-" if number < 0 else ""
    whole, _, frac = f"{abs(number):.{decimals}f}".partition(".")
    return sign + whole.zfill(digits) + (sep + frac if decimals else "")


def _quote(arg: str) -> str:
    return f'"{arg}"' if not arg or any(c in arg for c in ' \t"') else arg
CALL_DEPTH_MAX = 10   # CALL ve volaném skriptu – ochrana proti zacyklení


class ScriptError(Exception):
    def __init__(self, line: int, message: str, script: str = ""):
        self.line, self.message, self.script = line, message, script
        where = (f"{script}, řádek {line}" if script else f"Řádek {line}") if line else script
        super().__init__(f"{where}: {message}" if where else message)


class _Exit(Exception):
    pass


@dataclass
class ScriptEnv:
    """Vše, co skript o odevzdání ví."""
    pm: object                    # ProjectManager záložky, ze které se odevzdává
    project: str
    action: str                   # submit / deactivate / publish / test
    files: list[ScriptFile]
    comment: str = ""
    commit_id: int | None = None
    tab: TabInfo | None = None    # záložka, ze které se odevzdává
    tabs: list[TabInfo] | None = None
    pm_for_tab: Callable[[TabInfo], object] | None = None
    nick: str = ""
    now: datetime | None = None
    ui: ScriptUI | None = None    # dialogy (ASK, INPUT, CHOOSE, FORM)
    secrets: dict[str, str] | None = None  # tajné hodnoty uživatele ({SECRET:název})


# --- masky souborů -----------------------------------------------------------------

def _mask_regex(mask: str) -> re.Pattern:
    mask = mask.replace("\\", "/").strip("/")
    if "/" not in mask:
        mask = "**/" + mask  # bez složky = název souboru kdekoliv ve stromu
    out, i = [], 0
    while i < len(mask):
        if mask.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif mask.startswith("/**", i) and i + 3 == len(mask):
            out.append("(?:/.*)?")
            i += 3
        elif mask.startswith("**", i):
            out.append(".*")
            i += 2
        elif mask[i] == "*":
            out.append("[^/]*")
            i += 1
        elif mask[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(mask[i]))
            i += 1
    return re.compile("".join(out), re.IGNORECASE)


def compile_mask(text: str) -> Callable[[str], bool]:
    """Masky oddělené středníkem; ! na začátku = vynechat. "*.h" = kdekoliv, "Dily/**" = vše
    ve složce Dily, "**/CAM/*.nc" = .nc ve složkách CAM."""
    include, exclude = [], []
    for part in text.split(";"):
        part = part.strip()
        if not part:
            continue
        if part.startswith("!"):
            exclude.append(_mask_regex(part[1:].strip()))
        else:
            include.append(_mask_regex(part))

    def match(path: str) -> bool:
        if include and not any(r.fullmatch(path) for r in include):
            return False
        return not any(r.fullmatch(path) for r in exclude)
    return match


def select(files: list[ScriptFile], mask: str | None, statuses: list[str] | None) -> list[ScriptFile]:
    """Výběr z množiny. Přejmenovaný soubor se porovná s novou i starou cestou: odpovídá-li
    jen nová, je pro výběr nový (added); jen stará – je pro výběr smazaný (deleted)."""
    match = compile_mask(mask) if mask is not None else None
    result = []
    for f in files:
        g = f
        if match is not None:
            if f.status == RENAMED and f.old_path:
                new_ok, old_ok = match(f.path), match(f.old_path)
                if new_ok and not old_ok:
                    g = replace(f, status="added", old_path=None)
                elif old_ok and not new_ok:
                    g = replace(f, status=DELETED, path=f.old_path, old_path=None)
                elif not new_ok:
                    continue
            elif not match(f.path):
                continue
        if statuses and g.status not in statuses:
            continue
        result.append(g)
    return result


# --- mapování cest ------------------------------------------------------------------

def map_path(rel: str, mapping: lang.Mapping, base: str = "") -> str:
    """Cesta souboru v cíli podle BASE / KEEP / FLAT."""
    if mapping.kind == "keep":
        parts = rel.split("/")
        return "/".join(parts[-(mapping.keep + 1):])
    if mapping.kind == "base":
        prefix = base.replace("\\", "/").strip("/")
        if not prefix:
            return rel
        if rel.lower().startswith(prefix.lower() + "/"):
            return rel[len(prefix) + 1:]
        raise ValueError(f"Soubor {rel} neleží ve složce BASE „{prefix}“.")
    return rel


# --- interpret ----------------------------------------------------------------------

def _norm(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _inside(path: Path | str, root: Path | str) -> bool:
    p, r = _norm(path), _norm(root).rstrip("\\/")
    return p == r or p.startswith(r + os.sep)


def _count(n: int, one: str, few: str, many: str) -> str:
    return f"{n} {one if n == 1 else few if 2 <= n <= 4 else many}"


class Interpreter:
    def __init__(self, env: ScriptEnv, work: Path, report: RunReport, dry: bool = False,
                 progress: Progress | None = None):
        self.env, self.work, self.report, self.dry = env, work, report, dry
        self.progress = progress
        self.script = ""
        self.line = 0
        self.vars: dict[str, object] = {}
        self.virtual: set[str] = set()  # zkušební běh: cesty, které by vznikly
        now = env.now or datetime.now()
        pm = env.pm
        tab = env.tab
        self.builtins: dict[str, object] = {
            "user": pm.user, "pc": pm.pc, "nick": env.nick or pm.user,
            "project": env.project, "comment": env.comment,
            "commit_id": env.commit_id if env.commit_id is not None else "",
            "action": env.action,
            "date": now.strftime("%y%m%d"), "time": now.strftime("%H%M"),
            "yy": now.strftime("%y"), "yyyy": now.strftime("%Y"), "mm": now.strftime("%m"),
            "dd": now.strftime("%d"), "hh": now.strftime("%H"), "min": now.strftime("%M"),
            "ss": now.strftime("%S"),
            "local_dir": str(pm.local_dir(env.project)), "server_dir": str(pm.server_dir(env.project)),
            "server_root": str(pm.server_root), "work": str(work),
            "tab": tab.name if tab else "", "server_name": pm.settings.server_name,
        }
        self._project_id: str | None = None
        self._templates: dict[str, list] = {}
        self.depth = 0  # zanoření CALL
        self.phase = "after"  # before = blok BEFORE (před nahráním), after = po nahrání
        self.created_local: set[str] = set()  # zkušební běh: soubory, které by BEFORE vytvořil v projektu
        self.used_secrets: set[str] = set()
        self.activated: list[str] = []  # soubory projektu aktivované skriptem v BEFORE
        self._writable_rel: set[str] = set()  # soubory, do kterých už smí zapisovat (kontrola proběhla)
        self.mirrored: set[tuple[str, str]] = set()  # (file_id ve zdroji, cílový projekt) z MIRROR

    # --- běh ---

    def block(self, body: list) -> None:
        for cmd in body:
            self.line = cmd.line
            if self.progress:
                self.progress(0, 0, f"{self.script}: řádek {cmd.line} – {cmd.keyword}")
            try:
                getattr(self, "cmd_" + type(cmd).__name__)(cmd)
            except (ScriptError, _Exit):
                raise
            except ScriptSyntaxError as e:
                raise self.error(e.message) from None
            except MirrorError as e:
                raise self.error(str(e)) from None
            except OSError as e:
                raise self.error(f"{cmd.keyword}: {e}") from None
            except ValueError as e:
                raise self.error(str(e)) from None

    def error(self, message: str) -> ScriptError:
        return ScriptError(self.line, self.mask(message), self.script)

    def log(self, text: str) -> None:
        self.report.log.append(self.mask(("(zkouška) " if self.dry else "") + text))

    def mask(self, text: str) -> str:
        """Použité tajné hodnoty se do záznamu běhu ani do chyb nevypíšou."""
        for secret in sorted(self.used_secrets, key=len, reverse=True):
            text = text.replace(secret, SECRET_MASK)
        return text

    # --- hodnoty ---

    def lookup(self, name: str):
        key = name.lower()
        if key in self.vars:
            return self.vars[key]
        if key == COMMIT_SET:
            return list(self.env.files)
        if key == "project_id":
            return self.project_id()
        if key in self.builtins:
            return self.builtins[key]
        raise self.error(f"Neznámá proměnná „{name}“.")

    def project_id(self) -> str:
        if self._project_id is None:
            server = self.env.pm.server_dir(self.env.project)
            pid = None
            if db_path(server).exists():
                with ProjectDB.for_project(server) as db:
                    pid = db.project_id()
                if pid is None and not self.dry:
                    with self.env.pm._write(server) as db:
                        pid = db.ensure_project_id()
            self._project_id = pid or ""
        return self._project_id

    def text(self, value) -> str:
        if value is None:
            return ""
        if isinstance(value, ScriptFile):
            return value.path
        if isinstance(value, list):
            raise self.error("Množinu souborů nelze vložit do textu – použijte např. {COUNT(množina)} "
                             "nebo FOR. (Číslo odevzdání je {COMMIT_ID}.)")
        if isinstance(value, float):
            return number_text(value)
        if isinstance(value, Record):
            fields = ", ".join(f"{{x.{k}}}" for k in value.fields)
            raise self.error(f"Výsledek {value.kind} nelze vložit do textu celý – použijte vlastnost: "
                             f"{fields}" + (", {x.json(\"klíč\")}" if value.kind == "POST" else "") + ".")
        return str(value)

    def interp(self, raw: str, extra: dict | None = None) -> str:
        parts = self._templates.get(raw)
        if parts is None:
            parts = [(kind, lang.parse_inline(src, self.line) if kind == "expr" else src)
                     for kind, src in lang.split_template(raw, self.line)]
            self._templates[raw] = parts
        saved = {}
        if extra:
            saved = {k: self.vars.get(k) for k in extra}
            self.vars.update(extra)
        try:
            return "".join(src if kind == "text" else self.text(self.eval(src)) for kind, src in parts)
        finally:
            for k, v in saved.items():
                if v is None:
                    self.vars.pop(k, None)
                else:
                    self.vars[k] = v

    def eval(self, node):
        if isinstance(node, lang.Str):
            return self.interp(node.raw)
        if isinstance(node, lang.BinOp):
            return self.arithmetic(node.op, self.eval(node.left), self.eval(node.right))
        if isinstance(node, lang.Neg):
            value = self.number(self.eval(node.expr), "-")
            return -value
        if isinstance(node, lang.Num):
            return node.value
        if isinstance(node, lang.Var):
            return self.lookup(node.name)
        if isinstance(node, lang.SetExpr):
            return self.eval_set(node)
        if isinstance(node, lang.Prop):
            return self.prop(self.eval(node.base), node.name, [self.eval(a) for a in node.args])
        if isinstance(node, lang.Call):
            return self.call(node)
        raise self.error("Neplatný výraz.")

    def eval_set(self, node: lang.SetExpr) -> list[ScriptFile]:
        source = self.lookup(node.source)
        if isinstance(source, ScriptFile):
            source = [source]
        if not isinstance(source, list):
            raise self.error(f"„{node.source}“ není množina souborů (vytvoříte ji příkazem FILES).")
        mask = self.interp(node.mask.raw) if node.mask is not None else None
        return select(source, mask, node.statuses)

    def files_of(self, value, what: str) -> list[ScriptFile]:
        if isinstance(value, ScriptFile):
            return [value]
        if isinstance(value, list):
            return value
        raise self.error(f"{what}: očekává se množina souborů nebo soubor.")

    def call(self, node: lang.Call):
        f = node.func
        args = [self.eval(a) for a in node.args]

        def need(n: int) -> None:
            if len(args) != n:
                raise self.error(f"{f}() potřebuje {_count(n, 'argument', 'argumenty', 'argumentů')}.")
        if f in ("FIRST", "LAST", "ONE", "COUNT"):
            need(1)
            files = self.files_of(args[0], f)
            if f == "COUNT":
                return len(files)
            if not files:
                raise self.error(f"{f}(): množina je prázdná – žádný soubor neodpovídá.")
            ordered = sorted(files, key=lambda x: x.path.lower())
            if f == "ONE" and len(files) > 1:
                if self.env.ui is None:
                    raise self.error("ONE(): odpovídá více souborů, očekává se právě jeden:\n"
                                     + "\n".join(x.path for x in ordered[:10]))
                return ordered[self.choose_index("Odpovídá více souborů – vyberte jeden:",
                                                 [x.path for x in ordered], 0)]
            return ordered[-1] if f == "LAST" else ordered[0]
        if f in ("UPPER", "LOWER"):
            need(1)
            text = self.text(args[0])
            return text.upper() if f == "UPPER" else text.lower()
        if f == "REPLACE":
            need(3)
            text, old, new = (self.text(a) for a in args)
            return re.sub(re.escape(old), lambda _: new, text, flags=re.IGNORECASE) if old else text
        if f == "MATCH":
            if len(args) not in (2, 3):
                raise self.error("MATCH(text, \"regulární výraz\" [, skupina]).")
            try:
                m = re.search(self.text(args[1]), self.text(args[0]), re.IGNORECASE)
            except re.error as e:
                raise self.error(f"MATCH: chybný regulární výraz ({e}).") from None
            if m is None:
                return ""
            group = args[2] if len(args) == 3 else (1 if m.groups() else 0)
            try:
                return m.group(int(group)) or ""
            except (IndexError, ValueError):
                raise self.error(f"MATCH: výraz nemá skupinu {group}.") from None
        if f == "TAB":
            need(1)
            return self.tab_by_name(self.text(args[0])).server_root
        if f == "ACTIVATE":
            need(1)
            return 1 if self.activate(args[0]) else 0
        if f == "FORMAT":
            need(2)
            return self.format(args[0], self.text(args[1]))
        if f == "SECRET":
            need(1)
            name = self.text(args[0]).strip()
            secrets = self.env.secrets or {}
            key = next((k for k in secrets if k.casefold() == name.casefold()), None)
            if key is None or not secrets[key]:
                raise self.error(f"Tajná hodnota „{name}“ není nastavená (Nastavení → Tajné hodnoty).")
            self.used_secrets.add(secrets[key])
            return secrets[key]
        if f == "ASK":
            need(1)
            answer = self.ui().ask(self.dialog_title(), self.text(args[0]))
            self.log(f"ASK „{self.text(args[0])}“: {'ano' if answer else 'ne'}")
            return 1 if answer else 0
        if f == "INPUT":
            if len(args) not in (1, 2):
                raise self.error("INPUT(\"popisek\" [, výchozí hodnota]).")
            prompt = self.text(args[0])
            value = self.ui().input(self.dialog_title(), prompt, self.text(args[1]) if len(args) > 1 else "")
            if value is None:
                raise self.error(f"Zadání „{prompt}“ bylo zrušeno.")
            self.log(f"INPUT „{prompt}“: {value}")
            return value
        if f == "CHOOSE":
            if len(args) < 2:
                raise self.error("CHOOSE(\"popisek\", \"a\", \"b\", …) nebo CHOOSE(\"popisek\", množina).")
            values = self.options(args[1:])
            index = self.choose_index(self.text(args[0]), [self.text(v) for v in values], 0)
            self.log(f"CHOOSE „{self.text(args[0])}“: {self.text(values[index])}")
            return values[index]
        raise self.error(f"Neznámá funkce {f}.")

    # --- dialogy ---

    def ui(self) -> ScriptUI:
        if self.env.ui is None:
            raise self.error("Dialog nelze zobrazit – skript neběží v okně aplikace.")
        return self.env.ui

    def dialog_title(self) -> str:
        return f"Skript {self.script} – projekt {self.env.project}"

    def options(self, values: list) -> list:
        """Volby CHOOSE: texty, nebo soubory jedné množiny."""
        if len(values) == 1 and isinstance(values[0], list):
            files = sorted((f for f in values[0] if f.status != DELETED), key=lambda x: x.path.lower())
            if not files:
                raise self.error("CHOOSE: množina je prázdná – není z čeho vybírat.")
            return files
        if any(isinstance(v, list) for v in values):
            raise self.error("CHOOSE: množinu lze použít jen samotnou (CHOOSE(\"popisek\", množina)).")
        return values

    def choose_index(self, prompt: str, labels: list[str], default: int) -> int:
        index = self.ui().choose(self.dialog_title(), prompt, labels, default)
        if index is None:
            raise self.error(f"Výběr „{prompt}“ byl zrušen.")
        return index

    def prop(self, value, name: str, args: list):
        if isinstance(value, Record):
            return self.record_prop(value, name, args)
        if isinstance(value, ScriptFile):
            rel = value.path
            if name == "full":
                path = self.source_of(value)
                return str(path) if path else str(self.env.pm.server_dir(self.env.project) / rel)
            if name == "status":
                return value.status
            if name == "old_path":
                return value.old_path or ""
            if name == "old_name":
                return posixpath.basename(value.old_path) if value.old_path else ""
            if name == "size":
                return value.size if value.size is not None else ""
            if name == "path":
                return rel
        elif isinstance(value, str):
            rel = value.replace("\\", "/")
            if name in ("full", "path"):
                return value
        else:
            raise self.error(f"Vlastnost .{name} má jen soubor nebo cesta.")
        directory, file_name = posixpath.split(rel.rstrip("/"))
        if name == "name":
            return file_name
        if name in ("stem", "ext"):
            stem, ext = posixpath.splitext(file_name)
            return stem if name == "stem" else ext
        if name == "dir":
            return directory
        if name == "folder":
            return posixpath.basename(directory)
        if name == "parent":
            n = int(args[0]) if args else 1
            parts = directory.split("/") if directory else []
            return parts[-n] if 1 <= n <= len(parts) else ""
        raise self.error(f"Neznámá vlastnost .{name} (možné: name, stem, ext, folder, dir, "
                         "parent(n), path, full, status, old_path, old_name, size).")

    # --- aktivace souborů projektu (BEFORE) ---

    def project_rel(self, value) -> str:
        """Cesta souboru v projektu: soubor, úplná cesta v místní kopii / na serveru, nebo
        cesta relativní k projektu."""
        if isinstance(value, ScriptFile):
            return value.path
        if not isinstance(value, str) or not value.strip():
            raise self.error("ACTIVATE: očekává se soubor projektu (např. \"{LOCAL_DIR}/test.txt\").")
        path = Path(value.strip())
        if not path.is_absolute():
            parts = [p for p in path.as_posix().split("/") if p not in ("", ".")]
            if not parts or ".." in parts or path.drive:
                raise self.error(f"ACTIVATE: neplatná cesta „{value}“.")
            return "/".join(parts)
        for root in (self.local_dir(), self.env.pm.server_dir(self.env.project)):
            if _inside(path, root) and _norm(path) != _norm(root):
                return Path(os.path.relpath(path, root)).as_posix()
        raise self.error(f"ACTIVATE: „{value}“ neleží v projektu {self.env.project}.")

    def _file_state(self, rel: str):
        try:
            return self.env.pm.script_file_state(self.env.project, rel)
        except Exception as e:  # noqa: BLE001 – např. projekt mezitím deaktivován
            raise self.error(str(e)) from None

    def _take(self, rel: str) -> ScriptFile:
        try:
            sf = self.env.pm.script_activate(self.env.project, rel, self.progress)
        except Exception as e:  # noqa: BLE001
            raise self.error(str(e)) from None
        self.activated.append(sf.path)
        return sf

    def _add_to_commit(self, sf: ScriptFile) -> None:
        if all(f.path.lower() != sf.path.lower() for f in self.env.files):
            self.env.files = self.env.files + [sf]

    def activate(self, value) -> bool:
        """ACTIVATE: soubor v projektu není -> False (skript může pokračovat a vytvořit ho);
        existuje -> zamkne se (stáhne se aktuální verze), přidá do commit -> True; drží ho jiný
        nebo je zrcadlený -> False (důvod v záznamu)."""
        if self.phase != "before":
            raise self.error("ACTIVATE lze použít jen v bloku BEFORE (před odevzdáním).")
        rel = self.project_rel(value)
        state, sf, who = self._file_state(rel)
        if state == "missing":
            self.log(f"ACTIVATE {rel}: v projektu není")
            return False
        if state in ("other", "mirror"):
            self.log(f"ACTIVATE {rel}: nelze – " + (f"drží ho {who}" if state == "other"
                                                    else f"spravuje ho zrcadlo z {who}"))
            return False
        if state == "free":
            if self.dry:
                self.log(f"ACTIVATE {rel}: aktivoval by se (zkušební běh – nezamčeno)")
            else:
                sf = self._take(rel)
                self.log(f"ACTIVATE {rel}: aktivován (stažena aktuální verze)")
        else:
            self.log(f"ACTIVATE {rel}: už je aktivovaný")
        self._add_to_commit(sf)
        self._writable_rel.add(sf.path.lower())
        return True

    def guard(self, path: Path) -> None:
        """V BEFORE: zápis do existujícího souboru projektu, který nemám aktivovaný, se nesmí
        stát potichu – zeptá se, zda soubor aktivovat (a přepsat), jinak chyba."""
        local = self.local_dir()
        if self.phase != "before" or not _inside(path, local) or _norm(path) == _norm(local):
            return
        rel = Path(os.path.relpath(path, local)).as_posix()
        if rel.lower() in self._writable_rel:
            return
        state, sf, who = self._file_state(rel)
        if state == "other":
            raise self.error(f"Soubor {rel} drží {who} – skript do něj nesmí zapisovat.")
        if state == "mirror":
            raise self.error(f"Soubor {rel} spravuje zrcadlo z {who} – skript do něj nesmí zapisovat.")
        if state == "free":
            question = (f"Skript chce zapsat do souboru {rel}, který nemáte aktivovaný.\n\n"
                        "Aktivovat ho (zamknout) a přepsat? Původní obsah na serveru se při "
                        "odevzdání nahradí.")
            if self.dry:
                self.log(f"   {rel} není aktivovaný – při odevzdání se skript zeptá na aktivaci")
            elif self.env.ui is None or not self.env.ui.ask(self.dialog_title(), question):
                raise self.error(f"Zápis do neaktivovaného souboru {rel} byl odmítnut – nic se "
                                 "neodevzdá. (Soubor aktivujte ve skriptu příkazem ACTIVATE.)")
            else:
                sf = self._take(rel)
                self._add_to_commit(sf)
                self.log(f"   {rel} aktivován na žádost (bude přepsán)")
        self._writable_rel.add(rel.lower())

    def record_prop(self, value: Record, name: str, args: list):
        if value.kind == "POST" and name == "json":
            return self.json_get(value.fields["body"], self.text(args[0]) if args else "")
        if name in value.fields:
            return value.fields[name]
        names = ", ".join(value.fields) + (", json(\"klíč\")" if value.kind == "POST" else "")
        raise self.error(f"Výsledek {value.kind} nemá vlastnost .{name} (možné: {names}).")

    def json_get(self, body: str, path: str):
        """Hodnota z JSON odpovědi: "klíč", "a.b", "polozky.0.nazev"; nenalezeno = prázdný text."""
        try:
            data = json.loads(body) if body.strip() else None
        except ValueError:
            raise self.error("Odpověď POST není JSON – použijte .body.") from None
        for part in [p for p in path.split(".") if p] if path else []:
            if isinstance(data, dict):
                data = data.get(part)
            elif isinstance(data, list) and part.lstrip("-").isdigit() and -len(data) <= int(part) < len(data):
                data = data[int(part)]
            else:
                data = None
            if data is None:
                return ""
        if isinstance(data, bool):
            return 1 if data else 0
        if isinstance(data, int):
            return data
        if isinstance(data, (dict, list)):
            return json.dumps(data, ensure_ascii=False)
        return "" if data is None else str(data)

    def truthy(self, value) -> bool:
        if isinstance(value, list):
            return bool(value)
        if isinstance(value, ScriptFile):
            return True
        if isinstance(value, (int, float)):
            return value != 0
        return bool(value)

    def cond(self, node) -> bool:
        if isinstance(node, lang.BoolOp):
            results = (self.cond(c) for c in node.items)
            return all(results) if node.op == "AND" else any(results)
        if isinstance(node, lang.Not):
            return not self.cond(node.cond)
        if isinstance(node, lang.Truthy):
            return self.truthy(self.eval(node.expr))
        if isinstance(node, lang.Exists):
            value = self.eval(node.expr)
            if isinstance(value, ScriptFile):
                return value.status != DELETED
            return self.exists(self.in_path(value))
        if isinstance(node, lang.Compare):
            return self.compare(self.eval(node.left), node.op, self.eval(node.right))
        raise self.error("Neplatná podmínka.")

    # --- počítání ---

    @staticmethod
    def to_number(value) -> int | float | None:
        """Číslo, nebo text, který je číslem („15“, „2,5“, „-3.0“); jinak None."""
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            return value
        if not isinstance(value, str):
            return None
        text = value.strip().replace(",", ".")
        try:
            return int(text)
        except ValueError:
            pass
        try:
            number = float(text)
        except ValueError:
            return None
        return number if number == number and number not in (float("inf"), float("-inf")) else None

    def number(self, value, op: str) -> int | float:
        number = self.to_number(value)
        if number is None:
            shown = "množina" if isinstance(value, list) else f"„{self.text(value)}“" \
                if not isinstance(value, Record) else "výsledek " + value.kind
            raise self.error(f"Počítání ({op}): {shown} není číslo. (Text se spojuje v uvozovkách: "
                             "\"{a}{b}\".)")
        return number

    def arithmetic(self, op: str, left, right) -> int | float:
        a, b = self.number(left, op), self.number(right, op)
        if op == "+":
            return a + b
        if op == "-":
            return a - b
        if op == "*":
            return a * b
        if b == 0:
            raise self.error("Dělení nulou.")
        result = a / b
        return int(result) if isinstance(a, int) and isinstance(b, int) and a % b == 0 else result

    def format(self, value, mask: str) -> str:
        number = self.to_number(value)
        if number is None:
            raise self.error(f"FORMAT: „{self.text(value)}“ není číslo.")
        try:
            return format_number(number, mask)
        except (ValueError, InvalidOperation) as e:
            raise self.error(str(e)) from None

    def compare(self, left, op: str, right) -> bool:
        if isinstance(left, list) or isinstance(right, list):
            raise self.error("Množinu nelze porovnávat – použijte COUNT(množina).")
        a, b = self.text(left), self.text(right)
        if op in ("==", "!=", "<", ">", "<=", ">="):
            x, y = self.to_number(left), self.to_number(right)
            if x is None or y is None:
                x, y = a.casefold(), b.casefold()
            return {"==": x == y, "!=": x != y, "<": x < y, ">": x > y,
                    "<=": x <= y, ">=": x >= y}[op]
        a, b = a.casefold(), b.casefold()
        if op == "CONTAINS":
            return b in a
        if op == "STARTS":
            return a.startswith(b)
        if op == "ENDS":
            return a.endswith(b)
        return fnmatch.fnmatchcase(a, b)  # LIKE

    # --- cesty ---

    def tab_by_name(self, name: str) -> TabInfo:
        key = name.strip().casefold()
        for tab in self.env.tabs or []:
            if key in (tab.name.casefold(), tab.server_name.casefold()) and tab.server_root:
                return tab
        names = ", ".join(t.name for t in self.env.tabs or [] if t.server_root)
        raise self.error(f"Záložka „{name}“ neexistuje (máte: {names or 'žádné'}).")

    def in_path(self, value) -> Path:
        if isinstance(value, ScriptFile):
            path = self.source_of(value)
            if path is None:
                raise self.error(f"Soubor {value.path} je smazaný.")
            return path
        if not isinstance(value, str):
            raise self.error("Očekává se cesta (text).")
        if not value.strip():
            raise self.error("Prázdná cesta.")
        path = Path(value)
        if not path.is_absolute():
            path = self.work / path
        return Path(os.path.normpath(path))

    def out_path(self, value, modify: bool = False) -> Path:
        """Cesta, kam smí skript zapisovat – mimo složky záložek SPM. V bloku BEFORE navíc do
        místní kopie projektu (jen vytvářet a přepisovat soubory; modify = mazání / přejmenování)."""
        if not isinstance(value, str):
            raise self.error("Očekává se cesta (text v uvozovkách).")
        path = self.in_path(value)
        local = self.local_dir()
        if self.phase == "before" and _inside(path, local):
            if _inside(path, local / SPM_DIR):
                raise self.error("Do servisní složky .spm projektu nelze zapisovat.")
            if modify:
                raise self.error("V bloku BEFORE lze v místní kopii projektu soubory jen vytvářet a "
                                 "přepisovat – ne mazat ani přejmenovávat.")
            return path
        for tab in self.env.tabs or []:
            for root in (tab.server_root, tab.local_root):
                if root and _inside(path, root):
                    raise self.error(f"Do složky záložky „{tab.name}“ ({root}) nesmí skript zapisovat "
                                     "přímo – do místní kopie projektu jen v bloku BEFORE, do projektů "
                                     "jiných záložek zrcadlete příkazem MIRROR.")
        return path

    def local_dir(self) -> Path:
        return self.env.pm.local_dir(self.env.project)

    @staticmethod
    def writable(path: Path) -> None:
        """Přepisovaný soubor místní kopie bývá jen pro čtení – zrušit atribut."""
        if path.is_file():
            set_readonly(path, False)

    def exists(self, path: Path) -> bool:
        if path.exists():
            return True
        key = _norm(path)
        return any(v == key or v.startswith(key + os.sep) for v in self.virtual)

    def created(self, path: Path) -> None:
        if self.dry:
            self.virtual.add(_norm(path))
            local = self.local_dir()
            if self.phase == "before" and _inside(path, local) and _norm(path) != _norm(local):
                self.created_local.add(Path(os.path.relpath(path, local)).as_posix())

    def source_of(self, f: ScriptFile) -> Path | None:
        """Odkud soubor číst: místní kopie (odevzdaná verze, rychlejší), jinak server."""
        if f.status == DELETED:
            return None
        pm, name = self.env.pm, self.env.project
        local = pm.local_dir(name).joinpath(*f.path.split("/"))
        if local.is_file():
            return local
        return pm.server_dir(name).joinpath(*f.path.split("/"))

    def mapped(self, files: list[ScriptFile], mapping: lang.Mapping) -> list[tuple[ScriptFile, str]]:
        base = self.interp(mapping.base.raw) if mapping.base is not None else ""
        return [(f, map_path(f.path, mapping, base)) for f in files]

    def sources(self, value, mapping: lang.Mapping, what: str) -> list[tuple[Path, str, ScriptFile | None]]:
        """(zdrojový soubor, cesta v cíli, soubor odevzdání) – z množiny / souboru / cesty."""
        if isinstance(value, (list, ScriptFile)):
            files = self.files_of(value, what)
            skipped = [f.path for f in files if f.status == DELETED]
            if skipped:
                self.log(f"{what}: přeskočeny smazané soubory ({len(skipped)}).")
            return [(self.source_of(f), rel, f)
                    for f, rel in self.mapped([f for f in files if f.status != DELETED], mapping)]
        path = self.in_path(value)
        if path.is_dir():
            return [(p, p.relative_to(path).as_posix(), None)
                    for p in sorted(path.rglob("*")) if p.is_file()]
        if self.exists(path):
            return [(path, path.name, None)]
        raise self.error(f"{what}: „{path}“ neexistuje.")

    # --- příkazy ---

    def cmd_SetCmd(self, cmd: lang.SetCmd) -> None:
        value = self.eval(cmd.expr)
        if cmd.format is not None:
            mask = self.interp(cmd.format.raw)
            value = self.format(value, mask)
            self._check_name(cmd.name)
            self.vars[FORMAT_KEY + cmd.name.lower()] = mask  # proměnná si formát pamatuje
        self.assign(cmd.name, value)

    def _check_name(self, name: str) -> None:
        key = name.lower()
        if key in self.builtins or key in (COMMIT_SET, "project_id"):
            raise self.error(f"„{name}“ je vestavěná proměnná – zvolte jiný název.")

    def assign(self, name: str, value) -> None:
        """Uloží hodnotu; proměnná s formátem (SET … FORMAT) dostane každé číslo v tom tvaru."""
        self._check_name(name)
        key = name.lower()
        mask = self.vars.get(FORMAT_KEY + key)
        if mask is not None and self.to_number(value) is not None:
            value = self.format(value, mask)
        self.vars[key] = value

    def cmd_FilesCmd(self, cmd: lang.FilesCmd) -> None:
        files = self.eval_set(cmd.expr)
        self.assign(cmd.name, files)
        self.log(f"FILES {cmd.name}: {_count(len(files), 'soubor', 'soubory', 'souborů')}")

    def cmd_ParseCmd(self, cmd: lang.ParseCmd) -> None:
        text = self.text(self.eval(cmd.expr))
        names, regex = [], []
        for kind, src in lang.split_template(cmd.pattern.raw, self.line):
            if kind == "text":
                regex.append("".join(".*?" if ch == "*" else "." if ch == "?" else re.escape(ch)
                                     for ch in src))
            else:
                if not re.fullmatch(r"[^\W\d]\w*", src):
                    raise self.error(f"PARSE: {{{src}}} – ve vzoru mohou být jen názvy proměnných.")
                names.append(src)
                regex.append(f"(?P<g{len(names) - 1}>.+?)")
        m = re.fullmatch("".join(regex), text, re.IGNORECASE | re.DOTALL)
        if m is None:
            raise self.error(f"PARSE: „{text}“ neodpovídá vzoru „{cmd.pattern.raw}“.")
        for i, name in enumerate(names):
            self.assign(name, m.group(f"g{i}"))
        self.log(f"PARSE: " + ", ".join(f"{n} = {m.group(f'g{i}')}" for i, n in enumerate(names)))

    def cmd_IfCmd(self, cmd: lang.IfCmd) -> None:
        self.block(cmd.then if self.cond(cmd.cond) else cmd.otherwise)

    def cmd_ForCmd(self, cmd: lang.ForCmd) -> None:
        files = self.files_of(self.eval(cmd.expr), "FOR")
        for f in files:
            self.assign(cmd.name, f)
            self.block(cmd.body)

    def cmd_CopyCmd(self, cmd: lang.CopyCmd) -> None:
        dest = self.out_path(self.eval(cmd.dest))
        value = self.eval(cmd.src)
        if isinstance(value, str) and self.in_path(value).is_dir() and cmd.rename is not None:
            raise self.error("COPY složky nelze s NAME – NAME přejmenovává jednotlivé soubory.")
        items = self.sources(value, cmd.mapping, "COPY")
        self.log(f"COPY: {_count(len(items), 'soubor', 'soubory', 'souborů')} → {dest}")
        for src, rel, f in items:
            if cmd.rename is not None:
                new_name = self.interp(cmd.rename.raw, {"file": f if f is not None else str(src)})
                if not new_name or "/" in new_name or "\\" in new_name:
                    raise self.error(f"COPY NAME: neplatný název „{new_name}“.")
                rel = posixpath.join(posixpath.dirname(rel), new_name)
            target = dest.joinpath(*rel.split("/"))
            self.guard(target)
            self.log(f"   {f.path if f else src} → {target}")
            if self.dry:
                self.created(target)
                continue
            copy_with_hash(src, target, writable=True)  # kopie nikdy jen pro čtení

    def cmd_ZipCmd(self, cmd: lang.ZipCmd) -> None:
        dest = self.out_path(self.eval(cmd.dest))
        items = self.sources(self.eval(cmd.src), cmd.mapping, "ZIP")
        seen: dict[str, Path] = {}
        for src, rel, _ in items:
            if rel.lower() in seen:
                raise self.error(f"ZIP: dva soubory by měly v archivu stejnou cestu {rel} – použijte "
                                 "jiné KEEP / BASE.")
            seen[rel.lower()] = src
        if not items:
            self.log(f"ZIP: nic k zabalení – archiv {dest} se nevytvořil.")
            return
        self.guard(dest)
        self.log(f"ZIP: {_count(len(items), 'soubor', 'soubory', 'souborů')} → {dest}")
        for src, rel, f in items:
            self.log(f"   {f.path if f else src} → {rel}")
        if self.dry:
            self.created(dest)
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".spmtmp")
        try:
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
                for src, rel, _ in items:
                    zf.write(src, rel)
            self.writable(dest)
            os.replace(tmp, dest)
        finally:
            tmp.unlink(missing_ok=True)

    def cmd_UnzipCmd(self, cmd: lang.UnzipCmd) -> None:
        src = self.in_path(self.eval(cmd.src))
        dest = self.out_path(self.eval(cmd.dest))
        if self.dry:
            if not self.exists(src):
                raise self.error(f"UNZIP: archiv „{src}“ neexistuje.")
            self.log(f"UNZIP: {src} → {dest}")
            self.created(dest)
            return
        if not src.is_file():
            raise self.error(f"UNZIP: archiv „{src}“ neexistuje.")
        try:
            with zipfile.ZipFile(src) as zf:
                names = [n for n in zf.namelist() if not n.endswith("/")]
                for n in names:
                    if not _inside(dest / n, dest):
                        raise self.error(f"UNZIP: archiv obsahuje nebezpečnou cestu {n}.")
                    self.guard(dest / n)
                    self.writable(dest / n)
                zf.extractall(dest)
        except zipfile.BadZipFile:
            raise self.error(f"UNZIP: „{src}“ není platný archiv ZIP.") from None
        self.log(f"UNZIP: {src} → {dest} ({_count(len(names), 'soubor', 'soubory', 'souborů')})")

    def cmd_PathCmd(self, cmd: lang.PathCmd) -> None:
        path = self.out_path(self.eval(cmd.path), modify=cmd.keyword == "DELETE")
        if cmd.keyword == "MKDIR":
            self.log(f"MKDIR: {path}")
            if self.dry:
                self.created(path)
            else:
                path.mkdir(parents=True, exist_ok=True)
            return
        if not self.exists(path):  # DELETE
            self.log(f"DELETE: {path} neexistuje.")
            return
        self.log(f"DELETE: {path}")
        if self.dry:
            return
        if _inside(path, self.work):
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        else:
            recycle.to_recycle_bin([path])

    def cmd_RenameCmd(self, cmd: lang.RenameCmd) -> None:
        path = self.out_path(self.eval(cmd.path), modify=True)
        new_name = self.text(self.eval(cmd.new_name))
        if not new_name or "/" in new_name or "\\" in new_name:
            raise self.error(f"RENAME: nový název „{new_name}“ nesmí obsahovat složky.")
        target = path.with_name(new_name)
        if not self.exists(path):
            raise self.error(f"RENAME: „{path}“ neexistuje.")
        if target.exists() and _norm(target) != _norm(path):
            raise self.error(f"RENAME: „{target}“ už existuje.")
        self.log(f"RENAME: {path} → {new_name}")
        if self.dry:
            self.created(target)
            return
        path.rename(target)

    def cmd_ReplaceNamesCmd(self, cmd: lang.ReplaceNamesCmd) -> None:
        folder = self.out_path(self.eval(cmd.folder), modify=True)
        old, new = self.text(self.eval(cmd.old)), self.text(self.eval(cmd.new))
        if not old:
            raise self.error("REPLACE NAMES: co nahradit nesmí být prázdné.")
        if not folder.is_dir():
            if self.dry and self.exists(folder):
                self.log(f"REPLACE NAMES v {folder}: „{old}“ → „{new}“")
                return
            raise self.error(f"REPLACE NAMES: složka „{folder}“ neexistuje.")
        pattern = re.compile(re.escape(old), re.IGNORECASE)
        renamed = 0
        # od nejhlubších, aby přejmenování složky nerozbilo cesty k jejímu obsahu
        for path in sorted(folder.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            name = pattern.sub(lambda _: new, path.name)
            if name == path.name:
                continue
            if not name or "/" in name or "\\" in name:
                raise self.error(f"REPLACE NAMES: neplatný nový název „{name}“.")
            target = path.with_name(name)
            if target.exists() and _norm(target) != _norm(path):
                raise self.error(f"REPLACE NAMES: „{target}“ už existuje.")
            self.log(f"   {path.name} → {name}")
            renamed += 1
            if not self.dry:
                path.rename(target)
        self.log(f"REPLACE NAMES v {folder}: „{old}“ → „{new}“, přejmenováno {renamed}")

    def cmd_WriteCmd(self, cmd: lang.WriteCmd) -> None:
        path = self.out_path(self.eval(cmd.path))
        self.guard(path)
        lines = [self.interp(line) for line in cmd.lines]
        text = cmd.newline.join(lines) + cmd.newline
        try:
            data = text.encode(cmd.encoding)
        except UnicodeEncodeError as e:
            raise self.error(f"{cmd.keyword}: znak „{text[e.start:e.end]}“ nelze zapsat v kódování "
                             f"{cmd.encoding}.") from None
        self.log(f"{cmd.keyword}: {path} ({_count(len(lines), 'řádek', 'řádky', 'řádků')})")
        if self.dry:
            self.created(path)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        self.writable(path)
        if cmd.append and path.exists():
            if cmd.encoding == "utf-8-sig" and path.stat().st_size:
                data = text.encode("utf-8")  # BOM jen na začátku souboru
            with open(path, "ab") as fh:
                fh.write(data)
        else:
            path.write_bytes(data)

    def cmd_MirrorCmd(self, cmd: lang.MirrorCmd) -> None:
        if self.phase == "before":  # např. v síťovém skriptu volaném z BEFORE
            raise self.error("MIRROR nelze použít v BEFORE – data ještě nejsou na serveru.")
        dest_text = self.eval(cmd.dest)
        if not isinstance(dest_text, str):
            raise self.error("MIRROR … TO \"{TAB:název}\" nebo \"{TAB:název}/projekt/složka\".")
        dest = self.in_path(dest_text)
        tab = next((t for t in self.env.tabs or [] if t.server_root and _inside(dest, t.server_root)),
                   None)
        if tab is None:
            raise self.error(f"MIRROR: cíl „{dest}“ neleží v žádné záložce – zrcadlit lze jen do "
                             "síťové složky záložky, např. \"{TAB:Vyroba}\".")
        rest = [] if _norm(dest) == _norm(tab.server_root).rstrip("\\/") else \
            Path(os.path.relpath(dest, tab.server_root)).parts
        project = rest[0] if rest else self.env.project
        sub = "/".join(rest[1:])
        source_tab = self.env.tab
        if source_tab is not None and tab.ws_id == source_tab.ws_id and project.lower() == \
                self.env.project.lower():
            raise self.error("MIRROR: projekt nelze zrcadlit sám do sebe.")
        files = self.files_of(self.eval(cmd.src), "MIRROR")
        items = []
        for f, rel in self.mapped(files, cmd.mapping):
            target_rel = posixpath.join(sub, rel) if sub else rel
            items.append(MirrorItem(f.file_id, f.status, target_rel, f.sha256, self.source_of(f), f.path))
        where = f"záložka {tab.name}, projekt {project}" + (f", složka {sub}" if sub else "")
        self.log(f"MIRROR: {_count(len(items), 'soubor', 'soubory', 'souborů')} → {where}")
        for item in items:
            self.log(f"   {item.path} ({item.status}) → {item.target_rel}")
        if self.dry or not items:
            return
        pm = self.env.pm
        server = pm.server_dir(self.env.project)
        source = MirrorSource(self.project_id(), str(server), pm.settings.server_id,
                              pm.settings.server_name, legacy_key=str(server))
        target_pm = self.env.pm_for_tab(tab)
        res = Mirror(source, target_pm, project, self.env.comment).run(items, self.progress)
        self.log(f"MIRROR hotovo: {format_counts(res)}")
        self.mirrored |= {(fid, res.target_key) for fid in res.mirrored}
        try:  # označení zrcadlených souborů ve zdroji (strom projektu)
            pm.record_mirror_targets(self.env.project, res, str(target_pm.server_dir(project)),
                                     target_pm.settings.server_id, target_pm.settings.server_name)
        except OSError as e:
            self.log(f"Označení zrcadlených souborů ve zdroji se nepodařilo: {e}")
        if res.conflicts:
            raise self.error("MIRROR neproběhl celý:\n" + "\n".join(res.conflicts))

    def cmd_ActivateCmd(self, cmd: lang.ActivateCmd) -> None:
        self.activate(self.eval(cmd.path))

    def cmd_RunCmd(self, cmd: lang.RunCmd) -> None:
        args: list[str] = []
        for node in cmd.args:
            value = self.eval(node)
            if isinstance(value, (list, ScriptFile)):  # soubory -> jejich úplné cesty
                args += [str(self.source_of(f)) for f in self.files_of(value, "RUN") if f.status != DELETED]
            else:
                args.append(self.text(value))
        if not args or not args[0].strip():
            raise self.error("RUN: chybí program.")
        cwd = self.in_path(self.eval(cmd.cwd)) if cmd.cwd is not None else self.work
        shown = " ".join(_quote(a) for a in args)
        if self.dry:
            self.log(f"RUN: {shown} – zkušební běh, program se nespustil")
            if cmd.result:
                self.assign(cmd.result, Record("RUN", code=0, output=""))
            return
        if not cwd.is_dir():
            raise self.error(f"RUN: pracovní složka „{cwd}“ neexistuje.")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            if cmd.nowait:
                subprocess.Popen(args, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
                self.log(f"RUN: {shown} – spuštěno (NOWAIT, nečeká se na dokončení)")
                return
            timeout = None if cmd.wait == 0 else (cmd.wait or RUN_TIMEOUT)
            # skripty v Pythonu ať vypisují v UTF-8 (jinak by výpis do roury byl v ANSI)
            env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
            proc = subprocess.run(args, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, timeout=timeout, creationflags=flags, env=env)
        except FileNotFoundError:
            raise self.error(f"RUN: program „{args[0]}“ nebyl nalezen.") from None
        except subprocess.TimeoutExpired:
            raise self.error(f"RUN: program neskončil do {timeout} s – byl ukončen (delší čekání: "
                             "WAIT sekundy, bez limitu WAIT 0).") from None
        output = _decode(proc.stdout or b"")
        self.log(f"RUN: {shown} – kód {proc.returncode}")
        for row in _tail(output):
            self.log(row)
        if cmd.result:
            self.assign(cmd.result, Record("RUN", code=proc.returncode, output=output.strip()))
        elif proc.returncode != 0:
            raise self.error(f"RUN: program skončil s kódem {proc.returncode}"
                             + (":\n" + "\n".join(r.strip() for r in _tail(output, 10)) if output.strip() else ".")
                             + "\n(Kód zkontrolujete sami přes -> proměnná a .code.)")

    def json_value(self, value):
        if isinstance(value, ScriptFile):
            return value.path
        if isinstance(value, list):
            return [f.path for f in value]
        if isinstance(value, Record):
            raise self.error("POST: výsledek RUN / POST nelze poslat celý – použijte jeho vlastnost.")
        return value

    def cmd_PostCmd(self, cmd: lang.PostCmd) -> None:
        url = self.text(self.eval(cmd.url)).strip()
        if not url.lower().startswith(("http://", "https://")):
            raise self.error(f"POST: adresa musí začínat http:// nebo https:// („{url}“).")
        headers = {"Content-Type": "application/json; charset=utf-8", "User-Agent": f"SPM/{__version__}"}
        for name, value in cmd.headers:
            headers[self.text(self.eval(name))] = self.text(self.eval(value))
        data = {key: self.json_value(self.eval(expr)) for key, expr in cmd.body}
        payload = json.dumps(data, ensure_ascii=False)
        self.log(f"POST {url}")
        self.log(f"   {payload}")
        if self.dry:
            self.log("   zkušební běh – neodesláno (odpověď nasimulována: 200)")
            if cmd.result:
                self.assign(cmd.result, Record("POST", status=200, body=""))
            return
        request = urllib.request.Request(url, data=payload.encode("utf-8"), method="POST", headers=headers)
        timeout = cmd.timeout or POST_TIMEOUT
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status, raw = response.status, response.read()
                charset = response.headers.get_content_charset() or "utf-8"
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read() or b""
            charset = e.headers.get_content_charset() if e.headers else None
            charset = charset or "utf-8"
        except urllib.error.URLError as e:
            raise self.error(f"POST: spojení s {url} se nepodařilo ({e.reason}).") from None
        except TimeoutError:
            raise self.error(f"POST: server {url} neodpověděl do {timeout} s.") from None
        body = raw.decode(charset, errors="replace")
        self.log(f"   odpověď {status}")
        for row in _tail(body, 10):
            self.log(row)
        if cmd.result:
            self.assign(cmd.result, Record("POST", status=status, body=body))
        elif status >= 400:
            raise self.error(f"POST: server vrátil chybu {status}: {body.strip()[:500]}"
                             "\n(Stav zkontrolujete sami přes -> proměnná a .status.)")

    def cmd_MessageCmd(self, cmd: lang.MessageCmd) -> None:
        text = self.text(self.eval(cmd.expr))
        if cmd.keyword == "STOP":
            raise self.error(f"STOP: {text}")
        self.report.log.append(self.mask(text))

    def cmd_ExitCmd(self, cmd: lang.ExitCmd) -> None:
        raise _Exit()

    def cmd_FormCmd(self, cmd: lang.FormCmd) -> None:
        title = self.text(self.eval(cmd.title))
        fields, choices = [], {}
        for f in cmd.fields:
            self.line = f.line
            item = {"kind": f.kind, "name": f.name, "label": self.text(self.eval(f.label)),
                    "default": None, "options": []}
            default = self.eval(f.default) if f.default is not None else None
            if f.kind == "CHOOSE":
                values = self.options([self.eval(o) for o in f.options])
                choices[f.name] = values
                item["options"] = [self.text(v) for v in values]
                text = self.text(default).casefold() if default is not None else ""
                item["default"] = next((i for i, o in enumerate(item["options"]) if o.casefold() == text), 0)
            elif f.kind == "ASK":
                item["default"] = self.truthy(default) if default is not None else False
            else:
                item["default"] = self.text(default) if default is not None else ""
            fields.append(item)
        self.line = cmd.line
        result = self.ui().form(self.dialog_title(), title, fields)
        if result is None:
            raise self.error(f"Formulář „{title}“ byl zrušen.")
        shown = []
        for f in cmd.fields:
            value = result[f.name]
            if f.kind == "CHOOSE":
                value = choices[f.name][value]
            elif f.kind == "ASK":
                value = 1 if value else 0
            self.assign(f.name, value)
            shown.append(f"{f.name} = {self.text(value)}")
        self.log(f"FORM „{title}“: " + ", ".join(shown))

    def call_path(self, cmd: lang.CallCmd) -> Path:
        name = self.text(self.eval(cmd.path)).strip()
        if not name:
            raise self.error("CALL: chybí název skriptu.")
        if not name.lower().endswith(store.SCRIPT_EXT):
            name += store.SCRIPT_EXT
        if cmd.where == "server":
            folder = store.network_dir(self.env.pm.server_root)
        elif cmd.where == "project":
            folder = store.network_dir(self.env.pm.server_dir(self.env.project))
        else:
            folder = store.scripts_dir()
        path = Path(name)
        return path if path.is_absolute() else folder / path

    def cmd_CallCmd(self, cmd: lang.CallCmd) -> None:
        path = self.call_path(cmd)
        if self.depth >= CALL_DEPTH_MAX:
            raise self.error(f"CALL: příliš hluboké volání (víc než {CALL_DEPTH_MAX}) – skripty se "
                             "nejspíš volají navzájem dokola.")
        try:
            text = path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            raise self.error(f"CALL: skript {path} neexistuje.") from None
        try:
            script = lang.parse_script(text, path.name)
        except ScriptSyntaxError as e:
            raise self.error(f"CALL {path}: chyba na řádku {e.line}: {e.message}") from None
        self.log(f"CALL {path}")
        saved = self.script, self.line
        self.depth += 1
        self.script = script.name
        try:
            self.block(script.body)
        except _Exit:
            self.log(f"EXIT – skript {script.name} ukončen.")
        finally:
            self.depth -= 1
            self.script, self.line = saved


def check_scripts(scripts: list[tuple[str, str]]) -> list[lang.Script]:
    """Rozbor skriptů (název, text); chyba zápisu -> ScriptSyntaxError."""
    return [lang.parse_script(text, name) for name, text in scripts]


class ScriptSession:
    """Skripty jednoho odevzdání ve dvou fázích:
    1. run_before() – bloky BEFORE všech skriptů, před nahráním (smí vytvářet soubory v místní
       kopii projektu; ty se pak přidají k odevzdání),
    2. run_after()  – zbytek skriptů po nahrání (soubory odevzdání včetně vytvořených).
    Každý skript má vlastní proměnné, které platí v obou fázích; pracovní složka je společná."""

    def __init__(self, env: ScriptEnv, scripts: list[tuple[str, str]], dry: bool = False,
                 progress: Progress | None = None):
        self.env, self.dry = env, dry
        self.report = RunReport(dry=dry)
        self.parsed: list[lang.Script] = []
        self.vars: dict[str, dict] = {}
        self.exited: set[str] = set()   # skripty ukončené EXIT už v BEFORE
        self.closed = False
        try:
            self.parsed = check_scripts(scripts)
        except ScriptSyntaxError as e:
            self.report.ok, self.report.error = False, str(e)
        self.work = Path(tempfile.gettempdir()) / "spm_skript_zkouska" if dry else \
            Path(tempfile.mkdtemp(prefix="spm_skript_"))
        self.interp = Interpreter(env, self.work, self.report, dry, progress)

    @property
    def ok(self) -> bool:
        return self.report.ok

    @property
    def has_before(self) -> bool:
        return any(s.before for s in self.parsed)

    def _run(self, phase: str, part) -> bool:
        interp = self.interp
        interp.phase = phase
        try:
            for script in self.parsed:
                body = part(script)
                if not body or script.name in self.exited:
                    continue
                self.report.log.append(f"== {script.name}" + (" – BEFORE ==" if phase == "before" else " =="))
                interp.script = script.name
                interp.vars = self.vars.setdefault(script.name, {})
                try:
                    interp.block(body)
                except _Exit:
                    interp.log("EXIT – skript ukončen.")
                    self.exited.add(script.name)
        except ScriptError as e:
            self.report.ok, self.report.error = False, str(e)
        except Exception as e:  # noqa: BLE001 – chyba skriptu nesmí shodit aplikaci
            self.report.ok, self.report.error = False, f"{interp.script}, řádek {interp.line}: {e}"
        self.report.error = interp.mask(self.report.error)
        return self.report.ok

    def run_before(self) -> bool:
        """Fáze před nahráním. False = chyba – nic se nenahraje (zavolejte abort())."""
        if not self.ok:
            return False
        if self.has_before:
            self.report.log.append(f"Projekt {self.env.project}, před odevzdáním – souborů: "
                                   f"{len(self.env.files)}")
        return self._run("before", lambda script: script.before)

    def run_after(self, files: list[ScriptFile] | None = None, commit_id: int | None = None,
                  action: str | None = None) -> bool:
        """Fáze po nahrání; files = soubory odevzdání (včetně vytvořených v BEFORE)."""
        env = self.env
        if action is not None:  # odevzdání mohlo skončit automatickou deaktivací
            env.action = action
            self.interp.builtins["action"] = action
        if files is not None:
            env.files = list(files)
        elif self.dry and self.interp.created_local:  # zkušební běh: co by BEFORE vytvořil
            known = {f.path.lower() for f in env.files}
            env.files = env.files + [ScriptFile(p, ADDED) for p in sorted(self.interp.created_local)
                                     if p.lower() not in known]
        if commit_id is not None:
            env.commit_id = commit_id
            self.interp.builtins["commit_id"] = commit_id
        self.report.log.append(f"Projekt {env.project}, souborů v odevzdání: {len(env.files)}")
        if self.ok:
            self._run("after", lambda script: script.after)
        self.close()
        return self.ok

    def abort(self) -> None:
        """Konec bez fáze po nahrání (BEFORE selhal – nic se nenahrálo)."""
        if not self.closed and not self.dry:
            shutil.rmtree(self.work, ignore_errors=True)
        self.closed = True

    def close(self) -> None:
        if self.closed:
            return
        self.abort()
        if not self.dry:
            clear_stale_marks(self.env, self.report, self.interp.mirrored)

    # --- proměnné pro opakovaný běh (nedokončený skript) ---

    def export_vars(self) -> dict:
        def dump(value):
            if isinstance(value, ScriptFile):
                return {"file": value.to_dict()}
            if isinstance(value, list):
                return {"files": [f.to_dict() for f in value]}
            if isinstance(value, Record):
                return value.to_dict()
            return value
        return {name: {k: dump(v) for k, v in values.items()} for name, values in self.vars.items()}

    def import_vars(self, data: dict) -> None:
        def load(value):
            if isinstance(value, dict) and "file" in value:
                return ScriptFile.from_dict(value["file"])
            if isinstance(value, dict) and "files" in value:
                return [ScriptFile.from_dict(d) for d in value["files"]]
            if isinstance(value, dict) and "record" in value:
                return Record(value["record"], **value.get("fields", {}))
            return value
        self.vars = {name: {k: load(v) for k, v in values.items()}
                     for name, values in (data or {}).items() if isinstance(values, dict)}


def run_scripts(env: ScriptEnv, scripts: list[tuple[str, str]], dry: bool = False,
                progress: Progress | None = None) -> RunReport:
    """Obě fáze najednou nad stejnými soubory (zkušební běh, testy)."""
    session = ScriptSession(env, scripts, dry, progress)
    if session.run_before():
        session.run_after()
    else:
        session.close()
    return session.report


def target_resolver(env: ScriptEnv) -> Callable[[object], object | None]:
    """Cíl zrcadla (db.MirrorTarget) -> ProjectManager záložky s jeho síťovou složkou
    (podle identity síťové složky, jinak podle cesty); None = cíl není mezi záložkami."""
    def resolve(target):
        if env.pm_for_tab is None:
            return None
        tabs = [t for t in env.tabs or [] if t.server_root]
        if target.server_id:
            for tab in tabs:
                try:
                    pm = env.pm_for_tab(tab)
                except Exception:  # noqa: BLE001 – nedostupná záložka
                    continue
                if pm is not None and pm.settings.server_id == target.server_id:
                    return pm
        parent = Path(target.label).parent
        tab = next((t for t in tabs if _norm(t.server_root).rstrip("\\/") == _norm(parent)), None)
        return env.pm_for_tab(tab) if tab is not None else None
    return resolve


def clear_stale_marks(env: ScriptEnv, report: RunReport, mirrored: set[tuple[str, str]]) -> None:
    """Odebere označení „zrcadleno do“ u změněných souborů, které se v tomto běhu nezrcadlily,
    a jejich kopie v cíli označí jako neaktuální."""
    warnings: list[str] = []
    try:
        removed = env.pm.clear_stale_mirror_marks(env.project, env.files, mirrored,
                                                  target_resolver(env), warnings)
    except OSError as e:
        report.log.append(f"Označení zrcadlených souborů se nepodařilo aktualizovat: {e}")
        return
    if removed:
        report.log.append(f"Zrcadlo nebylo aktualizováno – u {removed} změněných souborů odebráno "
                          "označení „zrcadleno do“, kopie v cíli jsou označené jako neaktuální.")
    report.log.extend(warnings)
