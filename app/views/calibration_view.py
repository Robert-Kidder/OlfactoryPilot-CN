from __future__ import annotations

import time
from typing import Iterable

import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from app.services import BreathSampleBuffer, FrameRateTracker, FrameStats


class CalibrationView(QWidget):
    breath_metrics = Signal(dict)
    threshold_changed = Signal(str, float)

    def __init__(
        self,
        *,
        inhale_threshold: float = 0.2,
        exhale_threshold: float = -0.2,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.buffer = BreathSampleBuffer()
        self.tracker = FrameRateTracker()
        self._safety_state = "SAFE"
        self._build_ui()
        self.set_thresholds(inhale_threshold, exhale_threshold)
        self._render_timer = QTimer(self)
        self._render_timer.setInterval(33)  # ~30 FPS target
        self._render_timer.timeout.connect(self._render_frame)
        self._render_timer.start()

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
        label = f"安全状态：{safety_state}"
        if timestamp:
            ts_text = time.strftime("%H:%M:%S", time.localtime(timestamp))
            label += f"（更新时间 {ts_text}）"
        self._safety_label.setText(label)
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
        self._plot = pg.PlotWidget(background="k")
        self._plot.showGrid(x=True, y=True, alpha=0.2)
        self._plot.setLabel("left", "气流 (L/s)")
        self._plot.setLabel("bottom", "采样点")
        self._curve = self._plot.plot(pen=pg.mkPen("w", width=1.5))
        
        # InfiniteLines are movable=True to support dragging (AC3)
        self._exhale_line = pg.InfiniteLine(
            angle=0, pen=pg.mkPen("r", style=Qt.DashLine), movable=True
        )
        self._inhale_line = pg.InfiniteLine(
            angle=0, pen=pg.mkPen("y", style=Qt.DotLine), movable=True
        )
        
        # Connect dragging events to sync spinboxes
        self._exhale_line.sigPositionChangeFinished.connect(
            lambda line: self._exhale_spin.setValue(line.value())
        )
        self._inhale_line.sigPositionChangeFinished.connect(
            lambda line: self._inhale_spin.setValue(line.value())
        )

        self._plot.addItem(self._exhale_line)
        self._plot.addItem(self._inhale_line)

        self._fps_label = QLabel("FPS：-- / --")
        self._warning_label = QLabel("")
        self._warning_label.setStyleSheet("color: #d9534f;")
        self._warning_label.hide()
        self._safety_label = QLabel("安全状态：SAFE")
        self._gating_label = QLabel("Gating：区间内 (NEUTRAL)")

        self._inhale_led = self._build_led_label("吸气 LED", "#f0ad4e")
        self._exhale_led = self._build_led_label("呼气 LED", "#d9534f")

        controls = self._build_threshold_controls()
        info_bar = QHBoxLayout()
        info_bar.addWidget(self._fps_label)
        info_bar.addWidget(self._warning_label)
        info_bar.addStretch()
        info_bar.addWidget(self._gating_label)
        info_bar.addWidget(self._inhale_led)
        info_bar.addWidget(self._exhale_led)

        layout = QVBoxLayout()
        layout.addWidget(self._plot)
        layout.addLayout(info_bar)
        layout.addWidget(controls)
        layout.addWidget(self._safety_label)
        self.setLayout(layout)

    def _build_threshold_controls(self) -> QWidget:
        box = QGroupBox("阈值与同步")
        layout = QGridLayout()
        self._inhale_spin = QDoubleSpinBox()
        self._inhale_spin.setRange(-5.0, 5.0)
        self._inhale_spin.setSingleStep(0.01)
        self._inhale_spin.valueChanged.connect(
            lambda value: self._on_threshold_changed("inhale", float(value))
        )

        self._exhale_spin = QDoubleSpinBox()
        self._exhale_spin.setRange(-5.0, 5.0)
        self._exhale_spin.setSingleStep(0.01)
        self._exhale_spin.valueChanged.connect(
            lambda value: self._on_threshold_changed("exhale", float(value))
        )

        layout.addWidget(QLabel("吸气阈值（黄点线）"), 0, 0)
        layout.addWidget(self._inhale_spin, 0, 1)
        layout.addWidget(QLabel("呼气阈值（红虚线）"), 1, 0)
        layout.addWidget(self._exhale_spin, 1, 1)
        box.setLayout(layout)
        self._threshold_group = box
        return box

    # Rendering & metrics -----------------------------------------------
    def _render_frame(self) -> None:
        """
        Main rendering loop called at ~30Hz.
        
        Optimized for performance:
        1. Fetches pre-buffered samples (fixed size ring buffer).
        2. updates pyqtgraph curve efficiently.
        3. Calculates FPS metrics after rendering to measure actual throughput.
        """
        values = self.buffer.values()
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
        self._update_fps_label(stats)
        self._update_warning_label(warning_flag, reason)
        self._emit_metrics(stats, warning_flag, reason)

    def _update_leds(self, value: float | None) -> None:
        inhale_on = False
        exhale_on = False
        
        # AC4: Gray out LEDs if unsafe (BLOCKED)
        if self._safety_state != "SAFE":
             # All off
             pass
        elif value is not None:
            inhale_on = value >= self._inhale_spin.value()
            exhale_on = value <= self._exhale_spin.value()
            
        self._set_led(self._inhale_led, inhale_on, "#f0ad4e")
        self._set_led(self._exhale_led, exhale_on, "#d9534f")

    def _update_threshold_lines(self) -> None:
        self._inhale_line.setValue(self._inhale_spin.value())
        self._exhale_line.setValue(self._exhale_spin.value())

    def _update_fps_label(self, stats: FrameStats) -> None:
        self._fps_label.setText(
            f"FPS：avg {stats.fps_avg:.1f} | p95 {stats.fps_p95:.1f}（窗口 {stats.window_s:.0f}s）"
        )

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

    @staticmethod
    def _build_led_label(title: str, active_color: str) -> QLabel:
        label = QLabel(title)
        label.setMargin(4)
        label.setProperty("activeColor", active_color)
        label.setStyleSheet("background: #6c757d; color: white;")
        return label

    @staticmethod
    def _set_led(label: QLabel, active: bool, color: str) -> None:
        if active:
            label.setStyleSheet(f"background: {color}; color: white;")
        else:
            label.setStyleSheet("background: #6c757d; color: white;")
