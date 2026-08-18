from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.models import (
    ChannelRegistry,
    ManualExperimentIntent,
    ManualExperimentSnapshot,
    ManualExperimentStatus,
    ManualSupplyIntent,
)


@dataclass(frozen=True, slots=True)
class ManualExperimentDraft:
    selected_external_ports: tuple[int, ...] = ()
    total_sccm: float = 1000.0
    sample_a_sccm: float = 200.0
    vacuum_c_sccm: float = 0.0
    duration_s: float = 5.0

    def __post_init__(self) -> None:
        ports = tuple(sorted(self.selected_external_ports))
        if len(set(ports)) != len(ports) or any(not 1 <= port <= 20 for port in ports):
            raise ValueError("机外气口必须是不重复的 1–20。")
        object.__setattr__(self, "selected_external_ports", ports)
        values = (
            (self.total_sccm, "T 总流量"),
            (self.sample_a_sccm, "A 样品流量"),
            (self.vacuum_c_sccm, "C 真空流量"),
            (self.duration_s, "刺激时长"),
        )
        for value, label in values:
            if isinstance(value, bool) or not math.isfinite(float(value)):
                raise ValueError(f"{label}必须是有限数值。")
        if self.total_sccm < 0 or self.sample_a_sccm < 0 or self.vacuum_c_sccm < 0:
            raise ValueError("T/A/C 流量不得为负数。")
        if self.sample_a_sccm > self.total_sccm:
            raise ValueError("A 样品流量必须满足 0 ≤ A ≤ T。")
        if self.duration_s <= 0:
            raise ValueError("刺激时长必须大于 0 秒。")

    @property
    def main_b_sccm(self) -> float:
        return self.total_sccm - self.sample_a_sccm


@dataclass(frozen=True, slots=True)
class ManualPortSnapshot:
    external_port: int
    display_name: str = ""
    available: bool = False
    actually_open: bool = False
    fault: str = ""

    def __post_init__(self) -> None:
        if not 1 <= self.external_port <= 20:
            raise ValueError("机外气口必须位于 1–20。")


@dataclass(frozen=True, slots=True)
class ManualExperimentViewSnapshot:
    experiment: ManualExperimentSnapshot = ManualExperimentSnapshot()
    draft: ManualExperimentDraft = ManualExperimentDraft()
    ports: tuple[ManualPortSnapshot, ...] = ()
    controls_enabled: bool = False
    can_apply_flow: bool = False
    can_release: bool = False
    can_stop: bool = False
    supply_enabled: bool | None = None
    supply_transitioning: bool = False
    telemetry_a_sccm: float | None = None
    status_text: str = "手动实验空闲"
    detail_text: str = "请选择可用机外气口。"
    max_total_sccm: float = 5000.0
    max_sample_a_sccm: float = 5000.0
    max_vacuum_c_sccm: float = 5000.0
    max_duration_s: float = 3600.0


@dataclass(frozen=True, slots=True)
class ManualDraftChangedIntent:
    draft: ManualExperimentDraft


@dataclass(frozen=True, slots=True)
class StopManualExperimentIntent:
    reason: str = "用户请求停止手动实验。"


