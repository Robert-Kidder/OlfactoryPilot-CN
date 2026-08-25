from __future__ import annotations

import time
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    FluentWindow,
    InfoBadge,
    InfoLevel,
    PushButton,
    SimpleCardWidget,
    TitleLabel,
)
from qfluentwidgets import (
    FluentIcon as FIF,
)

from app.models import AppState, Telemetry
from app.views.manual_experiment_view import SAFETY_NOTICE_TITLES, ManualExperimentView
from app.views.product_text import user_facing_text

if TYPE_CHECKING:
    from app.controllers import MainController


_INTERNAL_UI_TERMS = (
    "Controller",
    "Worker",
    "HAL",
    "snapshot",
    "intent",
    "epoch",
    "owner",
    "lease",
    "Mock",
    "关闭阶段",
)


class MainWindow(FluentWindow):
    """The phase-one product shell containing only manual experiment controls."""

    def __init__(self, controller: MainController, state: AppState) -> None:
        super().__init__()
        self.controller = controller
        self.state = state
        self._last_safety_notice_state = "SAFE"
        self._last_safety_notice_connected = False
        self._safety_transition_sequence = 0
        self.setWindowTitle(state.window_title)
        self.setMinimumSize(1180, 720)
        self.resize(1360, 820)
        self.navigationInterface.setReturnButtonVisible(False)
        self.navigationInterface.setExpandWidth(180)

        self._status_label = BodyLabel(user_facing_text(state.status_message), self)
        self._telemetry_label = BodyLabel(self)
        self._shutdown_label = BodyLabel(self._format_shutdown(state.last_shutdown_event), self)
        self._self_check_label = BodyLabel(self)
        self._self_check_label.setWordWrap(True)
        for diagnostic in (
            self._status_label,
            self._telemetry_label,
            self._shutdown_label,
            self._self_check_label,
        ):
            diagnostic.hide()

        self._build_actions()
        self._build_manual_interface()
        self.render_telemetry(state.telemetry)

    def _build_actions(self) -> None:
        self._connect_button = PushButton(FIF.CONNECT, "连接设备", self)
        self._reset_button = PushButton(FIF.SYNC, "重置设备", self)
        self._stop_button = PushButton(FIF.POWER_BUTTON, "全局停止", self)
        self._help_button = PushButton(FIF.HELP, "帮助", self)
        self._stop_button.setObjectName("globalStopButton")
        self._stop_button.setStyleSheet(
            "QPushButton { color: #FFD5D1; background: rgba(168, 48, 43, 92); "
            "border: 1px solid #9A4D48; border-radius: 6px; padding: 7px 14px; }"
            "QPushButton:hover { background: rgba(190, 57, 50, 122); border-color: #D26A63; }"
            "QPushButton:pressed { background: rgba(122, 36, 32, 145); }"
            "QPushButton:disabled { color: #787E7B; background: #282D2B; border-color: #3A403D; }"
        )
        self._connect_button.clicked.connect(self.controller.connect_hardware)
        self._reset_button.clicked.connect(self.controller.reset_hardware)
        self._stop_button.clicked.connect(self.controller.stop_hardware)
        self._help_button.clicked.connect(self.controller.open_help_manual)
        self._reset_button.hide()
        self._help_button.hide()

    def _build_manual_interface(self) -> None:
        interface = QWidget(self)
        interface.setObjectName("manualExperimentInterface")
        palette = interface.palette()
        palette.setColor(QPalette.ColorRole.Window, QColor("#101613"))
        interface.setPalette(palette)
        interface.setAutoFillBackground(True)
        layout = QVBoxLayout(interface)
        layout.setContentsMargins(20, 14, 20, 18)
        layout.setSpacing(12)

        header = SimpleCardWidget(interface)
        header.setFixedHeight(72)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(18, 10, 14, 10)
        header_layout.setSpacing(10)

        title = TitleLabel("手动实验", header)
        self._actuation_alert_label = CaptionLabel("", header)
        self._actuation_alert_label.setStyleSheet("color: #FF9A92;")
        self._actuation_alert_label.setVisible(False)
        self._actuation_alert_label.setMaximumWidth(360)
        header_layout.addWidget(title)
        header_layout.addSpacing(12)
        header_layout.addWidget(self._actuation_alert_label)
        header_layout.addStretch(1)

        self._connection_badge = InfoBadge.error("设备未连接", parent=header)
        self._connection_badge.setAccessibleName("设备连接状态")
        self._connection_action_label = CaptionLabel("", header)
        self._connection_action_label.setStyleSheet("color: #FF9A92;")
        self._connection_action_label.setMaximumWidth(220)
        self._connection_action_label.setVisible(False)
        header_layout.addWidget(self._connection_badge, 0, Qt.AlignmentFlag.AlignVCenter)
        header_layout.addWidget(self._connection_action_label)
        header_layout.addWidget(self._connect_button)
        header_layout.addWidget(self._stop_button)
        layout.addWidget(header)

        self.manual_experiment_view = ManualExperimentView(interface)
        self.manual_experiment_view.release_requested.connect(
            self.controller.handle_manual_release_requested
        )
        self.manual_experiment_view.supply_requested.connect(
            self.controller.handle_manual_supply_requested
        )
        self.manual_experiment_view.stop_requested.connect(
            lambda _request: self.controller.handle_manual_stop_requested()
        )
        layout.addWidget(self.manual_experiment_view, 1)

        self._manual_interface = interface
        self.addSubInterface(interface, FIF.LEAF, "手动实验")

    def _format_shutdown(self, event: dict | None) -> str:
        if not event:
            return "上次关闭：暂无记录"
        ts_value = event.get("ts") or event.get("timestamp")
        ts_text = (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts_value))
            if ts_value
            else "未知时间"
        )
        result = event.get("result") or ""
        status_word = "已正常关闭" if result == "success" else "关闭未完成"
        reason = event.get("error") or event.get("reason") or ""
        return user_facing_text(f"上次关闭：{status_word}（{ts_text}）{reason}")

    @staticmethod
    def _connection_summary(telemetry: Telemetry) -> str:
        if not telemetry.connected:
            return "设备未连接"
        return {
            "SAFE": "设备已连接",
            "LOW_FLOW": "气流不足",
            "DATA_STALE": "设备通信中断",
            "RECOVERY_REQUIRED": "需要安全恢复",
            "FAULT": "设备状态异常",
            "UNKNOWN": "设备状态待确认",
        }.get(telemetry.safety_state, "设备状态异常")

    @staticmethod
    def _connection_action(telemetry: Telemetry) -> str:
        if not telemetry.connected or telemetry.safety_state == "SAFE":
            return ""
        return {
            "LOW_FLOW": "请检查供气和管路",
            "DATA_STALE": "请检查设备连接",
            "RECOVERY_REQUIRED": "请执行全局停止并检查设备",
        }.get(telemetry.safety_state, "请检查设备状态")

    def _format_telemetry(self, telemetry: Telemetry) -> str:
        stale_hint = "（数据过期）" if telemetry.safety_state == "DATA_STALE" else ""
        reason = f" | 原因: {telemetry.safety_reason}" if telemetry.safety_reason else ""
        return (
            f"连接: {'是' if telemetry.connected else '否'} | "
            f"气流: {telemetry.airflow:.2f} | "
            f"安全: {telemetry.safety_state}{stale_hint}{reason}"
        )

    def render_last_shutdown(self, event: dict | None) -> None:
        text = self._format_shutdown(event)
        self._shutdown_label.setText(text)
        if event and event.get("result") != "success":
            self.manual_experiment_view.show_notice("上次关闭未完成", text, severity="error")

    def render_telemetry(self, telemetry: Telemetry) -> None:
        connected = bool(telemetry.connected)
        self._connect_button.setVisible(not connected)
        summary = self._connection_summary(telemetry)
        self._telemetry_label.setText(self._format_telemetry(telemetry))
        self._connection_badge.setText(summary)
        connection_action = self._connection_action(telemetry)
        self._connection_action_label.setText(connection_action)
        self._connection_action_label.setVisible(bool(connection_action))
        self._connection_badge.setLevel(
            InfoLevel.SUCCESS
            if connected and telemetry.safety_state == "SAFE"
            else InfoLevel.ERROR
        )
        current_state = telemetry.safety_state if connected else "DATA_STALE"
        self.manual_experiment_view.set_header_safety_state(
            current_state
        )
        previous_state = self._last_safety_notice_state
        entered_connected_abnormal = bool(
            connected
            and not self._last_safety_notice_connected
            and current_state != "SAFE"
        )
        self._last_safety_notice_connected = connected
        if current_state == previous_state and not entered_connected_abnormal:
            return
        self._last_safety_notice_state = current_state
        self._safety_transition_sequence += 1
        transition_key = (
            "safety",
            self._safety_transition_sequence,
            previous_state,
            current_state,
        )
        if current_state == "LOW_FLOW":
            self.manual_experiment_view.show_notice(
                "气流不足",
                "已停止相关操作，请检查供气、管路和流量设置。",
                severity="error",
                notice_key=transition_key,
            )
        elif current_state == "DATA_STALE" and connected:
            self.manual_experiment_view.show_notice(
                "设备数据中断",
                "请检查设备连接和通信线路。",
                severity="error",
                notice_key=transition_key,
            )
        elif connected and current_state != "SAFE":
            self.manual_experiment_view.show_notice(
                "当前状态不允许操作",
                self._connection_action(telemetry) + "，确认正常后再继续。",
                severity="error",
                notice_key=transition_key,
            )
        elif current_state == "SAFE":
            self.manual_experiment_view.clear_safety_notice()

    def update_status(self, message: str) -> None:
        friendly = user_facing_text(message)
        self._status_label.setText(friendly)
        if not friendly or any(term in friendly for term in _INTERNAL_UI_TERMS):
            return
        if self.state.telemetry.connected and self.state.telemetry.safety_state != "SAFE":
            return
        severity = "error" if self.manual_experiment_view._is_actionable_notice("", friendly) else "info"
        if (
            self.manual_experiment_view.current_notice_severity == "error"
            and self.manual_experiment_view.current_notice_title in SAFETY_NOTICE_TITLES
        ):
            return
        title = "操作未完成" if severity == "error" else "状态"
        self.manual_experiment_view.show_notice(title, friendly, severity=severity)

    def render_actuation_alert(self, message: str, *, severe: bool) -> None:
        friendly = user_facing_text(message)
        self._actuation_alert_label.setText(friendly)
        self._actuation_alert_label.setVisible(bool(friendly))
        if severe and friendly:
            self.manual_experiment_view.show_notice("需要立即处理", friendly, severity="error")

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
        del tooltips
        self._connect_button.setEnabled(connect_enabled)
        self._reset_button.setEnabled(reset_enabled)
        self._stop_button.setEnabled(stop_enabled)
        self._help_button.setEnabled(True)

    def render_self_check(self, results, ready: bool) -> None:
        failed = [
            item
            for item in results
            if getattr(item, "status", "") not in {"PASS", "通过"}
        ]
        summary = [
            user_facing_text(f"{item.name}：{item.reason}。建议：{item.suggestion}")
            for item in failed
        ]
        diagnostic_summary = [
            f"{item.name}: {item.status}（{item.reason}；建议：{item.suggestion}）"
            for item in failed
        ]
        effective_ready = ready and not summary
        prefix = "最近自检：通过" if effective_ready else "最近自检：失败"
        self._self_check_label.setText(
            prefix
            + ("\n" + "\n".join(diagnostic_summary) if diagnostic_summary else "")
        )
        if not effective_ready:
            self.manual_experiment_view.show_notice(
                "连接失败",
                "；".join(summary) or "设备检查未通过，请检查连接后重试。",
                severity="error",
            )
