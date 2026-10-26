"""Úpravy vzhledu nad výchozím stylem Qt."""

from __future__ import annotations

from PySide6.QtWidgets import QAbstractItemView, QProxyStyle, QStyle, QStyleOptionViewItem, QTableView, QTreeView

OWN_SELECTION = "spm_own_selection"  # vlastnost seznamu: výběr kreslí selection_style


def _own_selection(widget) -> bool:
    view = widget if isinstance(widget, QAbstractItemView) else (
        widget.parent() if widget is not None and isinstance(widget.parent(), QAbstractItemView) else None)
    return bool(view is not None and view.property(OWN_SELECTION))


class AppStyle(QProxyStyle):
    """Styl Windows 11 v Qt kreslí u označeného řádku tabulky do každé buňky svislý
    proužek v barvě zvýraznění. Kreslí ho tabulka přes PE_PanelItemViewRow (ne delegát),
    proto se zde tomuto prvku odebere stav „vybráno“. Podbarvení vybraného řádku
    zůstává, to kreslí samotná buňka (CE_ItemViewItem).

    U seznamů a stromů s vlastním vzhledem výběru (selection_style) styl výběr ani najetí
    myší nekreslí vůbec. Odsazení vybrané položky stromu (PE_PanelItemViewRow se stavem
    „vybráno“ – buňky ho díky ShowDecorationSelected = 0 nemají) podbarví selection_style."""

    def drawPrimitive(self, element, option, painter, widget=None):
        if (element == QStyle.PrimitiveElement.PE_PanelItemViewRow and isinstance(widget, QTreeView)
                and option.state & QStyle.StateFlag.State_Selected and _own_selection(widget)):
            from . import selection_style
            selection_style.paint_branches(widget, painter, option.rect)
            return
        if element == QStyle.PrimitiveElement.PE_PanelItemViewRow and (
                isinstance(widget, QTableView) or _own_selection(widget)):
            option = QStyleOptionViewItem(option)
            option.state &= ~QStyle.StateFlag.State_Selected
            if _own_selection(widget):
                option.state &= ~QStyle.StateFlag.State_MouseOver
        super().drawPrimitive(element, option, painter, widget)

    def styleHint(self, hint, option=None, widget=None, returnData=None):
        if hint == QStyle.StyleHint.SH_ItemView_ShowDecorationSelected and _own_selection(widget):
            return 0
        return super().styleHint(hint, option, widget, returnData)
