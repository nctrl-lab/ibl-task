import json
import sys
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

import pyqtgraph as pg
from PyQt6.QtCore import Qt, QProcess, QSettings
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
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

from ibl.config import (
    CONTRAST_PRESETS,
    ERROR_TIMEOUT_S,
    WHEEL_GAIN_DEG_PER_MM,
)

CALIBRATION_PATH = Path.home() / ".config/ibl-task/calibration.json"


def _load_calibration():
    if not CALIBRATION_PATH.exists():
        return []
    try:
        data = json.loads(CALIBRATION_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return sorted(data.get("targets", []), key=lambda t: t["target_ul"])


_DASH = pg.mkPen("gray", style=Qt.PenStyle.DashLine)


class PsychometricPlot(pg.PlotWidget):
    def __init__(self) -> None:
        super().__init__(title="Psychometric")
        self.setLabel("left", "P(right)")
        self.setLabel("bottom", "Signed contrast")
        self.setYRange(0, 1)
        self.setXRange(-1.05, 1.05)
        self.addLine(y=0.5, pen=_DASH)
        self.addLine(x=0.0, pen=_DASH)
        self.curve = self.plot(
            [], [], pen="y", symbol="o", symbolBrush="y", symbolSize=8
        )

    def refresh(self, results: list) -> None:
        bins: dict[float, list[int]] = defaultdict(list)
        for r in results:
            bins[r.side * r.contrast].append(1 if r.response > 0 else 0)
        xs = sorted(bins)
        ys = [sum(bins[x]) / len(bins[x]) for x in xs]
        self.curve.setData(xs, ys)


class AccuracyPlot(pg.PlotWidget):
    WINDOW = 10

    def __init__(self) -> None:
        super().__init__(title=f"Running accuracy ({self.WINDOW}-trial boxcar)")
        self.setLabel("left", "Fraction correct")
        self.setLabel("bottom", "Trial")
        self.setYRange(0, 1)
        self.addLine(y=0.5, pen=_DASH)
        self.curve = self.plot([], [], pen=pg.mkPen("g", width=2))

    def refresh(self, results: list) -> None:
        if not results:
            self.curve.setData([], [])
            return
        xs, ys = [], []
        for i, r in enumerate(results):
            window = results[max(0, i - self.WINDOW + 1) : i + 1]
            xs.append(r.trial_index)
            ys.append(sum(1 for w in window if w.correct) / len(window))
        self.curve.setData(xs, ys)


class OutcomePlot(pg.PlotWidget):
    """Per-trial bars: height encodes side+outcome (correct=±1, wrong=±0.2),
    color encodes correctness (green=correct, red=wrong, gray=no-go)."""

    SHOW = 80

    def __init__(self) -> None:
        super().__init__(
            title=f"Recent {self.SHOW} trials  (R↑ / L↓, green=correct, red=wrong)"
        )
        self.setLabel("bottom", "Trial")
        self.setLabel("left", "Side · outcome")
        self.setYRange(-1.1, 1.1)
        self.addLine(y=0.0, pen=_DASH)
        self.bars = pg.BarGraphItem(x=[], height=[], width=0.8)
        self.addItem(self.bars)

    def refresh(self, results: list) -> None:
        xs, hs, brushes = [], [], []
        for r in results:
            mag = 1.0 if r.correct else 0.2
            if r.correct:
                color = "g"
            elif r.response == 0:
                color = "gray"
            else:
                color = "r"
            xs.append(r.trial_index)
            hs.append(mag * (1 if r.side > 0 else -1))
            brushes.append(color)
        self.bars.setOpts(x=xs, height=hs, brushes=brushes)
        if results:
            last = results[-1].trial_index
            self.setXRange(max(-0.5, last - self.SHOW + 0.5), last + 0.5, padding=0)


class BiasPlot(pg.PlotWidget):
    def __init__(self) -> None:
        super().__init__(title="Side bias")
        self.setLabel("left", "P(right response)")
        self.setYRange(0, 1)
        self.setXRange(-0.7, 1.7)
        self.addLine(y=0.5, pen=_DASH)
        self.getAxis("bottom").setTicks([[(0, "Left stim"), (1, "Right stim")]])
        self.bars = pg.BarGraphItem(
            x=[0, 1], height=[0, 0], width=0.6, brushes=["#5588cc", "#cc5588"]
        )
        self.addItem(self.bars)

    def refresh(self, results: list) -> None:
        left = [1 if r.response > 0 else 0 for r in results if r.side < 0]
        right = [1 if r.response > 0 else 0 for r in results if r.side > 0]
        h_l = sum(left) / len(left) if left else 0.0
        h_r = sum(right) / len(right) if right else 0.0
        self.bars.setOpts(height=[h_l, h_r])


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("IBL Task")
        self.resize(1200, 820)
        self._settings = QSettings("POSTECH-NCTRL", "ibl-task")
        self._results = []
        self._proc = None
        self._calib_proc = None
        self._stdout_buf = b""
        self._last_error = None
        self._build_ui()
        self._reload_calibration()
        self._restore_settings()
        self._set_state("idle")

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(10)
        root.setContentsMargins(12, 12, 12, 12)

        # ---- controls: categorized group boxes ----
        ctrl_row = QHBoxLayout()
        ctrl_row.setSpacing(10)

        # Session
        session_box = QGroupBox("Session")
        session_form = QFormLayout(session_box)
        session_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.subject = QLineEdit()
        self.subject.setPlaceholderText("required")
        session_form.addRow("Subject:", self.subject)
        self.n_trials = QSpinBox()
        self.n_trials.setRange(1, 9999)
        self.n_trials.setValue(100)
        self.n_trials.setMaximumWidth(120)
        session_form.addRow("N trials:", self.n_trials)
        self.water_limit = QSpinBox()
        self.water_limit.setRange(1, 100000)
        self.water_limit.setSuffix(" µL")
        self.water_limit.setValue(1000)
        self.water_limit.setMaximumWidth(120)
        session_form.addRow("Water limit:", self.water_limit)
        ctrl_row.addWidget(session_box, 1)

        # Hardware
        hw_box = QGroupBox("Hardware")
        hw_form = QFormLayout(hw_box)
        hw_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.display = QComboBox()
        primary = QApplication.primaryScreen()
        for i, scr in enumerate(QApplication.screens()):
            g = scr.geometry()
            tag = " (primary)" if scr is primary else ""
            self.display.addItem(f"{i}: {scr.name()} {g.width()}×{g.height()}{tag}", i)
        hw_form.addRow("Display:", self.display)
        self.port = QLineEdit("/dev/ttyACM0")
        hw_form.addRow("Port:", self.port)
        self.mock_cb = QCheckBox("Mock (no Teensy)")
        hw_form.addRow("", self.mock_cb)
        ctrl_row.addWidget(hw_box, 1)

        # Training
        train_box = QGroupBox("Training")
        train_form = QFormLayout(train_box)
        train_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        # Reward dropdown is populated from the calibration file.
        reward_row = QHBoxLayout()
        reward_row.setSpacing(6)
        self.reward_combo = QComboBox()
        self.reward_combo.setMinimumWidth(170)
        reward_row.addWidget(self.reward_combo, 1)
        self.auto_reward_cb = QCheckBox("Auto")
        self.auto_reward_cb.setToolTip(
            "Advance one tier per correct trial (capped at the largest); "
            "reset to the smallest on wrong/no-go."
        )
        self.auto_reward_cb.toggled.connect(self._on_auto_reward_toggled)
        reward_row.addWidget(self.auto_reward_cb)
        self.free_btn = QPushButton("💧")
        self.free_btn.setToolTip("Free reward (current selection)")
        self.free_btn.setFixedSize(28, 28)
        self.free_btn.clicked.connect(self._on_free_reward)
        self.flush_btn = QPushButton("🚿")
        self.flush_btn.setToolTip("Flush valve (200 ms)")
        self.flush_btn.setFixedSize(28, 28)
        self.flush_btn.clicked.connect(self._on_flush)
        reward_row.addWidget(self.free_btn)
        reward_row.addWidget(self.flush_btn)
        self.calibrate_btn = QPushButton("Calibrate…")
        self.calibrate_btn.setToolTip("Open the water calibration GUI")
        self.calibrate_btn.clicked.connect(self._on_calibrate)
        reward_row.addWidget(self.calibrate_btn)
        train_form.addRow("Reward:", reward_row)

        self.gain = QDoubleSpinBox()
        self.gain.setRange(0.1, 20.0)
        self.gain.setSingleStep(0.5)
        self.gain.setDecimals(2)
        self.gain.setValue(WHEEL_GAIN_DEG_PER_MM)
        self.gain.setMaximumWidth(120)
        train_form.addRow("Gain (deg/mm):", self.gain)
        self.error_timeout = QDoubleSpinBox()
        self.error_timeout.setRange(0.1, 30.0)
        self.error_timeout.setSingleStep(0.5)
        self.error_timeout.setDecimals(1)
        self.error_timeout.setSuffix(" s")
        self.error_timeout.setValue(ERROR_TIMEOUT_S)
        self.error_timeout.setMaximumWidth(120)
        train_form.addRow("Error timeout:", self.error_timeout)
        self.contrast_combo = QComboBox()
        for preset in CONTRAST_PRESETS:
            label = ", ".join(f"{c * 100:g}" for c in preset)
            self.contrast_combo.addItem(label, list(preset))
        train_form.addRow("Contrast (%):", self.contrast_combo)
        ctrl_row.addWidget(train_box, 1)

        # Action buttons: Ready opens the PsychoPy window in standby; Start
        # then begins trials; Stop terminates the session.
        btn_col = QVBoxLayout()
        self.ready_btn = QPushButton("Ready")
        self.ready_btn.clicked.connect(self._on_ready)
        self.ready_btn.setMinimumSize(96, 36)
        self.ready_btn.setStyleSheet(
            "QPushButton { background-color: #2a6db2; color: white; "
            "font-weight: bold; border-radius: 4px; }"
            "QPushButton:disabled { background-color: #444; color: #aaa; }"
        )
        btn_col.addWidget(self.ready_btn)
        self.start_btn = QPushButton("Start")
        self.start_btn.clicked.connect(self._on_start)
        self.start_btn.setMinimumSize(96, 36)
        self.start_btn.setStyleSheet(
            "QPushButton { background-color: #2a8a3f; color: white; "
            "font-weight: bold; border-radius: 4px; }"
            "QPushButton:disabled { background-color: #444; color: #aaa; }"
        )
        btn_col.addWidget(self.start_btn)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(self._on_stop)
        self.stop_btn.setMinimumSize(96, 36)
        self.stop_btn.setStyleSheet(
            "QPushButton { background-color: #a83232; color: white; "
            "font-weight: bold; border-radius: 4px; }"
            "QPushButton:disabled { background-color: #444; color: #aaa; }"
        )
        btn_col.addWidget(self.stop_btn)
        btn_col.addStretch(1)
        ctrl_row.addLayout(btn_col)

        root.addLayout(ctrl_row)

        # ---- status panel ----
        status_box = QGroupBox("Status")
        grid = QGridLayout(status_box)
        grid.setHorizontalSpacing(20)
        grid.setVerticalSpacing(4)

        self.status_label = QLabel("Idle.")
        f = self.status_label.font()
        f.setBold(True)
        f.setPointSize(f.pointSize() + 1)
        self.status_label.setFont(f)
        grid.addWidget(self.status_label, 0, 0, 1, 6)

        grid.addWidget(self._kv_label("Trial"), 1, 0)
        self.trial_value = QLabel("—")
        grid.addWidget(self.trial_value, 1, 1)
        grid.addWidget(self._kv_label("Correct"), 1, 2)
        self.correct_value = QLabel("—")
        grid.addWidget(self.correct_value, 1, 3)
        grid.addWidget(self._kv_label("No-go"), 1, 4)
        self.nogo_value = QLabel("—")
        grid.addWidget(self.nogo_value, 1, 5)

        grid.addWidget(self._kv_label("Last"), 2, 0)
        self.last_trial_label = QLabel("—")
        grid.addWidget(self.last_trial_label, 2, 1, 1, 5)

        grid.addWidget(self._kv_label("Water"), 3, 0)
        self.water_label = QLabel("0.0 µL  (0 rewards)")
        grid.addWidget(self.water_label, 3, 1, 1, 5)

        grid.setColumnStretch(5, 1)
        root.addWidget(status_box)

        # ---- plots ----
        plots = QGridLayout()
        plots.setSpacing(8)
        self.psy = PsychometricPlot()
        self.acc = AccuracyPlot()
        self.raster = OutcomePlot()
        self.bias = BiasPlot()
        plots.addWidget(self.psy, 0, 0)
        plots.addWidget(self.acc, 0, 1)
        plots.addWidget(self.raster, 1, 0)
        plots.addWidget(self.bias, 1, 1)
        root.addLayout(plots, stretch=1)

    def _kv_label(self, text):
        lbl = QLabel(f"{text}:")
        f = lbl.font()
        f.setBold(True)
        lbl.setFont(f)
        lbl.setStyleSheet("color: #b0b0b0;")
        return lbl

    def _set_state(self, state):
        """state ∈ {'idle', 'calibrating', 'launching', 'ready', 'running', 'stopping'}."""
        self._state = state
        self.ready_btn.setEnabled(state == "idle")
        self.calibrate_btn.setEnabled(state == "idle")
        self.start_btn.setEnabled(state == "ready")
        self.stop_btn.setEnabled(state in ("launching", "ready", "running"))
        self.free_btn.setEnabled(state == "ready")
        self.flush_btn.setEnabled(state == "ready")
        self._set_inputs_enabled(state in ("idle", "calibrating"))

    def _on_free_reward(self):
        if self._proc is not None and self._state == "ready":
            self._proc.write(b"reward\n")

    def _on_flush(self):
        if self._proc is not None and self._state == "ready":
            self._proc.write(b"flush\n")

    # ---- ready / start / stop ----

    def _on_ready(self):
        if self._proc is not None:
            return
        subject = self.subject.text().strip()
        if not subject:
            self._set_status("Enter a subject before going Ready.")
            return
        self._save_settings()
        self._results = []
        self._stdout_buf = b""
        self._last_error = None
        self._refresh_all_plots()
        self.trial_value.setText("—")
        self.correct_value.setText("—")
        self.nogo_value.setText("—")
        self.last_trial_label.setText("—")
        self.water_label.setText("0.0 µL  (0 rewards)")

        contrasts = self.contrast_combo.currentData() or []
        screen_idx = self.display.currentData() or 0
        screens = QApplication.screens()
        scr = screens[screen_idx] if 0 <= screen_idx < len(screens) else screens[0]
        geom = scr.geometry()
        target = self.reward_combo.currentData()
        if target is None:
            self._set_status("No calibration loaded — click Calibrate first.")
            return
        argv = [
            "-m",
            "ibl.task",
            "--subject",
            subject,
            "--n-trials",
            str(self.n_trials.value()),
            "--water-limit",
            str(self.water_limit.value()),
            "--gain",
            str(self.gain.value()),
            "--error-timeout",
            str(self.error_timeout.value()),
            "--contrasts",
            ",".join(str(c) for c in contrasts),
            "--screen",
            str(screen_idx),
            "--screen-size",
            f"{geom.width()}x{geom.height()}",
            "--ready",
        ]
        if self.auto_reward_cb.isChecked():
            argv += [
                "--auto-reward",
                "--calibration",
                json.dumps([
                    {"target_ul": t["target_ul"], "ms": t["ms"]}
                    for t in self._calibration
                ]),
            ]
        else:
            argv += [
                "--reward-ms", str(int(target["ms"])),
                "--reward-ul", str(target["target_ul"]),
            ]
        if self.mock_cb.isChecked():
            argv.append("--mock")
        else:
            argv += ["--port", self.port.text().strip()]

        self._proc = QProcess(self)
        self._proc.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        self._proc.readyReadStandardOutput.connect(self._on_stdout)
        self._proc.readyReadStandardError.connect(self._on_stderr)
        self._proc.finished.connect(self._on_finished)
        self._proc.errorOccurred.connect(self._on_proc_error)
        self._proc.start(sys.executable, argv)

        self._set_state("launching")
        self._set_status(
            f"Opening window for {subject} "
            f"({'mock' if self.mock_cb.isChecked() else self.port.text()})..."
        )

    def _on_start(self):
        if self._proc is None or getattr(self, "_state", None) != "ready":
            return
        self._proc.write(b"start\n")
        self._set_state("running")
        self._set_status(f"Running session for {self.subject.text().strip()}.")

    def _on_stop(self):
        if self._proc is None:
            return
        self._set_status("Stopping...")
        self._set_state("stopping")
        self._proc.terminate()

    # ---- subprocess I/O ----

    def _on_stdout(self) -> None:
        assert self._proc is not None
        self._stdout_buf += bytes(self._proc.readAllStandardOutput())
        while b"\n" in self._stdout_buf:
            line, self._stdout_buf = self._stdout_buf.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                sys.stderr.write(f"[runner stdout, not JSON] {line!r}\n")
                continue
            self._handle_runner_message(msg)

    def _on_stderr(self) -> None:
        assert self._proc is not None
        chunk = bytes(self._proc.readAllStandardError())
        sys.stderr.write(chunk.decode("utf-8", errors="replace"))
        sys.stderr.flush()

    def _handle_runner_message(self, msg):
        kind = msg.get("type")
        if kind == "ready":
            self._set_state("ready")
            self._set_status(
                f"Standby — window open. Click Start to begin trials for "
                f"{self.subject.text().strip()}."
            )
        elif kind == "running":
            self._set_state("running")
            self._set_status(f"Running session for {self.subject.text().strip()}.")
        elif kind == "trial":
            self._on_trial_complete(
                SimpleNamespace(**{k: v for k, v in msg.items() if k != "type"})
            )
        elif kind == "error":
            self._last_error = msg.get("msg", "unknown error")
            self._set_status(f"ERROR: {self._last_error}")

    def _on_proc_error(self, err) -> None:
        self._last_error = f"QProcess error: {err}"
        self._set_status(f"ERROR: {self._last_error}")

    def _on_finished(self, exit_code, exit_status):
        self._proc = None
        self._set_state("idle")
        if self._last_error is not None:
            self._set_status(f"Session aborted (code {exit_code}): {self._last_error}")
            return
        msg = {0: "Session complete."}.get(
            exit_code, f"Session exited with code {exit_code}."
        )
        self._set_status(msg)

    # ---- per-trial UI update ----

    def _on_trial_complete(self, result):
        self._results.append(result)
        n = len(self._results)
        n_correct = sum(1 for r in self._results if r.correct)
        n_nogo = sum(1 for r in self._results if r.response == 0)
        n_total = self.n_trials.value()
        pct = 100 * n_correct / n if n else 0.0
        self._set_status(f"Running — trial {n} of {n_total}")
        self.trial_value.setText(f"{n} / {n_total}")
        self.correct_value.setText(f"{n_correct} / {n}  ({pct:.1f}%)")
        self.nogo_value.setText(str(n_nogo))
        side = "R" if result.side > 0 else "L"
        resp = "R" if result.response > 0 else ("L" if result.response < 0 else "no-go")
        outcome = (
            "✓ correct"
            if result.correct
            else ("✗ wrong" if result.response != 0 else "— no-go")
        )
        self.last_trial_label.setText(
            f"side={side}   c={result.contrast:.4g}   →   {resp}   {outcome}   "
            f"(RT {result.response_time_s:.2f} s)"
        )
        water_ul = sum(r.reward_ul for r in self._results)
        self.water_label.setText(f"{water_ul:.1f} µL  ({n_correct} rewards)")
        # Mirror the per-trial reward tier in the dropdown.
        target_ul = float(result.reward_ul)
        if target_ul > 0:
            for i in range(self.reward_combo.count()):
                if abs(self.reward_combo.itemData(i)["target_ul"] - target_ul) < 1e-6:
                    self.reward_combo.setCurrentIndex(i)
                    break
        elif self.auto_reward_cb.isChecked() and self.reward_combo.count() > 0:
            self.reward_combo.setCurrentIndex(0)
        self._refresh_all_plots()
        if water_ul >= self.water_limit.value() and self._state == "running":
            self._set_status(
                f"Water limit reached ({water_ul:.1f} ≥ "
                f"{self.water_limit.value()} µL). Stopping..."
            )
            self._set_state("stopping")
            self._proc.terminate()

    # ---- helpers ----

    def _refresh_all_plots(self) -> None:
        self.psy.refresh(self._results)
        self.acc.refresh(self._results)
        self.raster.refresh(self._results)
        self.bias.refresh(self._results)

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)

    def _set_inputs_enabled(self, enabled):
        for w in (
            self.subject,
            self.port,
            self.mock_cb,
            self.n_trials,
            self.water_limit,
            self.auto_reward_cb,
            self.gain,
            self.error_timeout,
            self.contrast_combo,
            self.display,
        ):
            w.setEnabled(enabled)
        self.reward_combo.setEnabled(enabled and not self.auto_reward_cb.isChecked())

    def _reload_calibration(self):
        self._calibration = _load_calibration()
        prev = self.reward_combo.currentData()
        self.reward_combo.clear()
        for t in self._calibration:
            self.reward_combo.addItem(f"{t['target_ul']:g} µL  ({t['ms']} ms)", t)
        if prev is not None:
            for i in range(self.reward_combo.count()):
                if abs(self.reward_combo.itemData(i)["target_ul"] - prev["target_ul"]) < 1e-9:
                    self.reward_combo.setCurrentIndex(i)
                    break
        if self.auto_reward_cb.isChecked() and self.reward_combo.count() > 0:
            self.reward_combo.setCurrentIndex(0)
        if not self._calibration:
            self._set_status("No calibration found — click Calibrate to set up rewards.")

    def _on_auto_reward_toggled(self, checked):
        self.reward_combo.setEnabled(not checked and self._state == "idle")
        if checked and self.reward_combo.count() > 0:
            self.reward_combo.setCurrentIndex(0)

    def _on_calibrate(self):
        if self._calib_proc is not None or self._proc is not None:
            return
        self._calib_proc = QProcess(self)
        self._calib_proc.finished.connect(self._on_calibrate_finished)
        self._calib_proc.start(sys.executable, ["-m", "ibl.calibrate"])
        self._set_state("calibrating")
        self._set_status("Calibration GUI open — close it to resume.")

    def _on_calibrate_finished(self, *_):
        self._calib_proc = None
        self._reload_calibration()
        self._set_state("idle")
        self._set_status("Calibration reloaded.")

    # ---- persistence ----

    def _save_settings(self):
        s = self._settings
        s.setValue("subject", self.subject.text())
        s.setValue("port", self.port.text())
        s.setValue("mock", self.mock_cb.isChecked())
        s.setValue("n_trials", self.n_trials.value())
        s.setValue("water_limit", self.water_limit.value())
        target = self.reward_combo.currentData()
        if target is not None:
            s.setValue("reward_target_ul", float(target["target_ul"]))
        s.setValue("auto_reward", self.auto_reward_cb.isChecked())
        s.setValue("gain", self.gain.value())
        s.setValue("error_timeout", self.error_timeout.value())
        s.setValue("contrast_index", self.contrast_combo.currentIndex())
        s.setValue("display_index", self.display.currentIndex())
        s.setValue("geometry", self.saveGeometry())

    def _restore_settings(self):
        s = self._settings
        self.subject.setText(s.value("subject", "", type=str))
        self.port.setText(s.value("port", "/dev/ttyACM0", type=str))
        self.mock_cb.setChecked(s.value("mock", False, type=bool))
        self.n_trials.setValue(s.value("n_trials", 100, type=int))
        self.water_limit.setValue(s.value("water_limit", 1000, type=int))
        if s.contains("reward_target_ul"):
            target_ul = s.value("reward_target_ul", type=float)
            for i in range(self.reward_combo.count()):
                if abs(self.reward_combo.itemData(i)["target_ul"] - target_ul) < 1e-9:
                    self.reward_combo.setCurrentIndex(i)
                    break
        self.auto_reward_cb.setChecked(s.value("auto_reward", False, type=bool))
        self.gain.setValue(s.value("gain", WHEEL_GAIN_DEG_PER_MM, type=float))
        self.error_timeout.setValue(s.value("error_timeout", ERROR_TIMEOUT_S, type=float))
        ci = s.value("contrast_index", 0, type=int)
        if 0 <= ci < self.contrast_combo.count():
            self.contrast_combo.setCurrentIndex(ci)
        di = s.value("display_index", 0, type=int)
        if 0 <= di < self.display.count():
            self.display.setCurrentIndex(di)
        geom = s.value("geometry")
        if geom:
            self.restoreGeometry(geom)

    def closeEvent(self, event):
        self._save_settings()
        if self._proc is not None:
            self._proc.terminate()
            if not self._proc.waitForFinished(3000):
                self._proc.kill()
                self._proc.waitForFinished(1000)
        super().closeEvent(event)


