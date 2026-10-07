"""Dialog publikování složky „Jen místně“ na server – výběr souborů a povinný komentář.

Publikovat lze celou složku nebo jen část. Publikované soubory se přesunou na server;
nepublikované se odloží do složky <projekt>_puvodni_<datum> (zůstane „Jen místně“).
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..scanner import to_abs
from .check_tree import CheckTree, TreeEntry, in_scope

COLOR_INVALID = QColor("#e67e22")


class PublishDialog(QDialog):
    def __init__(self, name: str, server_path: Path, local: Path, files: list[str],
                 invalid: set[str], parent: QWidget | None = None, preselect: str | None = None):
        """files: soubory v místní složce; invalid: soubory s nepovolenými znaky (nelze vybrat);
        preselect: soubor / složka ('/' na konci) předvybraná – jinak je vybráno vše."""
        super().__init__(parent)
        self.setWindowTitle(f"Publikovat projekt {name}")
        self.resize(780, 640)
        self.total = len(files)

        intro = QLabel(
            f"Vybrané soubory se přesunou na server jako nový projekt:<br><b>{server_path}</b><br><br>"
            "Po nahrání a ověření se z místní složky odstraní. Pokud nepublikujete vše, zbylé soubory "
            f"se odloží do místní složky <b>{name}_puvodni_&lt;datum&gt;</b> (zůstane „Jen místně“).")
        intro.setWordWrap(True)

        self.tree = CheckTree(["Soubor", "Poznámka"])
        checked = {p for p in files if p not in invalid and (preselect is None or in_scope(p, preselect))}
        self.tree.set_entries([
            TreeEntry(p, p, ["nepovolené znaky v názvu – nelze publikovat" if p in invalid else ""],
                      enabled=p not in invalid, color=COLOR_INVALID if p in invalid else None,
                      file_path=str(to_abs(local, p)))
            for p in files
        ], checked)
        self.tree.toggled.connect(lambda *_: self._validate())

        all_btn = QPushButton("Vybrat vše")
        all_btn.clicked.connect(lambda: self.tree.set_all(True))
        none_btn = QPushButton("Zrušit výběr")
        none_btn.clicked.connect(lambda: self.tree.set_all(False))
        row = QHBoxLayout()
        row.addWidget(all_btn)
        row.addWidget(none_btn)
        row.addStretch()

        self.count = QLabel()
        self.comment = QPlainTextEdit()
        self.comment.setPlaceholderText("Popište projekt / co publikujete…")
        self.comment.setMaximumHeight(80)
        self.comment.textChanged.connect(self._validate)
        self.error = QLabel()
        self.error.setStyleSheet("color: #c0392b;")

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Publikovat")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Zrušit")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addLayout(row)
        layout.addWidget(self.tree)
        layout.addWidget(self.count)
        layout.addWidget(QLabel("<b>Komentář</b> (povinný):"))
        layout.addWidget(self.comment)
        layout.addWidget(self.error)
        layout.addWidget(self.buttons)
        self._validate()

    def selected_paths(self) -> list[str]:
        return self.tree.checked_keys()

    def comment_text(self) -> str:
        return self.comment.toPlainText().strip()

    def _validate(self) -> None:
        n = len(self.selected_paths())
        self.count.setText(f"Vybráno k publikování: <b>{n}</b> z {self.total} souborů"
                           + ("" if n in (0, self.total) else " – zbytek se odloží do …_puvodni_…"))
        error = ""
        if not n:
            error = "Vyberte, co chcete publikovat."
        elif not self.comment_text():
            error = "Bez komentáře nelze projekt publikovat."
        self.error.setText(error)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(not error)
