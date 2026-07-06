from __future__ import annotations

import time
from collections.abc import Iterable

import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from app.services import BreathSampleBuffer, FrameRateTracker, FrameStats


class CalibrationView(QWidget):
    breath_metrics = Signal(dict)
    threshold_changed = Signal(str, float)
    calibration_requested = Signal(bool, int)  # active, duration_sec

    def __init__(
        self,
        *,
        inhale_threshold: float = 0.2,
        exhale_threshold: float = -0.2,
        signal_offset: float = 0.0,
        signal_gain: float = 1.0,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.buffer = BreathSampleBuffer()
        self.tracker = FrameRateTracker()
        self._safety_state = "SAFE"
        self._signal_offset = signal_offset
        self._signal_gain = signal_gain
        self._build_ui()
        self.set_thresholds(inhale_threshold, exhale_threshold)
        self._render_timer = QTimer(self)
        self._render_timer.setInterval(33)  # ~30 FPS target
        self._render_timer.timeout.connect(self._render_frame)
        self._render_timer.start()

    def set_signal_transform(self, offset: float, gain: float) -> None:
        self._signal_offset = offset
        self._signal_gain = gain
        # Force redraw? Not strictly needed as render timer handles it

    # Public API ---------------------------------------------------------
    def ingest_samples(self, samples: Iterable[float], *, timestamp: float | None = None) -> None:
        self.buffer.append_samples(samples, timestamp=timestamp)

    def set_thresholds(self, inhale: float, exhale: float) -> None:
        self._inhale_spin.blockSignals(True)
        self._exhale_spin.blockSignals(True)
        self._inhale_spin.setValue(inhale)
        self._exhale_spin.setValue(exhale)
        self._inhale_spin.blockSignals(False)
        self._exhale_spin.blockSignals(False)
        self._update_threshold_lines()

    def apply_safety_state(self, safety_state: str, timestamp: float | None = None) -> None:
        self._safety_state = safety_state
        controls_enabled = safety_state == "SAFE"
        self._threshold_group.setEnabled(controls_enabled)

        # AC4: Update gating label and LEDs immediately if unsafe
        if safety_state != "SAFE":
            self._gating_label.setText("Gating：已封锁 (BLOCKED)")
            self._update_leds(None) # Force LEDs off

    def update_gating_state(self, state: str) -> None:
        # Map state code to Chinese label
        mapping = {
            "NEUTRAL": "区间内 (NEUTRAL)",
            "INHALE_ABOVE": "吸气越界 (INHALE)",
            "EXHALE_ABOVE": "呼气越界 (EXHALE)",
            "BLOCKED": "已封锁 (BLOCKED)",
        }
        text = mapping.get(state, state)
        self._gating_label.setText(f"Gating：{text}")

    # UI construction ----------------------------------------------------
    def _build_ui(self) -> None:
        # 1. Top Control Bar (Controls & Thresholds)
        top_controls = self._build_top_controls()

        # 2. Bottom Split View
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # 2a. Left Pane: Waveform & Progress
        left_widget = QWidget()
        left_layout = QVBoxLayout()
        left_layout.setContentsMargins(0, 0, 0, 0)

        self._plot = pg.PlotWidget(background="k")
        self._plot.showGrid(x=True, y=True, alpha=0.2)
        self._plot.setLabel("left", "气流 (L/s)")
        self._plot.setLabel("bottom", "采样点")
        self._curve = self._plot.plot(pen=pg.mkPen("w", width=1.5))

        # InfiniteLines setup (moved here)
        self._exhale_line = pg.InfiniteLine(angle=0, pen=pg.mkPen("r", style=Qt.DashLine), movable=True)
        self._inhale_line = pg.InfiniteLine(angle=0, pen=pg.mkPen("y", style=Qt.DotLine), movable=True)
        self._plot.addItem(self._exhale_line)
        self._plot.addItem(self._inhale_line)

        self._calibration_progress = QProgressBar()
        self._calibration_progress.setRange(0, 100)
        self._calibration_progress.setValue(0)
        self._calibration_progress.setTextVisible(True)
        # Removed setFixedHeight(5) to make it visible

        left_layout.addWidget(self._plot)
        left_layout.addWidget(self._calibration_progress)
        left_widget.setLayout(left_layout)

        # 2b. Right Pane: Feedback & Stats
        right_widget = self._build_feedback_pane()

        splitter.addWidget(left_widget)
        splitter.addWidget(right_widget)
        splitter.setStretchFactor(0, 2) # Plot gets ~66% space
        splitter.setStretchFactor(1, 1) # Feedback gets ~33% space

        # Main Layout assembly
        main_layout = QVBoxLayout()
        main_layout.addWidget(top_controls)
        main_layout.addWidget(splitter)
        self.setLayout(main_layout)

        # Connect dragging events (needs spinboxes created in top controls)
        self._exhale_line.sigPositionChangeFinished.connect(lambda line: self._exhale_spin.setValue(line.value()))
        self._inhale_line.sigPositionChangeFinished.connect(lambda line: self._inhale_spin.setValue(line.value()))

    def _build_top_controls(self) -> QGroupBox:
        box = QGroupBox("操作与设定")
        layout = QHBoxLayout()

        # Calibration Start/Stop & Duration
        layout.addWidget(QLabel("校准时长:"))
        self._duration_spin = QSpinBox()
        self._duration_spin.setRange(5, 60)
        self._duration_spin.setValue(10)
        self._duration_spin.setSuffix(" 秒")
        self._duration_spin.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout.addWidget(self._duration_spin)

        self._calibration_btn = QPushButton("启动校准")
        self._calibration_btn.setStyleSheet("background-color: #28a745; color: white; font-weight: bold; padding: 5px 15px;")
        self._calibration_btn.setCheckable(True)
        self._calibration_btn.clicked.connect(self._on_calibration_toggled)
        layout.addWidget(self._calibration_btn)

        # Separator
        layout.addSpacing(20)
        line = QLabel("|")
        line.setStyleSheet("color: gray;")
        layout.addWidget(line)
        layout.addSpacing(20)

        # Thresholds
        layout.addWidget(QLabel("吸气阈值(黄):"))
        self._inhale_spin = QDoubleSpinBox()
        self._inhale_spin.setRange(-5.0, 5.0)
        self._inhale_spin.setSingleStep(0.01)
        self._inhale_spin.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._inhale_spin.valueChanged.connect(lambda v: self._on_threshold_changed("inhale", float(v)))
        layout.addWidget(self._inhale_spin)

        layout.addWidget(QLabel("呼气阈值(红):"))
        self._exhale_spin = QDoubleSpinBox()
        self._exhale_spin.setRange(-5.0, 5.0)
        self._exhale_spin.setSingleStep(0.01)
        self._exhale_spin.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._exhale_spin.valueChanged.connect(lambda v: self._on_threshold_changed("exhale", float(v)))
        layout.addWidget(self._exhale_spin)

        # Removed addStretch() to allow expanding widgets to fill space
        self._threshold_group = box # Logic compatibility alias
        box.setLayout(layout)
        return box

    def _build_feedback_pane(self) -> QGroupBox:
        box = QGroupBox("状态与数据")
        layout = QVBoxLayout()

        # 0. Warning Label (Top of feedback)
        self._warning_label = QLabel("")
        self._warning_label.setStyleSheet("color: #d9534f; font-weight: bold;")
        self._warning_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._warning_label.hide()
        layout.addWidget(self._warning_label)

        # 1. LED Status Cluster
        led_group = QGroupBox("信号状态")
        led_layout = QGridLayout()

        self._gating_label = QLabel("区间内")
        self._gating_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._inhale_led = self._build_circular_led()
        self._exhale_led = self._build_circular_led()

        led_layout.addWidget(QLabel("吸气"), 0, 0, alignment=Qt.AlignmentFlag.AlignCenter)
        led_layout.addWidget(self._inhale_led, 1, 0, alignment=Qt.AlignmentFlag.AlignCenter)

        led_layout.addWidget(QLabel("呼气"), 0, 1, alignment=Qt.AlignmentFlag.AlignCenter)
        led_layout.addWidget(self._exhale_led, 1, 1, alignment=Qt.AlignmentFlag.AlignCenter)

        led_layout.addWidget(self._gating_label, 2, 0, 1, 2)
        led_group.setLayout(led_layout)

        # 2. Stats Grid
        stats_group = QGroupBox("校准结果")
        stats_layout = QGridLayout()
        self._stats_max_label = QLabel("--")
        self._stats_min_label = QLabel("--")
        self._stats_offset_label = QLabel("--")
        self._stats_gain_label = QLabel("--")

        # Style stats for readability
        for lbl in [
            self._stats_max_label,
            self._stats_min_label,
            self._stats_offset_label,
            self._stats_gain_label,
        ]:
            lbl.setStyleSheet("font-weight: bold; font-size: 14px;")

        stats_layout.addWidget(QLabel("Max:"), 0, 0)
        stats_layout.addWidget(self._stats_max_label, 0, 1)
        stats_layout.addWidget(QLabel("Min:"), 1, 0)
        stats_layout.addWidget(self._stats_min_label, 1, 1)
        stats_layout.addWidget(QLabel("Offset:"), 2, 0)
        stats_layout.addWidget(self._stats_offset_label, 2, 1)
        stats_layout.addWidget(QLabel("Gain:"), 3, 0)
        stats_layout.addWidget(self._stats_gain_label, 3, 1)
        stats_group.setLayout(stats_layout)

        # 3. Calibration Text Status
        self._calibration_status = QLabel("就绪")
        self._calibration_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._calibration_status.setStyleSheet("color: gray; font-style: italic;")

        layout.addWidget(led_group, 1) # Stretch factor 1
        layout.addWidget(stats_group, 1) # Stretch factor 1
        layout.addWidget(self._calibration_status)
        # Removed addStretch() to allow groups to expand

        box.setLayout(layout)
        return box

    def set_calibration_progress(self, value: int) -> None:
        self._calibration_progress.setValue(value)

    @staticmethod
    def _build_circular_led() -> QLabel:
        lbl = QLabel()
        lbl.setFixedSize(24, 24)
        lbl.setStyleSheet("""
            border-radius: 12px;
            background-color: #343a40;
            border: 2px solid #6c757d;
        """)
        return lbl

    def _update_leds(self, value: float | None) -> None:
        inhale_active = False
        exhale_active = False
        if self._safety_state == "SAFE" and value is not None:
            inhale_active = value >= self._inhale_spin.value()
            exhale_active = value <= self._exhale_spin.value()

        self._set_circular_led(self._inhale_led, inhale_active, "#f0ad4e")
        self._set_circular_led(self._exhale_led, exhale_active, "#d9534f")

    @staticmethod
    def _set_circular_led(lbl: QLabel, active: bool, color: str) -> None:
        if active:
            lbl.setStyleSheet(f"""
                border-radius: 12px;
                background-color: {color};
                border: 2px solid white;
            """)
        else:
            lbl.setStyleSheet("""
                border-radius: 12px;
                background-color: #343a40;
                border: 2px solid #6c757d;
            """)

    def _on_calibration_toggled(self, checked: bool) -> None:
        duration = self._duration_spin.value()
        self.calibration_requested.emit(checked, duration)

        # Optimistic UI update
        if checked:
            self._calibration_btn.setText("中断校准")
            self._calibration_btn.setStyleSheet("background-color: #ffc107; color: black;")
            self._duration_spin.setEnabled(False)
            self._calibration_status.setText("准备中...")
        else:
            self._calibration_btn.setText("启动校准")
            self._calibration_btn.setStyleSheet("background-color: #28a745; color: white;")
            self._duration_spin.setEnabled(True)
            self._calibration_status.setText("已中断")

    def set_calibration_state(self, active: bool, status_text: str = "") -> None:
        self._calibration_btn.blockSignals(True)
        self._calibration_btn.setChecked(active)
        self._calibration_btn.blockSignals(False)

        if active:
            self._calibration_btn.setText("中断校准")
            self._calibration_btn.setStyleSheet("background-color: #ffc107; color: black;")
            self._duration_spin.setEnabled(False)
        else:
            self._calibration_btn.setText("启动校准")
            self._calibration_btn.setStyleSheet("background-color: #28a745; color: white;")
            self._duration_spin.setEnabled(True)

        if status_text:
            self._calibration_status.setText(status_text)

    def update_calibration_stats(self, max_val: float, min_val: float, offset: float | None = None, gain: float | None = None) -> None:
        self._stats_max_label.setText(f"Max: {max_val:.3f}")
        self._stats_min_label.setText(f"Min: {min_val:.3f}")
        if offset is not None:
            self._stats_offset_label.setText(f"Offset: {offset:.3f}")
        if gain is not None:
            self._stats_gain_label.setText(f"Gain: {gain:.3f}")

    # Rendering & metrics -----------------------------------------------
    def _render_frame(self) -> None:
        """
        Main rendering loop called at ~30Hz.

        Optimized for performance:
        1. Fetches pre-buffered samples (fixed size ring buffer).
        2. updates pyqtgraph curve efficiently.
        3. Calculates FPS metrics after rendering to measure actual throughput.
        """
        raw_values = self.buffer.values()
        # Apply transform: (Raw + Offset) * Gain
        values = [(v + self._signal_offset) * self._signal_gain for v in raw_values]

        self._curve.setData(values)  # pyqtgraph handles efficient update
        last_value = values[-1] if values else None

        # Track FPS and check for performance drops
        stats = self.tracker.record_frame(sample_count=len(values))
        stale = self.buffer.is_stale()
        reason = stats.reason
        warning_flag = stats.warning_flag

        if stale:
            warning_flag = True
            reason = "data_stale"

        self._update_leds(last_value)
        self._update_warning_label(warning_flag, reason)
        self._emit_metrics(stats, warning_flag, reason)

    def _update_threshold_lines(self) -> None:
        self._inhale_line.setValue(self._inhale_spin.value())
        self._exhale_line.setValue(self._exhale_spin.value())

    def _update_warning_label(self, warning: bool, reason: str | None) -> None:
        if not warning:
            self._warning_label.hide()
            self._warning_label.setText("")
            return

        if reason == "data_stale":
            text = "数据过期，等待新样本..."
        else:
            text = "FPS <30 持续 2s，已记录警告"
        self._warning_label.setText(text)
        self._warning_label.show()

    def _emit_metrics(self, stats: FrameStats, warning_flag: bool, reason: str | None) -> None:
        payload = {
            "ts": time.time(),
            "fps_avg": stats.fps_avg,
            "fps_p95": stats.fps_p95,
            "fps_p05": getattr(stats, "fps_p05", 0.0),
            "window_s": stats.window_s,
            "sample_count": stats.sample_count,
            "warning_flag": warning_flag,
            "reason": reason,
        }
        self.breath_metrics.emit(payload)

    # Helpers -----------------------------------------------------------
    def _on_threshold_changed(self, name: str, value: float) -> None:
        self._update_threshold_lines()
        self.threshold_changed.emit(name, value)
