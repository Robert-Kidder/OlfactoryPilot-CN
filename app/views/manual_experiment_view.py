from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace

import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFontMetrics, QKeyEvent, QMouseEvent
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    DoubleSpinBox,
    IconInfoBadge,
    InfoBadge,
    InfoBar,
    InfoBarPosition,
    InfoLevel,
    PrimaryPushButton,
    PushButton,
    StrongBodyLabel,
    ToolTipFilter,
    ToolTipPosition,
)
from qfluentwidgets import (
    FluentIcon as FIF,
)
from shiboken6 import isValid

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
DURATION_MIN_S = 1.0
AMBER = "#E2AD50"
SAFETY_NOTICE_TITLES = frozenset(
    {"气流不足", "设备数据中断", "当前状态不允许操作"}
)


def _normalize_alias(value: object) -> str:
    return " ".join(str(value or "").split())


def _operator_detail_text(value: object) -> str:
    text = user_facing_text(value)
    if text.startswith("当前不可操作："):
        text = text.split("；", 1)[0]
        replacements = (
            ("真实硬件尚未授权", "当前版本尚未开放现场操作"),
            ("设备尚未连接", "请先连接设备"),
            ("硬件自检尚未通过", "设备检查尚未通过"),
        )
        for source, target in replacements:
            text = text.replace(source, target)
        if "安全状态为" in text:
            return "当前不可操作：当前状态不允许操作。"
        if "控制权" in text and "当前属于" in text:
            return "当前不可操作：设备正在执行其他操作。"
        return text.rstrip("。") + "。"
    if text.startswith("手动实验需要恢复："):
        return (
            "需要安全恢复：本次操作未能确认安全完成。"
            "请执行全局停止，完成后重新连接并检查设备。"
        )
    return text


