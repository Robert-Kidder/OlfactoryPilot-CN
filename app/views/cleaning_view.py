from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.models import CleaningStatus, CleaningViewSnapshot


class CleaningView(QWidget):
    """清洗页只发布配置/运行意图，并渲染 Controller immutable snapshot。"""

    candidate_changed = Signal(object, float, float, int)
    save_requested = Signal(object, float, float, int)
    revert_requested = Signal()
    output_requested = Signal()
    start_requested = Signal()
    stop_requested = Signal()
    recover_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rendering = False
        self._snapshot = CleaningViewSnapshot()
        self.channel_checks: dict[int, QCheckBox] = {}
        channel_group = QGroupBox("清洗气路（软件通道 / 机外气路标签）")
        channel_layout = QGridLayout()
        for channel in range(1, 21):
            checkbox = QCheckBox(f"软件通道 {channel} / 机外气路 {channel}")
            checkbox.setEnabled(False)
            checkbox.toggled.connect(self._emit_candidate)
            self.channel_checks[channel] = checkbox
            channel_layout.addWidget(checkbox, (channel - 1) // 4, (channel - 1) % 4)
        channel_group.setLayout(channel_layout)

        self.select_all_button = QPushButton("全选已配置")
        self.clear_button = QPushButton("清空")
        self.flow_input = QDoubleSpinBox()
        self.flow_input.setDecimals(1)
        self.flow_input.setSuffix(" ml/min")
        self.duration_input = QDoubleSpinBox()
        self.duration_input.setDecimals(1)
        self.duration_input.setSuffix(" 秒")
        self.cycles_input = QSpinBox()
        self.cycles_input.setSuffix(" 轮")
        self.estimate_label = QLabel("预计总时长：-")
        self.saved_label = QLabel("已保存")
        self.output_label = QLabel("输出位置：未选择")
        self.output_button = QPushButton("前往“文件”页选择输出目录")
        self.status_label = QLabel("清洗空闲")
        self.detail_label = QLabel("-")
        for label in (
            self.estimate_label,
            self.saved_label,
            self.output_label,
            self.status_label,
            self.detail_label,
        ):
            label.setWordWrap(True)

        self.save_button = QPushButton("保存清洗配置")
        self.revert_button = QPushButton("撤销修改")
        self.start_button = QPushButton("开始清洗")
        self.stop_button = QPushButton("停止清洗")
        self.recover_button = QPushButton("安全恢复")

        select_actions = QHBoxLayout()
        select_actions.addWidget(self.select_all_button)
        select_actions.addWidget(self.clear_button)
        select_actions.addStretch()
        form = QFormLayout()
        form.addRow("清洗气流", self.flow_input)
        form.addRow("每路时间", self.duration_input)
        form.addRow("循环轮数", self.cycles_input)
        form.addRow("", self.estimate_label)
        actions = QHBoxLayout()
        for button in (
            self.save_button,
            self.revert_button,
            self.start_button,
            self.stop_button,
            self.recover_button,
        ):
            actions.addWidget(button)
        actions.addStretch()
        layout = QVBoxLayout()
        layout.addWidget(channel_group)
        layout.addLayout(select_actions)
        layout.addLayout(form)
        layout.addWidget(self.saved_label)
        layout.addWidget(self.output_label)
        layout.addWidget(self.output_button)
        layout.addLayout(actions)
        layout.addWidget(self.status_label)
        layout.addWidget(self.detail_label)
        layout.addStretch()
        self.setLayout(layout)

        self.select_all_button.clicked.connect(self._select_all)
        self.clear_button.clicked.connect(self._clear)
        self.flow_input.valueChanged.connect(self._emit_candidate)
        self.duration_input.valueChanged.connect(self._emit_candidate)
        self.cycles_input.valueChanged.connect(self._emit_candidate)
        self.save_button.clicked.connect(self._emit_save)
        self.revert_button.clicked.connect(self.revert_requested.emit)
        self.output_button.clicked.connect(self.output_requested.emit)
        self.start_button.clicked.connect(self._confirm_start)
        self.stop_button.clicked.connect(self.stop_requested.emit)
        self.recover_button.clicked.connect(self.recover_requested.emit)

    def render_snapshot(self, snapshot: CleaningViewSnapshot) -> None:
        self._snapshot = snapshot
        labels = dict(snapshot.external_labels)
        available = set(snapshot.available_channels)
        selected = set(snapshot.selected_channels)
        self._rendering = True
        try:
            for channel, checkbox in self.channel_checks.items():
                checkbox.setText(
                    f"软件通道 {channel} / 机外气路 {labels.get(channel, str(channel))}"
                )
                checkbox.setChecked(channel in selected)
                checkbox.setEnabled(snapshot.controls_enabled and channel in available)
            self.flow_input.setRange(0.1, max(0.1, snapshot.max_flow_sccm))
            self.duration_input.setRange(0.1, max(0.1, snapshot.max_open_duration_s))
            self.cycles_input.setRange(1, max(1, snapshot.max_cycles))
            self.flow_input.setValue(snapshot.flow_sccm)
            self.duration_input.setValue(snapshot.open_duration_s)
            self.cycles_input.setValue(snapshot.cycles)
        finally:
            self._rendering = False
        self.flow_input.setEnabled(snapshot.controls_enabled)
        self.duration_input.setEnabled(snapshot.controls_enabled)
        self.cycles_input.setEnabled(snapshot.controls_enabled)
        self.select_all_button.setEnabled(snapshot.controls_enabled)
        self.clear_button.setEnabled(snapshot.controls_enabled)
        minutes = snapshot.estimated_duration_s / 60.0
        self.estimate_label.setText(
            f"预计总时长：约 {minutes:.1f} 分钟"
        )
        self.saved_label.setText("有未保存修改" if snapshot.dirty else "已保存")
        self.output_label.setText(
            f"输出位置：{snapshot.output_root or '未选择本地实验输出目录'}"
        )
        self.status_label.setText(snapshot.status_text)
        self.detail_label.setText(
            " | ".join(
                (
                    f"步骤：{snapshot.current_step_id or '-'}",
                    f"通道：{snapshot.current_channel or '-'}",
                    f"剩余：{snapshot.remaining_s:.1f} 秒",
                    f"lease：{snapshot.lease_text}",
                    f"记录就绪：{'是' if snapshot.recording_ready else '否'}",
                    f"全关：{snapshot.close_progress_text}",
                    f"bundle：{snapshot.bundle_path or '-'}",
                    f"恢复：{snapshot.recovery_reason or '无'}",
                )
            )
        )
        self.save_button.setEnabled(snapshot.can_save)
        self.revert_button.setEnabled(snapshot.controls_enabled and snapshot.dirty)
        self.output_button.setEnabled(snapshot.controls_enabled)
        self.start_button.setEnabled(snapshot.can_start)
        self.stop_button.setEnabled(snapshot.can_stop)
        self.recover_button.setEnabled(snapshot.can_recover)
        self.start_button.setText(
            "正在清洗" if snapshot.status == CleaningStatus.RUNNING else "开始清洗"
        )

    def selected_channels(self) -> tuple[int, ...]:
        return tuple(
            channel
            for channel, checkbox in self.channel_checks.items()
            if checkbox.isChecked() and checkbox.isEnabled()
        )

    def _candidate_values(self) -> tuple[tuple[int, ...], float, float, int]:
        return (
            self.selected_channels(),
            self.flow_input.value(),
            self.duration_input.value(),
            self.cycles_input.value(),
        )

    def _emit_candidate(self, *_args) -> None:
        if not self._rendering:
            self.candidate_changed.emit(*self._candidate_values())

    def _emit_save(self) -> None:
        self.save_requested.emit(*self._candidate_values())

    def _select_all(self) -> None:
        self._rendering = True
        try:
            for checkbox in self.channel_checks.values():
                if checkbox.isEnabled():
                    checkbox.setChecked(True)
        finally:
            self._rendering = False
        self._emit_candidate()

    def _clear(self) -> None:
        self._rendering = True
        try:
            for checkbox in self.channel_checks.values():
                if checkbox.isEnabled():
                    checkbox.setChecked(False)
        finally:
            self._rendering = False
        self._emit_candidate()

    def _confirm_start(self) -> None:
        labels = dict(self._snapshot.external_labels)
        routes = "、".join(
            labels.get(channel, str(channel))
            for channel in self._snapshot.selected_channels
        )
        summary = (
            f"气体：{self._snapshot.gas_label}\n"
            f"A/B/C：{self._snapshot.flow_sccm:.1f}/0/0 ml/min\n"
            f"机外气路顺序：{routes}\n"
            f"每路：{self._snapshot.open_duration_s:.1f} 秒\n"
            f"循环：{self._snapshot.cycles} 轮\n"
            f"输出：{self._snapshot.output_root}"
        )
        answer = QMessageBox.question(
            self,
            "确认开始自动清洗",
            summary,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.start_requested.emit()
