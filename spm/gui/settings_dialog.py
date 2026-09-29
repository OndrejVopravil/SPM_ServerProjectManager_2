from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..config import Settings


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Nastavení")
        self.setMinimumWidth(560)
        self.settings = settings

        self.server = QLineEdit(settings.server_root)
        self.server.setPlaceholderText(r"\\server\projekty")
        self.local = QLineEdit(settings.local_root)
        self.local.setPlaceholderText(r"C:\Projekty")

        self.backup_enabled = QCheckBox("Zálohovat přepisované soubory na serveru")
        self.backup_enabled.setChecked(settings.backup_enabled)
        self.backup_count = QSpinBox()
        self.backup_count.setRange(1, 99)
        self.backup_count.setValue(settings.backup_count)
        self.backup_count.setSuffix(" verzí")
        self.backup_count.setEnabled(settings.backup_enabled)
        self.backup_enabled.toggled.connect(self.backup_count.setEnabled)

        self.delete_local = QCheckBox("Po deaktivaci smazat místní kopii projektu")
        self.delete_local.setChecked(settings.delete_local_after_deactivate)

        self.threshold = QSpinBox()
        self.threshold.setRange(30, 95)
        self.threshold.setSingleStep(5)
        self.threshold.setSuffix(" %")
        self.threshold.setValue(round(settings.rename_threshold * 100))

        self.ignore = QLineEdit("; ".join(settings.ignore_patterns))

        form = QFormLayout()
        form.addRow("Síťová složka s projekty:", self._with_browse(self.server))
        form.addRow("Místní složka:", self._with_browse(self.local))
        form.addRow(self.backup_enabled)
        form.addRow("Počet ponechaných záloh:", self.backup_count)
        hint = QLabel("Zálohy se ukládají do <i>.spm\\backup</i> v projektu jako <i>soubor.backup01</i>, "
                      "<i>.backup02</i>, … Starší než nastavený počet se mažou.")
        hint.setWordWrap(True)
        form.addRow(hint)
        form.addRow(self.delete_local)
        form.addRow("Min. shoda pro návrh přejmenování:", self.threshold)
        form.addRow("Ignorované soubory:", self.ignore)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Zrušit")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def _with_browse(self, edit: QLineEdit) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        btn = QPushButton("Procházet…")

        def browse() -> None:
            path = QFileDialog.getExistingDirectory(self, "Vyberte složku", edit.text())
            if path:
                edit.setText(str(Path(path)))

        btn.clicked.connect(browse)
        lay.addWidget(edit)
        lay.addWidget(btn)
        return w

    def _accept(self) -> None:
        server, local = self.server.text().strip(), self.local.text().strip()
        if not server or not local:
            QMessageBox.warning(self, "Nastavení", "Vyplňte síťovou i místní složku.")
            return
        if not Path(server).is_dir():
            QMessageBox.warning(self, "Nastavení", f"Síťová složka není dostupná:\n{server}")
            return
        if Path(local).resolve() == Path(server).resolve():
            QMessageBox.warning(self, "Nastavení", "Síťová a místní složka nesmí být stejné.")
            return
        s = self.settings
        s.server_root, s.local_root = server, local
        s.backup_enabled = self.backup_enabled.isChecked()
        s.backup_count = self.backup_count.value()
        s.delete_local_after_deactivate = self.delete_local.isChecked()
        s.rename_threshold = self.threshold.value() / 100
        s.ignore_patterns = [p.strip() for p in self.ignore.text().split(";") if p.strip()]
        s.save()
        self.accept()
