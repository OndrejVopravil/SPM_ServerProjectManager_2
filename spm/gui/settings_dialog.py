from __future__ import annotations

import html
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import shared_config as sc
from ..config import AUTO_REFRESH_MIN, AUTO_REFRESH_OFF, Settings
from .tab_bar import TAB_COLORS


def _color_icon(color: str) -> QIcon:
    """Čtvereček barvy pro výběr barvy záložky (prázdný = bez barvy)."""
    pixmap = QPixmap(14, 14)
    pixmap.fill(QColor(color) if color else QColor(0, 0, 0, 0))
    return QIcon(pixmap)


class SecretsDialog(QDialog):
    """Tajné hodnoty pro skripty: název a hodnota (skrytá, lze ukázat)."""

    def __init__(self, secrets: dict[str, str], parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Tajné hodnoty pro skripty")
        self.setMinimumWidth(520)
        info = QLabel("Ve skriptu se použijí jako <code>{SECRET:název}</code> – např. "
                      "<code>HEADER \"Authorization\" \"Bearer {SECRET:cam}\"</code>. Ukládají se jen "
                      "do nastavení v tomto počítači a do záznamu běhu skriptu se nevypisují.")
        info.setWordWrap(True)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Název", "Hodnota"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 160)
        for name, value in sorted(secrets.items(), key=lambda kv: kv[0].lower()):
            self._add_row(name, value)
        self.show_values = QCheckBox("Zobrazit hodnoty")
        self.show_values.toggled.connect(self._toggle_values)
        add = QPushButton("Přidat")
        add.clicked.connect(lambda: self._add_row("", ""))
        remove = QPushButton("Odebrat")
        remove.clicked.connect(self._remove_row)
        row = QHBoxLayout()
        for w in (add, remove):
            row.addWidget(w)
        row.addStretch()
        row.addWidget(self.show_values)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Zrušit")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(info)
        layout.addWidget(self.table)
        layout.addLayout(row)
        layout.addWidget(buttons)

    def _add_row(self, name: str, value: str) -> None:
        r = self.table.rowCount()
        self.table.insertRow(r)
        self.table.setItem(r, 0, QTableWidgetItem(name))
        edit = QLineEdit(value)
        edit.setEchoMode(QLineEdit.EchoMode.Normal if getattr(self, "show_values", None) is not None
                         and self.show_values.isChecked() else QLineEdit.EchoMode.Password)
        self.table.setCellWidget(r, 1, edit)

    def _remove_row(self) -> None:
        r = self.table.currentRow()
        if r >= 0:
            self.table.removeRow(r)

    def _toggle_values(self, on: bool) -> None:
        for r in range(self.table.rowCount()):
            self.table.cellWidget(r, 1).setEchoMode(
                QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password)

    def values(self) -> dict[str, str]:
        result = {}
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            name = item.text().strip() if item else ""
            if name:
                result[name] = self.table.cellWidget(r, 1).text()
        return result

    def _accept(self) -> None:
        names = [n.casefold() for n in self.values()]
        if len(names) != len(set(names)):
            QMessageBox.warning(self, self.windowTitle(), "Názvy se nesmí opakovat.")
            return
        bad = [n for n in self.values() if not n.replace("_", "").replace("-", "").isalnum()]
        if bad:
            QMessageBox.warning(self, self.windowTitle(),
                                "Název může obsahovat jen písmena, číslice, _ a -: " + ", ".join(bad))
            return
        self.accept()


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, shared: sc.SharedState | None = None,
                 parent: QWidget | None = None, workspace_name: str = "",
                 folder_check: Callable[[str, str], str | None] | None = None):
        super().__init__(parent)
        self.setWindowTitle(f"Nastavení – záložka {workspace_name}" if workspace_name else "Nastavení")
        self.setMinimumWidth(680)
        self.settings = settings
        # kolize složek s jinými záložkami: (síť, místní) -> popis chyby nebo None
        self.folder_check = folder_check
        # stav společné konfigurace na síti; při úpravě (tlačítko Upravit) se po OK uloží na síť
        self.shared = shared if shared is not None else sc.probe(settings.server_root)
        self.editing_shared = False
        self.exported = False  # během dialogu vznikla / změnila se konfigurace na síti

        self.server = QLineEdit(settings.server_root)
        self.server.setPlaceholderText(r"\\server\projekty")
        self.local = QLineEdit(settings.local_root)
        self.local.setPlaceholderText(r"C:\Projekty")

        self.backup_enabled = QCheckBox("Zálohovat přepisované soubory na serveru")
        self.backup_enabled.setChecked(settings.backup_enabled)
        self.backup_count = QSpinBox()
        self.backup_count.setRange(1, sc.BACKUP_COUNT_MAX)
        self.backup_count.setValue(settings.backup_count)
        self.backup_count.setSuffix(" verzí")
        self.backup_enabled.toggled.connect(lambda _: self._update_shared_ui())

        self.delete_local = QCheckBox("Po deaktivaci smazat místní kopii projektu")
        self.delete_local.setChecked(settings.delete_local_after_deactivate)

        self.threshold = QSpinBox()
        self.threshold.setRange(30, 95)
        self.threshold.setSingleStep(5)
        self.threshold.setSuffix(" %")
        self.threshold.setValue(round(settings.rename_threshold * 100))

        self.ignore = QLineEdit("; ".join(settings.ignore_patterns))

        # archivace dokončených projektů (společné nastavení)
        self.archive_root = QLineEdit(settings.archive_root)
        self.archive_root.setPlaceholderText("prázdné = archivace vypnutá")
        self.archive_root.setToolTip("Kam se ukládají archivované projekty (zip). Zadejte cestu "
                                     "\\\\server\\… nebo cestu vzhledem k síťové složce s projekty "
                                     "(např. ..\\Archiv) – písmeno disku má každý připojené jinak.")
        self.archive_keep = QSpinBox()
        self.archive_keep.setRange(0, sc.BACKUP_COUNT_MAX)
        self.archive_keep.setValue(settings.archive_keep_backups)
        self.archive_keep.setSuffix(" záloh")
        self.archive_keep.setToolTip("Kolik posledních záloh každého souboru se při archivaci zachová "
                                     "(koš projektu se vysype celý).")
        self.archivers = QLineEdit("; ".join(settings.archivers))
        self.archivers.setPlaceholderText("loginy oddělené středníkem – prázdné = nikdo")
        self.archivers.setToolTip("Kdo smí archivovat projekty a vracet je z archivu.")

        self.check_chars = QCheckBox("Kontrolovat nepovolené znaky v názvech souborů a složek")
        self.check_chars.setToolTip("Česká diakritika, mezera a znaky ( ) & , . ; = ^ "
                                    "(tečka před příponou souboru je povolená).\n"
                                    "Projekt s takovými názvy nelze aktivovat ani odevzdat.\n"
                                    "Název projektu se kontroluje vždy.")
        self.check_chars.setChecked(settings.check_forbidden_chars)

        self.skip_unlocked = QCheckBox("Při aktivaci nestahovat neaktivované soubory (výchozí stav volby)")
        self.skip_unlocked.setToolTip(
            "Výchozí stav zaškrtávátka „Nestahovat neaktivované soubory“ v dialogu aktivace;\n"
            "při každé aktivaci ho lze změnit. Zapnuto = stáhnou se jen zamčené soubory.")
        self.skip_unlocked.setChecked(settings.skip_unlocked_download)

        self.nickname = QLineEdit(settings.nickname)
        self.nickname.setPlaceholderText("prázdné = jméno z Windows")
        self.nickname.setToolTip("Jak vás uvidí ostatní u poznámek a při výběru adresáta poznámky.")

        self.tree_expanded = QCheckBox("Rozbalovat složky ve stromu projektu")
        self.tree_expanded.setToolTip(
            "Výchozí stav složek ve stromu pod seznamem projektů (po klepnutí na projekt a u nově\n"
            "přidaných složek). Složky, které sami rozbalíte nebo sbalíte, zůstanou tak i po obnovení.")
        self.tree_expanded.setChecked(settings.tree_expanded)

        self.warn_exit = QCheckBox("Při ukončení upozornit na aktivované projekty")
        self.warn_exit.setChecked(settings.warn_active_on_exit)

        auto_on = settings.auto_refresh_seconds > 0
        self.auto_refresh = QCheckBox("Automaticky obnovovat seznam projektů")
        self.auto_refresh.setToolTip("Vypnuto: seznam se obnoví při spuštění, tlačítkem Obnovit (F5) "
                                     "a po klepnutí na projekt (jen jeho řádek).")
        self.auto_refresh.setChecked(auto_on)
        self.refresh_seconds = QSpinBox()
        self.refresh_seconds.setRange(AUTO_REFRESH_MIN, 24 * 3600)
        self.refresh_seconds.setSingleStep(30)
        self.refresh_seconds.setSuffix(" s")
        self.refresh_seconds.setValue(settings.auto_refresh_seconds if auto_on else 300)
        self.refresh_seconds.setEnabled(auto_on)
        self.auto_refresh.toggled.connect(self.refresh_seconds.setEnabled)

        form = QFormLayout()
        form.addRow("Síťová složka s projekty:", self._with_browse(self.server))
        form.addRow("Místní složka:", self._with_browse(self.local))
        self.tab_color = QComboBox()
        for key, (label, light, dark) in TAB_COLORS.items():
            self.tab_color.addItem(_color_icon(light), label, key)
        self.tab_color.setCurrentIndex(max(self.tab_color.findData(settings.tab_color), 0))
        self.tab_color.setToolTip("Barva pozadí záložky – ve světlém režimu světlý odstín, "
                                  "v tmavém tmavý (text zůstane čitelný).")
        form.addRow("Barva záložky:", self.tab_color)
        self.server.editingFinished.connect(self._server_changed)

        # --- společná nastavení (mohou se řídit souborem spm_config.json na síti) ---
        shared_box = QGroupBox(f"Společné nastavení ({sc.CONFIG_NAME})")
        shared_form = QFormLayout(shared_box)
        shared_form.addRow(self.backup_enabled)
        shared_form.addRow("Počet ponechaných záloh:", self.backup_count)
        hint = QLabel("Zálohy se ukládají do <i>.spm\\backup</i> v projektu jako <i>soubor.backup01</i>, "
                      "<i>.backup02</i>, … Starší než nastavený počet se mažou.")
        hint.setWordWrap(True)
        shared_form.addRow(hint)
        shared_form.addRow("Ignorované soubory:", self.ignore)
        shared_form.addRow(self.check_chars)
        shared_form.addRow("Archivní složka:", self.archive_root)
        shared_form.addRow("Při archivaci ponechat:", self.archive_keep)
        shared_form.addRow("Archivovat smí:", self.archivers)
        self.shared_info = QLabel()
        self.shared_info.setWordWrap(True)
        self.shared_info.setTextFormat(Qt.TextFormat.RichText)
        shared_form.addRow(self.shared_info)
        self.btn_edit_shared = QPushButton("Upravit síťovou konfiguraci")
        self.btn_edit_shared.setToolTip("Odemkne společná nastavení; po stisknutí OK se uloží na síť "
                                        "a budou platit pro všechny uživatele.")
        self.btn_edit_shared.clicked.connect(self._edit_shared)
        self.btn_export = QPushButton("Exportovat na síť…")
        self.btn_export.setToolTip(f"Uloží společná nastavení do {sc.CONFIG_NAME} v síťové složce "
                                   "s projekty – budou platit pro všechny uživatele.")
        self.btn_export.clicked.connect(self._export)
        shared_buttons = QHBoxLayout()
        shared_buttons.addWidget(self.btn_edit_shared)
        shared_buttons.addWidget(self.btn_export)
        shared_buttons.addStretch()
        shared_form.addRow(shared_buttons)
        form.addRow(shared_box)

        form.addRow(QLabel("<b>Osobní nastavení</b> – platí pro všechny záložky"))
        form.addRow("Přezdívka:", self.nickname)
        form.addRow(self.delete_local)
        form.addRow("Min. shoda pro návrh přejmenování:", self.threshold)
        form.addRow(self.skip_unlocked)
        form.addRow(self.tree_expanded)
        form.addRow(self.warn_exit)
        form.addRow(self.auto_refresh)
        self.secrets = dict(settings.secrets)
        self.btn_secrets = QPushButton()
        self.btn_secrets.setToolTip("Hodnoty pro skripty – {SECRET:název}, např. API klíče pro POST.\n"
                                    "Jsou uložené jen v tomto počítači; do záznamu běhu se nevypíšou.")
        self.btn_secrets.clicked.connect(self._edit_secrets)
        self._update_secrets_button()
        refresh_row = QWidget()
        refresh_lay = QHBoxLayout(refresh_row)
        refresh_lay.setContentsMargins(0, 0, 0, 0)
        refresh_lay.addWidget(self.refresh_seconds)
        refresh_lay.addWidget(QLabel(f"(min. {AUTO_REFRESH_MIN} s)"))
        refresh_lay.addStretch()
        form.addRow("Interval obnovení:", refresh_row)
        secrets_row = QWidget()
        secrets_lay = QHBoxLayout(secrets_row)
        secrets_lay.setContentsMargins(0, 0, 0, 0)
        secrets_lay.addWidget(self.btn_secrets)
        secrets_lay.addStretch()
        form.addRow("Tajné hodnoty pro skripty:", secrets_row)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Zrušit")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)
        self._show_shared_values()
        self._update_shared_ui()
        # výška podle obsahu při dané šířce (zalomené texty se jinak ořezávají)
        self.resize(self.minimumWidth(), layout.totalHeightForWidth(self.minimumWidth())
                    if layout.hasHeightForWidth() else self.sizeHint().height())

    # --- společná konfigurace ---------------------------------------------------

    def _shared_widgets(self) -> list[QWidget]:
        return [self.backup_enabled, self.backup_count, self.ignore, self.check_chars,
                self.archive_root, self.archive_keep, self.archivers]

    def _shared_values(self) -> dict:
        return {
            "backup_enabled": self.backup_enabled.isChecked(),
            "backup_count": self.backup_count.value(),
            "ignore_patterns": [p.strip() for p in self.ignore.text().split(";") if p.strip()],
            "check_forbidden_chars": self.check_chars.isChecked(),
            "archive_root": self.archive_root.text().strip(),
            "archive_keep_backups": self.archive_keep.value(),
            "archivers": [u.strip() for u in self.archivers.text().replace(",", ";").split(";") if u.strip()],
        }

    def _show_shared_values(self) -> None:
        """Platná konfigurace ze sítě -> zobrazit její hodnoty (chybějící zůstanou místní)."""
        if self.shared.status != sc.OK:
            return
        v = self.shared.values
        if "backup_enabled" in v:
            self.backup_enabled.setChecked(v["backup_enabled"])
        if "backup_count" in v:
            self.backup_count.setValue(v["backup_count"])
        if "ignore_patterns" in v:
            self.ignore.setText("; ".join(v["ignore_patterns"]))
        if "check_forbidden_chars" in v:
            self.check_chars.setChecked(v["check_forbidden_chars"])
        if "archive_root" in v:
            self.archive_root.setText(v["archive_root"])
        if "archive_keep_backups" in v:
            self.archive_keep.setValue(v["archive_keep_backups"])
        if "archivers" in v:
            self.archivers.setText("; ".join(v["archivers"]))

    def _update_shared_ui(self) -> None:
        s = self.shared

        def locked(key: str) -> bool:
            """Zamčené je jen nastavení, které síťový soubor opravdu obsahuje (chybějící je místní)."""
            if self.editing_shared or not s.active:
                return False
            return s.status == sc.ERROR or key in s.values

        widgets = {"backup_enabled": self.backup_enabled, "backup_count": self.backup_count,
                   "ignore_patterns": self.ignore, "check_forbidden_chars": self.check_chars,
                   "archive_root": self.archive_root, "archive_keep_backups": self.archive_keep,
                   "archivers": self.archivers}
        for key, w in widgets.items():
            w.setEnabled(not locked(key))
        self.backup_count.setEnabled(not locked("backup_count") and self.backup_enabled.isChecked())
        path = html.escape(str(s.path)) if s.path else ""
        if self.editing_shared:
            text = f"Úpravy se po stisknutí OK uloží do <b>{path}</b> a budou platit pro všechny uživatele."
        elif s.status == sc.OK:
            text = f"Řízeno společnou konfigurací na síti: <b>{path}</b>"
            if s.read_only:
                text += "<br>Soubor je jen pro čtení – nastavení nelze změnit."
        elif s.status == sc.ERROR:
            text = (f"Soubor <b>{path}</b> je chybný: {html.escape(s.error)}<br>"
                    "Platí naposledy převzaté hodnoty.")
        elif s.status == sc.UNAVAILABLE:
            text = "Síťová složka není dostupná – nastavení je místní."
        elif path:
            text = ("Nastavení je místní. Tlačítkem <i>Exportovat na síť</i> ho lze uložit do "
                    f"<b>{path}</b> – pak bude platit pro všechny uživatele.")
        else:
            text = "Nastavení je místní."
        self.shared_info.setText(text)
        self.btn_edit_shared.setVisible(s.active)
        self.btn_edit_shared.setEnabled(not s.read_only and not self.editing_shared)
        self.btn_export.setVisible(not s.active)
        self.btn_export.setEnabled(s.status == sc.NONE)

    def _set_shared(self, state: sc.SharedState) -> None:
        self.shared = state
        self.editing_shared = False
        self._show_shared_values()
        self._update_shared_ui()

    def _server_changed(self) -> None:
        """Jiná síťová složka -> jiný (nebo žádný) soubor se společnou konfigurací."""
        path = sc.config_path(self.server.text().strip())
        if path != self.shared.path:
            self._set_shared(sc.probe(self.server.text().strip()))

    def _update_secrets_button(self) -> None:
        self.btn_secrets.setText(f"Upravit… ({len(self.secrets)})")

    def _edit_secrets(self) -> None:
        dlg = SecretsDialog(self.secrets, self)
        if dlg.exec():
            self.secrets = dlg.values()
            self._update_secrets_button()
        dlg.deleteLater()

    def _edit_shared(self) -> None:
        state = sc.probe(self.server.text().strip())  # mohl se mezitím změnit
        if state.stamp != self.shared.stamp or state.read_only:
            self._set_shared(state)
        if not state.active or state.read_only:
            return
        self.editing_shared = True
        self._update_shared_ui()

    def _export(self) -> None:
        server = self.server.text().strip()
        state = sc.probe(server)
        if state.active:
            QMessageBox.information(self, "Nastavení",
                                    f"Na síti už společná konfigurace existuje:\n{state.path}")
            self._set_shared(state)
            return
        if state.status != sc.NONE:
            QMessageBox.warning(self, "Nastavení", f"Síťová složka není dostupná:\n{server}")
            return
        answer = QMessageBox.question(
            self, "Exportovat na síť",
            f"Uložit společná nastavení do\n{state.path}?\n\n"
            "Budou platit pro všechny uživatele a místně je pak nepůjde měnit.")
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            sc.write(server, self._shared_values())
        except sc.SharedConfigError as e:
            QMessageBox.warning(self, "Exportovat na síť", str(e))
            return
        self.exported = True
        self._set_shared(sc.probe(server))

    def _with_browse(self, edit: QLineEdit) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        btn = QPushButton("Procházet…")

        def browse() -> None:
            path = QFileDialog.getExistingDirectory(self, "Vyberte složku", edit.text())
            if path:
                edit.setText(str(Path(path)))
                edit.editingFinished.emit()

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
        conflict = self.folder_check(server, local) if self.folder_check else None
        if conflict:
            QMessageBox.warning(self, "Nastavení", conflict)
            return
        self._server_changed()  # síťová složka změněná bez opuštění pole
        if self.editing_shared:
            try:
                sc.write(server, self._shared_values())
            except sc.SharedConfigError as e:
                QMessageBox.warning(self, "Uložit na síť", str(e))
                return
            self.exported = True
        s = self.settings
        s.server_root, s.local_root = server, local
        s.tab_color = self.tab_color.currentData() or ""
        # zamčená pole ukazují hodnoty ze sítě (nebo naposledy převzaté) -> převzetí je v pořádku
        sc.apply(s, self._shared_values())
        s.delete_local_after_deactivate = self.delete_local.isChecked()
        s.rename_threshold = self.threshold.value() / 100
        s.skip_unlocked_download = self.skip_unlocked.isChecked()
        s.warn_active_on_exit = self.warn_exit.isChecked()
        s.tree_expanded = self.tree_expanded.isChecked()
        s.nickname = self.nickname.text().strip()
        s.secrets = dict(self.secrets)
        s.auto_refresh_seconds = (self.refresh_seconds.value() if self.auto_refresh.isChecked()
                                  else AUTO_REFRESH_OFF)
        s.save()
        self.accept()
