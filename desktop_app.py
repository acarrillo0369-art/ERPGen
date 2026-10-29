"""Small native desktop interface for the ERPGen S/4HANA manufacturing generator."""
from __future__ import annotations

import sys
import traceback
from datetime import date
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal, QDate
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDateEdit, QFileDialog, QFormLayout,
    QFrame, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPushButton, QSpinBox, QVBoxLayout, QWidget, QProgressBar
)

from erpgen.config import load_config
from erpgen.layers import build_layers
from erpgen.outputs import write_run
from erpgen.pipeline import generate

APP_DIR = Path(__file__).resolve().parent
PROFILE = APP_DIR / "configs" / "manufacturing_v0.yaml"


class GeneratorWorker(QObject):
    finished = Signal(dict)
    failed = Signal(str)
    status = Signal(str)

    def __init__(self, seed: int, start: date, end: date, output: Path,
                 layer: str, internal: bool):
        super().__init__()
        self.seed, self.start, self.end = seed, start, end
        self.output, self.layer, self.internal = output, layer, internal

    def run(self):
        try:
            self.status.emit("Loading manufacturing profile...")
            cfg = load_config(PROFILE, {
                "seed": self.seed,
                "start_date": self.start.isoformat(),
                "end_date": self.end.isoformat(),
            })
            self.status.emit("Simulating ERP activity...")
            result = generate(cfg)
            self.status.emit("Building SAP tables and running validations...")
            layers = build_layers(result)
            self.status.emit("Writing output files...")
            run_dir = write_run(
                result, layers, self.output,
                which=self.layer, internal=self.internal
            )
            stats = result.report.stats
            self.finished.emit({
                "run_dir": str(run_dir),
                "events": stats.get("events", 0),
                "journal_entries": stats.get("journal_entries", 0),
                "raw_tables": len(layers.raw.tables),
                "raw_rows": sum(len(v) for v in layers.raw.tables.values()),
                "readable_tables": len(layers.readable),
                "readable_rows": sum(len(v) for v in layers.readable.values()),
                "canonical_ok": result.report.ok,
                "spec_ok": not layers.spec_problems,
                "raw_ok": not layers.raw_findings,
                "recon_ok": not layers.recon_findings,
                "issues": [f"{x.check} {x.ref}: {x.detail}"
                           for x in (layers.raw_findings + layers.recon_findings)[:8]]
                         + [f"Spec: {x}" for x in layers.spec_problems[:8]],
            })
        except Exception:
            self.failed.emit(traceback.format_exc())


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ERPGen | S/4HANA Manufacturing Data Generator")
        self.setMinimumSize(760, 570)
        self.thread = None
        self.worker = None
        self.last_run = None

        root = QWidget()
        self.setCentralWidget(root)
        page = QVBoxLayout(root)
        page.setContentsMargins(26, 22, 26, 22)
        page.setSpacing(14)

        heading = QLabel("ERPGen")
        heading.setObjectName("title")
        subtitle = QLabel("Synthetic SAP S/4HANA manufacturing data")
        subtitle.setObjectName("muted")
        page.addWidget(heading)
        page.addWidget(subtitle)

        card = QFrame()
        card.setObjectName("card")
        form = QFormLayout(card)
        form.setContentsMargins(20, 18, 20, 18)
        form.setVerticalSpacing(13)

        self.profile_label = QLabel("S/4HANA Manufacturing (current profile)")
        form.addRow("Data profile", self.profile_label)

        self.seed = QSpinBox()
        self.seed.setRange(0, 2_147_483_647)
        self.seed.setValue(42)
        form.addRow("Random seed", self.seed)

        self.start_date = QDateEdit()
        self.start_date.setCalendarPopup(True)
        self.start_date.setDisplayFormat("yyyy-MM-dd")
        self.start_date.setDate(QDate(2025, 1, 1))
        form.addRow("Start date", self.start_date)

        self.end_date = QDateEdit()
        self.end_date.setCalendarPopup(True)
        self.end_date.setDisplayFormat("yyyy-MM-dd")
        self.end_date.setDate(QDate(2025, 12, 31))
        form.addRow("End date", self.end_date)

        self.layer = QComboBox()
        self.layer.addItem("Both raw + readable", "both")
        self.layer.addItem("Raw SAP tables only", "raw")
        self.layer.addItem("Readable tables only", "readable")
        form.addRow("Output layers", self.layer)

        self.internal = QCheckBox("Also export internal event log")
        form.addRow("Advanced", self.internal)

        output_row = QHBoxLayout()
        self.output = QLineEdit(str(APP_DIR / "output"))
        browse = QPushButton("Browse…")
        browse.clicked.connect(self.choose_output)
        output_row.addWidget(self.output, 1)
        output_row.addWidget(browse)
        form.addRow("Output folder", output_row)
        page.addWidget(card)

        actions = QHBoxLayout()
        self.generate_btn = QPushButton("Generate dataset")
        self.generate_btn.setObjectName("primary")
        self.generate_btn.clicked.connect(self.start_generation)
        actions.addWidget(self.generate_btn)
        self.open_btn = QPushButton("Open last output")
        self.open_btn.setEnabled(False)
        self.open_btn.clicked.connect(self.open_output)
        actions.addWidget(self.open_btn)
        actions.addStretch()
        page.addLayout(actions)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        page.addWidget(self.progress)

        self.status = QLabel("Ready. Configure your run and generate a dataset.")
        self.status.setObjectName("muted")
        page.addWidget(self.status)

        self.results = QLabel("No dataset generated in this session.")
        self.results.setObjectName("results")
        self.results.setWordWrap(True)
        self.results.setTextInteractionFlags(self.results.textInteractionFlags())
        page.addWidget(self.results, 1)

        self.setStyleSheet("""
            QMainWindow, QWidget { background: #f4f6f9; color: #182230;
                font-family: 'Segoe UI'; font-size: 10pt; }
            QLabel#title { font-size: 25pt; font-weight: 700; color: #172b4d; }
            QLabel#muted { color: #64748b; }
            QFrame#card { background: white; border: 1px solid #dce3ec;
                border-radius: 10px; }
            QLineEdit, QSpinBox, QDateEdit, QComboBox {
                background: white; border: 1px solid #cbd5e1;
                border-radius: 5px; padding: 7px; min-height: 22px; }
            QPushButton { background: white; border: 1px solid #cbd5e1;
                border-radius: 6px; padding: 9px 15px; }
            QPushButton#primary { background: #2457c5; color: white;
                border: none; font-weight: 600; }
            QPushButton:disabled { color: #94a3b8; }
            QLabel#results { background: white; border: 1px solid #dce3ec;
                border-radius: 8px; padding: 15px; }
            QProgressBar { border: 0; background: #e2e8f0; height: 7px;
                border-radius: 3px; text-align: center; }
            QProgressBar::chunk { background: #2457c5; border-radius: 3px; }
        """)

    def choose_output(self):
        folder = QFileDialog.getExistingDirectory(self, "Choose output folder", self.output.text())
        if folder:
            self.output.setText(folder)

    def start_generation(self):
        start = self.start_date.date().toPython()
        end = self.end_date.date().toPython()
        if end < start:
            QMessageBox.warning(self, "Invalid date range", "End date must be on or after start date.")
            return
        out = Path(self.output.text()).expanduser()
        if not self.output.text().strip():
            QMessageBox.warning(self, "Missing output folder", "Choose an output folder.")
            return

        self.generate_btn.setEnabled(False)
        self.open_btn.setEnabled(False)
        self.progress.show()
        self.results.setText("Generation is running. Larger date ranges may take longer.")
        self.status.setText("Starting...")
        self.thread = QThread()
        self.worker = GeneratorWorker(
            self.seed.value(), start, end, out,
            self.layer.currentData(), self.internal.isChecked()
        )
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.status.connect(self.status.setText)
        self.worker.finished.connect(self.on_finished)
        self.worker.failed.connect(self.on_failed)
        self.worker.finished.connect(self.thread.quit)
        self.worker.failed.connect(self.thread.quit)
        self.thread.finished.connect(self.cleanup_worker)
        self.thread.start()

    def on_finished(self, data):
        self.last_run = data["run_dir"]
        self.progress.hide()
        self.generate_btn.setEnabled(True)
        self.open_btn.setEnabled(True)
        checks = [
            ("Canonical validation", data["canonical_ok"]),
            ("SAP table spec checks", data["spec_ok"]),
            ("Raw data checks", data["raw_ok"]),
            ("Reconciliation", data["recon_ok"]),
        ]
        lines = [
            "Generation complete",
            f"Events: {data['events']:,}    Journal entries: {data['journal_entries']:,}",
            f"Raw layer: {data['raw_rows']:,} rows across {data['raw_tables']} tables",
            f"Readable layer: {data['readable_rows']:,} rows across {data['readable_tables']} tables",
            "",
            *[f"{'PASS' if ok else 'REVIEW'}  {name}" for name, ok in checks],
            "",
            f"Output: {self.last_run}",
        ]
        if data["issues"]:
            lines += ["", "Findings:", *data["issues"]]
        self.results.setText("\n".join(lines))
        self.status.setText("Finished.")

    def on_failed(self, details):
        self.progress.hide()
        self.generate_btn.setEnabled(True)
        self.status.setText("Generation failed.")
        self.results.setText(details)
        QMessageBox.critical(self, "Generation failed", details[-2500:])

    def cleanup_worker(self):
        self.worker = None
        self.thread = None

    def open_output(self):
        if self.last_run and Path(self.last_run).exists():
            os_path = str(Path(self.last_run).resolve())
            import os
            os.startfile(os_path)


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
