from __future__ import annotations

import time
from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.models import AppState, Telemetry
from app.views.calibration_view import CalibrationView
from app.views.cleaning_view import CleaningView
from app.views.hardware_settings_view import HardwareSettingsView
from app.views.manual_experiment_view import ManualExperimentView
from app.views.pretest_view import PreTestView
from app.views.product_text import user_facing_text
from app.views.protocol_view import ProtocolView
from app.views.session_view import SessionView

if TYPE_CHECKING:
    from app.controllers import MainController


PRODUCT_STYLE = """
QMainWindow, QDialog, QWidget#productRoot, QWidget#manualExperiment,
QWidget#hardwareSettings { background: #1d2322; color: #edf1ed; }
QWidget { font-family: "Microsoft YaHei UI"; font-size: 13px; color: #edf1ed; }
QFrame#topBar { background: #151a1a; border-bottom: 1px solid #303735; }
QLabel#brand { color: #f1f3ef; font-size: 15px; font-weight: 700; }
QLabel#location, QLabel#mutedText { color: #aeb8b3; }
QLabel#connectionDot[connected="true"] { color: #58ae88; }
QLabel#connectionDot[connected="false"] { color: #df6b64; }
QLabel#connectionText { color: #cbd3cf; }
QPushButton {
    min-height: 34px; padding: 0 13px; border: 1px solid #66716d;
    border-bottom: 3px solid #202625; border-radius: 6px; color: #f1f4f2;
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #56615d,stop:1 #39423f);
}
QPushButton:hover { border-color: #9eaaa5; background: #5f6a66; }
QPushButton:focus { border: 2px solid #f0bd60; border-bottom: 3px solid #7d5724; }
QPushButton:pressed { padding-top: 4px; border-bottom: 1px solid #202625; background: #29302e; }
QPushButton:disabled { color: #78817d; border-color: #3d4642; border-bottom-color: #242a28; background: #2a302e; }
QPushButton#globalStopButton, QPushButton#experimentStopButton {
    color: #ffd0cc; border-color: #8b4c47; border-bottom-color: #291817; background: #4a2926; font-weight: 650;
}
QPushButton#globalStopButton:hover, QPushButton#experimentStopButton:hover { background: #65332f; border-color: #c56760; }
QPushButton#globalStopButton:pressed, QPushButton#experimentStopButton:pressed { background: #321d1b; border-bottom-width: 1px; }
QPushButton#settingsButton { background: transparent; border-color: #3e4643; }
QFrame#waveCard { background: #222928; border: 1px solid #414946; border-radius: 8px; }
QFrame#waveHeader { background: #262d2c; border-bottom: 1px solid #3b4340; }
QLabel#sectionTitle { color: #eef1ee; font-weight: 650; }
QLabel#flowReading { color: #f5cd7a; font-family: "Cascadia Mono"; font-size: 19px; font-weight: 700; }
QFrame#controlShelf {
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #3a413f,stop:1 #303735);
    border: 1px solid #4b5350; border-radius: 7px;
}
QFrame#consoleModule { background: #303735; border: 1px solid #202625; border-radius: 5px; }
QLabel#moduleTitle { color: #d1d8d4; font-size: 12px; font-weight: 700; }
QFrame#setterCard, QFrame#computedSetter {
    background: #242a29; border: 1px solid #4c5451; border-radius: 5px;
}
QFrame#computedSetter { background: #202625; }
QLabel#setterLabel { color: #bdc5c1; font-size: 11px; }
QDoubleSpinBox, QSpinBox, QLineEdit, QComboBox {
    min-height: 32px; padding: 0 8px; color: #ffe092;
    background: #171c1b; border: 1px solid #4b5350; border-radius: 4px;
    selection-background-color: #8c642d; selection-color: #ffffff;
}
QLineEdit { placeholder-text-color: #7f8a85; }
QDoubleSpinBox:focus, QSpinBox:focus, QLineEdit:focus, QComboBox:focus { border-color: #e2ad50; }
QDoubleSpinBox:disabled, QSpinBox:disabled, QLineEdit:disabled, QComboBox:disabled { color: #8d9893; background: #252b29; }
QComboBox QAbstractItemView { color: #eef2ef; background: #242b29; selection-background-color: #6e5129; border: 1px solid #59635f; }
QFrame#stepper { background: #171c1b; border: 1px solid #4b5350; border-radius: 5px; }
QFrame#stepper QDoubleSpinBox { border: 0; border-radius: 0; background: transparent; }
QPushButton#stepButton { min-width: 30px; max-width: 30px; min-height: 32px; padding: 0; border: 0; border-radius: 3px; background: #3d4743; font-size: 18px; font-weight: 700; }
QPushButton#stepButton:hover { color: #ffe29e; background: #596560; }
QPushButton#stepButton:focus { border: 1px solid #f0bd60; }
QPushButton#stepButton:pressed { padding-top: 3px; color: #fff0c8; background: #202725; }
QPushButton#stepButton:disabled { color: #59625e; background: #292f2d; }
QPushButton#supplyButton { border-color: #75ad91; border-bottom-color: #173a2a; background: #387459; }
QPushButton#supplyButton:hover { background: #43886a; }
QPushButton#supplyButton:pressed { background: #28523f; border-bottom-width: 1px; }
QLabel#supplyState { color: #8bc1a8; }
QFrame#portBay { background: #242a29; border: 1px solid #191e1d; border-radius: 5px; }
QPushButton#releaseButton, QPushButton#primaryButton {
    color: #24190a; border-color: #ffd27e; border-bottom-color: #74491c; font-weight: 750; background: #e1aa49;
}
QPushButton#releaseButton:hover, QPushButton#primaryButton:hover { background: #f0bd5f; }
QPushButton#releaseButton:pressed, QPushButton#primaryButton:pressed { background: #b77a2f; border-bottom-width: 1px; }
QPushButton#releaseButton:disabled, QPushButton#primaryButton:disabled { color: #858f8a; border-color: #46504c; border-bottom-color: #252b29; background: #2c3330; }
QPushButton#primaryButton:disabled { color: #929d98; background: #2c3330; border-color: #46504c; }
QLabel#countdownLabel { color: #f5cd7a; font-family: "Cascadia Mono"; font-size: 15px; }
QFrame#noticeStrip { background: #2c2920; border: 1px solid #8b6b32; border-radius: 5px; }
QFrame#noticeStrip[severity="error"] { background: #352321; border-color: #9b4f49; }
QLabel#pageStatus { color: #f1cf8b; font-weight: 700; padding-right: 12px; }
QLabel#pageDetail { color: #aeb7b3; }
QLabel#topAlert { color: #ff9f98; font-weight: 650; }
QFrame#settingsCard, QFrame#advancedPanel { background: #242a29; border: 1px solid #414946; border-radius: 7px; }
QLabel#settingsHeading { color: #edf1ed; font-size: 18px; font-weight: 700; }
QPushButton#settingsPortButton {
    min-height: 48px; max-height: 54px; padding: 3px 5px; color: #89938e; border-color: #46504c; background: #2b3230;
}
QPushButton#settingsPortButton[channelEnabled="true"] { color: #e8ece9; border-color: #6d7974; background: #46504c; }
QPushButton#settingsPortButton:checked { color: #fff2d1; border: 2px solid #ffd583; background: #8a632e; }
QPushButton#settingsPortButton[selected="true"] { color: #251b0d; border: 2px solid #ffd583; background: #d89b3f; }
QToolButton#advancedToggle { color: #d9dfdc; border: 1px solid #46504c; border-radius: 5px; padding: 8px; text-align: left; background: #29302e; }
QToolButton#advancedToggle:hover { color: #ffe19a; border-color: #707c77; background: #343d3a; }
QToolButton#advancedToggle:pressed { padding-top: 11px; background: #202624; }
QLabel#validationMessage[valid="true"] { color: #7ec6a4; }
QLabel#validationMessage[valid="false"] { color: #ff8e86; }
QLabel#verificationStatus { color: #9fcab5; }
QCheckBox { color: #e2e7e4; spacing: 8px; }
QCheckBox::indicator { width: 17px; height: 17px; border: 1px solid #73807a; border-radius: 3px; background: #171c1b; }
QCheckBox::indicator:hover { border-color: #e2ad50; }
QCheckBox::indicator:checked { border-color: #f1c66d; background: #d69b3e; }
QCheckBox:disabled { color: #7e8883; }
QScrollArea#settingsScroll, QWidget#settingsScrollContent { border: 0; background: #1d2322; }
QScrollBar:vertical { width: 12px; background: #1a201f; margin: 0; }
QScrollBar::handle:vertical { min-height: 34px; background: #4b5651; border-radius: 5px; }
QScrollBar::handle:vertical:hover { background: #66736d; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: #1a201f; }
"""


