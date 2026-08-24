from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace

import pyqtgraph as pg
from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFontMetrics, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
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
from app.views.product_text import user_facing_text

FLOW_STEP_ML_MIN = 500.0
DURATION_STEP_S = 5.0


class ConsolePortButton(QPushButton):
    """A compact, tactile two-line key for the physical 2 x 10 port bay."""

    def __init__(self, external_port: int, parent=None) -> None:
        super().__init__(f"气口 {external_port}", parent)
        self.external_port = external_port
        self._alias = ""
        self.setObjectName("portButton")
        self.setCheckable(True)
        self.setMinimumSize(54, 58)
        self.setMaximumHeight(62)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_port_content(self, display_name: str) -> None:
        alias = display_name.strip()
        if alias == f"气口 {self.external_port}":
            alias = ""
        self._alias = alias
        text = f"{alias}\n气口 {self.external_port}" if alias else f"气口 {self.external_port}"
        self.setText(text)
        self.setToolTip(text.replace("\n", " · ") if alias else "")
        self.update()

    def elided_alias(self, width: int | None = None) -> str:
        if not self._alias:
            return ""
        font = self.font()
        font.setPointSizeF(max(10.0, font.pointSizeF()))
        font.setBold(True)
        return QFontMetrics(font).elidedText(
            self._alias,
            Qt.TextElideMode.ElideRight,
            max(20, (width or self.width()) - 14),
        )

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        state = str(self.property("portState") or "available")
        pressed = self.isDown()
        hovered = self.underMouse() and self.isEnabled()
        offset = 3 if pressed else 0
        face = QRectF(self.rect()).adjusted(2, 2 + offset, -2, -6 + offset)

        palettes = {
            "available": ("#626d69" if hovered else "#555f5b", "#35403c", "#84918c", "#f0f3f1"),
            "selected": ("#f0bb58" if hovered else "#dfa548", "#a86725", "#ffd887", "#24180a"),
            "open": ("#75caa1" if hovered else "#5db88d", "#34795a", "#a7ebca", "#071c13"),
            "fault": ("#a74d46", "#642d2a", "#ff9b94", "#fff4f2"),
            "unavailable": ("#343b39", "#272d2b", "#424a47", "#747d79"),
        }
        top, bottom, border, text_color = palettes.get(state, palettes["available"])
        if not self.isEnabled() and state == "available":
            top, bottom, border, text_color = palettes["unavailable"]

        if not pressed:
            shadow = face.translated(0, 4)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(8, 12, 11, 175))
            painter.drawRoundedRect(shadow, 7, 7)

        gradient = QLinearGradient(face.topLeft(), face.bottomLeft())
        gradient.setColorAt(0, QColor(top))
        gradient.setColorAt(1, QColor(bottom if not pressed else top).darker(118))
        painter.setBrush(gradient)
        painter.setPen(QPen(QColor(border), 2 if state in {"selected", "open", "fault"} else 1))
        painter.drawRoundedRect(face, 7, 7)

        if self.hasFocus():
            focus = face.adjusted(3, 3, -3, -3)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor("#ffe0a1"), 1, Qt.PenStyle.DotLine))
            painter.drawRoundedRect(focus, 5, 5)

        painter.setPen(QColor(text_color))
        if self._alias:
            primary = self.font()
            primary.setPointSizeF(max(10.0, primary.pointSizeF()))
            primary.setBold(True)
            painter.setFont(primary)
            painter.drawText(
                face.adjusted(6, 7, -6, -22),
                Qt.AlignmentFlag.AlignCenter,
                self.elided_alias(int(face.width())),
            )
            secondary = self.font()
            secondary.setPointSizeF(max(8.0, secondary.pointSizeF() - 1.5))
            secondary.setBold(False)
            painter.setFont(secondary)
            secondary_color = QColor(text_color)
            secondary_color.setAlpha(205)
            painter.setPen(secondary_color)
            painter.drawText(
                face.adjusted(4, 31, -4, -5),
                Qt.AlignmentFlag.AlignCenter,
                f"气口 {self.external_port}",
            )
        else:
            font = self.font()
            font.setPointSizeF(max(9.5, font.pointSizeF()))
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(face.adjusted(4, 2, -4, -2), Qt.AlignmentFlag.AlignCenter, self.text())


