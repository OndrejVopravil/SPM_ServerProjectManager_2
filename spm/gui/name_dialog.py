"""Zadání názvu nové složky (projektu) s našeptáváním podle názvů sousedních složek – stejná
úroveň má obvykle stejné pojmenování (např. E01, E02 … – napíšu „E“ a nabídne se E01, E02)."""

from __future__ import annotations

from typing import Iterable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCompleter, QInputDialog, QLineEdit, QWidget


def ask_text(parent: QWidget | None, title: str, label: str, text: str = "",
             suggestions: Iterable[str] = ()) -> tuple[str, bool]:
    """Jako QInputDialog.getText; při psaní nabízí suggestions (část názvu kdekoliv, bez
    ohledu na velikost písmen). Vrací (text, potvrzeno)."""
    dlg = QInputDialog(parent)
    dlg.setWindowTitle(title)
    dlg.setLabelText(label)
    dlg.setInputMode(QInputDialog.InputMode.TextInput)
    dlg.setTextValue(text)
    names = sorted({s for s in suggestions if s}, key=str.lower)
    edit = dlg.findChild(QLineEdit)
    if names and edit is not None:
        completer = QCompleter(names, dlg)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setMaxVisibleItems(12)
        edit.setCompleter(completer)
        edit.setToolTip("Při psaní se nabízejí názvy sousedních složek – podle nich můžete "
                        "pojmenovat novou (šipkami vyberete, Enter převezme).")
    ok =dlg.exec() == QInputDialog.DialogCode.Accepted
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