class MainWindow(QMainWindow):
    def __init__(self, controller: MainController, state: AppState) -> None:
        super().__init__()
        self.controller = controller
        self.state = state
        self._developer_mode = bool(controller.config.get("developer_mode", False))
        self.setWindowTitle(state.window_title)
        self.setMinimumSize(1080, 680)
        self.resize(1280, 760)
        self.setStyleSheet(PRODUCT_STYLE)

        self._status_label = QLabel(user_facing_text(state.status_message), self)
        self._telemetry_label = QLabel(self)
        self._shutdown_label = QLabel(self._format_shutdown(state.last_shutdown_event), self)
        self._actuation_alert_label = QLabel(self)
        self._self_check_label = QLabel(self)
        self._self_check_label.setWordWrap(True)
        self._build_actions()
        self._build_product_layout()
        self._build_settings_dialog()
        self._build_legacy_compatibility_host()
        self.render_telemetry(state.telemetry)

    def _build_actions(self) -> None:
        self._connect_button = QPushButton("连接设备", self)
        self._reset_button = QPushButton("重置设备", self)
        self._stop_button = QPushButton("停止实验", self)
        self._help_button = QPushButton("帮助", self)
        self.settings_button = QPushButton("设置", self)
        self._stop_button.setObjectName("globalStopButton")
        self.settings_button.setObjectName("settingsButton")
        self._connect_button.clicked.connect(self.controller.connect_hardware)
        self._reset_button.clicked.connect(self.controller.reset_hardware)
        self._stop_button.clicked.connect(self.controller.stop_hardware)
        self._help_button.clicked.connect(self.controller.open_help_manual)

    def _build_product_layout(self) -> None:
        root = QWidget()
        root.setObjectName("productRoot")
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        topbar = QFrame()
        topbar.setObjectName("topBar")
        topbar.setFixedHeight(48)
        top_layout = QHBoxLayout(topbar)
        top_layout.setContentsMargins(17, 0, 14, 0)
        top_layout.setSpacing(8)
        brand = QLabel("OlfactoryPilot")
        brand.setObjectName("brand")
        location = QLabel("实验  /  手动实验")
        location.setObjectName("location")
        self._connection_dot = QLabel("●")
        self._connection_dot.setObjectName("connectionDot")
        self._connection_text = QLabel("设备未连接")
        self._connection_text.setObjectName("connectionText")
        self._actuation_alert_label.setObjectName("topAlert")
        self._actuation_alert_label.setVisible(False)
        top_layout.addWidget(brand)
        top_layout.addSpacing(14)
        top_layout.addWidget(location)
        top_layout.addWidget(self._actuation_alert_label)
        top_layout.addStretch()
        top_layout.addWidget(self._connection_dot)
        top_layout.addWidget(self._connection_text)
        top_layout.addWidget(self._connect_button)
        top_layout.addWidget(self.settings_button)
        top_layout.addWidget(self._stop_button)
        if self._developer_mode:
            top_layout.addWidget(self._reset_button)
            top_layout.addWidget(self._help_button)
        else:
            self._reset_button.setVisible(False)
            self._help_button.setVisible(False)
        layout.addWidget(topbar)

        self.manual_experiment_view = ManualExperimentView()
        self.manual_experiment_view.release_requested.connect(self.controller.handle_manual_release_requested)
        self.manual_experiment_view.supply_requested.connect(self.controller.handle_manual_supply_requested)
        self.manual_experiment_view.stop_requested.connect(
            lambda _request: self.controller.handle_manual_stop_requested()
        )
        layout.addWidget(self.manual_experiment_view, 1)
        self.setCentralWidget(root)

    def _build_settings_dialog(self) -> None:
        self.settings_dialog = QDialog(self)
        self.settings_dialog.setObjectName("settingsDialog")
        self.settings_dialog.setWindowTitle("气口设置")
        self.settings_dialog.setModal(False)
        self.settings_dialog.resize(960, 620)
        self.settings_dialog.setMinimumSize(820, 560)
        dialog_layout = QVBoxLayout(self.settings_dialog)
        dialog_layout.setContentsMargins(0, 0, 0, 0)
        self.hardware_settings_view = HardwareSettingsView()
        dialog_layout.addWidget(self.hardware_settings_view)
        self.settings_button.clicked.connect(self.open_settings)
        self.hardware_settings_view.mock_verify_requested.connect(self.controller.handle_hardware_mock_verify_requested)
        self.hardware_settings_view.save_requested.connect(self.controller.handle_hardware_profile_save_requested)
        self.hardware_settings_view.rollback_requested.connect(
            self.controller.handle_hardware_profile_rollback_requested
        )

    def open_settings(self) -> None:
        self.settings_dialog.show()
        self.settings_dialog.raise_()
        self.settings_dialog.activateWindow()

    def _build_legacy_compatibility_host(self) -> None:
        """Keep controller regression surfaces alive without product navigation."""

        self._legacy_host = QWidget(self)
        self._legacy_host.setVisible(False)
        self.tabs = QTabWidget(self._legacy_host)
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
        self.session_view = SessionView()
        self.cleaning_view = CleaningView()
        self.tabs.addTab(QWidget(), "概览")
        self.tabs.addTab(self.session_view, "文件")
        self.tabs.addTab(QWidget(), "手动实验")
        self.tabs.addTab(QWidget(), "气口设置")
        self.tabs.addTab(self.cleaning_view, "清洗")
        self.pretest_view.setParent(self._legacy_host)
        self.calibration_view.setParent(self._legacy_host)
        self.protocol_view.setParent(self._legacy_host)
        self._connect_legacy_signals()

    def _connect_legacy_signals(self) -> None:
        self.pretest_view.toggle_requested.connect(self.controller.handle_valve_toggle_request)
        self.pretest_view.apply_requested.connect(self.controller.handle_apply_request)
        self.pretest_view.valve_sequence_requested.connect(self.controller.handle_valve_sequence_request)
        self.pretest_view.sequence_requested.connect(self.controller.handle_pretest_sequence_request)
        self.protocol_view.load_requested.connect(self.controller.handle_protocol_file_selected)
        self.protocol_view.start_requested.connect(self.controller.handle_protocol_start_requested)
        self.protocol_view.stop_requested.connect(self.controller.handle_protocol_stop_requested)
        self.protocol_view.next_trial_requested.connect(self.controller.handle_protocol_next_requested)
        self.protocol_view.trigger_mode_requested.connect(self.controller.handle_protocol_trigger_mode_requested)
        self.protocol_view.manual_trigger_requested.connect(self.controller.handle_protocol_manual_trigger_requested)
        self.protocol_view.rearm_requested.connect(self.controller.handle_protocol_rearm_requested)
        self.protocol_view.pause_requested.connect(self.controller.handle_protocol_pause_requested)
        self.protocol_view.resume_requested.connect(self.controller.handle_protocol_resume_requested)
        self.session_view.preview_requested.connect(self.controller.handle_session_preview_requested)
        self.session_view.start_requested.connect(self.controller.handle_session_start_requested)
        self.session_view.end_requested.connect(self.controller.handle_session_end_requested)
        self.session_view.recovery_requested.connect(self.controller.handle_session_recovery_requested)
        self.cleaning_view.candidate_changed.connect(self.controller.handle_cleaning_candidate_changed)
        self.cleaning_view.save_requested.connect(self.controller.handle_cleaning_save_requested)
        self.cleaning_view.revert_requested.connect(self.controller.handle_cleaning_revert_requested)
        self.cleaning_view.start_requested.connect(self.controller.handle_cleaning_start_requested)
        self.cleaning_view.stop_requested.connect(self.controller.handle_cleaning_stop_requested)
        self.cleaning_view.recover_requested.connect(self.controller.handle_cleaning_recover_requested)
        self.cleaning_view.output_requested.connect(lambda: None)

    def _format_shutdown(self, event: dict | None) -> str:
        if not event:
            return "上次关闭：暂无记录"
        ts_value = event.get("ts") or event.get("timestamp")
        ts_text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts_value)) if ts_value else "未知时间"
        result = event.get("result") or ""
        status_word = "已正常关闭" if result == "success" else "关闭未完成"
        reason = event.get("error") or event.get("reason") or ""
        return user_facing_text(f"上次关闭：{status_word}（{ts_text}）{reason}")

    def _format_telemetry(self, telemetry: Telemetry) -> str:
        stale_hint = "（数据过期）" if telemetry.safety_state == "DATA_STALE" else ""
        reason = f" | 原因: {telemetry.safety_reason}" if telemetry.safety_reason else ""
        return (
            f"连接: {'是' if telemetry.connected else '否'} | "
            f"气流: {telemetry.airflow:.2f} | "
            f"安全: {telemetry.safety_state}{stale_hint}{reason}"
        )

    @staticmethod
    def _connection_summary(telemetry: Telemetry) -> str:
        if not telemetry.connected:
            return "设备未连接"
        if telemetry.safety_state == "DATA_STALE":
            return "连接异常，请检查设备和线缆"
        return "设备已连接"

    def render_last_shutdown(self, event: dict | None) -> None:
        self._shutdown_label.setText(self._format_shutdown(event))

    def render_telemetry(self, telemetry: Telemetry) -> None:
        connected = bool(telemetry.connected)
        self._telemetry_label.setText(self._format_telemetry(telemetry))
        self._connection_text.setText(self._connection_summary(telemetry))
        self._connection_dot.setProperty("connected", connected)
        self._connection_dot.style().unpolish(self._connection_dot)
        self._connection_dot.style().polish(self._connection_dot)
        self.manual_experiment_view.update_a_observation(telemetry.airflow)
        if telemetry.safety_state == "LOW_FLOW":
            self.manual_experiment_view.show_notice(
                "气流不足",
                "已停止相关操作，请检查供气、管路和流量设置。",
                severity="error",
            )
        elif telemetry.safety_state == "DATA_STALE" and telemetry.connected:
            self.manual_experiment_view.show_notice(
                "设备数据中断",
                "请检查设备连接和通信线路。",
                severity="error",
            )
        elif self.manual_experiment_view.status_label.text() in {
            "气流不足",
            "设备数据中断",
        }:
            self.manual_experiment_view.clear_notice()
        if hasattr(self, "calibration_view"):
            self.calibration_view.apply_safety_state(
                telemetry.safety_state,
                telemetry.timestamp,
            )

    def update_status(self, message: str) -> None:
        friendly = user_facing_text(message)
        self._status_label.setText(friendly)
        legacy_terms = ("呼吸", "协议", "预检", "校准", "FPS", "epoch", "关闭阶段")
        if not self._developer_mode and any(term in friendly for term in legacy_terms):
            return
        if friendly and self.manual_experiment_view._is_actionable_notice("", friendly):
            self.manual_experiment_view.show_notice("设备提示", friendly)

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
        self._connect_button.setEnabled(connect_enabled)
        self._reset_button.setEnabled(reset_enabled)
        self._stop_button.setEnabled(stop_enabled)
        self._help_button.setEnabled(True)

    def render_self_check(self, results, ready: bool) -> None:
        summary = [
            f"{item.name}: {item.status}（{item.reason}；建议：{item.suggestion}）"
            for item in results
            if getattr(item, "status", "") not in {"PASS", "通过"}
        ]
        prefix = "最近自检：通过" if ready else "最近自检：失败"
        self._self_check_label.setText(prefix + ("\n" + "\n".join(summary) if summary else ""))
        friendly = user_facing_text(
            ("设备检查通过" if ready else "设备检查未通过") + ("：" + "；".join(summary) if summary else "")
        )
        if not ready:
            self.manual_experiment_view.show_notice(
                "连接失败",
                friendly,
                severity="error",
            )
