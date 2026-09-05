from __future__ import annotations

import time
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    FluentWindow,
    InfoBadge,
    InfoLevel,
    NavigationItemPosition,
    PushButton,
    SimpleCardWidget,
    TitleLabel,
)
from qfluentwidgets import (
    FluentIcon as FIF,
)

from app.models import (
    AppState,
    ManualPresentationSnapshot,
    Telemetry,
)
from app.views.hardware_settings_view import HardwareSettingsView
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
        self.setMicaEffectEnabled(False)
        self.controller = controller
        self.state = state
        self._pending_presentation: ManualPresentationSnapshot | None = None
        self._presentation_flush_scheduled = False
        self._rendered_presentation_generation = -1
        self._closing = False
        self._last_rendered_connected = bool(state.telemetry.connected)
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
        self._build_settings_interface()
        self.render_telemetry(state.telemetry, hardware_ready=state.hardware_ready)

    def closeEvent(self, event) -> None:
        # InfoBarManager is process-global and keeps bars grouped by parent.
        # Remove the overlay while both Qt wrappers are still valid so a later
        # window cannot inherit a deleted bar from this parent.
        self._closing = True
        self._pending_presentation = None
        self.manual_experiment_view.clear_notice()
        super().closeEvent(event)

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
        self._connection_status_slot = QWidget(header)
        self._connection_status_slot.setFixedWidth(380)
        connection_layout = QHBoxLayout(self._connection_status_slot)
        connection_layout.setContentsMargins(0, 0, 0, 0)
        connection_layout.setSpacing(8)
        self._connection_action_label.setFixedWidth(220)
        connection_layout.addWidget(
            self._connection_badge, 0, Qt.AlignmentFlag.AlignVCenter
        )
        connection_layout.addWidget(self._connection_action_label)
        header_layout.addWidget(self._connection_status_slot)

        self._connection_action_slot = QWidget(header)
        self._connection_action_slot.setFixedWidth(
            max(108, self._connect_button.sizeHint().width())
        )
        action_layout = QHBoxLayout(self._connection_action_slot)
        action_layout.setContentsMargins(0, 0, 0, 0)
        action_layout.addWidget(self._connect_button)
        header_layout.addWidget(self._connection_action_slot)
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
        self.manual_experiment_view.settings_requested.connect(self.open_hardware_settings)
        layout.addWidget(self.manual_experiment_view, 1)

        self._manual_interface = interface
        self.addSubInterface(interface, FIF.LEAF, "手动实验")

    def _build_settings_interface(self) -> None:
        self.hardware_settings_view = HardwareSettingsView(self)
        self.hardware_settings_view.save_requested.connect(
            self.controller.handle_hardware_profile_save_requested
        )
        self.hardware_settings_view.mock_verify_requested.connect(
            self.controller.handle_hardware_mock_verify_requested
        )
        self.hardware_settings_view.physical_verify_requested.connect(
            self.controller.handle_hardware_physical_verify_requested
        )
        self.hardware_settings_view.verification_stop_requested.connect(
            self.controller.handle_hardware_verification_stop_requested
        )
        self._settings_navigation_item = self.addSubInterface(
            self.hardware_settings_view,
            FIF.SETTING,
            "设置",
            position=NavigationItemPosition.BOTTOM,
        )

    def open_hardware_settings(self) -> None:
        self.switchTo(self.hardware_settings_view)

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
    def _connection_summary(telemetry: Telemetry, *, hardware_ready: bool) -> str:
        if not telemetry.connected:
            return "设备未连接"
        if not hardware_ready:
            return "设备检查未通过"
        return {
            "SAFE": "设备已连接",
            "LOW_FLOW": "气流不足",
            "DATA_STALE": "设备通信中断",
            "RECOVERY_REQUIRED": "需要安全恢复",
            "FAULT": "设备状态异常",
            "UNKNOWN": "设备状态待确认",
        }.get(telemetry.safety_state, "设备状态异常")

    @staticmethod
    def _connection_action(telemetry: Telemetry, *, hardware_ready: bool) -> str:
        if telemetry.connected and not hardware_ready:
            return "请检查设备后重新连接"
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
        if self._shutdown_label.text() != text:
            self._shutdown_label.setText(text)
        if event and event.get("result") != "success":
            self.manual_experiment_view.show_condition_notice(
                "上次关闭未完成",
                text,
                source="last-shutdown",
                condition_key="last-shutdown-failed",
                severity="critical",
            )
        else:
            self.manual_experiment_view.resolve_notice_condition(source="last-shutdown")

    def render_telemetry(
        self,
        telemetry: Telemetry,
        *,
        expected_manual_flow_transition: bool = False,
        hardware_ready: bool | None = None,
    ) -> None:
        connected = bool(telemetry.connected)
        self._last_rendered_connected = connected
        effective_hardware_ready = (
            True if hardware_ready is None else bool(hardware_ready)
        )
        if self._connect_button.isVisible() == connected:
            self._connect_button.setVisible(not connected)
        summary = self._connection_summary(
            telemetry,
            hardware_ready=effective_hardware_ready,
        )
        telemetry_text = self._format_telemetry(telemetry)
        if self._telemetry_label.text() != telemetry_text:
            self._telemetry_label.setText(telemetry_text)
        if self._connection_badge.text() != summary:
            self._connection_badge.setText(summary)
        connection_action = self._connection_action(
            telemetry,
            hardware_ready=effective_hardware_ready,
        )
        if self._connection_action_label.text() != connection_action:
            self._connection_action_label.setText(connection_action)
        if self._connection_action_label.isVisible() != bool(connection_action):
            self._connection_action_label.setVisible(bool(connection_action))
        level = (
            InfoLevel.SUCCESS
            if connected and effective_hardware_ready and telemetry.safety_state == "SAFE"
            else InfoLevel.ERROR
        )
        if self._connection_badge.level != level:
            self._connection_badge.setLevel(level)
        current_state = telemetry.safety_state if connected else "DATA_STALE"
        self.manual_experiment_view.set_header_safety_state(current_state)
        if current_state == "LOW_FLOW" and expected_manual_flow_transition:
            # A=0 and supply restoration deliberately cross LOW_FLOW.  The
            # coherent manual presentation still exposes the real telemetry
            # state, but this expected, receipt-bounded transition is not a
            # user-actionable safety episode and must not create an InfoBar.
            self.manual_experiment_view.clear_safety_notice()
        elif current_state == "LOW_FLOW":
            self.manual_experiment_view.show_condition_notice(
                "气流不足",
                "已停止相关操作，请检查供气、管路和流量设置。",
                source="safety",
                condition_key=("safety", "LOW_FLOW"),
                severity="error",
            )
        elif current_state == "DATA_STALE" and connected:
            self.manual_experiment_view.show_condition_notice(
                "设备数据中断",
                "请检查设备连接和通信线路。",
                source="safety",
                condition_key=("safety", "DATA_STALE"),
                severity="error",
            )
        elif connected and current_state != "SAFE":
            self.manual_experiment_view.show_condition_notice(
                "当前状态不允许操作",
                self._connection_action(
                    telemetry,
                    hardware_ready=effective_hardware_ready,
                )
                + "，确认正常后再继续。",
                source="safety",
                condition_key=("safety", current_state),
                severity="error",
            )
        else:
            self.manual_experiment_view.clear_safety_notice()

    def queue_presentation(
        self,
        presentation: ManualPresentationSnapshot,
        *,
        queued: bool = True,
    ) -> None:
        """合并同一 GUI event-loop turn 内的帧，只保留最新 generation。"""

        if self._closing:
            return
        if presentation.generation <= self._rendered_presentation_generation:
            return
        pending = self._pending_presentation
        if pending is None or presentation.generation > pending.generation:
            self._pending_presentation = presentation
        if not queued:
            self._flush_presentation()
            return
        if self._presentation_flush_scheduled:
            return
        self._presentation_flush_scheduled = True
        QTimer.singleShot(0, self._flush_presentation)

    def _flush_presentation(self) -> None:
        self._presentation_flush_scheduled = False
        if self._closing:
            self._pending_presentation = None
            return
        presentation = self._pending_presentation
        self._pending_presentation = None
        if (
            presentation is None
            or presentation.generation <= self._rendered_presentation_generation
        ):
            return
        telemetry = Telemetry(
            airflow=presentation.airflow,
            safety_state=presentation.safety_state,
            safety_reason=presentation.safety_reason,
            connected=presentation.connected,
            timestamp=presentation.telemetry_timestamp,
        )
        self.render_telemetry(
            telemetry,
            expected_manual_flow_transition=(
                presentation.safety_state == "LOW_FLOW"
                and presentation.expected_flow_transition
            ),
            hardware_ready=presentation.hardware_ready,
        )
        self.manual_experiment_view.render_presentation(presentation)
        self._rendered_presentation_generation = presentation.generation

    def update_status(self, message: str) -> None:
        friendly = user_facing_text(message)
        self._status_label.setText(friendly)
        if not friendly or any(term in friendly for term in _INTERNAL_UI_TERMS):
            self.manual_experiment_view.clear_notice_event(source="status")
            return
        if self.state.telemetry.connected and self.state.telemetry.safety_state != "SAFE":
            return
        if not self.manual_experiment_view._is_actionable_notice("", friendly):
            self.manual_experiment_view.clear_notice_event(source="status")
            return
        severity = "error"
        if (
            self.manual_experiment_view.current_notice_severity == "error"
            and self.manual_experiment_view.current_notice_title in SAFETY_NOTICE_TITLES
        ):
            return
        title = "操作未完成" if severity == "error" else "状态"
        self.manual_experiment_view.show_notice(
            title,
            friendly,
            severity=severity,
            source="status",
            notice_key="operation-status",
        )

    def render_actuation_alert(self, message: str, *, severe: bool) -> None:
        friendly = user_facing_text(message)
        if self._actuation_alert_label.text() != friendly:
            self._actuation_alert_label.setText(friendly)
        if self._actuation_alert_label.isVisible() != bool(friendly):
            self._actuation_alert_label.setVisible(bool(friendly))
        if severe and friendly:
            self.manual_experiment_view.show_notice(
                "需要立即处理",
                friendly,
                severity="critical",
                notice_key="actuation-alert",
                source="actuation-alert",
            )
        else:
            self.manual_experiment_view.clear_notice_event(source="actuation-alert")

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
                source="self-check",
                notice_key="self-check-failed",
            )
        else:
            self.manual_experiment_view.clear_notice_event(source="self-check")
