"""Úpravy vzhledu nad výchozím stylem Qt."""

from __future__ import annotations

from PySide6.QtWidgets import QProxyStyle, QStyle, QStyleOptionViewItem, QTableView


class AppStyle(QProxyStyle):
    """Styl Windows 11 v Qt kreslí u označeného řádku tabulky do každé buňky svislý
    proužek v barvě zvýraznění. Kreslí ho tabulka přes PE_PanelItemViewRow (ne delegát),
    proto se zde tomuto prvku odebere stav „vybráno“. Podbarvení vybraného řádku
    zůstává, to kreslí samotná buňka (CE_ItemViewItem)."""

    def drawPrimitive(self, element, option, painter, widget=None):
        if element == QStyle.PrimitiveElement.PE_PanelItemViewRow and isinstance(widget, QTableView):
            option = QStyleOptionViewItem(option)
            option.state &= ~QStyle.StateFlag.State_Selected
        super().drawPrimitive(element, option, painter, widget)
