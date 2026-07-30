from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.models.session import SessionStatus, SessionViewSnapshot


class SessionView(QWidget):
    """文件页只发布用户意图并渲染 Controller 生成的 snapshot。"""

    preview_requested = Signal(str, str, str)
    start_requested = Signal(str, str, object)
    end_requested = Signal(str)
    recovery_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.subject_input = QLineEdit()
        self.condition_input = QLineEdit()
        self.output_path = QLineEdit()
        self.output_path.setReadOnly(True)
        self.output_button = QPushButton("选择本地输出目录")
        self.normalized_label = QLabel("-")
        self.stem_label = QLabel("-")
        self.staging_path_label = QLabel("-")
        self.final_path_label = QLabel("-")
        self.raw_path_label = QLabel("-")
        self.log_path_label = QLabel("-")
        self.status_label = QLabel("请填写受试者、条件并选择本地输出目录。")
        self.recovery_label = QLabel("未发现待恢复会话。")
        self.recovery_button = QPushButton("打开恢复目录")
        self.start_button = QPushButton("开始会话")
        self.end_button = QPushButton("结束会话")
        for label in (
            self.normalized_label,
            self.stem_label,
            self.staging_path_label,
            self.final_path_label,
            self.raw_path_label,
            self.log_path_label,
            self.status_label,
            self.recovery_label,
        ):
            label.setWordWrap(True)
        self.start_button.setEnabled(False)
        self.end_button.setEnabled(False)
        self.recovery_button.setEnabled(False)
        self._build_layout()
        self.subject_input.textChanged.connect(self._emit_preview)
        self.condition_input.textChanged.connect(self._emit_preview)
        self.output_button.clicked.connect(self._choose_output_directory)
        self.start_button.clicked.connect(self._emit_start)
        self.end_button.clicked.connect(
            lambda: self.end_requested.emit("user_end")
        )
        self.recovery_button.clicked.connect(self.recovery_requested.emit)

    @property
    def stem_input_is_editable(self) -> bool:
        return False

    def _build_layout(self) -> None:
        form = QFormLayout()
        form.addRow("受试者（原值）", self.subject_input)
        form.addRow("条件（原值）", self.condition_input)
        output_row = QWidget()
        output_layout = QHBoxLayout()
        output_layout.setContentsMargins(0, 0, 0, 0)
        output_layout.addWidget(self.output_path)
        output_layout.addWidget(self.output_button)
        output_row.setLayout(output_layout)
        form.addRow("本地输出目录", output_row)
        form.addRow("清洗后值", self.normalized_label)
        form.addRow("规范化文件名", self.stem_label)
        form.addRow("活动工作目录", self.staging_path_label)
        form.addRow("最终会话目录", self.final_path_label)
        form.addRow("最终 raw 路径", self.raw_path_label)
        form.addRow("最终 log 路径", self.log_path_label)
        actions = QHBoxLayout()
        actions.addWidget(self.start_button)
        actions.addWidget(self.end_button)
        actions.addStretch()
        layout = QVBoxLayout()
        layout.addLayout(form)
        layout.addLayout(actions)
        layout.addWidget(self.status_label)
        layout.addWidget(self.recovery_label)
        layout.addWidget(self.recovery_button)
        layout.addStretch()
        self.setLayout(layout)

    def set_output_directory(self, path: str | Path) -> None:
        self.output_path.setText(str(path))
        self._emit_preview()

    def render_snapshot(self, snapshot: SessionViewSnapshot) -> None:
        normalized = (
            f"{snapshot.subject_clean} / {snapshot.condition_clean}"
            if snapshot.subject_clean or snapshot.condition_clean
            else "-"
        )
        self.normalized_label.setText(normalized)
        self.stem_label.setText(snapshot.stem or "-")
        self.staging_path_label.setText(snapshot.staging_path or "-")
        self.final_path_label.setText(snapshot.final_path or "-")
        self.raw_path_label.setText(snapshot.raw_path or "-")
        self.log_path_label.setText(snapshot.log_path or "-")
        self.status_label.setText(snapshot.status_text)
        recovery_text = "\n".join(snapshot.recovery_messages)
        self.recovery_label.setText(recovery_text or "未发现待恢复会话。")
        self.recovery_button.setEnabled(bool(snapshot.recovery_messages))
        self.subject_input.setEnabled(snapshot.inputs_enabled)
        self.condition_input.setEnabled(snapshot.inputs_enabled)
        self.output_button.setEnabled(snapshot.inputs_enabled)
        self.start_button.setText(
            "确认开始记录"
            if snapshot.status == SessionStatus.PREPARED
            else "开始会话"
        )
        self.end_button.setText(
            "取消准备"
            if snapshot.status == SessionStatus.PREPARED
            else "结束会话"
        )
        self.start_button.setEnabled(snapshot.can_start)
        self.end_button.setEnabled(snapshot.can_end)

    def _emit_preview(self) -> None:
        self.preview_requested.emit(
            self.subject_input.text(),
            self.condition_input.text(),
            self.output_path.text(),
        )

    def _emit_start(self) -> None:
        self.start_requested.emit(
            self.subject_input.text(),
            self.condition_input.text(),
            self.output_path.text(),
        )

    def _choose_output_directory(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self,
            "选择本地输出目录",
            self.output_path.text(),
        )
        if selected:
            self.set_output_directory(selected)