@dataclass(frozen=True, slots=True)
class ManualExperimentDraft:
    selected_external_ports: tuple[int, ...] = ()
    total_sccm: float = 1500.0
    sample_a_sccm: float = 500.0
    vacuum_c_sccm: float = 0.0
    duration_s: float = 5.0

    def __post_init__(self) -> None:
        ports = tuple(sorted(self.selected_external_ports))
        if len(set(ports)) != len(ports) or any(not 1 <= port <= 20 for port in ports):
            raise ValueError("气口必须是不重复的 1–20。")
        object.__setattr__(self, "selected_external_ports", ports)
        values = (
            (self.total_sccm, "总流量"),
            (self.sample_a_sccm, "样品流量"),
            (self.vacuum_c_sccm, "真空流量"),
            (self.duration_s, "持续时间"),
        )
        for value, label in values:
            if isinstance(value, bool) or not math.isfinite(float(value)):
                raise ValueError(f"{label}必须是有限数值。")
        if self.total_sccm < 0 or self.sample_a_sccm < 0 or self.vacuum_c_sccm < 0:
            raise ValueError("流量不得为负数。")
        if self.sample_a_sccm > self.total_sccm:
            raise ValueError("样品流量不能大于总流量。")
        if self.duration_s <= 0:
            raise ValueError("持续时间必须大于 0 秒。")

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
            raise ValueError("气口必须位于 1–20。")


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
    status_text: str = ""
    detail_text: str = ""
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


def port_button_text(port: ManualPortSnapshot) -> str:
    alias = port.display_name.strip()
    if alias == f"气口 {port.external_port}":
        alias = ""
    return f"{alias}\n气口 {port.external_port}" if alias else f"气口 {port.external_port}"


