"""Janela principal: seleção -> dashboard de execução -> relatório final."""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QApplication, QFileDialog, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit,
                               QProgressBar, QPushButton, QStackedWidget, QVBoxLayout, QWidget, QCheckBox)

from .. import APP_NAME
from ..app import AppContext, bootstrap
from ..database.models import CompanyRow, RunStatus, Stage, StepStatus
from ..database.repositories import CompanyRepository
from ..dates import Competence, Period
from ..events import EventKind, ProgressEvent, RunControl
from ..exceptions import ConfigurationError
from ..portal.portal_registry import PortalRegistry
from ..reports.export import export
from ..spreadsheet.validator import ValidationReport, validate_spreadsheet
from .dialogs import PortalsDialog, SettingsDialog, TextDialog
from .worker import BatchWorker

SYMBOL = {"PENDENTE": "○", "EM_ANDAMENTO": "●", "OK": "✓", "PULADO": "–", "PENDENCIA": "!", "ERRO": "✗"}
SHOWN_STAGES = [Stage.LOGIN, Stage.CAPTCHA, Stage.XML, Stage.LIVRO, Stage.ORGANIZACAO]


class MainWindow(QMainWindow):
    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.registry = PortalRegistry(ctx.db)
        self.validation: ValidationReport | None = None
        self.worker: BatchWorker | None = None
        self.control = RunControl()
        self.last_summary = None
        self.counts = {"ok": 0, "pend": 0, "err": 0}
        self.resize(820, 680)
        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)
        self.stack.addWidget(self._home_page())
        self.stack.addWidget(self._dashboard_page())
        self.stack.addWidget(self._report_page())

    # ------------------------------------------------------------ tela inicial ---
    def _home_page(self) -> QWidget:
        w = QWidget()
        s = self.ctx.settings
        title = QLabel(f"<b>{APP_NAME}</b>")
        title.setAlignment(Qt.AlignCenter)
        self.sheet = QLineEdit()
        self.sheet.setReadOnly(True)
        self.sheet.setPlaceholderText("selecione a planilha .xlsx (CNPJ/USUÁRIO, SENHA, CIDADE)")
        b_sheet = QPushButton("Selecionar…")
        b_sheet.clicked.connect(self.choose_sheet)
        self.out = QLineEdit(s.output_root)
        b_out = QPushButton("Selecionar…")
        b_out.clicked.connect(self.choose_output)
        self.comp = QLineEdit(s.competence or "")
        self.comp.setPlaceholderText(f"{Competence.previous().label} (mês anterior)")
        self.cities = QListWidget()
        self.force = QCheckBox("Reprocessar empresas já concluídas nesta competência")
        self.status = QLabel("Aguardando início")
        b_all, b_sel = QPushButton("PROCESSAR TODAS"), QPushButton("PROCESSAR SELECIONADAS")
        b_resume, b_cfg = QPushButton("RETOMAR PENDENTES"), QPushButton("CONFIGURAÇÕES")
        b_all.clicked.connect(lambda: self.start(selected_only=False))
        b_sel.clicked.connect(lambda: self.start(selected_only=True))
        b_resume.clicked.connect(lambda: self.start(selected_only=False, from_db=True))
        b_cfg.clicked.connect(self.open_settings)
        lay = QVBoxLayout(w)
        lay.addWidget(title)
        for label, edit, btn in (("Planilha:", self.sheet, b_sheet), ("Pasta de saída:", self.out, b_out)):
            lay.addWidget(QLabel(label))
            row = QHBoxLayout()
            row.addWidget(edit)
            row.addWidget(btn)
            lay.addLayout(row)
        lay.addWidget(QLabel("Competência:"))
        lay.addWidget(self.comp)
        lay.addWidget(QLabel("<b>CIDADES ENCONTRADAS</b>"))
        lay.addWidget(self.cities, 1)
        lay.addWidget(self.force)
        row = QHBoxLayout()
        for b in (b_all, b_sel, b_resume, b_cfg):
            row.addWidget(b)
        lay.addLayout(row)
        lay.addWidget(QLabel("Status:"))
        lay.addWidget(self.status)
        return w

    def choose_sheet(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Planilha de empresas", "", "Excel (*.xlsx)")
        if not path:
            return
        self.sheet.setText(path)
        rep = validate_spreadsheet(path, self.registry.known_cities())
        self.validation = rep
        if rep.has_critical:
            self.cities.clear()
            TextDialog("Erros na planilha", rep.format(), self).exec()
            self.status.setText("Planilha com erros críticos — corrija e selecione novamente.")
            return
        if rep.warnings:
            TextDialog("Avisos da planilha", rep.format(), self).exec()
        self.cities.clear()
        for city, rows in rep.cities().items():
            note = "" if self._known(city) else "  (portal não cadastrado)"
            item = QListWidgetItem(f"{city:<28} {len(rows)} empresas{note}")
            item.setData(Qt.UserRole, city)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
            self.cities.addItem(item)
        self.status.setText(f"{len(rep.rows)} empresas em {self.cities.count()} cidades.")

    def _known(self, city: str) -> bool:
        return self.registry.resolve(city) is not None

    def choose_output(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Pasta de saída", self.out.text())
        if folder:
            self.out.setText(folder)

    def open_settings(self) -> None:
        if SettingsDialog(self.ctx, self).exec():
            self.out.setText(self.ctx.settings.output_root)
            self.comp.setText(self.ctx.settings.competence or "")

    # ------------------------------------------------------------- iniciar lote ---
    def start(self, *, selected_only: bool, from_db: bool = False) -> None:
        if self.worker and self.worker.isRunning():
            return
        s = self.ctx.settings
        try:
            period = Period.resolve(self.comp.text().strip() or s.competence, s.start_date, s.end_date)
        except ConfigurationError as exc:
            QMessageBox.warning(self, "Competência", exc.message)
            return
        if from_db:
            rows = CompanyRepository(self.ctx.db, self.ctx.cipher).load_rows()
            if not rows:
                QMessageBox.information(self, "Retomar", "Não há credenciais armazenadas. Selecione a planilha.")
                return
            chosen = {r.cidade for r in rows}
        else:
            if not self.validation or self.validation.has_critical:
                QMessageBox.information(self, "Planilha", "Selecione uma planilha válida primeiro.")
                return
            rows = self.validation.valid_rows
            chosen = {self.cities.item(i).data(Qt.UserRole) for i in range(self.cities.count())
                      if not selected_only or self.cities.item(i).checkState() == Qt.Checked}
        if not chosen:
            QMessageBox.information(self, "Cidades", "Nenhuma cidade selecionada.")
            return
        # portais ausentes: pede a URL (cadastro permanente)
        skipped = self.registry.ensure_portals(sorted(chosen), self._ask_portal)
        chosen -= set(skipped)
        if not chosen:
            return
        s.output_root = self.out.text().strip() or s.output_root
        s.competence = self.comp.text().strip() or None
        self.ctx.save_settings()
        norm = {c.lower() for c in chosen}
        city_filter = {r.cidade_norm for r in rows if r.cidade.lower() in norm}
        total = len([r for r in rows if r.cidade_norm in city_filter])
        self._prepare_dashboard(total, period)
        self.control = RunControl()
        self.worker = BatchWorker(self.ctx, rows, period, city_filter, self.force.isChecked(), self.control)
        self.worker.event.connect(self.on_event)
        self.worker.log_line.connect(self.on_log)
        self.worker.finished_ok.connect(self.on_finished)
        self.worker.failed.connect(self.on_failed)
        self.worker.start()

    def _ask_portal(self, city: str) -> str | None:
        url, ok = QInputDialog.getText(self, "Portal não cadastrado",
                                       f"Portal não cadastrado para:\n\nCidade: {city}\n\nInforme a URL do portal:")
        return url if ok and url.strip() else None

    # --------------------------------------------------------------- dashboard ---
    def _dashboard_page(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel("<h2>PROCESSAMENTO EM ANDAMENTO</h2>"))
        self.d_city, self.d_company = QLabel("-"), QLabel("-")
        self.d_comp, self.d_step = QLabel("-"), QLabel("-")
        for label, widget in (("Cidade:", self.d_city), ("Empresa:", self.d_company),
                            ("Competência:", self.d_comp), ("Etapa atual:", self.d_step)):
            row = QHBoxLayout()
            row.addWidget(QLabel(f"<b>{label}</b>"))
            row.addWidget(widget, 1)
            lay.addLayout(row)
        self.stage_labels: dict[Stage, QLabel] = {}
        for st in SHOWN_STAGES:
            lbl = QLabel()
            self.stage_labels[st] = lbl
            lay.addWidget(lbl)
        self.xml_bar = QProgressBar()
        lay.addWidget(self.xml_bar)
        self.banner = QLabel()
        self.banner.setWordWrap(True)
        self.banner.setStyleSheet("background:#b00020;color:white;padding:10px;font-weight:bold;")
        self.banner.hide()
        lay.addWidget(self.banner)
        self.d_counts, self.d_progress = QLabel(), QLabel()
        lay.addWidget(self.d_progress)
        lay.addWidget(self.d_counts)
        self.b_pause, self.b_cancel = QPushButton("PAUSAR"), QPushButton("CANCELAR")
        self.b_pause.clicked.connect(self.toggle_pause)
        self.b_cancel.clicked.connect(self.cancel)
        row = QHBoxLayout()
        row.addWidget(self.b_pause)
        row.addWidget(self.b_cancel)
        lay.addLayout(row)
        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumBlockCount(5000)
        self.log_box.setStyleSheet("font-family: monospace; font-size: 11px;")
        lay.addWidget(self.log_box, 1)
        return w

    def _prepare_dashboard(self, total: int, period: Period) -> None:
        self.total = total
        self.counts = {"ok": 0, "pend": 0, "err": 0}
        self.log_box.clear()
        self.d_comp.setText(period.competence.label)
        self._reset_stages()
        self._update_counts(0)
        self.banner.hide()
        self.b_pause.setText("PAUSAR")
        self.b_pause.setEnabled(True)
        self.b_cancel.setEnabled(True)
        self.stack.setCurrentIndex(1)

    def _reset_stages(self) -> None:
        for st in SHOWN_STAGES:
            self._set_stage(st, "PENDENTE")
        self.xml_bar.setValue(0)
        self.xml_bar.setMaximum(1)

    def _set_stage(self, st: Stage, status: str, msg: str = "") -> None:
        self.stage_labels[st].setText(f"{st.title:<14} {SYMBOL.get(status, '○')}  {msg}")

    def _update_counts(self, done: int) -> None:
        pending = max(self.total - done, 0)
        self.d_counts.setText(f"Concluídas: {self.counts['ok']}    Com pendência: {self.counts['pend']}    "
                            f"Pendentes: {pending}    Erros: {self.counts['err']}")

    def on_event(self, ev: ProgressEvent) -> None:
        if ev.kind is EventKind.CITY_START:
            self.d_city.setText(ev.city.upper())
        elif ev.kind is EventKind.COMPANY_START:
            self.d_city.setText(ev.city.upper())
            self.d_company.setText(ev.company)
            self.d_progress.setText(f"Empresa: {ev.current}/{ev.total}    Cidade: "
                                    f"{ev.data.get('city_index', '?')}/{ev.data.get('city_total', '?')}")
            self._reset_stages()
            self.banner.hide()
        elif ev.kind is EventKind.STAGE and ev.stage in self.stage_labels:
            self._set_stage(ev.stage, ev.status, ev.message)
            if ev.status == StepStatus.EM_ANDAMENTO.value:
                self.d_step.setText(ev.message or ev.stage.title)
            if ev.stage is Stage.CAPTCHA and ev.status in ("OK", "PULADO"):
                self.banner.hide()
        elif ev.kind is EventKind.CAPTCHA:
            self.banner.setStyleSheet("background:#b00020;color:white;padding:10px;font-weight:bold;")
            self.banner.setText(ev.message.replace(" | ", "\n"))
            self.banner.show()
            self._set_stage(Stage.CAPTCHA, "EM_ANDAMENTO", "aguardando resolução manual")
            self.d_step.setText("Aguardando CAPTCHA")
            QApplication.alert(self)
            self.raise_()
        elif ev.kind is EventKind.ASSIST:
            self.banner.setStyleSheet("background:#0b57d0;color:white;padding:10px;font-weight:bold;")
            self.banner.setText(ev.message)
            self.banner.show()
            self.d_step.setText("Aguardando seu clique no navegador")
            QApplication.alert(self)
        elif ev.kind is EventKind.PROGRESS:
            self.xml_bar.setMaximum(max(ev.total, 1))
            self.xml_bar.setValue(ev.current)
        elif ev.kind is EventKind.COMPANY_END:
            key = {"CONCLUIDO": "ok", "CONCLUIDO_COM_PENDENCIA": "pend", "ERRO": "err"}.get(ev.status)
            if key:
                self.counts[key] += 1
            self._update_counts(ev.current)

    def on_log(self, line: str, level: int) -> None:
        self.log_box.appendPlainText(line)

    def toggle_pause(self) -> None:
        if self.control.is_paused:
            self.control.resume()
            self.b_pause.setText("PAUSAR")
        else:
            self.control.pause()
            self.b_pause.setText("CONTINUAR")

    def cancel(self) -> None:
        if QMessageBox.question(self, "Cancelar", "Cancelar o processamento? O que foi concluído fica salvo.") == QMessageBox.Yes:
            self.control.cancel()
            self.b_cancel.setEnabled(False)

    def on_failed(self, message: str) -> None:
        QMessageBox.critical(self, "Erro", message)
        self.stack.setCurrentIndex(0)

    def on_finished(self, summary) -> None:  # type: ignore[no-untyped-def]
        self.last_summary = summary
        self.report_box.setPlainText(summary.report.to_text())
        self.b_retry.setEnabled(summary.report.erros + summary.report.pendencias > 0)
        self.stack.setCurrentIndex(2)
        self.status.setText("Cancelado — use 'Retomar pendentes'." if summary.cancelled else "Lote finalizado.")

    # --------------------------------------------------------------- relatório ---
    def _report_page(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.report_box = QPlainTextEdit()
        self.report_box.setReadOnly(True)
        self.report_box.setStyleSheet("font-family: monospace;")
        lay.addWidget(self.report_box, 1)
        row = QHBoxLayout()
        for ext in ("XLSX", "CSV", "PDF"):
            b = QPushButton(f"Exportar {ext}")
            b.clicked.connect(lambda _=False, e=ext.lower(): self.export_report(e))
            row.addWidget(b)
        lay.addLayout(row)
        self.b_retry = QPushButton("Tentar novamente empresas com erro/pendência")
        self.b_retry.clicked.connect(lambda: self.start(selected_only=False, from_db=True))
        back = QPushButton("Voltar")
        back.clicked.connect(lambda: self.stack.setCurrentIndex(0))
        lay.addWidget(self.b_retry)
        lay.addWidget(back)
        return w

    def export_report(self, ext: str) -> None:
        if not self.last_summary:
            return
        default = str(Path(self.ctx.settings.output_root) / f"relatorio_{self.last_summary.report.competencia.replace('/', '-')}.{ext}")
        path, _ = QFileDialog.getSaveFileName(self, "Exportar relatório", default, f"*.{ext}")
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            try:
                QMessageBox.information(self, "Exportado", str(export(self.last_summary.report, Path(path))))
            except OSError as exc:
                QMessageBox.warning(self, "Erro ao exportar", str(exc))

    def closeEvent(self, e) -> None:  # type: ignore[no-untyped-def]
        if self.worker and self.worker.isRunning():
            if QMessageBox.question(self, "Sair", "Há um processamento em andamento. Cancelar e sair?") != QMessageBox.Yes:
                e.ignore()
                return
            self.control.cancel()
            self.worker.wait(15000)
        e.accept()


def run_gui() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    ctx = bootstrap()
    win = MainWindow(ctx)
    win.show()
    code = app.exec()
    ctx.db.close()
    return code
