from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.models import ProtocolDocument, ProtocolExecutionSnapshot
from app.services import ProtocolParseError


class ProtocolView(QWidget):
    load_requested = Signal(str)
    start_requested = Signal()
    stop_requested = Signal()
    next_trial_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._load_button = QPushButton("加载协议")
        self._path_label = QLabel("尚未加载协议")
        self._summary_label = QLabel("等待加载 .txt 或 .csv 协议文件")
        self._metadata_label = QLabel("metadata：-")
        self._trigger_label = QLabel("trigger：-")
        self._error_label = QLabel("")
        self._start_button = QPushButton("开始门控")
        self._stop_button = QPushButton("停止门控")
        self._next_trial_button = QPushButton("下一 trial")
        self._manual_trigger_button = QPushButton("手动触发")
        self._ttl_trigger_button = QPushButton("TTL 触发")
        self._execution_status_label = QLabel("状态：空闲")
        self._execution_trial_label = QLabel("trial：-")
        self._execution_valve_label = QLabel("阀门：-")
        self._execution_trigger_label = QLabel("触发模式：-")
        self._execution_wait_label = QLabel("等待：0 ms")
        self._execution_event_label = QLabel("最近事件：-")
        self._preview_table = QTableWidget(0, 5)

        for label in (
            self._path_label,
            self._summary_label,
            self._metadata_label,
            self._trigger_label,
            self._error_label,
            self._execution_status_label,
            self._execution_trial_label,
            self._execution_valve_label,
            self._execution_trigger_label,
            self._execution_wait_label,
            self._execution_event_label,
        ):
            label.setWordWrap(True)
        self._error_label.setStyleSheet("color: #b00020; font-weight: 600;")

        for button in (
            self._start_button,
            self._stop_button,
            self._next_trial_button,
            self._manual_trigger_button,
            self._ttl_trigger_button,
        ):
            button.setEnabled(False)

        self._load_button.clicked.connect(self._choose_file)
        self._start_button.clicked.connect(self.start_requested.emit)
        self._stop_button.clicked.connect(self.stop_requested.emit)
        self._next_trial_button.clicked.connect(self.next_trial_requested.emit)
        self._build_layout()

    def _build_layout(self) -> None:
        layout = QVBoxLayout()
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        load_row = QHBoxLayout()
        load_row.addWidget(self._load_button)
        load_row.addWidget(self._path_label, 1)
        layout.addLayout(load_row)

        summary = QGroupBox("协议摘要")
        summary_layout = QVBoxLayout()
        summary_layout.addWidget(self._summary_label)
        summary_layout.addWidget(self._trigger_label)
        summary_layout.addWidget(self._metadata_label)
        summary.setLayout(summary_layout)
        layout.addWidget(summary)

        action_row = QHBoxLayout()
        action_row.addWidget(self._start_button)
        action_row.addWidget(self._stop_button)
        action_row.addWidget(self._next_trial_button)
        action_row.addWidget(self._manual_trigger_button)
        action_row.addWidget(self._ttl_trigger_button)
        action_row.addStretch()
        layout.addLayout(action_row)

        execution = QGroupBox("门控执行")
        execution_layout = QVBoxLayout()
        execution_layout.addWidget(self._execution_status_label)
        execution_layout.addWidget(self._execution_trial_label)
        execution_layout.addWidget(self._execution_valve_label)
        execution_layout.addWidget(self._execution_trigger_label)
        execution_layout.addWidget(self._execution_wait_label)
        execution_layout.addWidget(self._execution_event_label)
        execution.setLayout(execution_layout)
        layout.addWidget(execution)

        error_box = QGroupBox("错误信息")
        error_layout = QVBoxLayout()
        error_layout.addWidget(self._error_label)
        error_box.setLayout(error_layout)
        layout.addWidget(error_box)

        self._preview_table.setHorizontalHeaderLabels(["trial", "timing_ms", "duration_ms", "valve", "trigger"])
        layout.addWidget(self._preview_table)
        self.setLayout(layout)

    def _choose_file(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "选择协议文件",
            str(Path.cwd()),
            "协议文件 (*.txt *.csv);;所有文件 (*)",
        )
        if path:
            self.load_requested.emit(path)

    def render_protocol(self, document: ProtocolDocument) -> None:
        self._error_label.setText("")
        self._path_label.setText(str(document.source_path))
        self._summary_label.setText(f"文件：{document.source_name}；trial 数量：{len(document.trials)}")
        trigger_summary = "；".join(
            f"{trigger}={count}" for trigger, count in sorted(document.trigger_summary.items())
        )
        self._trigger_label.setText(f"trigger：{trigger_summary or '-'}")
        metadata = "；".join(f"{key}={value}" for key, value in document.metadata.items())
        self._metadata_label.setText(f"metadata：{metadata or '-'}")
        self._render_preview(document)

    def render_error(self, error: ProtocolParseError | str) -> None:
        message = str(error)
        self._error_label.setText(f"解析失败：{message}")

    def render_execution_state(self, snapshot: ProtocolExecutionSnapshot) -> None:
        valve_text = str(snapshot.valve) if snapshot.valve is not None else "-"
        duration = (
            f"{snapshot.planned_duration_ms:g} ms"
            if snapshot.planned_duration_ms is not None
            else "-"
        )
        self._execution_status_label.setText(f"状态：{snapshot.status_text}")
        self._execution_trial_label.setText(
            f"trial：{snapshot.trial_label}（{snapshot.trial_id}）"
        )
        self._execution_valve_label.setText(f"阀门：{valve_text}；计划时长：{duration}")
        self._execution_trigger_label.setText(f"触发模式：{snapshot.trigger}")
        self._execution_wait_label.setText(f"等待：{snapshot.wait_elapsed_ms} ms")
        self._execution_event_label.setText(f"最近事件：{snapshot.recent_event}")
        self._start_button.setEnabled(snapshot.can_start)
        self._stop_button.setEnabled(snapshot.can_stop)
        self._next_trial_button.setEnabled(snapshot.can_advance)

    def _render_preview(self, document: ProtocolDocument) -> None:
        rows = min(len(document.trials), 8)
        self._preview_table.setRowCount(rows)
        for row, trial in enumerate(document.trials[:rows]):
            values = [
                trial.trial_id,
                str(trial.timing_ms),
                str(trial.duration_ms),
                str(trial.valve),
                trial.trigger.value,
            ]
            for column, value in enumerate(values):
                self._preview_table.setItem(row, column, QTableWidgetItem(value))