class ManualExperimentView(QWidget):
    """Compact manual console driven only by immutable controller snapshots."""

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
        self.setObjectName("manualExperiment")
        self._clock_ns = monotonic_ns
        self._rendering = False
        self._snapshot = ManualExperimentViewSnapshot()
        self._draft = self._snapshot.draft
        self._registry: ChannelRegistry | None = None
        self._allow_mock = False
        self._flow_history: deque[float] = deque([0.0] * 300, maxlen=300)
        self.port_buttons: dict[int, QPushButton] = {}
        self.stepper_frames: dict[QDoubleSpinBox, QFrame] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 10)
        root.setSpacing(8)
        root.addWidget(self._build_waveform(), 5)
        root.addWidget(self._build_control_shelf(), 4)
        root.addWidget(self._build_notice_strip())

        self.total_input.valueChanged.connect(self._on_total_changed)
        self.sample_a_input.valueChanged.connect(self._on_draft_value_changed)
        self.vacuum_c_input.valueChanged.connect(self._on_draft_value_changed)
        self.duration_input.valueChanged.connect(self._on_draft_value_changed)
        self.apply_flow_button.clicked.connect(self._request_supply_change)
        self.release_button.clicked.connect(self._request_release)
        self.stop_button.clicked.connect(lambda: self.stop_requested.emit(StopManualExperimentIntent()))

        self._countdown_timer = QTimer(self)
        self._countdown_timer.setInterval(100)
        self._countdown_timer.timeout.connect(self.refresh_countdown_display)
        self._countdown_timer.start()
        self.render_snapshot(self._snapshot)

    def _build_waveform(self) -> QFrame:
        card = QFrame()
        card.setObjectName("waveCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        head = QFrame()
        head.setObjectName("waveHeader")
        head_layout = QHBoxLayout(head)
        head_layout.setContentsMargins(14, 8, 12, 8)
        title = QLabel("实时气流")
        title.setObjectName("sectionTitle")
        self.telemetry_a_label = QLabel("暂无数据")
        self.telemetry_a_label.setObjectName("flowReading")
        unit = QLabel("ml/min")
        unit.setObjectName("mutedText")
        self.wave_range_label = QLabel("最近 30 秒")
        self.wave_range_label.setObjectName("mutedText")
        head_layout.addWidget(title)
        head_layout.addSpacing(12)
        head_layout.addWidget(self.telemetry_a_label)
        head_layout.addWidget(unit)
        head_layout.addStretch()
        head_layout.addWidget(self.wave_range_label)
        layout.addWidget(head)

        self.plot_widget = pg.PlotWidget(background="#222928")
        self.plot_widget.setObjectName("flowPlot")
        self.plot_widget.setMouseEnabled(x=False, y=False)
        self.plot_widget.setMenuEnabled(False)
        self.plot_widget.showGrid(x=True, y=True, alpha=0.18)
        self.plot_widget.setLabel("left", "流量 · ml/min", color="#87928d")
        self.plot_widget.setLabel("bottom", "时间 · 秒", color="#87928d")
        self.plot_widget.setYRange(0, 2000, padding=0.03)
        self.plot_widget.setXRange(-30, 0, padding=0)
        axis_pen = pg.mkPen("#59635f")
        self.plot_widget.getAxis("left").setPen(axis_pen)
        self.plot_widget.getAxis("bottom").setPen(axis_pen)
        self.plot_widget.getAxis("left").enableAutoSIPrefix(False)
        self.plot_widget.getAxis("bottom").enableAutoSIPrefix(False)
        self._flow_curve = self.plot_widget.plot(
            pen=pg.mkPen("#e2ad50", width=2.2),
            fillLevel=0,
            brush=pg.mkBrush(226, 173, 80, 35),
        )
        self._refresh_plot()
        layout.addWidget(self.plot_widget, 1)
        return card

    def _build_control_shelf(self) -> QFrame:
        shelf = QFrame()
        shelf.setObjectName("controlShelf")
        shelf_layout = QHBoxLayout(shelf)
        shelf_layout.setContentsMargins(10, 10, 10, 10)
        shelf_layout.setSpacing(10)
        shelf_layout.addWidget(self._build_flow_module(), 32)
        shelf_layout.addWidget(self._build_port_module(), 51)
        shelf_layout.addWidget(self._build_action_module(), 17)
        return shelf

    def _build_flow_module(self) -> QFrame:
        module = QFrame()
        module.setObjectName("consoleModule")
        layout = QVBoxLayout(module)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(7)
        title = QLabel("流量设定")
        title.setObjectName("moduleTitle")
        layout.addWidget(title)
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(7)
        grid.setVerticalSpacing(7)
        self.total_input = self._flow_input()
        self.sample_a_input = self._flow_input()
        self.main_b_input = self._flow_input()
        self.main_b_input.setReadOnly(True)
        self.main_b_input.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
        self.vacuum_c_input = self._flow_input()
        grid.addWidget(self._setter("总流量", self.total_input), 0, 0)
        grid.addWidget(self._setter("样品流量", self.sample_a_input), 0, 1)
        grid.addWidget(self._setter("主气流 · 自动计算", self.main_b_input, computed=True), 1, 0)
        grid.addWidget(self._setter("真空流量", self.vacuum_c_input), 1, 1)
        layout.addLayout(grid)
        supply_row = QHBoxLayout()
        self.supply_state_label = QLabel("供气状态未知")
        self.supply_state_label.setObjectName("supplyState")
        self.apply_flow_button = QPushButton("开始供气")
        self.apply_flow_button.setObjectName("supplyButton")
        supply_row.addWidget(self.supply_state_label)
        supply_row.addStretch()
        supply_row.addWidget(self.apply_flow_button)
        layout.addLayout(supply_row)
        return module

    def _build_port_module(self) -> QFrame:
        module = QFrame()
        module.setObjectName("consoleModule")
        layout = QVBoxLayout(module)
        layout.setContentsMargins(9, 8, 9, 8)
        layout.setSpacing(6)
        title_row = QHBoxLayout()
        title = QLabel("气口选择")
        title.setObjectName("moduleTitle")
        title_row.addWidget(title)
        title_row.addStretch()
        layout.addLayout(title_row)
        bay = QFrame()
        bay.setObjectName("portBay")
        self.port_layout = QGridLayout(bay)
        self.port_layout.setContentsMargins(8, 10, 8, 11)
        self.port_layout.setHorizontalSpacing(6)
        self.port_layout.setVerticalSpacing(9)
        for port in range(1, 21):
            button = ConsolePortButton(port)
            button.clicked.connect(lambda checked, external_port=port: self._toggle_port(external_port, checked))
            self.port_buttons[port] = button
            self.port_layout.addWidget(button, (port - 1) // 10, (port - 1) % 10)
        layout.addWidget(bay, 1)
        return module

    def _build_action_module(self) -> QFrame:
        module = QFrame()
        module.setObjectName("consoleModule")
        layout = QVBoxLayout(module)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(8)
        title = QLabel("实验控制")
        title.setObjectName("moduleTitle")
        layout.addWidget(title)
        self.duration_input = QDoubleSpinBox()
        self.duration_input.setObjectName("durationInput")
        self.duration_input.setDecimals(0)
        self.duration_input.setSingleStep(DURATION_STEP_S)
        self.duration_input.setSuffix(" 秒")
        self.duration_input.setKeyboardTracking(False)
        self.duration_input.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._setter("持续时间", self.duration_input))
        self.countdown_label = QLabel("剩余 --")
        self.countdown_label.setObjectName("countdownLabel")
        self.countdown_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.countdown_label)
        layout.addStretch()
        self.release_button = QPushButton("释放气味")
        self.release_button.setObjectName("releaseButton")
        self.release_button.setMinimumHeight(46)
        self.stop_button = QPushButton("停止实验")
        self.stop_button.setObjectName("experimentStopButton")
        layout.addWidget(self.release_button)
        layout.addWidget(self.stop_button)
        return module

    def _build_notice_strip(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("noticeStrip")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(12, 7, 12, 7)
        self.status_label = QLabel("")
        self.status_label.setObjectName("pageStatus")
        self.detail_label = QLabel("")
        self.detail_label.setObjectName("pageDetail")
        self.detail_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        layout.addWidget(self.detail_label, 1)
        self.notice_frame = frame
        frame.setVisible(False)
        return frame

    def _setter(self, label_text: str, control: QWidget, *, computed: bool = False) -> QFrame:
        card = QFrame()
        card.setObjectName("computedSetter" if computed else "setterCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(7, 5, 7, 6)
        layout.setSpacing(3)
        label = QLabel(label_text)
        label.setObjectName("setterLabel")
        layout.addWidget(label)
        if isinstance(control, QDoubleSpinBox) and not computed:
            layout.addWidget(self._stepper(control))
        else:
            layout.addWidget(control)
        return card

    def _stepper(self, control: QDoubleSpinBox) -> QFrame:
        control.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
        frame = QFrame()
        frame.setObjectName("stepper")
        row = QHBoxLayout(frame)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        minus = QPushButton("−")
        minus.setObjectName("stepButton")
        minus.setAccessibleName("减小")
        plus = QPushButton("+")
        plus.setObjectName("stepButton")
        plus.setAccessibleName("增大")
        minus.clicked.connect(control.stepDown)
        plus.clicked.connect(control.stepUp)
        row.addWidget(minus)
        row.addWidget(control, 1)
        row.addWidget(plus)
        self.stepper_frames[control] = frame
        return frame

    @staticmethod
    def _flow_input() -> QDoubleSpinBox:
        control = QDoubleSpinBox()
        control.setDecimals(0)
        control.setSingleStep(FLOW_STEP_ML_MIN)
        control.setSuffix(" ml/min")
        control.setKeyboardTracking(False)
        control.setAlignment(Qt.AlignmentFlag.AlignCenter)
        return control

    @property
    def draft(self) -> ManualExperimentDraft:
        return self._draft

    @property
    def snapshot(self) -> ManualExperimentViewSnapshot:
        return self._snapshot

    def set_registry(self, registry: ChannelRegistry, allow_mock: bool) -> None:
        if not isinstance(registry, ChannelRegistry):
            raise TypeError("手动实验需要有效的气口映射。")
        self._registry = registry
        self._allow_mock = bool(allow_mock)
        ports = tuple(
            ManualPortSnapshot(
                external_port=channel.external_port,
                display_name=channel.display_name,
                available=(channel.enabled and channel.verification_valid_for(allow_mock=self._allow_mock)),
            )
            for channel in registry.channels
        )
        self._snapshot = replace(self._snapshot, ports=ports)
        available = {port.external_port for port in ports if port.available}
        selected = tuple(port for port in self._draft.selected_external_ports if port in available)
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
                    fault=(snapshot.recovery_reason if port.external_port in snapshot.possibly_open else ""),
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
            raise TypeError("手动实验只能显示有效状态。")
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
            self.duration_input.setRange(
                DURATION_STEP_S,
                max(DURATION_STEP_S, snapshot.max_duration_s),
            )
            self.total_input.setValue(snapshot.draft.total_sccm)
            self.sample_a_input.setValue(snapshot.draft.sample_a_sccm)
            self.vacuum_c_input.setValue(snapshot.draft.vacuum_c_sccm)
            self.duration_input.setValue(snapshot.draft.duration_s)
            self.main_b_input.setRange(0.0, max(0.0, snapshot.max_total_sccm))
            self.main_b_input.setValue(snapshot.draft.main_b_sccm)
        finally:
            self._rendering = False

        for control in (
            self.total_input,
            self.sample_a_input,
            self.vacuum_c_input,
            self.duration_input,
        ):
            control.setEnabled(snapshot.controls_enabled)
            self.stepper_frames[control].setEnabled(snapshot.controls_enabled)
        self.main_b_input.setEnabled(snapshot.controls_enabled)
        self.main_b_input.setReadOnly(True)
        self.apply_flow_button.setEnabled(snapshot.can_apply_flow and not snapshot.supply_transitioning)
        self.apply_flow_button.setText("停止供气" if snapshot.supply_enabled is True else "开始供气")
        if snapshot.supply_transitioning:
            self.supply_state_label.setText("正在切换")
        elif snapshot.supply_enabled is True:
            self.supply_state_label.setText("供气已开启")
        elif snapshot.supply_enabled is False:
            self.supply_state_label.setText("供气已停止")
        else:
            self.supply_state_label.setText("供气状态未知")
        self.release_button.setEnabled(snapshot.can_release)
        self.stop_button.setEnabled(snapshot.can_stop)
        status_text = user_facing_text(snapshot.status_text)
        detail_text = user_facing_text(snapshot.detail_text)
        self.status_label.setText(status_text)
        self.detail_label.setText(detail_text)
        if snapshot.experiment.status is ManualExperimentStatus.RECOVERY_REQUIRED:
            self.show_notice(status_text or "需要立即处理", detail_text, severity="error")
        elif self._is_actionable_notice(status_text, detail_text):
            self.show_notice(status_text or "设备提示", detail_text)
        else:
            self.clear_notice()
        telemetry = snapshot.telemetry_a_sccm
        self.telemetry_a_label.setText("暂无数据" if telemetry is None else f"{telemetry:.0f}")
        self._render_ports()
        self.refresh_countdown_display()

    def update_a_observation(self, value: float) -> None:
        if isinstance(value, bool) or not math.isfinite(float(value)):
            self._snapshot = replace(self._snapshot, telemetry_a_sccm=None)
            self.telemetry_a_label.setText("暂无数据")
            return
        numeric = float(value)
        self._snapshot = replace(self._snapshot, telemetry_a_sccm=numeric)
        self.telemetry_a_label.setText(f"{numeric:.0f}")
        self._flow_history.append(numeric)
        self._refresh_plot()

    def _refresh_plot(self) -> None:
        values = list(self._flow_history)
        count = len(values)
        xs = [(-30.0 + index * 30.0 / max(1, count - 1)) for index in range(count)]
        self._flow_curve.setData(xs, values)

    def render_supply_state(self, enabled: bool, message: str = "") -> None:
        self._snapshot = replace(
            self._snapshot,
            supply_enabled=bool(enabled),
            detail_text=message or self._snapshot.detail_text,
        )
        self.apply_flow_button.setText("停止供气" if enabled else "开始供气")
        self.supply_state_label.setText("供气已开启" if enabled else "供气已停止")
        if message and self._is_actionable_notice("", message):
            self.show_notice("设备提示", user_facing_text(message))

    def show_notice(self, title: str, message: str, *, severity: str = "warning") -> None:
        title = user_facing_text(title).strip()
        message = user_facing_text(message).strip()
        if not title and not message:
            self.clear_notice()
            return
        self.status_label.setText(title)
        self.detail_label.setText(message)
        self.notice_frame.setProperty("severity", severity)
        self.notice_frame.setVisible(True)
        self.notice_frame.style().unpolish(self.notice_frame)
        self.notice_frame.style().polish(self.notice_frame)

    def clear_notice(self) -> None:
        self.notice_frame.setVisible(False)

    @staticmethod
    def _is_actionable_notice(title: str, message: str) -> bool:
        text = f"{title} {message}"
        markers = (
            "失败",
            "异常",
            "冲突",
            "不足",
            "LOW_FLOW",
            "需要立即",
            "无法",
            "未完成",
            "过期",
        )
        return any(marker in text for marker in markers)

    def refresh_countdown_display(self) -> None:
        experiment = self._snapshot.experiment
        if experiment.status is ManualExperimentStatus.STIMULATING and experiment.deadline_ns is not None:
            remaining_ns = max(0, experiment.deadline_ns - self._clock_ns())
            self.countdown_label.setText(f"剩余 {remaining_ns / 1_000_000_000:.1f} 秒")
            return
        if experiment.status is ManualExperimentStatus.COMPLETED:
            self.countdown_label.setText("本次已完成")
        elif experiment.status is ManualExperimentStatus.RECOVERY_REQUIRED:
            self.countdown_label.setText("需要恢复")
        else:
            self.countdown_label.setText("剩余 --")

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
                states.append("实际开启")
            if port.fault:
                states.append(f"故障：{user_facing_text(port.fault)}")
            if isinstance(button, ConsolePortButton):
                button.set_port_content(port.display_name)
            else:
                button.setText(port_button_text(port))
            button.setAccessibleName(port_button_text(port).replace("\n", "，"))
            button.setAccessibleDescription("；".join(states))
            button.setEnabled(self._snapshot.controls_enabled and port.available)
            button.setChecked(is_selected)
            visual_state = (
                "fault"
                if port.fault
                else (
                    "open"
                    if port.actually_open
                    else ("selected" if is_selected else ("available" if port.available else "unavailable"))
                )
            )
            button.setProperty("portState", visual_state)
            button.style().unpolish(button)
            button.style().polish(button)

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
        self._snapshot = replace(self._snapshot, draft=self._draft)
        self._render_ports()
        self.release_button.setEnabled(self._snapshot.controls_enabled and bool(self._draft.selected_external_ports))
        self.draft_changed.emit(ManualDraftChangedIntent(self._draft))

    def _request_supply_change(self) -> None:
        self.supply_requested.emit(
            ManualSupplyIntent(
                enabled=self._snapshot.supply_enabled is not True,
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
            ManualExperimentStatus.IDLE: "",
            ManualExperimentStatus.FLOW_PENDING: "正在确认流量",
            ManualExperimentStatus.SELECTOR_PENDING: "正在切换气路",
            ManualExperimentStatus.OPENING: "正在开启所选气口",
            ManualExperimentStatus.STIMULATING: "正在释放气味",
            ManualExperimentStatus.CLOSING: "正在关闭气口",
            ManualExperimentStatus.ZEROING_A: "正在停止样品气流",
            ManualExperimentStatus.SELECTOR_COMPENSATION: "正在恢复气路",
            ManualExperimentStatus.RESTORING_SUPPLY: "正在恢复供气",
            ManualExperimentStatus.COMPLETED: "实验已完成",
            ManualExperimentStatus.RECOVERY_REQUIRED: "需要立即处理",
        }
        return labels[snapshot.status]

    def _on_total_changed(self, value: float) -> None:
        if self._rendering:
            return
        self._rendering = True
        try:
            self.sample_a_input.setMaximum(min(self._snapshot.max_sample_a_sccm, value))
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
        self._snapshot = replace(self._snapshot, draft=self._draft)
        self.main_b_input.setValue(self._draft.main_b_sccm)
        self.draft_changed.emit(ManualDraftChangedIntent(self._draft))
