"""Výraznější označení vybraných řádků ve stromech a seznamech (decentně, barvou zvýraznění
systému): podbarvení celého řádku a proužek vlevo. Text si ponechá svou barvu (stav souboru).
Výběr zůstane dobře vidět i ve chvíli, kdy seznam nemá fokus. Barvy se odvozují z palety –
sedí ve světlém i tmavém režimu.

Řádek pod myší se záměrně nezvýrazňuje. Odsazení stromu (větve) kreslí style.AppStyle
(PE_PanelItemViewRow), ne přepsané drawBranches: delegát spolu s přepsaným drawBranches
občas shodil proces při mazání položek stromu (QTreeWidget.clear)."""

from __future__ import annotations

from PySide6.QtCore import QModelIndex, QRect, Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QPalette
from PySide6.QtWidgets import QAbstractItemView, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QTreeView, QWidget

BAR = 3  # šířka proužku vlevo (px)
TEXT_INDENT = BAR + 4  # seznam: odsazení textu prvního sloupce od proužku (px)
# krytí podbarvení vybraného řádku: (světlý režim, tmavý režim)
FILL_ALPHA = (0.18, 0.22)
UNFOCUSED = 0.65   # bez fokusu: podbarvení slabší (proužek zůstává)


def is_dark(widget: QWidget) -> bool:
    return widget.palette().color(QPalette.ColorRole.Base).lightness() < 128


def colors(view: QAbstractItemView) -> tuple[QColor, QColor]:
    """(podbarvení výběru, proužek)."""
    accent = QColor(view.palette().color(QPalette.ColorGroup.Active, QPalette.ColorRole.Highlight))
    focused = view.hasFocus()
    fill = QColor(accent)
    fill.setAlphaF(FILL_ALPHA[1 if is_dark(view) else 0] * (1.0 if focused else UNFOCUSED))
    bar = QColor(accent)
    bar.setAlphaF(1.0 if focused else 0.75)
    return fill, bar


class SelectionDelegate(QStyledItemDelegate):
    """Buňka: vlastní podbarvení výběru; styl kreslí jen text a ikonu."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        view = self.parent()
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        # vlastní podbarvení položky (např. žluté u nalezených) pod výběrem, ne přes něj
        if opt.backgroundBrush.style() != Qt.BrushStyle.NoBrush:
            painter.fillRect(opt.rect, opt.backgroundBrush)
            opt.backgroundBrush = QBrush()
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        fill, bar = colors(view)
        if selected:
            painter.fillRect(opt.rect, fill)
            # proužek u prvního sloupce seznamu (ve stromu ho kreslí paint_branches úplně vlevo)
            if index.column() == 0 and not isinstance(view, QTreeView):
                painter.fillRect(QRect(opt.rect.left(), opt.rect.top(), BAR, opt.rect.height()), bar)
        if index.column() == 0 and not isinstance(view, QTreeView):
            # text prvního sloupce odsazený od proužku – u všech řádků, ať při výběru neposkočí
            opt.rect = opt.rect.adjusted(TEXT_INDENT, 0, 0, 0)
        opt.state &= ~(QStyle.StateFlag.State_Selected | QStyle.StateFlag.State_MouseOver
                       | QStyle.StateFlag.State_HasFocus)
        # jako QStyledItemDelegate.paint, ale s už připravenou volbou (jinak by znovu načetla
        # podbarvení položky z modelu)
        style = opt.widget.style() if opt.widget is not None else view.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex):  # noqa: N802 – Qt
        size = super().sizeHint(option, index)
        if index.column() == 0 and not isinstance(self.parent(), QTreeView):
            size.setWidth(size.width() + TEXT_INDENT)  # místo pro odsazení od proužku
        return size


def paint_branches(view: QTreeView, painter: QPainter, rect: QRect) -> None:
    """Strom: odsazení vybrané položky (větve) – stejné podbarvení výběru a proužek úplně
    vlevo. Volá style.AppStyle místo pozadí odsazení (PE_PanelItemViewRow)."""
    fill, bar = colors(view)
    painter.fillRect(rect, fill)
    painter.fillRect(QRect(0, rect.top(), BAR, rect.height()), bar)


def install(view: QAbstractItemView) -> None:
    """Zapne vzhled výběru u seznamu / stromu (odsazení stromu kreslí style.AppStyle)."""
    from .style import OWN_SELECTION
    view.setProperty(OWN_SELECTION, True)  # styl pak výběr nekreslí (viz style.AppStyle)
    view.setItemDelegate(SelectionDelegate(view))
