from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QLabel,
    QMainWindow,
    QPushButton,
    QStatusBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.models import AppState, Telemetry

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
        self._self_check_label = QLabel("尚未进行硬件自检")
        self._recheck_button = QPushButton("重新检测")
        self._recheck_button.clicked.connect(self._trigger_recheck)
        self._build_layout()

    def _build_layout(self) -> None:
        self.tabs.addTab(self._build_tab("概览", "硬件连接、安全状态占位"), "概览")
        self.tabs.addTab(self._build_tab("校准", "校准流程占位"), "校准")
        self.tabs.addTab(self._build_tab("协议", "协议执行占位"), "协议")

        container = QWidget()
        layout = QVBoxLayout()
        layout.addWidget(self.tabs)
        layout.addWidget(self._self_check_label)
        layout.addWidget(self._recheck_button)
        container.setLayout(layout)
        self.setCentralWidget(container)

        status_bar = QStatusBar()
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

    def _format_telemetry(self, telemetry: Telemetry) -> str:
        return (
            f"连接: {'是' if telemetry.connected else '否'} | "
            f"气流: {telemetry.airflow:.2f} | "
            f"安全: {telemetry.safety_state}"
        )

    def render_telemetry(self, telemetry: Telemetry) -> None:
        self._telemetry_label.setText(self._format_telemetry(telemetry))

    def update_status(self, message: str) -> None:
        self._status_label.setText(message)

    def render_self_check(self, results, ready: bool) -> None:
        status_word = "通过" if ready else "失败"
        summary = [
            f"{item.name}: {item.status}（{item.reason}，建议：{item.suggestion}）"
            for item in results
        ]
        prefix = f"最近自检：{status_word}"
        self._self_check_label.setText(prefix + " | " + " | ".join(summary))

    def _trigger_recheck(self) -> None:
        self.controller.request_self_check()