class PortTile(CardWidget):
    """Fluent gas-port tile with independent selection and hardware status."""

    def __init__(self, external_port: int, parent=None) -> None:
        self.external_port = external_port
        self._alias = ""
        self._selected = False
        self._available = False
        self._actually_open = False
        self._fault = ""
        super().__init__(parent)
        self.setObjectName(f"portTile{external_port:02d}")
        self.setClickEnabled(True)
        self.setFixedHeight(82)
        self.setMinimumWidth(72)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(7, 5, 7, 5)
        layout.setSpacing(2)
        self.selection_accent = QFrame(self)
        self.selection_accent.setFixedHeight(3)
        self.selection_accent.setStyleSheet(f"background: {AMBER}; border-radius: 1px;")
        self.selection_accent.hide()
        layout.addWidget(self.selection_accent)

        self.title_label = StrongBodyLabel(self)
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.title_label.installEventFilter(
            ToolTipFilter(self.title_label, showDelay=300, position=ToolTipPosition.TOP)
        )
        layout.addWidget(self.title_label)

        self.port_label = CaptionLabel(self)
        self.port_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.port_label.setStyleSheet("color: #858F8B;")
        layout.addWidget(self.port_label)

        state_row = QWidget(self)
        state_layout = QHBoxLayout(state_row)
        state_layout.setContentsMargins(0, 0, 0, 0)
        state_layout.setSpacing(4)
        state_layout.addStretch(1)
        self.open_group = self._status_group(
            state_row,
            IconInfoBadge.success(FIF.PLAY, parent=state_row),
            "开启",
            "#77C99D",
        )
        self.fault_group = self._status_group(
            state_row,
            IconInfoBadge.error(FIF.CANCEL, parent=state_row),
            "故障",
            "#FF918A",
        )
        state_layout.addWidget(self.open_group)
        state_layout.addWidget(self.fault_group)
        state_layout.addStretch(1)
        layout.addWidget(state_row)
        self.open_group.hide()
        self.fault_group.hide()
        self.set_port_content("")

    @staticmethod
    def _status_group(parent: QWidget, badge: IconInfoBadge, text: str, color: str) -> QWidget:
        group = QWidget(parent)
        row = QHBoxLayout(group)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2)
        badge.setFixedSize(16, 16)
        label = CaptionLabel(text, group)
        label.setStyleSheet(f"color: {color};")
        row.addWidget(badge)
        row.addWidget(label)
        return group

    def _normalBackgroundColor(self) -> QColor:
        if not self.isEnabled():
            return QColor(255, 255, 255, 5)
        if self._selected:
            return QColor(226, 173, 80, 42)
        return super()._normalBackgroundColor()

    def _hoverBackgroundColor(self) -> QColor:
        if self._selected:
            return QColor(226, 173, 80, 58)
        return super()._hoverBackgroundColor()

    def _pressedBackgroundColor(self) -> QColor:
        if self._selected:
            return QColor(226, 173, 80, 28)
        return super()._pressedBackgroundColor()

    @property
    def alias(self) -> str:
        return self._alias

    def set_port_content(self, display_name: str) -> None:
        alias = _normalize_alias(display_name)
        if alias in {f"气口 {self.external_port}", f"气口 {self.external_port:02d}"}:
            alias = ""
        self._alias = alias
        number = f"气口 {self.external_port:02d}"
        self.port_label.setText(number)
        self.port_label.setVisible(bool(alias))
        self._refresh_elision()

    def elided_alias(self, width: int | None = None) -> str:
        if not self._alias:
            return ""
        available = max(20, width if width is not None else self.title_label.width())
        return QFontMetrics(self.title_label.font()).elidedText(
            self._alias,
            Qt.TextElideMode.ElideRight,
            available,
        )

    def _refresh_elision(self) -> None:
        number = f"气口 {self.external_port:02d}"
        if not self._alias:
            self.title_label.setText(number)
            self.title_label.setToolTip("")
            return
        available = max(20, self.width() - 18)
        elided = self.elided_alias(available)
        self.title_label.setText(elided)
        self.title_label.setToolTip(
            f"{self._alias}\n{number}" if elided != self._alias else ""
        )

    def set_state(
        self,
        *,
        display_name: str,
        selected: bool,
        actually_open: bool,
        fault: str,
        available: bool,
        interactive: bool,
    ) -> None:
        self._selected = bool(selected and available)
        self._actually_open = bool(actually_open)
        self._fault = user_facing_text(fault)
        self._available = bool(available)
        self.set_port_content(display_name)
        self.selection_accent.setVisible(self._selected)
        self.open_group.setVisible(self._actually_open)
        self.fault_group.setVisible(bool(self._fault))
        self.setEnabled(bool(interactive and available))
        self.setClickEnabled(self.isEnabled())
        self.setCursor(
            Qt.CursorShape.PointingHandCursor
            if self.isEnabled()
            else Qt.CursorShape.ArrowCursor
        )
        states = ["可用" if available else "不可用"]
        if self._selected:
            states.append("已选择")
        if self._actually_open:
            states.append("实际开启")
        if self._fault:
            states.append(f"故障：{self._fault}")
        self.setAccessibleName(port_button_text_from_values(self.external_port, display_name))
        self.setAccessibleDescription("；".join(states))
        visual_state = (
            "fault"
            if self._fault
            else (
                "open"
                if self._actually_open
                else ("selected" if self._selected else ("available" if available else "disabled"))
            )
        )
        self.setProperty("portState", visual_state)
        self._updateBackgroundColor()
        self.update()

    def isChecked(self) -> bool:  # noqa: N802 - Qt-style compatibility
        return self._selected

    def setChecked(self, selected: bool) -> None:  # noqa: N802 - Qt-style compatibility
        self._selected = bool(selected)
        self.selection_accent.setVisible(self._selected)
        self._updateBackgroundColor()
        self.update()

    def click(self) -> None:
        if self.isEnabled():
            self.clicked.emit()

    def text(self) -> str:
        number = f"气口 {self.external_port:02d}"
        return f"{self._alias}\n{number}" if self._alias else number

    def toolTip(self) -> str:  # noqa: N802 - compatibility for tests/accessibility
        return self.title_label.toolTip()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._refresh_elision()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        if event.button() != Qt.MouseButton.LeftButton:
            event.ignore()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt override
        if event.key() in {Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter}:
            self.click()
            event.accept()
            return
        super().keyPressEvent(event)


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


def port_button_text_from_values(external_port: int, display_name: str) -> str:
    alias = _normalize_alias(display_name)
    number = f"气口 {external_port:02d}"
    if alias in {f"气口 {external_port}", number}:
        alias = ""
    return f"{alias}，{number}" if alias else number


def port_button_text(port: ManualPortSnapshot) -> str:
    alias = _normalize_alias(port.display_name)
    number = f"气口 {port.external_port:02d}"
    if alias in {f"气口 {port.external_port}", number}:
        alias = ""
    return f"{alias}\n{number}" if alias else number


