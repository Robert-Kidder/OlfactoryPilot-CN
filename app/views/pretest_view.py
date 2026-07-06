from __future__ import annotations

import time

import pyqtgraph as pg
from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QFrame,
    QGraphicsDropShadowEffect,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.services import BreathSampleBuffer


class SkeuoButton(QPushButton):
    """A simple skeuomorphic-styled button with shadow toggle."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.setCheckable(True)
        self.setMinimumSize(28, 21)
        self.setMaximumSize(44, 32)
        self._shadow = QGraphicsDropShadowEffect(self)
        self._shadow.setOffset(0, 1)
        self._shadow.setBlurRadius(5)
        self._shadow.setColor(Qt.black)
        self.setGraphicsEffect(self._shadow)
        self._base_style = """
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                            stop:0 #f3f4f6, stop:0.4 #e1e2e5,
                                            stop:1 #cfd1d4);
                border: 1px solid #8d8f92;
                border-radius: 7px;
                padding: 4px;
                color: #333;
                font-weight: 700;
                letter-spacing: 0.2px;
                min-width: 28px;
                min-height: 21px;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                            stop:0 #ffffff, stop:0.4 #f0f0f2,
                                            stop:1 #d9dbde);
                border: 1px solid #7e8083;
            }
            QPushButton:pressed, QPushButton:checked {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                            stop:0 #c9cbce, stop:0.5 #b6b8bb,
                                            stop:1 #a3a5a8);
                border: 1px solid #6f7174;
                padding-top: 7px;
                padding-bottom: 3px;
            }
            QPushButton:disabled {
                background: #e8e8e8;
                color: #9a9a9a;
                border: 1px solid #cccccc;
            }
        """
        self.setStyleSheet(self._base_style)
        self.toggled.connect(self._update_shadow)

    def _update_shadow(self, checked: bool) -> None:
        if not self._shadow:
            return
        # When pressed/checked, reduce drop shadow to simulate sinking
        self._shadow.setEnabled(not checked)
        if checked:
            self._shadow.setOffset(0, 0)
        else:
            self._shadow.setOffset(0, 2)


class ValveButtonWidget(QWidget):
    """Composite widget: skeuomorphic button with an LED and index label."""

    def __init__(self, channel_id: int, label_text: str) -> None:
        super().__init__()
        self.channel_id = channel_id
        self.button = SkeuoButton("")
        self.button.setCursor(Qt.PointingHandCursor)
        self._led = QLabel(self.button)
        self._led.setFixedSize(12, 12)
        self._led.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._set_led(False)
        self._center_led()
        self.button.installEventFilter(self)

        self.index_label = QLabel(label_text)
        self.index_label.setAlignment(Qt.AlignCenter)
        self.index_label.setStyleSheet("color: #666; font-size: 11px;")

        layout = QVBoxLayout()
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)
        layout.addWidget(self.button, alignment=Qt.AlignCenter)
        layout.addWidget(self.index_label)
        self.setLayout(layout)

    def eventFilter(self, watched, event):  # noqa: D401,N802
        """Center LED when the button resizes."""
        if watched is self.button and event.type() == QEvent.Resize:
            self._center_led()
        return super().eventFilter(watched, event)

    def _center_led(self) -> None:
        btn_w = max(self.button.width(), 1)
        btn_h = max(self.button.height(), 1)
        led_w = self._led.width()
        led_h = self._led.height()
        self._led.move((btn_w - led_w) // 2, (btn_h - led_h) // 2 - 4)

    def set_checked(self, is_open: bool) -> None:
        self.button.setChecked(is_open)
        self._set_led(is_open)

    def set_enabled(self, enabled: bool) -> None:
        self.button.setEnabled(enabled)
        self.index_label.setEnabled(enabled)
        if not enabled:
            self._set_led(False)

    def _set_led(self, active: bool) -> None:
        color = "#28a745" if active else "#444444"
        border = "#ffffff" if active else "#6c757d"
        radius = max(self._led.width(), self._led.height()) // 2
        self._led.setStyleSheet(
            f"border-radius: {radius}px; background-color: {color}; border: 1px solid {border};"
        )


class ManualLedButton(QPushButton):
    """Clickable circular LED-style button used for manual trigger."""

    def __init__(self, *, color: str = "#17a2b8") -> None:
        super().__init__()
        self.color = color
        self.setCheckable(True)
        self.setFixedSize(24, 24)
        self.setCursor(Qt.PointingHandCursor)
        self._apply_style(False)
        self.toggled.connect(self._apply_style)

    def _apply_style(self, active: bool) -> None:
        radius = self.width() // 2
        fill = self.color if active else "#343a40"
        border = "2px solid white" if active else "2px solid #6c757d"
        self.setStyleSheet(
            f"border-radius: {radius}px; background-color: {fill}; {border};"
        )

    def set_checked(self, is_open: bool) -> None:
        self.setChecked(is_open)
        self._apply_style(is_open)

    def set_enabled(self, enabled: bool) -> None:
        self.setEnabled(enabled)
        if not enabled:
            self.setChecked(False)
            self._apply_style(False)


class PreTestView(QWidget):
    """预检视图：通道矩阵 + 气流/阈值可视化 + 状态指示灯。"""

    toggle_requested = Signal(int, bool)
    apply_requested = Signal(float, float, float)
    flow_sequence_requested = Signal(str, float, float, float)
    valve_sequence_requested = Signal(str, list)
    sequence_requested = Signal(str, list, float, float, float)

    def __init__(
        self,
        *,
        valve_map: dict[int, str],
        variant: str,
        master_valve: str = "",
        inhale_threshold: float = 0.2,
        exhale_threshold: float = -0.2,
        signal_offset: float = 0.0,
        signal_gain: float = 1.0,
    ) -> None:
        super().__init__()
        self._valve_map = {int(k): v for k, v in valve_map.items()}
        self._variant = variant
        self._master_valve = master_valve
        self._config_error = False
        self._config_error_message = ""
        self._selected_states: dict[int, bool] = {}
        self._open_states: dict[int, bool] = {}
        self._buttons: dict[int, QPushButton] = {}
        self._master_open = bool(master_valve)
        self._warning_label = QLabel()
        self._warning_label.setStyleSheet("color: #DC3545; font-weight: 600;")
        self._warning_label.hide()
        self._status_label = QLabel(self._build_status_text("SAFE", ""))
        self._status_label.setFrameShape(QFrame.StyledPanel)
        self._status_label.setStyleSheet("padding: 6px; background-color: #f5f5f5;")
        self._status_label.hide()
        self._flow_message_label = QLabel("")
        self._flow_message_label.setStyleSheet("color: #555;")
        self._applied_label = QLabel("已应用: -")
        self._applied_label.setStyleSheet("color: #333;")
        self._airflow_label = QLabel("当前气流：0.00 sccm")
        self._applied_targets = {"A": 0.0, "B": 0.0, "C": 0.0, "A_comp": 0.0}

        # Flow / timing state
        self._manual_trigger_enabled = False
        self._armed = False
        self._odor_active = False
        self._delivery_started_at: float | None = None
        self._active_delivery_channels: list[int] = []
        self._current_odor_flow = 0.0
        self._breath_buffer = BreathSampleBuffer()
        self._odor_buffer = BreathSampleBuffer()
        self._signal_offset = signal_offset
        self._signal_gain = signal_gain
        self._dirty = False

        self._inhale_threshold = inhale_threshold
        self._exhale_threshold = exhale_threshold

        self._validate_valve_map()
        self._build_layout()

        self._render_timer = QTimer(self)
        self._render_timer.setInterval(50)
        self._render_timer.timeout.connect(self._render_frame)
        self._render_timer.start()

    def set_signal_transform(self, offset: float, gain: float) -> None:
        """Update calibration transform used for waveform rendering."""
        self._signal_offset = float(offset)
        self._signal_gain = float(gain)
        self._dirty = True

    def _validate_valve_map(self) -> None:
        """Ensure valve mapping exists; if missing, keep UI disabled with a clear warning."""
        # Keep a deterministic order for rendering (row-priority 1-10, 11-20).
        self._valve_map = dict(sorted(self._valve_map.items(), key=lambda item: int(item[0])))
        self._config_error = False
        self._config_error_message = ""
        if not self._valve_map:
            self._config_error = True
            self._config_error_message = (
                "未找到 20 通道映射，请检查 config/default_config.json 的 valve_mapping.variants[\"20-channel\"]"
            )

    def _set_controls_enabled(self, enabled: bool, *, reason: str = "") -> None:
        for widget in self._buttons.values():
            widget.set_enabled(enabled)
        for ctrl in (self._start_button, self._manual_button):
            ctrl.setEnabled(enabled)

    def _apply_config_state(self) -> None:
        if self._config_error:
            self._warning_label.setText(self._config_error_message)
            self._warning_label.show()
            self._status_label.setText(self._build_status_text("DATA_STALE", self._config_error_message))
            self._status_label.show()
            self._set_controls_enabled(False, reason=self._config_error_message)
        else:
            self._warning_label.hide()

    def _build_layout(self) -> None:
        layout = QVBoxLayout()
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(10)

        self._warning_label.setVisible(bool(self._config_error_message))
        layout.addWidget(self._warning_label)

        # Top half: valves + flow controls
        top_row = QHBoxLayout()
        top_row.setSpacing(12)
        top_row.addWidget(self._build_valve_panel(), 3)
        top_row.addWidget(self._build_command_panel(), 2)
        layout.addLayout(top_row)

        # Separator
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        layout.addWidget(line)

        # Bottom half: waveform + status LEDs
        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(12)
        bottom_row.addWidget(self._build_waveform_panel(), 3)
        bottom_row.addWidget(self._build_status_panel(), 1)
        layout.addLayout(bottom_row)

        self.setLayout(layout)
        self._apply_config_state()

    def _build_valve_panel(self) -> QGroupBox:
        box = QGroupBox("气道开关")
        grid = QGridLayout()
        grid.setContentsMargins(8, 8, 8, 8)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(8)
        columns = 10  # 固定两行十列
        if not self._valve_map:
            notice = QLabel("未找到 20 通道映射，已禁用通道开关")
            notice.setStyleSheet("color: #DC3545; font-weight: 600;")
            grid.addWidget(notice, 0, 0, 1, columns)
        else:
            for idx, channel_id in enumerate(sorted(self._valve_map.keys())):
                widget = ValveButtonWidget(channel_id, f"{channel_id}")
                widget.button.clicked.connect(lambda checked, ch=channel_id: self._handle_click(ch))
                row = idx // columns
                col = idx % columns
                grid.addWidget(widget, row, col, alignment=Qt.AlignCenter)
                self._buttons[channel_id] = widget
                self._selected_states[channel_id] = False
                self._open_states[channel_id] = False

        # Master valve indicator
        master_row = 1 if not self._valve_map else 2
        master_wrap = QHBoxLayout()
        master_wrap.addWidget(QLabel("主阀状态："))
        self._master_led = self._build_led(diameter=18, color="#28a745", active=False)
        master_wrap.addWidget(self._master_led)
        master_wrap.addStretch()
        grid.addLayout(master_wrap, master_row, 0, 1, columns)

        box.setLayout(grid)
        return box

    def _build_command_panel(self) -> QGroupBox:
        box = QGroupBox("流量命令/序列")
        layout = QGridLayout()
        layout.setContentsMargins(10, 8, 10, 10)
        layout.setVerticalSpacing(10)
        layout.setHorizontalSpacing(12)

        self._mfc_b_spin = QDoubleSpinBox()
        self._mfc_b_spin.setRange(0, 5000)
        self._mfc_b_spin.setValue(1000)
        self._mfc_b_spin.setSuffix(" sccm")

        self._mfc_c_spin = QDoubleSpinBox()
        self._mfc_c_spin.setRange(0, 5000)
        self._mfc_c_spin.setValue(500)
        self._mfc_c_spin.setSuffix(" sccm")

        self._mfc_a_spin = QDoubleSpinBox()
        self._mfc_a_spin.setRange(0, 5000)
        self._mfc_a_spin.setValue(500)
        self._mfc_a_spin.setSuffix(" sccm")

        self._duration_spin = QSpinBox()
        self._duration_spin.setRange(1, 30)
        self._duration_spin.setValue(5)
        self._duration_spin.setSuffix(" 秒")

        labels = ["MFC B 载气", "MFC C 排空", "MFC A 气味/补偿", "持续时长"]
        widgets = [self._mfc_b_spin, self._mfc_c_spin, self._mfc_a_spin, self._duration_spin]
        for row, (label, widget) in enumerate(zip(labels, widgets, strict=False)):
            layout.addWidget(QLabel(label), row, 0)
            layout.addWidget(widget, row, 1)

        self._comp_preview = QLabel("A_comp (Rest) = 0.0 sccm")
        self._comp_preview.setStyleSheet("color: #555;")
        self._apply_button = QPushButton("应用")
        self._apply_button.setStyleSheet(
            "background-color: #0d6efd; color: white; font-weight: 600; padding: 6px 10px;"
        )
        self._apply_button.clicked.connect(self._handle_apply_clicked)
        self._apply_button.hide()
        layout.addWidget(self._comp_preview, len(labels), 0, 1, 2)

        self._start_button = QPushButton("启动")
        self._start_button.setCheckable(True)
        self._start_button.setStyleSheet(
            "background-color: #28a745; color: white; font-weight: 600; padding: 6px 10px;"
        )
        self._start_button.clicked.connect(self._handle_start_clicked)
        layout.addWidget(self._start_button, len(labels) + 1, 0, 1, 2)

        self._phase_label = QLabel("等待触发...")
        self._phase_label.setStyleSheet("color: #555; font-style: italic;")
        layout.addWidget(self._phase_label, len(labels) + 2, 0, 1, 2)
        self._mfc_a_spin.valueChanged.connect(self._update_comp_preview)
        self._mfc_c_spin.valueChanged.connect(self._update_comp_preview)

        box.setLayout(layout)
        return box

    def _build_waveform_panel(self) -> QGroupBox:
        box = QGroupBox("呼吸/气味波形")
        layout = QVBoxLayout()
        layout.setContentsMargins(6, 6, 6, 6)

        self._plot = pg.PlotWidget(background="k")
        self._plot.showGrid(x=True, y=True, alpha=0.15)
        self._plot.setLabel("left", "流速 (L/s)")
        self._plot.setLabel("bottom", "样本序号")

        self._breath_curve = self._plot.plot(pen=pg.mkPen("w", width=1.6))
        self._odor_curve = self._plot.plot(pen=pg.mkPen("#28a745", width=2))
        self._exhale_line = pg.InfiniteLine(angle=0, pen=pg.mkPen("#ff6b6b", style=Qt.DashLine), movable=False)
        self._inhale_line = pg.InfiniteLine(angle=0, pen=pg.mkPen("#f6c344", style=Qt.DotLine), movable=False)
        self._plot.addItem(self._exhale_line)
        self._plot.addItem(self._inhale_line)
        self._update_threshold_lines()

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setFixedHeight(10)
        self._progress.setTextVisible(True)
        self._progress.setFormat("%p%")

        layout.addWidget(self._plot)
        layout.addWidget(self._progress)
        box.setLayout(layout)
        return box

    def _build_status_panel(self) -> QGroupBox:
        box = QGroupBox("状态/阈值")
        layout = QVBoxLayout()
        layout.setSpacing(12)

        # Manual trigger (clickable LED-style)
        manual_row = QHBoxLayout()
        manual_row.setSpacing(6)
        self._manual_button = ManualLedButton(color="#17a2b8")
        self._manual_button.clicked.connect(self._toggle_manual_mode)
        manual_row.addWidget(self._manual_button, alignment=Qt.AlignLeft)
        manual_row.addWidget(QLabel("手动触发"))
        manual_row.addStretch()
        layout.addLayout(manual_row)

        # Inhale / exhale indicators
        inhale_row = QHBoxLayout()
        inhale_row.addWidget(QLabel("吸气指示"))
        self._inhale_led = self._build_led()
        self._inhale_led.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        inhale_row.addWidget(self._inhale_led)
        inhale_row.addStretch()

        exhale_row = QHBoxLayout()
        exhale_row.addWidget(QLabel("呼气指示"))
        self._exhale_led = self._build_led(color="#d9534f")
        self._exhale_led.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        exhale_row.addWidget(self._exhale_led)
        exhale_row.addStretch()

        layout.addLayout(inhale_row)
        layout.addLayout(exhale_row)
        layout.addWidget(self._airflow_label)
        layout.addWidget(self._applied_label)
        layout.addWidget(self._flow_message_label)
        layout.addStretch()
        box.setLayout(layout)
        return box

    def _handle_click(self, channel_id: int) -> None:
        desired = not self._selected_states.get(channel_id, False)
        self._selected_states[channel_id] = desired
        widget = self._buttons.get(channel_id)
        if widget:
            widget.set_checked(desired)
        selected = self.selected_valves()
        if selected:
            joined = ", ".join(str(ch) for ch in selected)
            self._phase_label.setText(f"已选择气道 {joined}，点击启动后通断")
        else:
            self._phase_label.setText("请选择气道后点击启动")
        self._update_master_led()

    def selected_valves(self) -> list[int]:
        return sorted(ch for ch, selected in self._selected_states.items() if selected)

    def set_valve_state(self, channel_id: int, is_open: bool) -> None:
        self._open_states[channel_id] = is_open
        self._update_master_led()

    def set_master_state(self, is_open: bool) -> None:
        self._master_open = bool(is_open)
        self._update_master_led()

    def reset_valve_selection(self) -> None:
        for channel_id in list(self._selected_states):
            self._selected_states[channel_id] = False
        for channel_id in list(self._open_states):
            self._open_states[channel_id] = False
        for widget in self._buttons.values():
            widget.set_checked(False)
        self._active_delivery_channels = []
        self._armed = False
        self._odor_active = False
        self._delivery_started_at = None
        self._current_odor_flow = 0.0
        self._progress.setValue(0)
        self._set_start_idle()
        self._master_open = False
        self._update_master_led()

    def apply_safety_state(self, state: str, reason: str = "", *, disabled: bool = False) -> None:
        blocked = disabled or self._config_error
        detail = reason or self._config_error_message
        if state == "SAFE" and not blocked:
            detail = ""
        self._status_label.setText(self._build_status_text(state, detail))
        self._status_label.setVisible(blocked or state != "SAFE")
        self._set_controls_enabled(not blocked, reason=detail or state)

    def show_warning(self, message: str) -> None:
        if self._config_error:
            # Preserve configuration error messaging
            return
        if not message:
            self._warning_label.hide()
            return
        self._warning_label.setText(message)
        self._warning_label.show()

    def ingest_breath_samples(self, samples, *, timestamp: float | None = None) -> None:
        calibrated = [(v + self._signal_offset) * self._signal_gain for v in samples]
        if calibrated:
            self._breath_buffer.append_samples(calibrated, timestamp=timestamp)
            odor_value = self._current_odor_flow if self._odor_active else 0.0
            self._odor_buffer.append_samples([odor_value] * len(calibrated), timestamp=timestamp)
            self._dirty = True

    def update_gating_state(self, state: str) -> None:
        inhale_active = state in {"INHALE_ABOVE", "INHALE"}
        exhale_active = state in {"EXHALE_ABOVE", "EXHALE"}
        self._set_led(self._inhale_led, inhale_active, "#f6c344")
        self._set_led(self._exhale_led, exhale_active, "#d9534f")
        if self._armed and not self._manual_trigger_enabled and exhale_active:
            self._start_odor_delivery(source="呼气触发")

    def set_thresholds(self, inhale: float, exhale: float) -> None:
        self._inhale_threshold = inhale
        self._exhale_threshold = exhale
        self._update_threshold_lines()
        self._dirty = True

    def set_targets(self, *, a: float, b: float, c: float) -> None:
        self._mfc_a_spin.setValue(float(a))
        self._mfc_b_spin.setValue(float(b))
        self._mfc_c_spin.setValue(float(c))
        self._update_comp_preview()

    def _handle_apply_clicked(self) -> None:
        self.set_applying(True)
        a = float(self._mfc_a_spin.value())
        b = float(self._mfc_b_spin.value())
        c = float(self._mfc_c_spin.value())
        self.apply_requested.emit(a, b, c)

    def set_applied_values(self, *, a: float, b: float, c: float, a_comp: float) -> None:
        self._applied_targets = {"A": float(a), "B": float(b), "C": float(c), "A_comp": float(a_comp)}
        self._applied_label.setText("已应用: " + self._format_applied_text())

    def get_applied_targets(self) -> dict[str, float]:
        return dict(self._applied_targets)

    def set_flow_message(self, message: str) -> None:
        self._flow_message_label.setText(message)

    def update_airflow(self, airflow: float) -> None:
        self._airflow_label.setText(f"当前气流：{airflow:.2f} sccm")

    def set_applying(self, applying: bool) -> None:
        enabled = not applying and not self._config_error
        self._start_button.setEnabled(enabled)

    def is_apply_enabled(self) -> bool:
        return bool(self._start_button.isEnabled())

    def _render_frame(self) -> None:
        if not self._dirty and not self._odor_active:
            return
        self._dirty = False
        breath_values = self._breath_buffer.values()
        odor_values = self._odor_buffer.values()

        if breath_values:
            self._breath_curve.setData(breath_values)
        else:
            self._breath_curve.clear()

        if odor_values:
            self._odor_curve.setData(odor_values)
        else:
            self._odor_curve.clear()

        self._update_delivery_progress()
        self._update_comp_preview()

    def _update_comp_preview(self) -> None:
        comp = float(self._mfc_a_spin.value()) + float(self._mfc_c_spin.value())
        self._comp_preview.setText(f"A_comp (Rest) = {comp:.1f} sccm")

    def _format_applied_text(self) -> str:
        return (
            f"A={self._applied_targets['A']:.1f} / "
            f"B={self._applied_targets['B']:.1f} / "
            f"C={self._applied_targets['C']:.1f} / "
            f"A_comp={self._applied_targets['A_comp']:.1f}"
        )

    def _update_delivery_progress(self) -> None:
        if not self._odor_active or self._delivery_started_at is None:
            return
        duration = max(float(self._duration_spin.value()), 0.1)
        elapsed = time.time() - self._delivery_started_at
        percent = min(int((elapsed / duration) * 100), 100)
        self._progress.setValue(percent)
        if elapsed >= duration:
            self._finish_delivery()
        else:
            self._dirty = True

    def _update_threshold_lines(self) -> None:
        self._inhale_line.setValue(self._inhale_threshold)
        self._exhale_line.setValue(self._exhale_threshold)

    def _handle_start_clicked(self, checked: bool) -> None:
        if checked:
            selected = self.selected_valves()
            if not selected:
                self._phase_label.setText("启动静息流量")
                self._progress.setValue(0)
                self.sequence_requested.emit(
                    "rest",
                    [],
                    self._mfc_a_spin.value(),
                    self._mfc_b_spin.value(),
                    self._mfc_c_spin.value(),
                )
                self._set_start_idle()
                return
            self._armed = True
            self._start_button.setText("中断")
            self._start_button.setStyleSheet(
                "background-color: #ffc107; color: black; font-weight: 600; padding: 6px 14px;"
            )
            if self._manual_trigger_enabled:
                self._start_odor_delivery(source="手动触发")
            else:
                self._phase_label.setText("等待呼气触发...")
        else:
            self._reset_flow_sequence(reason="已中断")

    def _start_odor_delivery(self, *, source: str) -> None:
        if self._odor_active:
            return
        selected = self.selected_valves()
        if not selected:
            self._phase_label.setText("启动静息流量")
            self.sequence_requested.emit(
                "rest",
                [],
                self._mfc_a_spin.value(),
                self._mfc_b_spin.value(),
                self._mfc_c_spin.value(),
            )
            self._set_start_idle()
            return
        self._odor_active = True
        self._delivery_started_at = time.time()
        self._active_delivery_channels = selected
        self._current_odor_flow = self._mfc_a_spin.value()
        self._phase_label.setText(f"气味发送中（{source}）")
        self._progress.setValue(0)
        self._dirty = True
        self.sequence_requested.emit(
            "stim_start",
            selected,
            self._mfc_a_spin.value(),
            self._mfc_b_spin.value(),
            self._mfc_c_spin.value(),
        )

    def abort_flow_sequence(self, reason: str) -> None:
        self._active_delivery_channels = []
        self._reset_flow_sequence(reason=reason, restore_rest=False)

    def _finish_delivery(self) -> None:
        reason = "发送完成，手动模式保持" if self._manual_trigger_enabled else "发送完成"
        self._reset_flow_sequence(reason=reason)

    def _reset_flow_sequence(self, reason: str | None = None, *, restore_rest: bool = True) -> None:
        active_channels = list(self._active_delivery_channels)
        self._armed = False
        self._odor_active = False
        self._delivery_started_at = None
        self._active_delivery_channels = []
        self._current_odor_flow = 0.0
        self._progress.setValue(0)
        self._set_start_idle()
        self._phase_label.setText(reason or "准备就绪")
        self._dirty = True
        if active_channels:
            if restore_rest:
                self.sequence_requested.emit(
                    "rest",
                    active_channels,
                    self._mfc_a_spin.value(),
                    self._mfc_b_spin.value(),
                    self._mfc_c_spin.value(),
                )
            else:
                self.valve_sequence_requested.emit("rest", active_channels)
        if restore_rest:
            if not active_channels:
                self.sequence_requested.emit(
                    "rest",
                    [],
                    self._mfc_a_spin.value(),
                    self._mfc_b_spin.value(),
                    self._mfc_c_spin.value(),
                )

    def _set_start_idle(self) -> None:
        self._start_button.blockSignals(True)
        self._start_button.setChecked(False)
        self._start_button.blockSignals(False)
        self._start_button.setText("启动")
        self._start_button.setStyleSheet(
            "background-color: #28a745; color: white; font-weight: 600; padding: 6px 14px;"
        )

    def _toggle_manual_mode(self, checked: bool) -> None:
        self._manual_trigger_enabled = checked
        if checked and self._armed and not self._odor_active:
            self._start_odor_delivery(source="手动触发")
        elif not checked and self._armed and not self._odor_active:
            self._phase_label.setText("等待呼气触发...")

    def _update_master_led(self) -> None:
        active = self._master_open
        self._set_led(self._master_led, active, "#28a745")

    @staticmethod
    def _build_led(*, diameter: int = 20, color: str = "#28a745", active: bool = False) -> QLabel:
        lbl = QLabel()
        lbl.setFixedSize(diameter, diameter)
        lbl.setStyleSheet(
            f"border-radius: {diameter // 2}px; background-color: {'#343a40' if not active else color}; border: 2px solid #6c757d;"
        )
        return lbl

    @staticmethod
    def _set_led(lbl: QLabel, active: bool, color: str) -> None:
        radius = max(lbl.width(), lbl.height(), 20) // 2
        if active:
            style = (
                f"border-radius: {radius}px; "
                f"background-color: {color}; "
                "border: 2px solid white;"
            )
        else:
            style = (
                f"border-radius: {radius}px; "
                "background-color: #343a40; "
                "border: 2px solid #6c757d;"
            )
        lbl.setStyleSheet(style)

    @staticmethod
    def _set_led_button(btn: QPushButton, *, active: bool, color: str) -> None:
        # Deprecated: replaced by ManualLedButton
        return

    @staticmethod
    def _build_status_text(state: str, reason: str) -> str:
        detail = f" | 原因：{reason}" if reason else ""
        return f"安全状态：{state}{detail}"