def _apply_dark_palette(app):
    app.setStyle("Fusion")
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor(45, 45, 48))
    pal.setColor(QPalette.ColorRole.WindowText, Qt.GlobalColor.white)
    pal.setColor(QPalette.ColorRole.Base, QColor(30, 30, 32))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor(53, 53, 55))
    pal.setColor(QPalette.ColorRole.ToolTipBase, Qt.GlobalColor.white)
    pal.setColor(QPalette.ColorRole.ToolTipText, Qt.GlobalColor.white)
    pal.setColor(QPalette.ColorRole.Text, Qt.GlobalColor.white)
    pal.setColor(QPalette.ColorRole.Button, QColor(60, 60, 64))
    pal.setColor(QPalette.ColorRole.ButtonText, Qt.GlobalColor.white)
    pal.setColor(QPalette.ColorRole.BrightText, Qt.GlobalColor.red)
    pal.setColor(QPalette.ColorRole.Highlight, QColor(42, 130, 218))
    pal.setColor(QPalette.ColorRole.HighlightedText, Qt.GlobalColor.black)
    pal.setColor(QPalette.ColorRole.Link, QColor(42, 130, 218))
    pal.setColor(QPalette.ColorRole.PlaceholderText, QColor(160, 160, 160))
    app.setPalette(pal)


def main():
    pg.setConfigOptions(antialias=True, background="k", foreground="w")
    app = QApplication(sys.argv)
    _apply_dark_palette(app)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
