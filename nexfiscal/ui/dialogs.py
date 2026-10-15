"""Diálogos: erros de validação, configurações e gerenciamento de portais."""
from __future__ import annotations

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMessageBox,
                               QPlainTextEdit, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem,
                               QVBoxLayout)

from ..app import AppContext
from ..database.repositories import CompanyRepository
from ..exceptions import ConfigurationError
from ..memory.portal_memory_manager import PortalMemoryManager
from ..portal.portal_registry import PortalRegistry


class TextDialog(QDialog):
    def __init__(self, title: str, text: str, parent=None) -> None:  # type: ignore[no-untyped-def]
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(560, 420)
        box = QPlainTextEdit(text)
        box.setReadOnly(True)
        box.setStyleSheet("font-family: monospace;")
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(box)
        lay.addWidget(buttons)


class PortalsDialog(QDialog):
    """Cadastro de portais (cidade -> URL) e limpeza da memória aprendida."""

    def __init__(self, ctx: AppContext, parent=None) -> None:  # type: ignore[no-untyped-def]
        super().__init__(parent)
        self.ctx = ctx
        self.registry, self.memory = PortalRegistry(ctx.db), PortalMemoryManager(ctx.db)
        self.setWindowTitle("Portais cadastrados")
        self.resize(700, 380)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["URL", "Cidades", "Último reconhecimento"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 300)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        add, rem, clr = QPushButton("Adicionar…"), QPushButton("Remover"), QPushButton("Limpar memória do portal")
        add.clicked.connect(self.add)
        rem.clicked.connect(self.remove)
        clr.clicked.connect(self.clear_memory)
        row = QHBoxLayout()
        for b in (add, rem, clr):
            row.addWidget(b)
        row.addStretch()
        lay = QVBoxLayout(self)
        lay.addWidget(self.table)
        lay.addLayout(row)
        self.reload()

    def reload(self) -> None:
        portals = self.registry.list_all()
        self.table.setRowCount(len(portals))
        for i, p in enumerate(portals):
            self.table.setItem(i, 0, QTableWidgetItem(p.url))
            self.table.setItem(i, 1, QTableWidgetItem(", ".join(p.cidades)))
            self.table.setItem(i, 2, QTableWidgetItem(p.ultimo_reconhecimento or "—"))
            self.table.item(i, 0).setData(256, p.id)

    def _selected_id(self) -> int | None:
        r = self.table.currentRow()
        return self.table.item(r, 0).data(256) if r >= 0 else None

    def add(self) -> None:
        cities, ok = QInputDialog.getText(self, "Novo portal", "Cidade(s), separadas por vírgula:")
        if not ok or not cities.strip():
            return
        url, ok = QInputDialog.getText(self, "Novo portal", "URL do portal:")
        if ok and url.strip():
            try:
                self.registry.register([c.strip() for c in cities.split(",") if c.strip()], url)
            except ConfigurationError as exc:
                QMessageBox.warning(self, "URL inválida", exc.message)
            self.reload()

    def remove(self) -> None:
        pid = self._selected_id()
        if pid and QMessageBox.question(self, "Remover", "Remover o portal e a memória aprendida?") == QMessageBox.Yes:
            self.registry.repo.delete(pid)
            self.reload()

    def clear_memory(self) -> None:
        pid = self._selected_id()
        if pid and QMessageBox.question(self, "Limpar memória", "Apagar o que foi aprendido deste portal?") == QMessageBox.Yes:
            self.memory.clear(pid)
            self.reload()