class ManualExperimentView(QWidget):
    """方案 B V3：只发布 typed intent，并渲染冻结快照。"""

    draft_changed = Signal(object)
    supply_requested = Signal(object)
    release_requested = Signal(object)
    stop_requested = Signal(object)

    def __init__(
        self,
        parent=None,
        *,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        super().__init__(parent)
        self._clock_ns = monotonic_ns
        self._rendering = False
        self._snapshot = ManualExperimentViewSnapshot()
        self._draft = self._snapshot.draft
        self._registry: ChannelRegistry | None = None
        self._allow_mock = False
        self.port_buttons: dict[int, QPushButton] = {}

        flow_group = QGroupBox("实时气流与流量设定")
        flow_layout = QFormLayout()
        self.telemetry_a_label = QLabel("A路当前观测：暂无数据")
        self.total_input = self._flow_input(" sccm")
        self.sample_a_input = self._flow_input(" sccm")
        self.main_b_input = self._flow_input(" sccm")
        self.main_b_input.setReadOnly(True)
        self.main_b_input.setFocusPolicy(self.main_b_input.focusPolicy())
        self.vacuum_c_input = self._flow_input(" sccm")
        flow_layout.addRow(self.telemetry_a_label)
        flow_layout.addRow("T 总流量", self.total_input)
        flow_layout.addRow("A 样品流量", self.sample_a_input)
        flow_layout.addRow("B 主气流（T-A，只读）", self.main_b_input)
        flow_layout.addRow("C 真空流量", self.vacuum_c_input)
        flow_group.setLayout(flow_layout)

        port_group = QGroupBox("机外气口（固定 2×10）")
        self.port_layout = QGridLayout()
        for port in range(1, 21):
            button = QPushButton()
            button.setCheckable(True)
            button.setMinimumSize(92, 70)
            button.clicked.connect(
                lambda checked, external_port=port: self._toggle_port(
                    external_port, checked
                )
            )
            self.port_buttons[port] = button
            self.port_layout.addWidget(button, (port - 1) // 10, (port - 1) % 10)
        port_group.setLayout(self.port_layout)

        action_group = QGroupBox("手动刺激")
        action_layout = QHBoxLayout()
        self.duration_input = QDoubleSpinBox()
        self.duration_input.setDecimals(2)
        self.duration_input.setSingleStep(0.5)
        self.duration_input.setSuffix(" 秒")
        self.apply_flow_button = QPushButton("开始供气")
        self.release_button = QPushButton("释放气味")
        self.stop_button = QPushButton("停止并安全收敛")
        self.countdown_label = QLabel("剩余：--")
        action_layout.addWidget(QLabel("刺激时长"))
        action_layout.addWidget(self.duration_input)
        action_layout.addWidget(self.apply_flow_button)
        action_layout.addWidget(self.release_button)
        action_layout.addWidget(self.stop_button)
        action_layout.addStretch()
        action_layout.addWidget(self.countdown_label)
        action_group.setLayout(action_layout)

        self.status_label = QLabel("手动实验空闲")
        self.status_label.setWordWrap(True)
        self.detail_label = QLabel("请选择可用机外气口。")
        self.detail_label.setWordWrap(True)

        layout = QVBoxLayout()
        layout.addWidget(flow_group)
        layout.addWidget(port_group)
        layout.addWidget(action_group)
        layout.addWidget(self.status_label)
        layout.addWidget(self.detail_label)
        layout.addStretch()
        self.setLayout(layout)

        self.total_input.valueChanged.connect(self._on_total_changed)
        self.sample_a_input.valueChanged.connect(self._on_draft_value_changed)
        self.vacuum_c_input.valueChanged.connect(self._on_draft_value_changed)
        self.duration_input.valueChanged.connect(self._on_draft_value_changed)
        self.apply_flow_button.clicked.connect(self._request_supply_change)
        self.release_button.clicked.connect(self._request_release)
        self.stop_button.clicked.connect(
            lambda: self.stop_requested.emit(StopManualExperimentIntent())
        )

        self._countdown_timer = QTimer(self)
        self._countdown_timer.setInterval(100)
        self._countdown_timer.timeout.connect(self.refresh_countdown_display)
        self._countdown_timer.start()
        self.render_snapshot(self._snapshot)

    @staticmethod
    def _flow_input(suffix: str) -> QDoubleSpinBox:
        control = QDoubleSpinBox()
        control.setDecimals(1)
        control.setSingleStep(10.0)
        control.setSuffix(suffix)
        return control

    @property
    def draft(self) -> ManualExperimentDraft:
        return self._draft

    @property
    def snapshot(self) -> ManualExperimentViewSnapshot:
        return self._snapshot

    def set_registry(self, registry: ChannelRegistry, allow_mock: bool) -> None:
        if not isinstance(registry, ChannelRegistry):
            raise TypeError("手动实验 View 需要 ChannelRegistry。")
        self._registry = registry
        self._allow_mock = bool(allow_mock)
        ports = tuple(
            ManualPortSnapshot(
                external_port=channel.external_port,
                display_name=channel.display_name,
                available=(
                    channel.enabled
                    and channel.verification_valid_for(allow_mock=self._allow_mock)
                ),
            )
            for channel in registry.channels
        )
        self._snapshot = replace(self._snapshot, ports=ports)
        available = {port.external_port for port in ports if port.available}
        selected = tuple(
            port for port in self._draft.selected_external_ports if port in available
        )
        if selected != self._draft.selected_external_ports:
            self._draft = replace(self._draft, selected_external_ports=selected)
            self._snapshot = replace(self._snapshot, draft=self._draft)
        self._render_ports()

    def render_snapshot(
        self,
        snapshot: ManualExperimentViewSnapshot | ManualExperimentSnapshot,
    ) -> None:
        if isinstance(snapshot, ManualExperimentSnapshot):
            editable = snapshot.status in {
                ManualExperimentStatus.IDLE,
                ManualExperimentStatus.COMPLETED,
            }
            ports = tuple(
                replace(
                    port,
                    actually_open=(
                        port.external_port in snapshot.open_confirmed
                        and port.external_port not in snapshot.close_confirmed
                    ),
                    fault=(
                        snapshot.recovery_reason
                        if port.external_port in snapshot.possibly_open
                        else ""
                    ),
                )
                for port in self._snapshot.ports
            )
            snapshot = replace(
                self._snapshot,
                experiment=snapshot,
                ports=ports,
                controls_enabled=editable,
                can_apply_flow=editable,
                can_release=(
                    editable
                    and bool(
                        set(self._draft.selected_external_ports)
                        & {port.external_port for port in ports if port.available}
                    )
                ),
                can_stop=snapshot.status
                not in {
                    ManualExperimentStatus.IDLE,
                    ManualExperimentStatus.COMPLETED,
                    ManualExperimentStatus.RECOVERY_REQUIRED,
                },
                supply_enabled=snapshot.supply_enabled,
                supply_transitioning=snapshot.supply_transitioning,
                status_text=self._status_text(snapshot),
                detail_text=(snapshot.recovery_reason or self._snapshot.detail_text),
            )
        if not isinstance(snapshot, ManualExperimentViewSnapshot):
            raise TypeError("手动实验 View 只能渲染冻结的手动实验快照。")
        self._snapshot = snapshot
        self._draft = snapshot.draft
        self._rendering = True
        try:
            self.total_input.setRange(0.0, max(0.0, snapshot.max_total_sccm))
            self.sample_a_input.setRange(
                0.0,
                min(snapshot.max_sample_a_sccm, snapshot.draft.total_sccm),
            )
            self.vacuum_c_input.setRange(0.0, max(0.0, snapshot.max_vacuum_c_sccm))
            self.duration_input.setRange(0.01, max(0.01, snapshot.max_duration_s))
            self.total_input.setValue(snapshot.draft.total_sccm)
            self.sample_a_input.setValue(snapshot.draft.sample_a_sccm)
            self.vacuum_c_input.setValue(snapshot.draft.vacuum_c_sccm)
            self.duration_input.setValue(snapshot.draft.duration_s)
            self.main_b_input.setRange(0.0, max(0.0, snapshot.max_total_sccm))
            self.main_b_input.setValue(snapshot.draft.main_b_sccm)
        finally:
            self._rendering = False

        self.total_input.setEnabled(snapshot.controls_enabled)
        self.sample_a_input.setEnabled(snapshot.controls_enabled)
        self.vacuum_c_input.setEnabled(snapshot.controls_enabled)
        self.duration_input.setEnabled(snapshot.controls_enabled)
        self.main_b_input.setEnabled(snapshot.controls_enabled)
        self.main_b_input.setReadOnly(True)
        self.apply_flow_button.setEnabled(snapshot.can_apply_flow)
        if snapshot.supply_transitioning:
            supply_text = "供气切换中"
        elif snapshot.supply_enabled is True:
            supply_text = "供气中（点击停止）"
        elif snapshot.supply_enabled is False:
            supply_text = "已停止供气（点击开始）"
        else:
            supply_text = "供气状态未知"
        self.apply_flow_button.setText(supply_text)
        self.apply_flow_button.setEnabled(
            snapshot.can_apply_flow
            and not snapshot.supply_transitioning
        )
        self.release_button.setEnabled(snapshot.can_release)
        self.stop_button.setEnabled(snapshot.can_stop)
        self.status_label.setText(snapshot.status_text)
        self.detail_label.setText(snapshot.detail_text)
        telemetry = snapshot.telemetry_a_sccm
        self.telemetry_a_label.setText(
            "A路当前观测：暂无数据"
            if telemetry is None
            else f"A路当前观测：{telemetry:.1f} sccm"
        )
        self._render_ports()
        self.refresh_countdown_display()

    def update_a_observation(self, value: float) -> None:
        if isinstance(value, bool) or not math.isfinite(float(value)):
            self._snapshot = replace(self._snapshot, telemetry_a_sccm=None)
        else:
            self._snapshot = replace(self._snapshot, telemetry_a_sccm=float(value))
        telemetry = self._snapshot.telemetry_a_sccm
        self.telemetry_a_label.setText(
            "A路当前观测：暂无数据"
            if telemetry is None
            else f"A路当前观测：{telemetry:.1f} sccm"
        )

    def render_supply_state(self, enabled: bool, message: str = "") -> None:
        self._snapshot = replace(
            self._snapshot,
            supply_enabled=bool(enabled),
            detail_text=message or self._snapshot.detail_text,
        )
        self.apply_flow_button.setText("停止供气" if enabled else "开始供气")
        if message:
            self.detail_label.setText(message)

    def refresh_countdown_display(self) -> None:
        experiment = self._snapshot.experiment
        if (
            experiment.status is ManualExperimentStatus.STIMULATING
            and experiment.deadline_ns is not None
        ):
            remaining_ns = max(0, experiment.deadline_ns - self._clock_ns())
            self.countdown_label.setText(f"剩余：{remaining_ns / 1_000_000_000:.1f} 秒")
            return
        if experiment.status is ManualExperimentStatus.COMPLETED:
            self.countdown_label.setText("刺激已完成")
        elif experiment.status is ManualExperimentStatus.RECOVERY_REQUIRED:
            self.countdown_label.setText("需要安全恢复")
        else:
            self.countdown_label.setText("剩余：--")

    def _render_ports(self) -> None:
        snapshots = {port.external_port: port for port in self._snapshot.ports}
        selected = set(self._draft.selected_external_ports)
        for external_port, button in self.port_buttons.items():
            port = snapshots.get(external_port, ManualPortSnapshot(external_port))
            is_selected = external_port in selected and port.available
            states = ["可用" if port.available else "不可用"]
            if is_selected:
                states.append("已选择")
            if port.actually_open:
                states.append("开启回执已确认（非机械确认）")
            if port.fault:
                states.append(f"故障：{port.fault}")
            name = f"\n{port.display_name}" if port.display_name else ""
            button.setText(f"气口 {external_port}{name}\n" + "｜".join(states))
            button.setAccessibleName(f"机外气口 {external_port}")
            button.setAccessibleDescription("；".join(states))
            button.setEnabled(self._snapshot.controls_enabled and port.available)
            button.setChecked(is_selected)
            visual_state = "fault" if port.fault else (
                "open" if port.actually_open else ("selected" if is_selected else (
                    "available" if port.available else "unavailable"
                ))
            )
            button.setProperty("portState", visual_state)
            button.setStyleSheet(self._port_style(visual_state))

    @staticmethod
    def _port_style(state: str) -> str:
        styles = {
            "unavailable": "background:#e5e7eb;color:#6b7280;border:1px solid #9ca3af;",
            "available": "background:#ffffff;color:#111827;border:2px solid #64748b;",
            "selected": "background:#dbeafe;color:#1e3a8a;border:3px solid #2563eb;",
            "open": "background:#dcfce7;color:#14532d;border:3px solid #16a34a;",
            "fault": "background:#fee2e2;color:#7f1d1d;border:3px solid #dc2626;",
        }
        return f"QPushButton {{{styles[state]}}}"

    def _toggle_port(self, external_port: int, checked: bool) -> None:
        if self._rendering or not self.port_buttons[external_port].isEnabled():
            return
        selected = set(self._draft.selected_external_ports)
        if checked:
            selected.add(external_port)
        else:
            selected.discard(external_port)
        self._draft = replace(
            self._draft,
            selected_external_ports=tuple(sorted(selected)),
        )
        self._render_ports()
        self.release_button.setEnabled(
            self._snapshot.controls_enabled
            and bool(self._draft.selected_external_ports)
        )
        self.draft_changed.emit(ManualDraftChangedIntent(self._draft))

    def _request_supply_change(self) -> None:
        self.supply_requested.emit(
            ManualSupplyIntent(
                enabled=not self._snapshot.supply_enabled,
                total_sccm=self._draft.total_sccm,
                sample_a_sccm=self._draft.sample_a_sccm,
                vacuum_c_sccm=self._draft.vacuum_c_sccm,
            )
        )

    def _request_release(self) -> None:
        self.release_requested.emit(
            ManualExperimentIntent(
                external_ports=self._draft.selected_external_ports,
                total_sccm=self._draft.total_sccm,
                sample_a_sccm=self._draft.sample_a_sccm,
                vacuum_c_sccm=self._draft.vacuum_c_sccm,
                duration_ns=round(self._draft.duration_s * 1_000_000_000),
            )
        )

    @staticmethod
    def _status_text(snapshot: ManualExperimentSnapshot) -> str:
        labels = {
            ManualExperimentStatus.IDLE: "手动实验空闲",
            ManualExperimentStatus.FLOW_PENDING: "正在确认流量",
            ManualExperimentStatus.SELECTOR_PENDING: "正在切换气味路线",
            ManualExperimentStatus.OPENING: "正在开启所选气口",
            ManualExperimentStatus.STIMULATING: "正在释放气味",
            ManualExperimentStatus.CLOSING: "正在关闭气口",
            ManualExperimentStatus.ZEROING_A: "正在将 A 路清零",
            ManualExperimentStatus.SELECTOR_COMPENSATION: "正在切换补偿路线",
            ManualExperimentStatus.RESTORING_SUPPLY: "正在恢复供气",
            ManualExperimentStatus.COMPLETED: "手动刺激已完成",
            ManualExperimentStatus.RECOVERY_REQUIRED: "需要安全恢复",
        }
        return labels[snapshot.status]

    def _on_total_changed(self, value: float) -> None:
        if self._rendering:
            return
        self._rendering = True
        try:
            self.sample_a_input.setMaximum(
                min(self._snapshot.max_sample_a_sccm, value)
            )
        finally:
            self._rendering = False
        self._on_draft_value_changed()

    def _on_draft_value_changed(self, *_args) -> None:
        if self._rendering:
            return
        self._draft = replace(
            self._draft,
            total_sccm=self.total_input.value(),
            sample_a_sccm=self.sample_a_input.value(),
            vacuum_c_sccm=self.vacuum_c_input.value(),
            duration_s=self.duration_input.value(),
        )
        self.main_b_input.setValue(self._draft.main_b_sccm)
        self.draft_changed.emit(ManualDraftChangedIntent(self._draft))
