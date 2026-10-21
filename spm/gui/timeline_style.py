"""Společný vzhled historie (časová osa, historie souboru): barva uživatele, kolečko dostupnosti
obnovy, barvy a popisky změn a akcí."""

from __future__ import annotations

import hashlib
from datetime import date, datetime

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap

from .. import db as dbm

# kolečko na ose: lze obnovit celé / částečně / nelze
COLOR_FULL = QColor("#2e8b57")
COLOR_PARTIAL = QColor("#e08a00")
COLOR_NONE = QColor("#9e9e9e")

# barvy uživatelů – stálé podle loginu (stejný člověk = stejná barva)
USER_COLORS = ["#2f6fb5", "#8e44ad", "#16a085", "#c0392b", "#d35400", "#27ae60", "#7f8c8d",
               "#b7950b", "#1f618d", "#a93226"]

# změny souborů: (značka, barva)
CHANGE_STYLE = {
    dbm.ADDED: ("+", "#2e8b57"), dbm.PUBLISHED: ("+", "#2e8b57"), dbm.IMPORTED: ("+", "#7f8c8d"),
    dbm.MODIFIED: ("Δ", "#2f6fb5"), dbm.RENAMED: ("→", "#8e44ad"), dbm.RENAMED_MODIFIED: ("→", "#8e44ad"),
    dbm.DELETED: ("−", "#c0392b"), dbm.EXTERNAL_MODIFIED: ("!", "#d35400"),
    dbm.EXTERNAL_DELETED: ("!", "#d35400"), dbm.EXTERNAL_RENAMED: ("!", "#d35400"),
    dbm.RESTORED: ("↺", "#16a085"),
}
# souhrn změn v kartě: druh -> (popisek jednotné / množné číslo)
CHANGE_GROUPS = [
    ({dbm.ADDED, dbm.PUBLISHED}, "nový", "nové"),
    ({dbm.MODIFIED}, "upravený", "upravené"),
    ({dbm.RENAMED, dbm.RENAMED_MODIFIED}, "přejmenovaný", "přejmenované"),
    ({dbm.DELETED}, "smazaný", "smazané"),
    ({dbm.EXTERNAL_MODIFIED, dbm.EXTERNAL_DELETED, dbm.EXTERNAL_RENAMED}, "mimo aplikaci", "mimo aplikaci"),
    ({dbm.RESTORED}, "obnovený", "obnovené"),
]
# akce (záznamy) – barva štítku; změny mimo aplikaci výrazně (varování)
KIND_COLORS = {
    dbm.UPLOAD: "#2f6fb5", dbm.PUBLISH: "#2e8b57", dbm.MIRROR: "#16a085", dbm.SYNC: "#d35400",
    dbm.CANCELLED: "#7f8c8d", dbm.FORCE_UNLOCK: "#c0392b", dbm.PROJECT_RENAMED: "#8e44ad",
    dbm.PROJECT_COMMENT: "#7f8c8d",
}


def user_color(login: str) -> QColor:
    digest = hashlib.md5(login.lower().encode("utf-8")).digest()
    return QColor(USER_COLORS[digest[0] % len(USER_COLORS)])


def initials(name: str) -> str:
    parts = [p for p in name.replace(".", " ").replace("_", " ").split() if p]
    if len(parts) >= 2:
        return (parts[0][0] + parts[-1][0]).upper()
    return name[:2].upper() if name else "?"


def dot_icon(color: QColor, size: int = 12, hollow: bool = False, half: bool = False) -> QIcon:
    """Kolečko dostupnosti: plné (celé), napůl (částečně), prázdné (nelze)."""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    rect = QRectF(1.5, 1.5, size - 3, size - 3)
    p.setPen(color)
    p.setBrush(Qt.BrushStyle.NoBrush if hollow or half else color)
    p.drawEllipse(rect)
    if half:
        p.setBrush(color)
        p.drawPie(rect, 90 * 16, 180 * 16)
    p.end()
    return QIcon(pix)


def fmt_size(size: int | None) -> str:
    if size is None:
        return ""
    for unit in ("B", "kB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}".replace(".", ",")
        size /= 1024
    return ""


def day_label(iso: str) -> str:
    """Den pro seskupení: Dnes / Včera / 18. 10. 2026."""
    try:
        d = datetime.fromisoformat(iso).date()
    except ValueError:
        return iso[:10]
    today = date.today()
    if d == today:
        return "Dnes"
    if (today - d).days == 1:
        return "Včera"
    return f"{d.day}. {d.month}. {d.year}"


def time_label(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%H:%M")
    except ValueError:
        return iso[11:16]


def date_time(iso: str) -> str:
    try:
        t = datetime.fromisoformat(iso)
    except ValueError:
        return iso.replace("T", " ")
    return f"{t.day}. {t.month}. {t.year} {t:%H:%M}"
