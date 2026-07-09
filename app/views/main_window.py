from __future__ import annotations

import time
from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QStatusBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.models import AppState, Telemetry
from app.views.calibration_view import CalibrationView
from app.views.pretest_view import PreTestView
from app.views.protocol_view import ProtocolView

if TYPE_CHECKING:
    from app.controllers import MainController


class MainWindow(QMainWindow):
    def __init__(self, controller: MainController, state: AppState) -> None:
        super().__init__()
        self.controller = controller
        self.state = state
        self.setWindowTitle(state.window_title)
        self.tabs = QTabWidget()
        self._status_label = QLabel(state.status_message)
        self._telemetry_label = QLabel(self._format_telemetry(state.telemetry))
        self._shutdown_label = QLabel(self._format_shutdown(state.last_shutdown_event))
        self._self_check_label = QLabel("尚未进行硬件自检")
        for label in (
            self._status_label,
            self._telemetry_label,
            self._shutdown_label,
            self._self_check_label,
        ):
            label.setWordWrap(True)
            label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._connect_button = QPushButton("连接")
        self._reset_button = QPushButton("重置")
        self._stop_button = QPushButton("停止")
        self._help_button = QPushButton("帮助")
        self._connect_button.clicked.connect(self.controller.connect_hardware)
        self._reset_button.clicked.connect(self.controller.reset_hardware)
        self._stop_button.clicked.connect(self.controller.stop_hardware)
        self._help_button.clicked.connect(self.controller.open_help_manual)
        self._build_layout()

    def _build_layout(self) -> None:
        self.calibration_view = CalibrationView(
            inhale_threshold=self.state.inhale_threshold,
            exhale_threshold=self.state.exhale_threshold,
        )
        self.pretest_view = PreTestView(
            valve_map=self.state.get_active_valve_map(),
            variant=self.state.hardware_variant,
            master_valve=self.state.master_valve_line,
            inhale_threshold=self.state.inhale_threshold,
            exhale_threshold=self.state.exhale_threshold,
            signal_offset=self.state.signal_offset,
            signal_gain=self.state.signal_gain,
        )
        self.protocol_view = ProtocolView()
        self.pretest_view.toggle_requested.connect(self.controller.handle_valve_toggle_request)
        self.pretest_view.apply_requested.connect(self.controller.handle_apply_request)
        self.pretest_view.valve_sequence_requested.connect(self.controller.handle_valve_sequence_request)
        self.pretest_view.sequence_requested.connect(self.controller.handle_pretest_sequence_request)
        self.protocol_view.load_requested.connect(self.controller.handle_protocol_file_selected)
        self.protocol_view.start_requested.connect(self.controller.handle_protocol_start_requested)
        self.protocol_view.stop_requested.connect(self.controller.handle_protocol_stop_requested)
        self.protocol_view.next_trial_requested.connect(self.controller.handle_protocol_next_requested)
        self.tabs.addTab(self._build_tab("概览", "硬件连接、安全状态概览"), "概览")
        self.tabs.addTab(self.calibration_view, "校准")
        self.tabs.addTab(self.pretest_view, "预检")
        self.tabs.addTab(self.protocol_view, "协议")

        container = QWidget()
        layout = QVBoxLayout()
        toolbar = QWidget()
        toolbar_layout = QHBoxLayout()
        for btn in (
            self._connect_button,
            self._reset_button,
            self._stop_button,
            self._help_button,
        ):
            toolbar_layout.addWidget(btn)
        toolbar_layout.addStretch()
        toolbar.setLayout(toolbar_layout)
        layout.addWidget(toolbar)
        layout.addWidget(self.tabs)
        layout.addWidget(self._self_check_label)
        container.setLayout(layout)
        self.setCentralWidget(container)

        status_bar = QStatusBar()
        status_bar.addPermanentWidget(self._shutdown_label)
        status_bar.addPermanentWidget(self._telemetry_label)
        status_bar.addWidget(self._status_label)
        self.setStatusBar(status_bar)

    def _build_tab(self, title: str, body: str) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout()
        layout.addWidget(QLabel(title))
        layout.addWidget(QLabel(body))
        widget.setLayout(layout)
        return widget

    def _format_shutdown(self, event: dict | None) -> str:
        if not event:
            return "上次关闭：暂无记录"
        ts_value = event.get("ts") or event.get("timestamp")
        ts_text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts_value)) if ts_value else "未知时间"
        result = event.get("result") or ""
        status_word = "已安全关闭" if result == "success" else "关闭未完成"
        reason = event.get("error") or event.get("reason") or ""
        return f"上次关闭：{status_word}（{ts_text}）{reason}"

    def _format_telemetry(self, telemetry: Telemetry) -> str:
        stale_hint = "（数据过期）" if telemetry.safety_state == "DATA_STALE" else ""
        reason = f" | 原因: {telemetry.safety_reason}" if telemetry.safety_reason else ""
        return (
            f"连接: {'是' if telemetry.connected else '否'} | "
            f"气流: {telemetry.airflow:.2f} | "
            f"安全: {telemetry.safety_state}{stale_hint}{reason}"
        )

    def render_last_shutdown(self, event: dict | None) -> None:
        self._shutdown_label.setText(self._format_shutdown(event))

    def render_telemetry(self, telemetry: Telemetry) -> None:
        self._telemetry_label.setText(self._format_telemetry(telemetry))
        if hasattr(self, "calibration_view"):
            self.calibration_view.apply_safety_state(
                telemetry.safety_state,
                telemetry.timestamp,
            )

    def update_status(self, message: str) -> None:
        self._status_label.setText(message)

    def ingest_breath_samples(self, samples, *, timestamp: float | None = None) -> None:
        if hasattr(self, "calibration_view"):
            self.calibration_view.ingest_samples(samples, timestamp=timestamp)
        if hasattr(self, "pretest_view"):
            self.pretest_view.ingest_breath_samples(samples, timestamp=timestamp)

    def update_gating_state(self, state: str) -> None:
        if hasattr(self, "calibration_view"):
            self.calibration_view.update_gating_state(state)
        if hasattr(self, "pretest_view"):
            self.pretest_view.update_gating_state(state)

    def update_toolbar(
        self,
        *,
        connect_enabled: bool,
        reset_enabled: bool,
        stop_enabled: bool,
        tooltips: dict[str, str] | None = None,
    ) -> None:
        self._connect_button.setEnabled(connect_enabled)
        self._reset_button.setEnabled(reset_enabled)
        self._stop_button.setEnabled(stop_enabled)
        self._help_button.setEnabled(True)

    def render_self_check(self, results, ready: bool) -> None:
        status_word = "通过" if ready else "失败"
        summary = [
            f"{item.name}: {item.status}（{item.reason}，建议：{item.suggestion}）"
            for item in results
        ]
        prefix = f"最近自检：{status_word}"
        text = prefix + ("\n" + "\n".join(summary) if summary else "")
        self._self_check_label.setText(text)