class ManualExperimentView(QWidget):
    """Manual experiment product page driven by immutable controller snapshots."""

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
        self._flow_history: deque[tuple[float, float]] = deque(maxlen=1000)
        self.port_tiles: dict[int, PortTile] = {}
        self.port_buttons = self.port_tiles
        self._notice_signature: tuple[str, str, str] | None = None
        self._dismissed_notice_signature: tuple[str, str, str] | None = None
        self._notice_key: object | None = None
        self._dismissed_notice_key: object | None = None
        self._notice_severity: str | None = None
        self._header_safety_state = "SAFE"

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)
        root.addWidget(self._build_waveform(), 5)

        controls = QWidget(self)
        control_layout = QHBoxLayout(controls)
        control_layout.setContentsMargins(0, 0, 0, 0)
        control_layout.setSpacing(10)
        control_layout.addWidget(self._build_flow_card(), 7)
        control_layout.addWidget(self._build_action_card(), 3)
        root.addWidget(controls)
        root.addWidget(self._build_port_card())

        self._notice_host = QWidget(self)
        self._notice_layout = QVBoxLayout(self._notice_host)
        self._notice_layout.setContentsMargins(0, 0, 0, 0)
        self._notice_host.hide()
        root.addWidget(self._notice_host)
        self.notice_frame: QWidget | None = QWidget(self._notice_host)
        self.notice_frame.hide()
        self.status_label = BodyLabel("", self)
        self.detail_label = BodyLabel("", self)
        self.status_label.hide()
        self.detail_label.hide()

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
        self.render_snapshot(self._snapshot)

    @property
    def current_notice_title(self) -> str:
        return self.status_label.text()

    @property
    def current_notice_severity(self) -> str | None:
        return self._notice_severity

    def showEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        self._countdown_timer.start()

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._countdown_timer.stop()
        super().hideEvent(event)

    def _build_waveform(self) -> CardWidget:
        card = CardWidget(self)
        card.setObjectName("flowTelemetryCard")
        card.setMinimumHeight(190)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 10, 14, 12)
        layout.setSpacing(8)
        header = QHBoxLayout()
        title = StrongBodyLabel("实时气流", card)
        self.telemetry_a_label = StrongBodyLabel("暂无数据", card)
        self.telemetry_a_label.setStyleSheet(f"color: {AMBER}; font-size: 18px;")
        unit = CaptionLabel("A 路实测 · ml/min", card)
        range_label = CaptionLabel("最近 30 秒", card)
        header.addWidget(title)
        header.addSpacing(14)
        header.addWidget(self.telemetry_a_label)
        header.addWidget(unit)
        header.addStretch(1)
        header.addWidget(range_label)
        layout.addLayout(header)

        self.plot_widget = pg.PlotWidget(background="#111715")
        self.plot_widget.setObjectName("flowPlot")
        self.plot_widget.setMouseEnabled(x=False, y=False)
        self.plot_widget.setMenuEnabled(False)
        self.plot_widget.showGrid(x=True, y=True, alpha=0.16)
        self.plot_widget.setLabel("left", "A 路流量 · ml/min", color="#858F8B")
        self.plot_widget.setLabel("bottom", "时间 · 秒", color="#858F8B")
        self.plot_widget.setYRange(0, 2000, padding=0.03)
        self.plot_widget.setXRange(-30, 0, padding=0)
        axis_pen = pg.mkPen("#48524E")
        self.plot_widget.getAxis("left").setPen(axis_pen)
        self.plot_widget.getAxis("bottom").setPen(axis_pen)
        self.plot_widget.getAxis("left").enableAutoSIPrefix(False)
        self.plot_widget.getAxis("bottom").enableAutoSIPrefix(False)
        self._flow_curve = self.plot_widget.plot(
            pen=pg.mkPen(AMBER, width=2.2),
            fillLevel=0,
            brush=pg.mkBrush(226, 173, 80, 30),
        )
        self._refresh_plot()
        layout.addWidget(self.plot_widget, 1)
        return card

    def _build_flow_card(self) -> CardWidget:
        card = CardWidget(self)
        card.setObjectName("flowSettingsCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 10, 14, 12)
        layout.setSpacing(8)
        layout.addWidget(StrongBodyLabel("流量设置", card))
        fields = QGridLayout()
        fields.setHorizontalSpacing(10)
        fields.setVerticalSpacing(6)
        self.total_input = self._flow_input()
        self.sample_a_input = self._flow_input()
        self.main_b_input = self._flow_input()
        self.main_b_input.setReadOnly(True)
        self.vacuum_c_input = self._flow_input()
        fields.addWidget(self._field("总流量 T", self.total_input), 0, 0)
        fields.addWidget(self._field("样品流量 A", self.sample_a_input), 0, 1)
        fields.addWidget(self._field("主气流 B", self.main_b_input), 0, 2)
        fields.addWidget(self._field("真空流量 C", self.vacuum_c_input), 0, 3)
        layout.addLayout(fields)

        supply_row = QHBoxLayout()
        supply_row.setSpacing(8)
        self.supply_state_badge = InfoBadge.info("供气状态未知", parent=card)
        self.supply_state_label = CaptionLabel("供气状态未知", card)
        self.supply_state_label.hide()
        self.apply_flow_button = PushButton(FIF.POWER_BUTTON, "开始供气", card)
        supply_row.addWidget(self.supply_state_badge)
        supply_row.addStretch(1)
        supply_row.addWidget(self.apply_flow_button)
        layout.addLayout(supply_row)
        return card

    def _build_action_card(self) -> CardWidget:
        card = CardWidget(self)
        card.setObjectName("experimentActionsCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 10, 14, 12)
        layout.setSpacing(8)
        layout.addWidget(StrongBodyLabel("实验控制", card))
        self.duration_input = DoubleSpinBox(card)
        self.duration_input.setObjectName("durationInput")
        self.duration_input.setDecimals(0)
        self.duration_input.setSingleStep(DURATION_STEP_S)
        self.duration_input.setSuffix(" 秒")
        self.duration_input.setKeyboardTracking(False)
        self.duration_input.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._field("持续时间", self.duration_input))
        self.countdown_label = CaptionLabel("剩余 --", card)
        self.countdown_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.countdown_label.setStyleSheet(f"color: {AMBER};")
        layout.addWidget(self.countdown_label)
        action_row = QHBoxLayout()
        self.release_button = PrimaryPushButton(FIF.PLAY, "释放气味", card)
        self.stop_button = PushButton(FIF.CANCEL, "停止实验", card)
        self.stop_button.setStyleSheet("color: #FFAAA4;")
        action_row.addWidget(self.release_button, 2)
        action_row.addWidget(self.stop_button, 1)
        layout.addLayout(action_row)
        return card

    def _build_port_card(self) -> CardWidget:
        card = CardWidget(self)
        card.setObjectName("portGridCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 9, 12, 11)
        layout.setSpacing(7)
        layout.addWidget(StrongBodyLabel("气口", card))
        bay = QWidget(card)
        self.port_layout = QGridLayout(bay)
        self.port_layout.setContentsMargins(0, 0, 0, 0)
        self.port_layout.setHorizontalSpacing(6)
        self.port_layout.setVerticalSpacing(7)
        for port in range(1, 21):
            tile = PortTile(port, bay)
            tile.clicked.connect(lambda external_port=port: self._toggle_port(external_port))
            self.port_tiles[port] = tile
            self.port_layout.addWidget(tile, (port - 1) // 10, (port - 1) % 10)
        layout.addWidget(bay)
        return card

    @staticmethod
    def _field(label_text: str, control: QWidget) -> QWidget:
        field = QWidget()
        layout = QVBoxLayout(field)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        layout.addWidget(CaptionLabel(label_text, field))
        layout.addWidget(control)
        return field

    @staticmethod
    def _flow_input() -> DoubleSpinBox:
        control = DoubleSpinBox()
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
                detail_text=(
                    f"手动实验需要恢复：{snapshot.recovery_reason}"
                    if snapshot.status is ManualExperimentStatus.RECOVERY_REQUIRED
                    else self._snapshot.detail_text
                ),
            )
        if not isinstance(snapshot, ManualExperimentViewSnapshot):
            raise TypeError("手动实验只能显示有效状态。")
        snapshot = replace(
            snapshot,
            status_text=user_facing_text(snapshot.status_text),
            detail_text=_operator_detail_text(snapshot.detail_text),
        )
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
                DURATION_MIN_S,
                max(DURATION_MIN_S, snapshot.max_duration_s),
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
        self.main_b_input.setEnabled(snapshot.controls_enabled)
        self.main_b_input.setReadOnly(True)
        self.apply_flow_button.setEnabled(
            snapshot.can_apply_flow and not snapshot.supply_transitioning
        )
        self.apply_flow_button.setText(
            "开始供气" if snapshot.supply_enabled is False else "停止供气"
        )
        self._render_supply_badge(snapshot)
        self.release_button.setEnabled(snapshot.can_release)
        self.stop_button.setEnabled(snapshot.can_stop)

        status_text = snapshot.status_text
        detail_text = snapshot.detail_text
        if snapshot.experiment.status is ManualExperimentStatus.RECOVERY_REQUIRED:
            self.show_notice(
                status_text or "需要立即处理",
                detail_text,
                severity="error",
                notice_key=(
                    "manual-recovery",
                    snapshot.experiment.identity,
                    snapshot.experiment.recovery_reason,
                ),
            )
        elif self._header_safety_state != "SAFE":
            # MainWindow already keeps the current abnormal state and next
            # action visible in the header. Snapshot rendering must not
            # recreate a dismissed transition-driven safety InfoBar.
            pass
        elif status_text or detail_text:
            severity = "error" if self._is_actionable_notice(status_text, detail_text) else "info"
            self.show_notice(status_text or "状态", detail_text, severity=severity)
        else:
            self.clear_notice()
        self.telemetry_a_label.setText(
            "暂无数据"
            if snapshot.telemetry_a_sccm is None
            else f"{snapshot.telemetry_a_sccm:.0f}"
        )
        self._render_ports()
        self.refresh_countdown_display()

    def set_header_safety_state(self, state: str) -> None:
        """Keep snapshot notices consistent with MainWindow's durable header state."""

        self._header_safety_state = str(state or "UNKNOWN")

    def _render_supply_badge(self, snapshot: ManualExperimentViewSnapshot) -> None:
        if snapshot.supply_transitioning:
            text, level = "正在切换", InfoLevel.ATTENTION
        elif snapshot.supply_enabled is True:
            text, level = "供气已开启", InfoLevel.SUCCESS
        elif snapshot.supply_enabled is False:
            text, level = "供气已停止", InfoLevel.INFOAMTION
        else:
            text, level = "供气状态未知", InfoLevel.INFOAMTION
        self.supply_state_badge.setText(text)
        self.supply_state_badge.setLevel(level)
        self.supply_state_label.setText(text)

    def update_a_observation(self, value: float) -> None:
        if isinstance(value, bool) or not math.isfinite(float(value)):
            self._snapshot = replace(self._snapshot, telemetry_a_sccm=None)
            self.telemetry_a_label.setText("暂无数据")
            return
        numeric = float(value)
        self._snapshot = replace(self._snapshot, telemetry_a_sccm=numeric)
        self.telemetry_a_label.setText(f"{numeric:.0f}")
        self._flow_history.append((self._clock_ns() / 1_000_000_000, numeric))
        self._refresh_plot()

    def _refresh_plot(self) -> None:
        now_s = self._clock_ns() / 1_000_000_000
        cutoff_s = now_s - 30.0
        while self._flow_history and self._flow_history[0][0] < cutoff_s:
            self._flow_history.popleft()
        xs = [sampled_at - now_s for sampled_at, _value in self._flow_history]
        values = [value for _sampled_at, value in self._flow_history]
        self._flow_curve.setData(xs, values)

    def render_supply_state(self, enabled: bool, message: str = "") -> None:
        self._snapshot = replace(
            self._snapshot,
            supply_enabled=bool(enabled),
            detail_text=message or self._snapshot.detail_text,
        )
        self.apply_flow_button.setText("停止供气" if enabled else "开始供气")
        self._render_supply_badge(self._snapshot)
        if message:
            self.show_notice("状态", user_facing_text(message), severity="info")

    def show_notice(
        self,
        title: str,
        message: str,
        *,
        severity: str = "warning",
        notice_key: object | None = None,
    ) -> None:
        title = user_facing_text(title).strip()
        message = user_facing_text(message).strip()
        if not title and not message:
            self.clear_notice()
            return
        signature = (title, message, severity)
        effective_key = signature if notice_key is None else notice_key
        incoming_manual_recovery = bool(
            isinstance(effective_key, tuple)
            and effective_key
            and effective_key[0] == "manual-recovery"
        )
        if effective_key == self._dismissed_notice_key:
            return
        if (
            self._notice_severity == "error"
            and self.current_notice_title in SAFETY_NOTICE_TITLES
            and title not in SAFETY_NOTICE_TITLES
            and not incoming_manual_recovery
        ):
            return
        if (
            effective_key == self._notice_key
            and self.notice_frame is not None
            and isValid(self.notice_frame)
            and self.notice_frame.isVisible()
        ):
            return
        old = self.notice_frame
        if old is not None and isValid(old):
            self._notice_layout.removeWidget(old)
            old.hide()
            old.deleteLater()
        self.status_label.setText(title)
        self.detail_label.setText(message)
        factory = {
            "error": InfoBar.error,
            "success": InfoBar.success,
            "info": InfoBar.info,
            "warning": InfoBar.warning,
        }.get(severity, InfoBar.warning)
        bar = factory(
            title,
            message,
            isClosable=True,
            duration=-1,
            position=InfoBarPosition.NONE,
            parent=self._notice_host,
        )
        bar.setObjectName("manualExperimentInfoBar")
        bar.closedSignal.connect(
            lambda: self._dismiss_notice(signature, effective_key, bar)
        )
        self._notice_layout.addWidget(bar)
        self.notice_frame = bar
        self._notice_signature = signature
        self._notice_key = effective_key
        self._dismissed_notice_signature = None
        self._dismissed_notice_key = None
        self._notice_severity = severity
        self._notice_host.show()
        bar.show()

    def _dismiss_notice(
        self,
        signature: tuple[str, str, str],
        notice_key: object,
        bar: InfoBar,
    ) -> None:
        if self.notice_frame is not bar:
            return
        self._notice_layout.removeWidget(bar)
        self.notice_frame = None
        self._notice_signature = signature
        self._notice_key = notice_key
        self._dismissed_notice_signature = signature
        self._dismissed_notice_key = notice_key
        self._notice_severity = None
        self._notice_host.hide()
        self.status_label.setText("")
        self.detail_label.setText("")

    def clear_notice(self) -> None:
        if self.notice_frame is not None and isValid(self.notice_frame):
            self.notice_frame.hide()
        self._notice_host.hide()
        self._notice_signature = None
        self._notice_key = None
        self._dismissed_notice_signature = None
        self._dismissed_notice_key = None
        self._notice_severity = None
        self.status_label.setText("")
        self.detail_label.setText("")

    def clear_safety_notice(self) -> None:
        """恢复 SAFE 时只清除 safety transition，不覆盖其他操作通知。"""

        current_is_safety = (
            isinstance(self._notice_key, tuple)
            and bool(self._notice_key)
            and self._notice_key[0] == "safety"
        )
        dismissed_is_safety = (
            isinstance(self._dismissed_notice_key, tuple)
            and bool(self._dismissed_notice_key)
            and self._dismissed_notice_key[0] == "safety"
        )
        if current_is_safety:
            self.clear_notice()
        elif dismissed_is_safety:
            self._dismissed_notice_signature = None
            self._dismissed_notice_key = None

    @staticmethod
    def _is_actionable_notice(title: str, message: str) -> bool:
        text = f"{title} {message}"
        markers = (
            "失败",
            "异常",
            "冲突",
            "不足",
            "需要立即",
            "无法",
            "未完成",
            "过期",
            "中断",
            "紧急",
            "阻断",
            "低于",
            "不允许",
        )
        return any(marker in text for marker in markers)

    def refresh_countdown_display(self) -> None:
        experiment = self._snapshot.experiment
        if (
            experiment.status is ManualExperimentStatus.STIMULATING
            and experiment.deadline_ns is not None
        ):
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
        for external_port, tile in self.port_tiles.items():
            port = snapshots.get(external_port, ManualPortSnapshot(external_port))
            tile.set_state(
                display_name=port.display_name,
                selected=external_port in selected,
                actually_open=port.actually_open,
                fault=port.fault,
                available=port.available,
                interactive=self._snapshot.controls_enabled,
            )

    def _toggle_port(self, external_port: int) -> None:
        tile = self.port_tiles[external_port]
        if self._rendering or not tile.isEnabled():
            return
        selected = set(self._draft.selected_external_ports)
        if external_port in selected:
            selected.remove(external_port)
        else:
            selected.add(external_port)
        self._draft = replace(
            self._draft,
            selected_external_ports=tuple(sorted(selected)),
        )
        self._snapshot = replace(self._snapshot, draft=self._draft)
        self._render_ports()
        self.release_button.setEnabled(False)
        self.draft_changed.emit(ManualDraftChangedIntent(self._draft))

    def _request_supply_change(self) -> None:
        self.supply_requested.emit(
            ManualSupplyIntent(
                enabled=self._snapshot.supply_enabled is False,
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
