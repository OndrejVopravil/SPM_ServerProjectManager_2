"""Výběr uživatelů zaškrtnutím ze seznamu (adresář uživatelů síťové složky) – loginy se
nepíšou ručně. Používá ho Nastavení (kdo smí archivovat) a vzkazy (adresáti)."""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


INACTIVE_ROLE = Qt.ItemDataRole.UserRole + 1


class UsersDialog(QDialog):
    """Výběr uživatelů zaškrtnutím – z adresáře uživatelů síťové složky (jako adresát poznámky)."""

    def __init__(self, title: str, users: list, selected: list[str], parent: QWidget | None = None,
                 exclude: str = ""):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(380, 360)
        info = QLabel("Nabízejí se všichni, kdo SPM s touto síťovou složkou spustili. Kdo ho delší "
                      "dobu nespustil, je mezi neaktivními.")
        info.setWordWrap(True)
        self.show_inactive = QCheckBox("Zobrazit i neaktivní")
        self.show_inactive.setToolTip("Uživatelé, kteří SPM déle než měsíc nespustili (dovolená, "
                                      "občasní uživatelé…) – vybraní se zobrazí vždy")
        self.show_inactive.toggled.connect(lambda _: self._filter(self.search.text()))
        self.search = QLineEdit()
        self.search.setPlaceholderText("Hledat…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        self.list = QListWidget()
        chosen = {u.lower() for u in selected}
        known = set()
        for u in users:
            if exclude and u.login.lower() == exclude.lower():
                continue
            inactive = u.inactive()
            self._add(f"{u.display} – {u.seen_label}" if inactive else u.display, u.login,
                      u.login.lower() in chosen, inactive)
            known.add(u.login.lower())
        for login in selected:  # vybraný uživatel, který SPM ještě nespustil
            if login.lower() not in known:
                self._add(login, login, True)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Zrušit")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(info)
        layout.addWidget(self.search)
        layout.addWidget(self.list)
        layout.addWidget(self.show_inactive)
        layout.addWidget(buttons)
        self.show_inactive.setVisible(any(self.list.item(i).data(INACTIVE_ROLE) for i in range(self.list.count())))
        self._filter("")

    def _add(self, text: str, login: str, checked: bool, inactive: bool = False) -> None:
        item = QListWidgetItem(text)
        item.setData(Qt.ItemDataRole.UserRole, login)
        item.setData(INACTIVE_ROLE, inactive)
        if inactive:
            item.setForeground(self.palette().color(QPalette.ColorRole.PlaceholderText))
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self.list.addItem(item)

    def _filter(self, text: str) -> None:
        needle = text.strip().lower()
        for i in range(self.list.count()):
            item = self.list.item(i)
            hidden_inactive = (item.data(INACTIVE_ROLE) and not self.show_inactive.isChecked()
                               and item.checkState() != Qt.CheckState.Checked)
            item.setHidden(hidden_inactive or (bool(needle) and needle not in item.text().lower()
                                               and needle not in item.data(Qt.ItemDataRole.UserRole).lower()))

    def selected(self) -> list[str]:
        return [self.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.CheckState.Checked]


class UsersPicker(QWidget):
    """Seznam vybraných uživatelů (jen ke čtení) a tlačítko Vybrat…"""

    changed = Signal()

    def __init__(self, logins: list[str], users: Callable[[], list], title: str,
                 empty: str = "nikdo", parent: QWidget | None = None, exclude: str = ""):
        """users: adresář uživatelů (UserInfo) – volá se při zobrazení a výběru;
        exclude: login, který se nenabízí (např. já sám u adresátů vzkazu)."""
        super().__init__(parent)
        self.users, self.title, self.empty, self.exclude = users, title, empty, exclude
        self._logins: list[str] = []
        self.text = QLineEdit()
        self.text.setReadOnly(True)
        self.button = QPushButton("Vybrat…")
        self.button.clicked.connect(lambda: self.choose())
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.text, stretch=1)
        lay.addWidget(self.button)
        self.set_logins(logins)

    def _users(self) -> list:
        try:
            return list(self.users())
        except OSError:
            return []

    def logins(self) -> list[str]:
        return list(self._logins)

    def people(self) -> list[tuple[str, str]]:
        """Vybraní jako (login, zobrazované jméno)."""
        names = {u.login.lower(): u.display for u in self._users()}
        return [(u.lower(), names.get(u.lower(), "")) for u in self._logins]

    def set_logins(self, logins: list[str]) -> None:
        self._logins = []
        for u in (u.strip() for u in logins):
            if u and u.lower() not in (x.lower() for x in self._logins):
                self._logins.append(u)
        names = {u.login.lower(): u.display for u in self._users()}
        shown = [names.get(u.lower(), u) for u in self._logins]
        self.text.setText(", ".join(shown))
        self.text.setPlaceholderText(self.empty)
        self.text.setToolTip("\n".join(shown) or self.empty)
        self.changed.emit()

    def add_login(self, login: str) -> None:
        self.set_logins(self._logins + [login])

    def choose(self) -> bool:
        dlg = UsersDialog(self.title, self._users(), self._logins, self, exclude=self.exclude)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return False
        self.set_logins(dlg.selected())
        return True
