"""Přehled aktivity nad časovou osou: sloupeček za každý den (u dlouhé historie týden / měsíc),
složený z barev uživatelů podle počtu jejich záznamů. Bublina ukáže, kdo co dělal; klepnutí
skočí na dané období v časové ose."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from PySide6.QtCore import QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPalette
from PySide6.QtWidgets import QToolTip, QWidget

from .timeline_style import user_color

MIN_BAR = 4          # nejužší sloupeček (px) – jinak se seskupí po týdnech / měsících
GAP = 1
MONTHS = ["led", "úno", "bře", "dub", "kvě", "čvn", "čvc", "srp", "zář", "říj", "lis", "pro"]


@dataclass
class Bucket:
    start: date
    end: date                                       # včetně
    users: Counter = field(default_factory=Counter)  # uživatel -> počet záznamů

    @property
    def total(self) -> int:
        return sum(self.users.values())


def _day(iso: str) -> date | None:
    try:
        return datetime.fromisoformat(iso).date()
    except (TypeError, ValueError):
        return None


def buckets(records: list[tuple[str, str]], width: int) -> tuple[str, list[Bucket]]:
    """Záznamy (čas ISO, uživatel) do období podle šířky: den, týden nebo měsíc.
    Vrací (druh období, období od nejstaršího)."""
    days = [(d, u) for iso, u in records if (d := _day(iso)) is not None]
    if not days:
        return "day", []
    first, last = min(d for d, _ in days), max(max(d for d, _ in days), date.today())
    span = (last - first).days + 1
    slots = max(1, width // (MIN_BAR + GAP))
    if span <= slots:
        unit = "day"
        start = first
    elif span // 7 + 1 <= slots:
        unit = "week"
        start = first - timedelta(days=first.weekday())
    else:
        unit = "month"
        start = first.replace(day=1)
    result: list[Bucket] = []
    cur = start
    while cur <= last:
        if unit == "day":
            end = cur
        elif unit == "week":
            end = cur + timedelta(days=6)
        else:
            nxt = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
            end = nxt - timedelta(days=1)
        result.append(Bucket(cur, end))
        cur = end + timedelta(days=1)
    for d, u in days:
        for b in result:  # období je málo (podle šířky), stačí lineárně
            if b.start <= d <= b.end:
                b.users[u] += 1
                break
    return unit, result


def bucket_label(unit: str, b: Bucket) -> str:
    if unit == "day":
        return f"{b.start.day}. {b.start.month}. {b.start.year}"
    if unit == "week":
        return f"týden {b.start.day}. {b.start.month}. – {b.end.day}. {b.end.month}. {b.end.year}"
    return f"{MONTHS[b.start.month - 1]} {b.start.year}"


class ActivityStrip(QWidget):
    """Pruh aktivity; signál jump(start, end) při klepnutí na sloupeček s aktivitou."""

    jump = Signal(object, object)  # date od, date do (včetně)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.records: list[tuple[str, str]] = []
        self.unit, self.items = "day", []
        self.setMouseTracking(True)
        self.setMinimumHeight(46)
        self.setToolTip("")

    def sizeHint(self) -> QSize:  # noqa: N802 – Qt
        return QSize(400, 46)

    def set_records(self, records: list[tuple[str, str]]) -> None:
        self.records = list(records)
        self._rebuild()

    def _rebuild(self) -> None:
        self.unit, self.items = buckets(self.records, max(1, self.width() - 8))
        self.update()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._rebuild()

    def _bar_rects(self) -> list[QRect]:
        n = len(self.items)
        if not n:
            return []
        area = self.rect().adjusted(4, 4, -4, -16)
        w = max(1.0, area.width() / n)
        return [QRect(int(area.left() + i * w), area.top(), max(1, int(w) - GAP), area.height())
                for i in range(n)]

    def bucket_at(self, pos: QPoint) -> Bucket | None:
        for rect, b in zip(self._bar_rects(), self.items):
            if rect.left() <= pos.x() <= rect.right() + GAP:
                return b
        return None

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        pal = self.palette()
        p.fillRect(self.rect(), pal.color(QPalette.ColorRole.Base))
        rects = self._bar_rects()
        peak = max((b.total for b in self.items), default=0) or 1
        base_line = QColor(pal.color(QPalette.ColorRole.Mid))
        for rect, b in zip(rects, self.items):
            p.fillRect(QRect(rect.left(), rect.bottom(), rect.width(), 1), base_line)
            if not b.total:
                continue
            height = max(3, int(rect.height() * b.total / peak))
            y = rect.bottom()
            for user, count in sorted(b.users.items(), key=lambda x: x[0].lower()):
                h = max(1, round(height * count / b.total))
                p.fillRect(QRect(rect.left(), y - h + 1, rect.width(), h), user_color(user))
                y -= h
        # popisky: první, poslední a začátky měsíců (u denních / týdenních období)
        p.setPen(pal.color(QPalette.ColorRole.PlaceholderText))
        font = p.font()
        font.setPointSizeF(max(6.5, font.pointSizeF() * 0.8))
        p.setFont(font)
        last_x = -100
        for rect, b in zip(rects, self.items):
            first_of_month = b.start.day == 1 or (self.unit == "week" and b.start.day <= 7)
            if self.unit == "month":
                first_of_month = b.start.month == 1
            if (first_of_month or b is self.items[0]) and rect.left() - last_x > 60:
                text = (f"{MONTHS[b.start.month - 1]} {b.start.year}" if self.unit != "month"
                        else str(b.start.year))
                p.drawText(rect.left(), self.height() - 3, text)
                last_x = rect.left()
        p.end()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        b = self.bucket_at(event.position().toPoint())
        if b is None:
            QToolTip.hideText()
            return
        lines = [bucket_label(self.unit, b)]
        if b.total:
            lines += [f"{u}: {n}×" for u, n in sorted(b.users.items(), key=lambda x: -x[1])]
            lines.append("(klepnutím zobrazíte v časové ose)")
        else:
            lines.append("bez záznamů")
        QToolTip.showText(event.globalPosition().toPoint(), "\n".join(lines), self)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        b = self.bucket_at(event.position().toPoint())
        if b is not None and b.total:
            self.jump.emit(b.start, b.end)
