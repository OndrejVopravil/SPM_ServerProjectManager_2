"""Časová osa projektu: historie a obnova ze záloh v jednom okně.

Každý záznam historie (odevzdání, zrcadlení, publikování, změny mimo aplikaci…) je karta na
svislé ose, seskupená po dnech. Kolečko na ose říká, zda lze projekt k tomuto stavu obnovit:
● celé, ◐ jen částečně (některé zálohy chybí), ○ vůbec. Čára ukazuje hranici, za kterou už
celý projekt obnovit nejde. Vpravo podrobnosti vybraného záznamu: změněné soubory (dvojklik
= historie souboru), obnovení projektu k tomuto stavu, záznam skriptů.

Filtry: hledání (uživatel, komentář, soubory – bez diakritiky), druh akce, uživatel, jen moje,
k souborům vybraným ve stromu.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QKeySequence, QPainter, QPalette, QPen, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStyle,
    QStyledItemDelegate,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import db as dbm
from ..db import ProjectDB
from ..restore import RestoreCatalog, RestorePoint
from .activity_strip import ActivityStrip
from .check_tree import in_scope
from .search import contains, normalize
from .timeline_style import (
    CHANGE_GROUPS,
    CHANGE_STYLE,
    COLOR_FULL,
    COLOR_NONE,
    COLOR_PARTIAL,
    KIND_COLORS,
    date_time,
    day_label,
    initials,
    time_label,
    user_color,
)

ROLE = Qt.ItemDataRole.UserRole
ROW_DAY, ROW_COMMIT, ROW_HORIZON = "day", "commit", "horizon"
CARD_H, DAY_H, HORIZON_H = 66, 28, 30
LINE_X, DOT_R, AVATAR = 16, 6, 28

KIND_FILTERS = [
    ("Všechny akce", None),
    ("Odevzdání a deaktivace", {dbm.UPLOAD}),
    ("Zrcadlení", {dbm.MIRROR}),
    ("Publikování", {dbm.PUBLISH}),
    ("Změny mimo aplikaci", {dbm.SYNC}),
    ("Ostatní", "other"),
]
MAIN_KINDS = {dbm.UPLOAD, dbm.MIRROR, dbm.PUBLISH, dbm.SYNC}


def _change_text(ch) -> str:
    return " ".join([dbm.CHANGE_LABELS.get(ch["change_type"], ch["change_type"]),
                     ch["old_path"] or "", ch["new_path"] or "", ch["backup_path"] or ""])


def _comment(c) -> str:
    """Komentář záznamu pro zobrazení (u změny komentáře projektu jeho nový text)."""
    text = c["comment"] or ""
    if c["kind"] == dbm.PROJECT_COMMENT:
        return text.replace("\n", " ↵ ") or "(komentář smazán)"
    return text


class CardDelegate(QStyledItemDelegate):
    """Kreslí řádky časové osy: den, kartu záznamu, hranici obnovitelnosti."""

    def sizeHint(self, option, index) -> QSize:  # noqa: N802 – Qt
        kind = index.data(ROLE)["row"]
        return QSize(200, {ROW_DAY: DAY_H, ROW_HORIZON: HORIZON_H}.get(kind, CARD_H))

    def paint(self, painter: QPainter, option, index) -> None:
        data = index.data(ROLE)
        rect: QRect = option.rect
        pal: QPalette = option.palette
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        line_pen = QPen(pal.color(QPalette.ColorRole.Mid), 2)
        x = rect.left() + LINE_X
        if data["row"] == ROW_DAY:
            painter.setPen(line_pen)
            painter.drawLine(x, rect.top(), x, rect.bottom())
            font = QFont(option.font)
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(pal.color(QPalette.ColorRole.Text))
            painter.drawText(QRect(x + 14, rect.top(), rect.width(), rect.height()),
                             Qt.AlignmentFlag.AlignVCenter, data["label"].upper())
        elif data["row"] == ROW_HORIZON:
            pen = QPen(COLOR_PARTIAL, 1, Qt.PenStyle.DashLine)
            painter.setPen(pen)
            y = rect.center().y()
            painter.drawLine(rect.left() + 4, y, rect.right() - 4, y)
            font = QFont(option.font)
            font.setPointSizeF(max(7.0, font.pointSizeF() * 0.9))
            painter.setFont(font)
            text = "  " + data["label"] + "  "
            w = painter.fontMetrics().horizontalAdvance(text)
            box = QRect(rect.center().x() - w // 2, rect.top() + 4, w, rect.height() - 8)
            painter.fillRect(box, pal.color(QPalette.ColorRole.Base))
            painter.setPen(COLOR_PARTIAL)
            painter.drawText(box, Qt.AlignmentFlag.AlignCenter, text)
        else:
            self._paint_card(painter, option, rect, pal, data, line_pen, x)
        painter.restore()

    @staticmethod
    def _paint_card(painter, option, rect, pal, data, line_pen, x) -> None:
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        if selected:
            hl = QColor(pal.color(QPalette.ColorRole.Highlight))
            hl.setAlpha(45)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(hl)
            painter.drawRoundedRect(QRectF(rect.adjusted(LINE_X + 12, 2, -4, -2)), 6, 6)
        painter.setPen(line_pen)
        painter.drawLine(x, rect.top(), x, rect.bottom())
        # kolečko dostupnosti obnovy
        cy = rect.top() + 20
        status = data["status"]
        color = {"full": COLOR_FULL, "partial": COLOR_PARTIAL, "none": COLOR_NONE}.get(status, COLOR_NONE)
        dot = QRectF(x - DOT_R, cy - DOT_R, 2 * DOT_R, 2 * DOT_R)
        painter.setPen(QPen(color, 2))
        painter.setBrush(pal.color(QPalette.ColorRole.Base))
        if status is None:  # záznam bez změn souborů (komentář, zrušení aktivace…)
            painter.setBrush(pal.color(QPalette.ColorRole.Mid))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(dot.adjusted(2, 2, -2, -2))
        else:
            if status == "full":
                painter.setBrush(color)
            painter.drawEllipse(dot)
            if status == "partial":
                painter.setBrush(color)
                painter.drawPie(dot, 90 * 16, 180 * 16)
        # avatar uživatele
        ax = x + 18
        avatar = QRectF(ax, rect.top() + 7, AVATAR, AVATAR)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(user_color(data["user"]))
        painter.drawEllipse(avatar)
        font = QFont(option.font)
        font.setBold(True)
        font.setPointSizeF(max(6.5, font.pointSizeF() * 0.85))
        painter.setFont(font)
        painter.setPen(QColor("#ffffff"))
        painter.drawText(avatar, Qt.AlignmentFlag.AlignCenter, initials(data["user"]))
        # texty
        tx = int(avatar.right()) + 10
        width = rect.right() - tx - 8
        base = QFont(option.font)
        text_color = pal.color(QPalette.ColorRole.Text)
        bold = QFont(base)
        bold.setBold(True)
        painter.setFont(bold)
        painter.setPen(text_color)
        line1 = rect.top() + 4
        fm_b = painter.fontMetrics()
        painter.drawText(tx, line1 + fm_b.ascent(), data["time"])
        cx = tx + fm_b.horizontalAdvance(data["time"]) + 8
        painter.setFont(base)
        fm = painter.fontMetrics()
        user_text = f"{data['user']} · "
        painter.drawText(cx, line1 + fm.ascent(), user_text)
        cx += fm.horizontalAdvance(user_text)
        painter.setPen(QColor(KIND_COLORS.get(data["kind"], "#2f6fb5")))
        painter.setFont(bold)
        painter.drawText(cx, line1 + fm.ascent(), data["kind_label"])
        # komentář
        painter.setFont(base)
        painter.setPen(text_color)
        line2 = line1 + fm.height() + 1
        comment = fm.elidedText(data["comment"] or "—", Qt.TextElideMode.ElideRight, width)
        if not data["comment"]:
            painter.setPen(pal.color(QPalette.ColorRole.PlaceholderText))
        painter.drawText(tx, line2 + fm.ascent(), comment)
        # štítky změn a stav obnovy
        small = QFont(base)
        small.setPointSizeF(max(6.5, base.pointSizeF() * 0.88))
        painter.setFont(small)
        fs = painter.fontMetrics()
        line3 = line2 + fm.height() + 2
        cx = tx
        for text, chip_color in data["chips"]:
            painter.setPen(QColor(chip_color))
            painter.drawText(cx, line3 + fs.ascent(), text)
            cx += fs.horizontalAdvance(text) + 12
        if data["missing"]:
            painter.setPen(COLOR_PARTIAL if status == "partial" else COLOR_NONE)
            painter.drawText(cx, line3 + fs.ascent(), data["missing"])


class TimelineDialog(QDialog):
    def __init__(self, db: ProjectDB, name: str, catalog: RestoreCatalog | None = None,
                 me: str = "", scopes: list[str] | None = None, parent: QWidget | None = None,
                 file_history: Callable[[str], None] | None = None):
        """catalog: obnovitelné stavy (None = bez obnovy); me: přihlášený uživatel (filtr Jen
        moje); scopes: soubory / složky vybrané ve stromu (filtr); file_history: otevře historii
        souboru (dvojklik v podrobnostech)."""
        super().__init__(parent)
        self.db, self.name, self.catalog, self.me = db, name, catalog, me
        self.scopes = list(scopes or [])
        self.file_history = file_history
        self.chosen_point: RestorePoint | None = None
        self.setWindowTitle(f"Historie projektu {name}")
        self.resize(1150, 720)

        self.commits = db.commits()
        self._changes: dict[int, list] = defaultdict(list)
        for ch in db.all_changes():
            self._changes[ch["commit_id"]].append(ch)
        self._runs: dict[int, list] = defaultdict(list)
        for run in db.script_runs():
            self._runs[run["commit_id"]].append(run)
        self.points = {p.commit_id: p for p in (catalog.points if catalog else [])}
        # hranice: pod nejstarším stavem, který lze obnovit celý (všechny starší celé nejdou)
        complete = [p.commit_id for p in self.points.values() if p.complete]
        self._horizon_after = min(complete) if complete else None
        self._texts = [" ".join([date_time(c["created_at"]), c["user"], c["pc"],
                                 dbm.COMMIT_LABELS.get(c["kind"], c["kind"]), _comment(c)])
                       for c in self.commits]

        # --- filtry ---
        self.search = QLineEdit()
        self.search.setPlaceholderText("Hledat – uživatel, komentář, soubor…  (Ctrl+F)")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _: self._apply_filter())
        QShortcut(QKeySequence.StandardKey.Find, self,
                  activated=lambda: (self.search.setFocus(), self.search.selectAll()))
        self.kind_filter = QComboBox()
        for label, _ in KIND_FILTERS:
            self.kind_filter.addItem(label)
        self.kind_filter.currentIndexChanged.connect(lambda _: self._apply_filter())
        self.user_filter = QComboBox()
        self.user_filter.addItem("Všichni uživatelé", "")
        for user in sorted({c["user"] for c in self.commits}, key=str.lower):
            self.user_filter.addItem(user, user)
        self.user_filter.currentIndexChanged.connect(lambda _: self._apply_filter())
        self.only_mine = QCheckBox("Jen moje")
        self.only_mine.setVisible(bool(me))
        self.only_mine.toggled.connect(lambda _: self._apply_filter())
        self.only_scopes = QCheckBox(f"K vybraným souborům ({len(self.scopes)})")
        self.only_scopes.setVisible(bool(self.scopes))
        self.only_scopes.setToolTip("\n".join(self.scopes))
        self.only_scopes.toggled.connect(lambda _: self._apply_filter())
        self.show_imported = QCheckBox("I soubory načtené do evidence")
        self.show_imported.setToolTip("Soubory, které se načetly do evidence při první aktivaci")
        self.show_imported.toggled.connect(lambda _: self._apply_filter())
        filters = QHBoxLayout()
        filters.addWidget(self.search, stretch=1)
        filters.addWidget(self.kind_filter)
        filters.addWidget(self.user_filter)
        filters.addWidget(self.only_mine)
        filters.addWidget(self.only_scopes)
        filters.addWidget(self.show_imported)
        self.found = QLabel()
        legend = QLabel("Obnova projektu k danému stavu: "
                        "<span style='color:#2e8b57'>●</span> celý &nbsp; "
                        "<span style='color:#e08a00'>◐</span> jen částečně &nbsp; "
                        "<span style='color:#9e9e9e'>○</span> nelze (zálohy chybí)")
        legend.setTextFormat(Qt.TextFormat.RichText)

        # --- přehled aktivity (sloupeček za den / týden / měsíc, barvy uživatelů) ---
        self.activity = ActivityStrip()
        self.activity.jump.connect(self.jump_to)

        # --- časová osa ---
        self.timeline = QListWidget()
        self.timeline.setItemDelegate(CardDelegate(self.timeline))
        self.timeline.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.timeline.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.timeline.setUniformItemSizes(False)
        self.timeline.setMouseTracking(True)
        self.timeline.itemSelectionChanged.connect(self._show_details)

        # --- podrobnosti ---
        self.detail_head = QLabel()
        self.detail_head.setTextFormat(Qt.TextFormat.RichText)
        self.detail_head.setWordWrap(True)
        self.detail_head.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.restore_info = QLabel()
        self.restore_info.setWordWrap(True)
        self.restore_info.setTextFormat(Qt.TextFormat.RichText)
        self.btn_restore = QPushButton("Obnovit projekt k tomuto stavu…")
        self.btn_restore.setToolTip("Obnoví celý projekt do nové místní složky; na serveru se nic nemění")
        self.btn_restore.clicked.connect(self._choose_restore)
        self.btn_scripts = QPushButton()
        self.btn_scripts.clicked.connect(lambda: self.show_script_log())
        self.changes = QTreeWidget()
        self.changes.setHeaderLabels(["Změna", "Soubor", "Původně", "Záloha / koš"])
        self.changes.setRootIsDecorated(False)
        self.changes.setUniformRowHeights(True)
        self.changes.itemDoubleClicked.connect(lambda item, _c: self._open_file_history(item))
        self.btn_file = QPushButton("Historie souboru…")
        self.btn_file.setToolTip("Všechny verze souboru – otevřít nebo obnovit jednu verzi")
        self.btn_file.clicked.connect(lambda: self._open_file_history(self.changes.currentItem()))
        self.btn_file.setVisible(file_history is not None)
        self.changes.itemSelectionChanged.connect(self._update_file_button)
        right = QWidget()
        rlay = QVBoxLayout(right)
        rlay.setContentsMargins(6, 0, 0, 0)
        rlay.addWidget(self.detail_head)
        rlay.addWidget(self.restore_info)
        brow = QHBoxLayout()
        brow.addWidget(self.btn_restore)
        brow.addWidget(self.btn_scripts)
        brow.addStretch()
        rlay.addLayout(brow)
        rlay.addWidget(self.changes, stretch=1)
        frow = QHBoxLayout()
        frow.addWidget(self.btn_file)
        frow.addStretch()
        rlay.addLayout(frow)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self.timeline)
        split.addWidget(right)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        close = QPushButton("Zavřít")
        close.clicked.connect(self.reject)
        bottom = QHBoxLayout()
        bottom.addWidget(legend)
        bottom.addStretch()
        bottom.addWidget(close)

        layout = QVBoxLayout(self)
        layout.addLayout(filters)
        layout.addWidget(self.found)
        layout.addWidget(self.activity)
        layout.addWidget(split, stretch=1)
        layout.addLayout(bottom)
        if catalog is None:
            self.btn_restore.hide()
        self._apply_filter()

    # --- filtry ---------------------------------------------------------------

    def _needle(self) -> str:
        return normalize(self.search.text().strip())

    def _changes_of(self, i: int) -> list:
        changes = [ch for ch in self._changes.get(self.commits[i]["commit_id"], [])
                   if self.show_imported.isChecked() or ch["change_type"] != dbm.IMPORTED]
        if self.only_scopes.isChecked() and self.scopes:
            changes = [ch for ch in changes
                       if in_scope(ch["new_path"] or "", self.scopes) or in_scope(ch["old_path"] or "", self.scopes)]
        return changes

    def matching_changes(self, i: int) -> list:
        """Změny záznamu, které se mají zobrazit (filtr souborů a hledání)."""
        changes = self._changes_of(i)
        needle = self._needle()
        if not needle or contains(self._texts[i], needle):
            return changes
        return [ch for ch in changes if contains(_change_text(ch), needle)]

    def _commit_visible(self, i: int) -> bool:
        c = self.commits[i]
        wanted = KIND_FILTERS[self.kind_filter.currentIndex()][1]
        if wanted == "other" and c["kind"] in MAIN_KINDS:
            return False
        if isinstance(wanted, set) and c["kind"] not in wanted:
            return False
        user = self.user_filter.currentData()
        if user and c["user"] != user:
            return False
        if self.only_mine.isChecked() and c["user"].lower() != self.me.lower():
            return False
        if self.only_scopes.isChecked() and self.scopes and not self._changes_of(i):
            return False
        needle = self._needle()
        return not needle or contains(self._texts[i], needle) or bool(self.matching_changes(i))

    def visible_commits(self) -> list:
        return [self.timeline.item(r).data(ROLE)["commit"] for r in range(self.timeline.count())
                if self.timeline.item(r).data(ROLE)["row"] == ROW_COMMIT]

    def _apply_filter(self) -> None:
        selected = self.selected_commit()
        keep = selected["commit_id"] if selected is not None else None
        self.timeline.clear()
        shown = 0
        last_day = None
        first_row = None
        for i, c in enumerate(self.commits):
            if not self._commit_visible(i):
                continue
            day = day_label(c["created_at"])
            if day != last_day:
                self._add_row({"row": ROW_DAY, "label": day})
                last_day = day
            item = self._add_row(self._card_data(i))
            shown += 1
            if first_row is None or c["commit_id"] == keep:
                first_row = item
            if self._horizon_after is not None and c["commit_id"] == self._horizon_after:
                if any(p.commit_id < c["commit_id"] for p in self.points.values()):
                    self._add_row({"row": ROW_HORIZON, "label": "starší stavy už nelze obnovit celé – "
                                                                 "zálohy byly smazány (ponechává se jen nastavený počet)"})
        needle = self._needle()
        filtered = bool(needle) or self.kind_filter.currentIndex() or self.user_filter.currentIndex() \
            or self.only_mine.isChecked() or self.only_scopes.isChecked()
        self.found.setText(f"Nalezeno záznamů: {shown} z {len(self.commits)}")
        self.found.setVisible(bool(filtered))
        if first_row is not None:
            self.timeline.setCurrentItem(first_row)
            first_row.setSelected(True)
        self.activity.set_records([(c["created_at"], c["user"]) for c in self.visible_commits()])
        self._show_details()

    def jump_to(self, start, end) -> None:
        """Klepnutí do přehledu aktivity: vybrat nejnovější záznam z daného období."""
        for r in range(self.timeline.count()):
            item = self.timeline.item(r)
            data = item.data(ROLE)
            if data["row"] != ROW_COMMIT:
                continue
            try:
                day = datetime.fromisoformat(data["commit"]["created_at"]).date()
            except ValueError:
                continue
            if start <= day <= end:
                self.timeline.setCurrentItem(item)
                item.setSelected(True)
                self.timeline.scrollToItem(item, QAbstractItemView.ScrollHint.PositionAtTop)
                return

    def _add_row(self, data: dict) -> QListWidgetItem:
        item = QListWidgetItem()
        item.setData(ROLE, data)
        if data["row"] != ROW_COMMIT:
            item.setFlags(Qt.ItemFlag.NoItemFlags)
        else:
            item.setToolTip(data["tooltip"])
        self.timeline.addItem(item)
        return item

    def _card_data(self, i: int) -> dict:
        c = self.commits[i]
        changes = self.matching_changes(i)
        chips = []
        for kinds, one, many in CHANGE_GROUPS:
            n = sum(1 for ch in changes if ch["change_type"] in kinds)
            if n:
                sample = next(ch for ch in changes if ch["change_type"] in kinds)
                mark, color = CHANGE_STYLE.get(sample["change_type"], ("", "#000000"))
                chips.append((f"{mark}{n} {one if n == 1 else many}", color))
        point = self.points.get(c["commit_id"])
        status, missing, tip = None, "", ""
        if point is not None:
            if point.complete:
                status, tip = "full", f"Projekt lze k tomuto stavu obnovit celý ({len(point.files)} souborů)."
            else:
                status = "none" if len(point.missing) == len(point.files) else "partial"
                missing = f"chybí {len(point.missing)} z {len(point.files)} souborů"
                tip = ("K tomuto stavu nelze projekt obnovit celý – chybí zálohy:\n"
                       + "\n".join(point.missing[:15]) + ("\n…" if len(point.missing) > 15 else ""))
        return {"row": ROW_COMMIT, "commit": c, "index": i, "user": c["user"],
                "time": time_label(c["created_at"]), "kind": c["kind"],
                "kind_label": dbm.COMMIT_LABELS.get(c["kind"], c["kind"]),
                "comment": _comment(c).split("\n", 1)[0], "chips": chips, "status": status,
                "missing": missing,
                "tooltip": f"{date_time(c['created_at'])} – {c['user']} ({c['pc']})\n"
                           f"{_comment(c)}" + (f"\n\n{tip}" if tip else "")}

    # --- podrobnosti ---------------------------------------------------------------

    def selected_commit(self):
        item = self.timeline.currentItem()
        if item is None or not item.isSelected():
            return None
        data = item.data(ROLE)
        return data["commit"] if data and data["row"] == ROW_COMMIT else None

    def _selected_index(self) -> int | None:
        item = self.timeline.currentItem()
        data = item.data(ROLE) if item is not None and item.isSelected() else None
        return data["index"] if data and data["row"] == ROW_COMMIT else None

    def _show_details(self) -> None:
        i = self._selected_index()
        self.changes.clear()
        self.btn_scripts.hide()
        if i is None:
            self.detail_head.setText("<i>Vyberte záznam na časové ose.</i>")
            self.restore_info.setText("")
            self.btn_restore.setEnabled(False)
            self._update_file_button()
            return
        c = self.commits[i]
        kind = dbm.COMMIT_LABELS.get(c["kind"], c["kind"])
        color = KIND_COLORS.get(c["kind"], "#2f6fb5")
        comment = _comment(c) if c["kind"] == dbm.PROJECT_COMMENT else (c["comment"] or "—")
        self.detail_head.setText(
            f"<b>{date_time(c['created_at'])}</b> &nbsp; "
            f"<span style='color:{user_color(c['user']).name()}'><b>{c['user']}</b></span> ({c['pc']})"
            f"<br><span style='color:{color}'><b>{kind}</b></span><br>"
            + comment.replace("&", "&amp;").replace("<", "&lt;").replace("\n", "<br>"))
        point = self.points.get(c["commit_id"])
        if point is None:
            self.restore_info.setText("<span style='color:#9e9e9e'>Záznam bez změn souborů.</span>"
                                      if self.catalog else "")
            self.btn_restore.setEnabled(False)
        elif point.complete:
            self.restore_info.setText(f"<span style='color:#2e8b57'>●</span> Projekt lze k tomuto stavu "
                                      f"obnovit celý ({len(point.files)} souborů).")
            self.btn_restore.setEnabled(True)
        else:
            self.restore_info.setText(
                f"<span style='color:#e08a00'>◐</span> Celý projekt k tomuto stavu obnovit nelze – "
                f"chybí zálohy {len(point.missing)} z {len(point.files)} souborů. Jednotlivé "
                "dostupné verze souborů lze obnovit v historii souboru.")
            self.btn_restore.setEnabled(False)
        runs = self._runs.get(c["commit_id"], [])
        if runs:
            ok = bool(runs[0]["ok"])  # poslední běh
            self.btn_scripts.setText("Skripty: proběhly…" if ok else "Skripty: SELHALY…")
            self.btn_scripts.setToolTip("Záznam běhu skriptů spuštěných po tomto odevzdání")
            self.btn_scripts.show()
        for ch in self.matching_changes(i):
            mark, ccolor = CHANGE_STYLE.get(ch["change_type"], ("", "#000000"))
            label = dbm.CHANGE_LABELS.get(ch["change_type"], ch["change_type"])
            path = ch["new_path"] or ch["old_path"] or ""
            old = ch["old_path"] if ch["old_path"] and ch["old_path"] != path else ""
            item = QTreeWidgetItem([f"{mark} {label}".strip(), path, old, ch["backup_path"] or ""])
            item.setForeground(0, QColor(ccolor))
            item.setData(0, ROLE, path)
            for col, text in ((1, path), (2, old), (3, ch["backup_path"] or "")):
                if text:
                    item.setToolTip(col, self.path_tooltip(text))
            self.changes.addTopLevelItem(item)
        for col in range(4):
            self.changes.resizeColumnToContents(col)
        self._update_file_button()

    def shown_changes(self) -> list[str]:
        return [self.changes.topLevelItem(r).text(1) for r in range(self.changes.topLevelItemCount())]

    def path_tooltip(self, rel: str) -> str:
        """Relativní cesta v projektu a úplná cesta na serveru."""
        project = Path(self.db.path).parent.parent  # <projekt>/.spm/project.db
        return f"{rel}\n{project.joinpath(*rel.split('/'))}"

    def _update_file_button(self) -> None:
        item = self.changes.currentItem()
        self.btn_file.setEnabled(item is not None and bool(item.data(0, ROLE)))

    def _open_file_history(self, item: QTreeWidgetItem | None) -> None:
        if item is None or self.file_history is None:
            return
        path = item.data(0, ROLE)
        if path:
            self.file_history(path)

    def _choose_restore(self) -> None:
        c = self.selected_commit()
        point = self.points.get(c["commit_id"]) if c is not None else None
        if point is None or not point.complete:
            return
        self.chosen_point = point
        self.accept()

    def show_script_log(self) -> None:
        c = self.selected_commit()
        if c is None:
            return
        runs = self._runs.get(c["commit_id"], [])
        parts = [f"{date_time(r['created_at'])}  {r['user']} ({r['pc']})  {r['scripts']}  – "
                 + ("v pořádku" if r["ok"] else "CHYBA") + "\n\n" + r["log"] for r in runs]
        box = QMessageBox(QMessageBox.Icon.Information, "Záznam skriptů",
                          "Skripty spuštěné po odevzdání (nejnovější první) – podrobnosti níže.", parent=self)
        box.setDetailedText(("\n\n" + "-" * 60 + "\n\n").join(parts))
        box.exec()
