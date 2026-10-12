"""Dialog pro úpravu komentáře projektu (víceřádkový text, nejvýše MAX_LENGTH znaků)."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..comment import MAX_LENGTH, ProjectComment, normalize_text


class CommentDialog(QDialog):
    def __init__(self, name: str, comment: ProjectComment | None, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(f"Komentář projektu {name}")
        self.resize(520, 300)

        info = QLabel("Komentář vidí všichni uživatelé v seznamu projektů (první řádek ve sloupci, "
                      "celý text v bublině). Každá změna se zapíše do historie projektu. "
                      "Prázdný text komentář smaže.")
        info.setWordWrap(True)
        self.edit = QPlainTextEdit(comment.text if comment else "")
        self.edit.textChanged.connect(self._update)
        self.changed = QLabel(comment.describe_change() if comment else "")
        self.changed.setVisible(comment is not None)
        self.counter = QLabel()

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                        | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Zrušit")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        bottom = QHBoxLayout()
        bottom.addWidget(self.changed)
        bottom.addStretch()
        bottom.addWidget(self.counter)
        layout = QVBoxLayout(self)
        layout.addWidget(info)
        layout.addWidget(self.edit)
        layout.addLayout(bottom)
        layout.addWidget(self.buttons)
        self.edit.setFocus()
        self.edit.moveCursor(self.edit.textCursor().MoveOperation.End)
        self._update()

    def text(self) -> str:
        return normalize_text(self.edit.toPlainText())

    def _update(self) -> None:
        n = len(self.text())
        over = n > MAX_LENGTH
        self.counter.setText(f"{n} / {MAX_LENGTH} znaků" + (" – příliš dlouhý" if over else ""))
        self.counter.setStyleSheet("color: #c0392b;" if over else "")
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(not over)
