from __future__ import annotations

import html
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
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
    QVBoxLayout,
    QWidget,
)

from .. import shared_config as sc
from ..config import AUTO_REFRESH_MIN, AUTO_REFRESH_OFF, Settings


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, shared: sc.SharedState | None = None,
                 parent: QWidget | None = None, workspace_name: str = "",
                 folder_check: Callable[[str, str], str | None] | None = None):
        super().__init__(parent)
        self.setWindowTitle(f"Nastavení – záložka {workspace_name}" if workspace_name else "Nastavení")
        self.setMinimumWidth(560)
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

        self.mirror_enabled = QCheckBox("Zrcadlit z tohoto počítače (pravidla v okně Zrcadlení)")
        self.mirror_enabled.setToolTip(
            "Po odevzdání se soubory zrcadlí podle pravidel do jiných síťových složek.\n"
            "Vypněte, pokud do cílové složky nemáte právo zapisovat (sdílená pravidla platí\n"
            "pro všechny, kdo mají zdroj i cíl jako záložky).")
        self.mirror_enabled.setChecked(settings.mirror_enabled)

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
        form.addRow(self.mirror_enabled)
        form.addRow(self.auto_refresh)
        refresh_row = QWidget()
        refresh_lay = QHBoxLayout(refresh_row)
        refresh_lay.setContentsMargins(0, 0, 0, 0)
        refresh_lay.addWidget(self.refresh_seconds)
        refresh_lay.addWidget(QLabel(f"(min. {AUTO_REFRESH_MIN} s)"))
        refresh_lay.addStretch()
        form.addRow("Interval obnovení:", refresh_row)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Zrušit")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)
        self._show_shared_values()
        self._update_shared_ui()

    # --- společná konfigurace ---------------------------------------------------

    def _shared_widgets(self) -> list[QWidget]:
        return [self.backup_enabled, self.backup_count, self.ignore, self.check_chars]

    def _shared_values(self) -> dict:
        return {
            "backup_enabled": self.backup_enabled.isChecked(),
            "backup_count": self.backup_count.value(),
            "ignore_patterns": [p.strip() for p in self.ignore.text().split(";") if p.strip()],
            "check_forbidden_chars": self.check_chars.isChecked(),
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

    def _update_shared_ui(self) -> None:
        s = self.shared

        def locked(key: str) -> bool:
            """Zamčené je jen nastavení, které síťový soubor opravdu obsahuje (chybějící je místní)."""
            if self.editing_shared or not s.active:
                return False
            return s.status == sc.ERROR or key in s.values

        widgets = {"backup_enabled": self.backup_enabled, "backup_count": self.backup_count,
                   "ignore_patterns": self.ignore, "check_forbidden_chars": self.check_chars}
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
        # zamčená pole ukazují hodnoty ze sítě (nebo naposledy převzaté) -> převzetí je v pořádku
        sc.apply(s, self._shared_values())
        s.delete_local_after_deactivate = self.delete_local.isChecked()
        s.rename_threshold = self.threshold.value() / 100
        s.skip_unlocked_download = self.skip_unlocked.isChecked()
        s.warn_active_on_exit = self.warn_exit.isChecked()
        s.tree_expanded = self.tree_expanded.isChecked()
        s.nickname = self.nickname.text().strip()
        s.mirror_enabled = self.mirror_enabled.isChecked()
        s.auto_refresh_seconds = (self.refresh_seconds.value() if self.auto_refresh.isChecked()
                                  else AUTO_REFRESH_OFF)
        s.save()
        self.accept()
