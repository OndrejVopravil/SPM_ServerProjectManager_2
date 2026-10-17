"""Data, se kterými skript pracuje: soubory odevzdání, záložky a výsledek běhu."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

# stav souboru v odevzdání (vlastnost .status)
ADDED, MODIFIED, RENAMED, DELETED, UNCHANGED = "added", "modified", "renamed", "deleted", "unchanged"

# kdy se skript spouští (proměnná {ACTION})
SUBMIT, DEACTIVATE, PUBLISH, TEST = "submit", "deactivate", "publish", "test"


@dataclass(frozen=True)
class ScriptFile:
    """Soubor odevzdání. path = cesta v projektu (u smazaného poslední cesta)."""
    path: str
    status: str
    file_id: str = ""
    old_path: str | None = None   # u přejmenovaného původní cesta
    sha256: str | None = None
    size: int | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}

    @classmethod
    def from_dict(cls, data: dict) -> "ScriptFile":
        return cls(**{k: data[k] for k in ("path", "status", "file_id", "old_path", "sha256", "size")
                      if k in data})


@dataclass
class TabInfo:
    """Záložka aplikace, kterou skript může použít (cíl MIRROR, {TAB:název})."""
    ws_id: str
    name: str            # název záložky u uživatele
    server_name: str     # název síťové složky ze spm_config.json (stejný u všech)
    server_root: str
    local_root: str


class ScriptUI:
    """Dialogy skriptu (ASK, INPUT, CHOOSE, FORM, výběr v ONE). V aplikaci gui.script_ui;
    bez okna (ui = None) skript s dialogem skončí chybou. Zrušení dialogu = None."""

    def ask(self, title: str, text: str) -> bool:
        raise NotImplementedError

    def input(self, title: str, prompt: str, default: str) -> str | None:
        raise NotImplementedError

    def choose(self, title: str, prompt: str, options: list[str], default: int) -> int | None:
        raise NotImplementedError

    def form(self, title: str, text: str, fields: list[dict]) -> dict | None:
        """fields: {"kind": INPUT/CHOOSE/ASK, "name", "label", "default", "options"};
        vrací {name: text / index volby / bool}."""
        raise NotImplementedError


@dataclass
class RunReport:
    ok: bool = True
    log: list[str] = field(default_factory=list)
    error: str = ""
    dry: bool = False

    @property
    def text(self) -> str:
        lines = list(self.log)
        if self.error:
            lines += ["", "CHYBA: " + self.error]
        return "\n".join(lines)


def files_from_dicts(items: list[dict]) -> list[ScriptFile]:
    return [ScriptFile.from_dict(d) for d in items if isinstance(d, dict) and "path" in d]
