"""Společné pomocné funkce poznámek: formát času, Markdown, barvy karty „pro mě“, export.

Samotné zobrazení poznámek je v panelu poznámek (notes_panel.py).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from PySide6.QtGui import QPalette, QTextDocument
from PySide6.QtWidgets import QWidget

from ..notes import Note


def fmt_time(iso: str | None) -> str:
    try:
        t = datetime.fromisoformat(iso or "")
    except ValueError:
        return iso or ""
    return f"{t.day}. {t.month}. {t.year} {t:%H:%M}"


def recipient_from_text(text: str, known: dict[str, str]) -> tuple[str, str]:
    """Adresát z textu pole „Pro“: známé jméno -> (login, jméno); jinak text = login."""
    text = text.strip()
    if not text:
        return "", ""
    if text in known:
        return known[text], text
    if text.endswith(")") and " (" in text:
        return text.rsplit(" (", 1)[1][:-1].strip(), ""
    return text, ""


def notes_markdown(notes: list[Note], me: str, seen: int, show_resolved: bool,
                   links: bool = True) -> str:
    """Poznámky jako jeden Markdown dokument (export do .md)."""
    parts = []
    for n in notes:
        if n.resolved and not show_resolved:
            continue
        flags = []
        if n.is_for(me):
            flags.append("**pro vás**")
        elif n.recipient:
            flags.append(f"pro {n.recipient_name or n.recipient}")
        if n.edited_at:
            flags.append(f"upraveno {fmt_time(n.edited_at)}")
        if n.resolved:
            flags.append(f"vyřízeno ({n.resolved_by}, {fmt_time(n.resolved_at)})")
        head = f"### {n.author} · {fmt_time(n.created_at)}"
        lines = [head + (" · " + " · ".join(flags) if flags else "")]
        if n.targets:
            lines.append("Týká se: " + ", ".join(f"`{t}`" for t in n.targets))
        lines.append("")
        lines.append(n.text)
        for r in n.replies:
            edited = f" · upraveno {fmt_time(r.edited_at)}" if r.edited_at else ""
            lines.append("")
            lines.append(f"> **{r.author}** · {fmt_time(r.created_at)}{edited}  ")
            lines += [f"> {line}" for line in r.text.splitlines()]
        parts.append("\n".join(lines))
    return "\n\n---\n\n".join(parts)


@dataclass(frozen=True)
class ForMeColors:
    """Barvy karty poznámky „pro mě“."""
    background: str
    border: str
    label: str      # nápis „PRO VÁS“
    text: str


# světlý režim: tlumená teplá béžová, tmavý text
FOR_ME_LIGHT = ForMeColors(background="#faf6ea", border="#d8c9a0", label="#8a6d1f", text="#000000")
# tmavý režim: šedožlutá (tmavá olivově šedá), bílý text
FOR_ME_DARK = ForMeColors(background="#4a4636", border="#7d7350", label="#e3c86f", text="#ffffff")
FOR_ME_BACKGROUND = FOR_ME_LIGHT.background  # (pro testy a zpětnou kompatibilitu)


def is_dark(widget: QWidget) -> bool:
    """Tmavý režim – podle jasu pozadí (funguje se systémovým tmavým motivem i vlastní paletou)."""
    return widget.palette().color(QPalette.ColorRole.Base).lightness() < 128


REPLY_BORDER = "#9e9e9e"        # svislý pruh vlevo od odpovědi


def markdown_to_html(text: str) -> str:
    """Text poznámky (Markdown) jako HTML fragment (obsah <body>)."""
    doc = QTextDocument()
    doc.setMarkdown(text)
    html = doc.toHtml()
    match = re.search(r"<body[^>]*>(.*)</body>", html, re.S)
    return match.group(1) if match else html


@dataclass(frozen=True)
class TextSizes:
    """Velikosti písma v okně poznámek (body): text poznámky největší, ostatní menší."""
    text: float = 11.0   # text poznámky a doplnění
    meta: float = 8.0    # autor, datum, příznaky, „Týká se“, odkazy (Upravit, Doplnit…)

    @classmethod
    def from_base(cls, base: float) -> "TextSizes":
        base = base if base > 0 else 9.0
        return cls(text=round(base * 1.2, 1), meta=round(base * 0.9, 1))