class SettingsDialog(QDialog):
    def __init__(self, ctx: AppContext, parent=None) -> None:  # type: ignore[no-untyped-def]
        super().__init__(parent)
        self.ctx, s = ctx, ctx.settings
        self.setWindowTitle("Configurações")
        self.resize(520, 560)
        self.out = QLineEdit(s.output_root)
        pick = QPushButton("…")
        pick.setMaximumWidth(32)
        pick.clicked.connect(self._pick_folder)
        out_row = QHBoxLayout()
        out_row.addWidget(self.out)
        out_row.addWidget(pick)
        self.comp = QLineEdit(s.competence or "")
        self.comp.setPlaceholderText("MM/AAAA — vazio = mês anterior")
        self.start, self.end = QLineEdit(s.start_date or ""), QLineEdit(s.end_date or "")
        self.start.setPlaceholderText("DD/MM/AAAA (opcional)")
        self.end.setPlaceholderText("DD/MM/AAAA (opcional)")
        self.delay = QDoubleSpinBox()
        self.delay.setRange(0, 30)
        self.delay.setSuffix(" s")
        self.delay.setValue(s.delay_between_ops)
        self.timeout = self._spin(s.timeout_seconds, 2, 600, " s")
        self.captcha_t = self._spin(s.captcha_timeout_seconds, 5, 7200, " s")
        self.retries = self._spin(s.max_retries, 1, 10)
        self.existing = QComboBox()
        self.existing.addItem("Manter arquivos válidos (não baixar de novo)", "skip_valid")
        self.existing.addItem("Sobrescrever", "overwrite")
        self.existing.setCurrentIndex(0 if s.existing_files == "skip_valid" else 1)
        self.browser = QComboBox()
        self.browser.addItems(["chromium", "firefox", "webkit"])
        self.browser.setCurrentText(s.browser)
        self.channel = QLineEdit(s.browser_channel or "")
        self.channel.setPlaceholderText("opcional: chrome, msedge")
        self.headless = QCheckBox("Navegador oculto (impede resolver CAPTCHA)")
        self.headless.setChecked(s.headless)
        self.shots = QCheckBox("Salvar captura de tela em erros (campos mascarados)")
        self.shots.setChecked(s.save_error_screenshots)
        self.assist = QCheckBox("Modo assistido: pedir um clique quando não reconhecer um elemento")
        self.assist.setChecked(s.assisted_recognition)
        self.level = QComboBox()
        self.level.addItems(["DEBUG", "INFO", "WARNING", "ERROR"])
        self.level.setCurrentText(s.log_level)
        form = QFormLayout()
        form.addRow("Pasta de saída:", out_row)
        form.addRow("Competência:", self.comp)
        form.addRow("Data inicial:", self.start)
        form.addRow("Data final:", self.end)
        form.addRow("Intervalo entre operações:", self.delay)
        form.addRow("Tempo máximo de espera:", self.timeout)
        form.addRow("Tempo máximo p/ CAPTCHA:", self.captcha_t)
        form.addRow("Tentativas:", self.retries)
        form.addRow("Arquivos existentes:", self.existing)
        form.addRow("Navegador:", self.browser)
        form.addRow("Canal do navegador:", self.channel)
        form.addRow("", self.headless)
        form.addRow("", self.shots)
        form.addRow("", self.assist)
        form.addRow("Nível de log:", self.level)
        form.addRow("Banco de dados:", QLabel(str(s.db_file)))

        manage = QPushButton("Gerenciar portais e memória…")
        manage.clicked.connect(lambda: PortalsDialog(ctx, self).exec())
        clear_mem = QPushButton("Limpar memória de todos os portais")
        clear_mem.clicked.connect(self._clear_memory)
        clear_cred = QPushButton("Remover credenciais armazenadas")
        clear_cred.clicked.connect(self._clear_credentials)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        for b in (manage, clear_mem, clear_cred):
            lay.addWidget(b)
        lay.addWidget(buttons)

    @staticmethod
    def _spin(value: int, lo: int, hi: int, suffix: str = "") -> QSpinBox:
        sp = QSpinBox()
        sp.setRange(lo, hi)
        sp.setValue(value)
        sp.setSuffix(suffix)
        return sp

    def _pick_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Pasta de saída", self.out.text())
        if folder:
            self.out.setText(folder)

    def _clear_memory(self) -> None:
        if QMessageBox.question(self, "Limpar memória", "Apagar tudo que foi aprendido dos portais?") == QMessageBox.Yes:
            PortalMemoryManager(self.ctx.db).clear()

    def _clear_credentials(self) -> None:
        if QMessageBox.question(self, "Remover credenciais", "Remover todas as senhas armazenadas?") == QMessageBox.Yes:
            n = CompanyRepository(self.ctx.db, self.ctx.cipher).clear_credentials()
            QMessageBox.information(self, "Credenciais", f"{n} credencial(is) removida(s).")

    def _save(self) -> None:
        s = self.ctx.settings
        s.output_root = self.out.text().strip() or s.output_root
        s.competence = self.comp.text().strip() or None
        s.start_date, s.end_date = self.start.text().strip() or None, self.end.text().strip() or None
        s.delay_between_ops, s.timeout_seconds = self.delay.value(), self.timeout.value()
        s.captcha_timeout_seconds, s.max_retries = self.captcha_t.value(), self.retries.value()
        s.existing_files = self.existing.currentData()
        s.browser, s.browser_channel = self.browser.currentText(), self.channel.text().strip() or None
        s.headless, s.save_error_screenshots = self.headless.isChecked(), self.shots.isChecked()
        s.assisted_recognition = self.assist.isChecked()
        s.log_level = self.level.currentText()
        self.ctx.save_settings()
        self.accept()
