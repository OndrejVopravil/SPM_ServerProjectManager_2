"""Zadání názvu (nová složka, přejmenování) s našeptáváním podle názvů na stejné úrovni –
stejná úroveň má obvykle stejné pojmenování (např. E01, E02 … – napíšu „E“ a nabídne se E01,
E02). Při přejmenování souboru se zachová koncovka, pokud ji uživatel nenahradí jinou."""

from __future__ import annotations

import os
from typing import Iterable

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QCompleter, QInputDialog, QLineEdit, QWidget


FOLDER_HINT = ("Při psaní se nabízejí názvy sousedních složek – podle nich můžete "
               "pojmenovat novou (šipkami vyberete, Enter převezme).")
RENAME_HINT = ("Při psaní se nabízejí názvy souborů a složek na stejné úrovni (šipkami "
               "vyberete, Enter převezme).")


def split_ext(name: str) -> tuple[str, str]:
    """('výkres', '.pdf') – koncovka jen u názvu s textem před tečkou (ne '.gitignore')."""
    return os.path.splitext(name)


def keep_extension(old: str, new: str) -> str:
    """Nový název souboru s koncovkou starého, když v novém koncovka chybí (smazaná omylem);
    jinou napsanou koncovku nechá být."""
    ext = split_ext(old)[1]
    if not ext or split_ext(new)[1] not in ("", "."):  # samotná tečka na konci = bez koncovky
        return new
    return new.rstrip(".") + ext


def select_stem(edit: QLineEdit, is_file: bool) -> None:
    """Jako Průzkumník: u souboru označí jen název bez koncovky."""
    stem = split_ext(edit.text())[0] if is_file else edit.text()
    edit.setSelection(0, len(stem))


def set_suggestions(edit: QLineEdit, suggestions: Iterable[str], hint: str = RENAME_HINT) -> None:
    """Našeptávání do pole: část názvu kdekoliv, bez ohledu na velikost písmen."""
    names = sorted({s for s in suggestions if s}, key=str.lower)
    if not names:
        return
    completer = QCompleter(names, edit)
    completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
    completer.setFilterMode(Qt.MatchFlag.MatchContains)
    completer.setMaxVisibleItems(12)
    edit.setCompleter(completer)
    edit.setToolTip(hint)


def ask_text(parent: QWidget | None, title: str, label: str, text: str = "",
             suggestions: Iterable[str] = (), hint: str = FOLDER_HINT,
             file_name: bool = False) -> tuple[str, bool]:
    """Jako QInputDialog.getText; při psaní nabízí suggestions (část názvu kdekoliv, bez
    ohledu na velikost písmen). file_name = přejmenování souboru: označí se jen název bez
    koncovky. Vrací (text, potvrzeno)."""
    dlg = QInputDialog(parent)
    dlg.setWindowTitle(title)
    dlg.setLabelText(label)
    dlg.setInputMode(QInputDialog.InputMode.TextInput)
    dlg.setTextValue(text)
    edit = dlg.findChild(QLineEdit)
    if edit is not None:
        set_suggestions(edit, suggestions, hint)
        if file_name:
            # až po zobrazení – QInputDialog při otevření označí celý text
            QTimer.singleShot(0, lambda: select_stem(edit, True))
    ok = dlg.exec() == QInputDialog.DialogCode.Accepted
    value = dlg.textValue()
    dlg.deleteLater()
    return value, ok


def child_folders(paths: Iterable[str], parent: str) -> list[str]:
    """Názvy podsložek složky parent ('' = kořen, jinak 'a/b/') z cest projektu (soubory
    'a/b/c.txt' i složky 'a/b/' nebo 'a/b')."""
    prefix = parent.strip("/").lower()
    prefix = prefix + "/" if prefix else ""
    result: dict[str, str] = {}
    for path in paths:
        p = path.strip("/")
        if not p.lower().startswith(prefix):
            continue
        rest = p[len(prefix):]
        if not rest:
            continue
        head, sep, _ = rest.partition("/")
        if sep or path.endswith("/"):  # je to složka (něco pod ní, nebo končí '/')
            result.setdefault(head.lower(), head)
    return sorted(result.values(), key=str.lower)


def child_names(paths: Iterable[str], parent: str) -> list[str]:
    """Názvy souborů i podsložek ve složce parent ('' = kořen, jinak 'a/b/') z cest projektu –
    nabízejí se při přejmenování."""
    prefix = parent.strip("/").lower()
    prefix = prefix + "/" if prefix else ""
    result: dict[str, str] = {}
    for path in paths:
        p = path.strip("/")
        if not p.lower().startswith(prefix) or len(p) == len(prefix):
            continue
        head = p[len(prefix):].partition("/")[0]
        result.setdefault(head.lower(), head)
    return sorted(result.values(), key=str.lower)
