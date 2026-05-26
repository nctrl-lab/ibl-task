"""GUI to calibrate the reward valve.

Each row is one target volume. Dispense N pulses, measure externally (pipette
N=1 for one drop or N=50-100 for cumulative), nudge ms, repeat. Save writes
to ~/.config/ibl-task/calibration.json and reloads on next launch.
"""
import json
import sys
from pathlib import Path

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ibl.gui import _apply_dark_palette
from ibl.io import FakeTeensy, Teensy

DEFAULT_TARGETS_UL = (5.0, 10.0, 15.0, 20.0)
CONFIG_PATH = Path.home() / ".config/ibl-task/calibration.json"


class TargetRow(QWidget):
    """One calibration row. Persisted fields: target_ul, ms, n_pulses."""

    def __init__(self, window, target_ul=10.0, ms=100, n_pulses=100):
        super().__init__()
        self._win = window
        self._timer = QTimer()
        self._timer.timeout.connect(self._pulse)
        self._remaining = 0
        self._total = 0

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        self.target_ul = QDoubleSpinBox()
        self.target_ul.setRange(0.1, 100.0)
        self.target_ul.setDecimals(1)
        self.target_ul.setSuffix(" µL target")
        self.target_ul.setValue(target_ul)
        self.target_ul.setMinimumWidth(120)
        row.addWidget(self.target_ul)

        self.ms = QSpinBox()
        self.ms.setRange(1, 1000)
        self.ms.setSuffix(" ms")
        self.ms.setValue(ms)
        self.ms.setMinimumWidth(90)
        row.addWidget(self.ms)

        self.n = QSpinBox()
        self.n.setRange(1, 1000)
        self.n.setSuffix(" pulses")
        self.n.setValue(n_pulses)
        self.n.setMinimumWidth(110)
        row.addWidget(self.n)

        self.btn = QPushButton("Dispense")
        self.btn.clicked.connect(self._toggle)
        row.addWidget(self.btn)

        self.progress = QLabel("—")
        self.progress.setMinimumWidth(70)
        row.addWidget(self.progress, 1)

        self.remove_btn = QPushButton("✕")
        self.remove_btn.setFixedSize(28, 28)
        self.remove_btn.setToolTip("Remove this row")
        self.remove_btn.clicked.connect(lambda: window.remove_row(self))
        row.addWidget(self.remove_btn)

    def _toggle(self):
        if self._timer.isActive():
            self._stop_dispense(user_cancel=True)
        else:
            self._start()

    def _start(self):
        hw = self._win.hw
        if hw is None:
            self._win.set_status("Connect to hardware first.")
            return
        hw.set_reward_duration(self.ms.value())
        self._total = self.n.value()
        self._remaining = self._total
        self.btn.setText("Stop")
        self.progress.setText(f"0/{self._total}")
        self._timer.start(self._win.gap.value())

    def _pulse(self):
        hw = self._win.hw
        if hw is None:
            self._stop_dispense(user_cancel=False)
            return
        hw.reward()
        self._remaining -= 1
        self.progress.setText(f"{self._total - self._remaining}/{self._total}")
        if self._remaining == 0:
            self._stop_dispense(user_cancel=False)

    def _stop_dispense(self, *, user_cancel):
        if not self._timer.isActive():
            return
        self._timer.stop()
        self.btn.setText("Dispense")
        done = self._total - self._remaining
        if user_cancel:
            self._win.set_status(
                f"{self.target_ul.value():g} µL target: stopped after "
                f"{done}/{self._total} pulses."
            )
        elif done == self._total and self._total > 0:
            self._win.set_status(
                f"{self.target_ul.value():g} µL target: dispensed "
                f"{self._total} pulses."
            )

    def stop(self):
        self._stop_dispense(user_cancel=False)

    def to_dict(self):
        return {
            "target_ul": self.target_ul.value(),
            "ms": self.ms.value(),
            "n_pulses": self.n.value(),
        }


class CalibWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("IBL water calibration")
        self.resize(900, 380)
        self.hw = None
        self.rows: list[TargetRow] = []
        self._build_ui()
        self._load()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(10)
        root.setContentsMargins(12, 12, 12, 12)

        # Hardware
        hw_box = QGroupBox("Hardware")
        hw_form = QFormLayout(hw_box)
        self.port = QLineEdit("/dev/ttyACM0")
        hw_form.addRow("Port:", self.port)
        self.mock_cb = QCheckBox("Mock (no Teensy)")
        hw_form.addRow("", self.mock_cb)
        self.gap = QSpinBox()
        self.gap.setRange(50, 5000)
        self.gap.setSuffix(" ms")
        self.gap.setValue(500)
        self.gap.setMaximumWidth(120)
        hw_form.addRow("Gap between pulses:", self.gap)
        btn_row = QHBoxLayout()
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.clicked.connect(self._connect)
        self.disconnect_btn = QPushButton("Disconnect")
        self.disconnect_btn.clicked.connect(self._disconnect)
        self.disconnect_btn.setEnabled(False)
        btn_row.addWidget(self.connect_btn)
        btn_row.addWidget(self.disconnect_btn)
        btn_row.addStretch(1)
        btn_w = QWidget()
        btn_w.setLayout(btn_row)
        hw_form.addRow("", btn_w)
        root.addWidget(hw_box)

        # Calibration rows (dynamic add/remove)
        calib_box = QGroupBox("Calibration  (pipette dispensed water, enter µL)")
        calib_layout = QVBoxLayout(calib_box)
        self._rows_layout = QVBoxLayout()
        self._rows_layout.setSpacing(4)
        calib_layout.addLayout(self._rows_layout)
        add_row = QHBoxLayout()
        self.add_btn = QPushButton("+ Add target")
        self.add_btn.clicked.connect(lambda: self.add_row())
        add_row.addWidget(self.add_btn)
        add_row.addStretch(1)
        calib_layout.addLayout(add_row)
        root.addWidget(calib_box)

        # Save + status
        bottom = QHBoxLayout()
        self.status_label = QLabel("Not connected.")
        bottom.addWidget(self.status_label, 1)
        self.save_btn = QPushButton("Save")
        self.save_btn.clicked.connect(self._save)
        bottom.addWidget(self.save_btn)
        root.addLayout(bottom)

    def set_status(self, text):
        self.status_label.setText(text)

    def add_row(self, target_ul=10.0, ms=100, n_pulses=100):
        row = TargetRow(self, target_ul=target_ul, ms=ms, n_pulses=n_pulses)
        self.rows.append(row)
        self._rows_layout.addWidget(row)

    def remove_row(self, row):
        row.stop()
        self._rows_layout.removeWidget(row)
        self.rows.remove(row)
        row.deleteLater()

    def _connect(self):
        try:
            if self.mock_cb.isChecked():
                self.hw = FakeTeensy()
            else:
                self.hw = Teensy(self.port.text().strip())
        except Exception as exc:
            self.set_status(f"Connect failed: {exc}")
            return
        self.connect_btn.setEnabled(False)
        self.disconnect_btn.setEnabled(True)
        self.port.setEnabled(False)
        self.mock_cb.setEnabled(False)
        self.set_status("Connected.")

    def _disconnect(self):
        for r in self.rows:
            r.stop()
        if self.hw is not None:
            self.hw.close()
            self.hw = None
        self.connect_btn.setEnabled(True)
        self.disconnect_btn.setEnabled(False)
        self.port.setEnabled(True)
        self.mock_cb.setEnabled(True)
        self.set_status("Disconnected.")

    def _save(self):
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "gap_ms": self.gap.value(),
            "targets": [r.to_dict() for r in self.rows],
        }
        CONFIG_PATH.write_text(json.dumps(data, indent=2) + "\n")
        self.set_status(f"Saved: {CONFIG_PATH}")

    def _load(self):
        if not CONFIG_PATH.exists():
            for t in DEFAULT_TARGETS_UL:
                self.add_row(target_ul=t)
            return
        try:
            data = json.loads(CONFIG_PATH.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            self.set_status(f"Could not load {CONFIG_PATH}: {exc}")
            for t in DEFAULT_TARGETS_UL:
                self.add_row(target_ul=t)
            return
        if "gap_ms" in data:
            self.gap.setValue(int(data["gap_ms"]))
        for t in data.get("targets", []):
            self.add_row(
                target_ul=float(t.get("target_ul", 10.0)),
                ms=int(t.get("ms", 100)),
                n_pulses=int(t.get("n_pulses", 100)),
            )
        if not self.rows:
            for t in DEFAULT_TARGETS_UL:
                self.add_row(target_ul=t)
        self.set_status(f"Loaded {CONFIG_PATH}")

    def closeEvent(self, event):
        self._disconnect()
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    _apply_dark_palette(app)
    win = CalibWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
