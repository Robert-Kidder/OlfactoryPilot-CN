from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace

import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QKeyEvent, QMouseEvent
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
    InfoBarIcon,
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
    ManualPresentationSnapshot,
    ManualSupplyIntent,
)
from app.views.notification_coordinator import NotificationCoordinator
from app.views.port_formatting import (
    format_port_number,
    format_port_text,
    normalize_port_alias,
)
from app.views.product_text import user_facing_text
from app.views.product_theme import COLORS

FLOW_STEP_ML_MIN = 500.0
DURATION_STEP_S = 5.0
DURATION_MIN_S = 1.0
AMBER = COLORS.amber
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
        self._configuration_mode = False
        self._visual_state: tuple[str, bool, bool, str, bool, bool] | None = None
        self.visual_mutation_count = 0
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
        layout.addWidget(self.title_label)

        self.port_label = CaptionLabel(self)
        self.port_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.port_label.setStyleSheet(f"color: {COLORS.secondary};")
        self.port_label.installEventFilter(
            ToolTipFilter(self.port_label, showDelay=300, position=ToolTipPosition.TOP)
        )
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
        if self._configuration_mode and not self._available:
            return QColor(255, 255, 255, 9)
        if self._fault:
            return QColor(255, 145, 138, 38)
        if self._actually_open:
            return QColor(119, 201, 157, 38)
        if not self.isEnabled():
            return QColor(255, 255, 255, 5)
        if self._selected:
            return QColor(226, 173, 80, 42)
        return super()._normalBackgroundColor()

    def _hoverBackgroundColor(self) -> QColor:
        if self._fault:
            return QColor(255, 145, 138, 52)
        if self._actually_open:
            return QColor(119, 201, 157, 52)
        if self._selected:
            return QColor(226, 173, 80, 58)
        return super()._hoverBackgroundColor()

    def _pressedBackgroundColor(self) -> QColor:
        if self._fault:
            return QColor(255, 145, 138, 28)
        if self._actually_open:
            return QColor(119, 201, 157, 28)
        if self._selected:
            return QColor(226, 173, 80, 28)
        return super()._pressedBackgroundColor()

    @property
    def alias(self) -> str:
        return self._alias

    def set_port_content(self, display_name: str) -> None:
        alias = normalize_port_alias(display_name, self.external_port)
        if alias == self._alias and self.title_label.text():
            return
        self._visual_state = None
        self._alias = alias
        number = format_port_number(self.external_port)
        self.title_label.setText(number)
        self.port_label.setText(alias)
        self.port_label.setVisible(bool(alias))
        self._refresh_elision()

    def elided_alias(self, width: int | None = None) -> str:
        if not self._alias:
            return ""
        available = max(20, width if width is not None else self.title_label.width())
        return format_port_text(
            self.external_port,
            self._alias,
            font=self.port_label.font(),
            width=available,
        ).elided_alias

    def _refresh_elision(self) -> None:
        number = format_port_number(self.external_port)
        self.title_label.setText(number)
        if not self._alias:
            self.port_label.setText("")
            self.title_label.setToolTip("")
            self.port_label.setToolTip("")
            return
        available = max(20, self.width() - 18)
        formatted = format_port_text(
            self.external_port,
            self._alias,
            font=self.port_label.font(),
            width=available,
        )
        self.port_label.setText(formatted.elided_alias)
        self.port_label.setToolTip(formatted.tooltip)
        self.title_label.setToolTip(formatted.tooltip)

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
        self._configuration_mode = False
        normalized_name = _normalize_alias(display_name)
        normalized_fault = user_facing_text(fault)
        visual_state = (
            normalized_name,
            bool(selected and available),
            bool(actually_open),
            normalized_fault,
            bool(available),
            bool(interactive),
        )
        if visual_state == self._visual_state:
            return
        self.visual_mutation_count += 1
        self._selected = visual_state[1]
        self._actually_open = visual_state[2]
        self._fault = visual_state[3]
        self._available = visual_state[4]
        self.set_port_content(normalized_name)
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
        self._visual_state = (
            normalized_name,
            self._selected,
            self._actually_open,
            normalized_fault,
            self._available,
            bool(interactive),
        )

    def set_configuration_state(
        self, *, display_name: str, configured: bool, selected: bool
    ) -> None:
        """Render Settings availability while keeping every panel port editable."""

        self._configuration_mode = True
        self._available = bool(configured)
        self._selected = bool(selected)
        self.set_port_content(display_name)
        self.selection_accent.setVisible(self._selected)
        self.open_group.hide()
        self.fault_group.hide()
        self.setEnabled(True)
        self.setClickEnabled(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setProperty(
            "portState",
            "selected" if self._selected else ("available" if configured else "disabled"),
        )
        self._updateBackgroundColor()
        self.update()

    def isChecked(self) -> bool:  # noqa: N802 - Qt-style compatibility
        return self._selected

    def setChecked(self, selected: bool) -> None:  # noqa: N802 - Qt-style compatibility
        selected = bool(selected)
        if self._selected == selected:
            return
        self._visual_state = None
        self._selected = selected
        self.selection_accent.setVisible(self._selected)
        self._updateBackgroundColor()
        self.update()

    def click(self) -> None:
        if self.isEnabled():
            self.clicked.emit()

    def text(self) -> str:
        number = format_port_number(self.external_port)
        return f"{number}\n{self._alias}" if self._alias else number

    def toolTip(self) -> str:  # noqa: N802 - compatibility for tests/accessibility
        return self.port_label.toolTip()

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
    sample_a_sccm: float = 500.0
    main_b_sccm: float = 1000.0
    vacuum_c_sccm: float = 0.0
    duration_s: float = 5.0

    def __post_init__(self) -> None:
        ports = tuple(sorted(self.selected_external_ports))
        if len(set(ports)) != len(ports) or any(not 1 <= port <= 20 for port in ports):
            raise ValueError("气口必须是不重复的 1–20。")
        object.__setattr__(self, "selected_external_ports", ports)
        values = (
            (self.sample_a_sccm, "样品流量"),
            (self.main_b_sccm, "主气流"),
            (self.vacuum_c_sccm, "真空流量"),
            (self.duration_s, "持续时间"),
        )
        for value, label in values:
            if isinstance(value, bool) or not math.isfinite(float(value)):
                raise ValueError(f"{label}必须是有限数值。")
        if self.sample_a_sccm < 0 or self.main_b_sccm < 0 or self.vacuum_c_sccm < 0:
            raise ValueError("流量不得为负数。")
        if self.duration_s <= 0:
            raise ValueError("持续时间必须大于 0 秒。")

    @property
    def derived_total_sccm(self) -> float:
        return self.sample_a_sccm + self.main_b_sccm


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
    alias = normalize_port_alias(display_name, external_port)
    number = format_port_number(external_port)
    return f"{number}，{alias}" if alias else number


def port_button_text(port: ManualPortSnapshot) -> str:
    alias = normalize_port_alias(port.display_name, port.external_port)
    number = format_port_number(port.external_port)
    return f"{number}\n{alias}" if alias else number


class ManualExperimentView(QWidget):
    """Manual experiment product page driven by immutable controller snapshots."""

    draft_changed = Signal(object)
    supply_requested = Signal(object)
    release_requested = Signal(object)
    stop_requested = Signal(object)
    settings_requested = Signal()

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
        self._profile_revision = 0
        self._registry_signature: tuple[object, ...] | None = None
        self._allow_mock = False
        self._flow_history: deque[tuple[float, float]] = deque(maxlen=1000)
        self._last_telemetry_timestamp: float | None = None
        self._last_plot_payload: tuple[tuple[float, ...], tuple[float, ...]] | None = None
        self._last_rendered_snapshot: ManualExperimentViewSnapshot | None = None
        self._last_presentation_generation = -1
        self.port_tiles: dict[int, PortTile] = {}
        self.port_buttons = self.port_tiles
        self._notification_coordinator = NotificationCoordinator()
        self._notice_identity: tuple[object, ...] | None = None
        self._notice_order: int | None = None
        self._notice_severity: str | None = None
        self._notice_actionable: bool | None = None
        self._notice_timer = QTimer(self)
        self._notice_timer.setSingleShot(True)
        self._notice_timer.timeout.connect(self._expire_current_notice)
        self.notice_creation_count = 0
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

        self.notice_frame: QWidget | None = None
        self.status_label = BodyLabel("", self)
        self.detail_label = BodyLabel("", self)
        self.status_label.hide()
        self.detail_label.hide()

        self.sample_a_input.valueChanged.connect(self._on_draft_value_changed)
        self.main_b_input.valueChanged.connect(self._on_draft_value_changed)
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
        self.sample_a_input = self._flow_input()
        self.main_b_input = self._flow_input()
        self.vacuum_c_input = self._flow_input()
        self.derived_total_label = CaptionLabel("总流量 1500 ml/min", card)
        fields.addWidget(self._field("样品流量 A", self.sample_a_input), 0, 0)
        fields.addWidget(self._field("主气流 B", self.main_b_input), 0, 1)
        fields.addWidget(self._field("真空流量 C", self.vacuum_c_input), 0, 2)
        fields.addWidget(self.derived_total_label, 0, 3)
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
        header = QHBoxLayout()
        header.addWidget(StrongBodyLabel("气口", card))
        header.addStretch(1)
        self.port_settings_button = PushButton(FIF.SETTING, "气口设置", card)
        self.port_settings_button.clicked.connect(self.settings_requested)
        header.addWidget(self.port_settings_button)
        layout.addLayout(header)
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

    @property
    def profile_revision(self) -> int:
        return self._profile_revision

    def set_registry(
        self,
        registry: ChannelRegistry,
        allow_mock: bool,
        revision: int | None = None,
    ) -> None:
        if not isinstance(registry, ChannelRegistry):
            raise TypeError("手动实验需要有效的气口映射。")
        resolved_revision = self._profile_revision if revision is None else int(revision)
        signature = (registry.channels, bool(allow_mock), resolved_revision)
        if signature == self._registry_signature:
            return
        self._registry_signature = signature
        self._registry = registry
        self._profile_revision = resolved_revision
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
        if snapshot == self._last_rendered_snapshot:
            return
        self._snapshot = snapshot
        self._draft = snapshot.draft
        self._rendering = True
        try:
            self._set_range_if_changed(
                self.sample_a_input,
                0.0,
                snapshot.max_sample_a_sccm,
            )
            self._set_range_if_changed(
                self.main_b_input, 0.0, max(0.0, snapshot.max_total_sccm)
            )
            self._set_range_if_changed(
                self.vacuum_c_input, 0.0, max(0.0, snapshot.max_vacuum_c_sccm)
            )
            self._set_range_if_changed(
                self.duration_input,
                DURATION_MIN_S,
                max(DURATION_MIN_S, snapshot.max_duration_s),
            )
            self._set_value_if_changed(self.sample_a_input, snapshot.draft.sample_a_sccm)
            self._set_value_if_changed(self.main_b_input, snapshot.draft.main_b_sccm)
            self._set_value_if_changed(self.vacuum_c_input, snapshot.draft.vacuum_c_sccm)
            self._set_value_if_changed(self.duration_input, snapshot.draft.duration_s)
            self.derived_total_label.setText(
                f"总流量 {snapshot.draft.derived_total_sccm:g} ml/min"
            )
        finally:
            self._rendering = False

        for control in (
            self.sample_a_input,
            self.main_b_input,
            self.vacuum_c_input,
            self.duration_input,
        ):
            if control.isEnabled() != snapshot.controls_enabled:
                control.setEnabled(snapshot.controls_enabled)
        apply_enabled = snapshot.can_apply_flow and not snapshot.supply_transitioning
        if self.apply_flow_button.isEnabled() != apply_enabled:
            self.apply_flow_button.setEnabled(apply_enabled)
        apply_text = "开始供气" if snapshot.supply_enabled is False else "停止供气"
        if self.apply_flow_button.text() != apply_text:
            self.apply_flow_button.setText(apply_text)
        self._render_supply_badge(snapshot)
        if self.release_button.isEnabled() != snapshot.can_release:
            self.release_button.setEnabled(snapshot.can_release)
        if self.stop_button.isEnabled() != snapshot.can_stop:
            self.stop_button.setEnabled(snapshot.can_stop)

        status_text = snapshot.status_text
        detail_text = snapshot.detail_text
        if snapshot.experiment.status is ManualExperimentStatus.RECOVERY_REQUIRED:
            self.show_condition_notice(
                status_text or "需要立即处理",
                detail_text,
                source="manual-recovery",
                condition_key=("safety", "RECOVERY_REQUIRED"),
                severity="critical",
            )
        else:
            self.resolve_notice_condition(source="manual-recovery")
        if snapshot.experiment.status is ManualExperimentStatus.RECOVERY_REQUIRED:
            pass
        elif self._header_safety_state != "SAFE":
            # MainWindow already keeps the current abnormal state and next
            # action visible in the header. Snapshot rendering must not
            # recreate a dismissed transition-driven safety InfoBar.
            pass
        elif detail_text:
            severity = "error" if self._is_actionable_notice(status_text, detail_text) else "info"
            self.show_notice(
                status_text or "状态",
                detail_text,
                severity=severity,
                source="manual-status",
            )
        elif snapshot.experiment.status is ManualExperimentStatus.COMPLETED:
            completion_message = (
                "供气已开启。"
                if not snapshot.experiment.selected_external_ports
                else "所选气口已关闭，供气已恢复。"
            )
            self.show_notice(
                status_text or "实验已完成",
                completion_message,
                severity="success",
                notice_key=("manual-completed", snapshot.experiment.identity),
                source="manual-status",
            )
        else:
            self.clear_notice_event(source="manual-status")
        telemetry_text = (
            "暂无数据"
            if snapshot.telemetry_a_sccm is None
            else f"{snapshot.telemetry_a_sccm:.0f}"
        )
        if self.telemetry_a_label.text() != telemetry_text:
            self.telemetry_a_label.setText(telemetry_text)
        self._render_ports()
        self.refresh_countdown_display()
        self._last_rendered_snapshot = snapshot

    def render_presentation(self, presentation: ManualPresentationSnapshot) -> None:
        """以单一 generation 原子更新页面，拒绝迟到旧帧。"""

        if not isinstance(presentation, ManualPresentationSnapshot):
            raise TypeError("手动实验页面只能显示有效 presentation。")
        if presentation.generation <= self._last_presentation_generation:
            return
        experiment = presentation.experiment
        ports = tuple(
            replace(
                port,
                actually_open=(
                    port.external_port in experiment.open_confirmed
                    and port.external_port not in experiment.close_confirmed
                ),
                fault=(
                    experiment.recovery_reason
                    if port.external_port in experiment.possibly_open
                    else ""
                ),
            )
            for port in self._snapshot.ports
        )
        snapshot = replace(
            self._snapshot,
            experiment=experiment,
            ports=ports,
            controls_enabled=presentation.controls_enabled,
            can_apply_flow=presentation.can_apply_flow,
            can_release=presentation.can_release,
            can_stop=presentation.can_stop,
            supply_enabled=experiment.supply_enabled,
            supply_transitioning=experiment.supply_transitioning,
            status_text=self._status_text(experiment),
            detail_text=presentation.detail_text,
        )
        if presentation.connected:
            self.update_a_observation(
                presentation.airflow,
                sampled_at_s=presentation.telemetry_timestamp,
            )
            snapshot = replace(snapshot, telemetry_a_sccm=self._snapshot.telemetry_a_sccm)
        else:
            snapshot = replace(snapshot, telemetry_a_sccm=None)
        self.render_snapshot(snapshot)
        self._last_presentation_generation = presentation.generation

    @staticmethod
    def _set_range_if_changed(control: DoubleSpinBox, minimum: float, maximum: float) -> None:
        if control.minimum() != minimum or control.maximum() != maximum:
            control.setRange(minimum, maximum)

    @staticmethod
    def _set_value_if_changed(control: DoubleSpinBox, value: float) -> None:
        if not math.isclose(control.value(), value, rel_tol=0.0, abs_tol=1e-9):
            control.setValue(value)

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
        if self.supply_state_badge.text() != text:
            self.supply_state_badge.setText(text)
        if self.supply_state_badge.level != level:
            self.supply_state_badge.setLevel(level)
        if self.supply_state_label.text() != text:
            self.supply_state_label.setText(text)

    def update_a_observation(
        self,
        value: float,
        *,
        sampled_at_s: float | None = None,
    ) -> None:
        sampled_at = (
            self._clock_ns() / 1_000_000_000
            if sampled_at_s is None
            else float(sampled_at_s)
        )
        if not math.isfinite(sampled_at):
            return
        previous_timestamp = self._last_telemetry_timestamp
        if previous_timestamp is not None and sampled_at < previous_timestamp:
            return
        if isinstance(value, bool) or not math.isfinite(float(value)):
            self._last_telemetry_timestamp = sampled_at
            self._snapshot = replace(self._snapshot, telemetry_a_sccm=None)
            if self.telemetry_a_label.text() != "暂无数据":
                self.telemetry_a_label.setText("暂无数据")
            return
        numeric = float(value)
        if previous_timestamp == sampled_at:
            if self._snapshot.telemetry_a_sccm == numeric:
                return
            self._snapshot = replace(self._snapshot, telemetry_a_sccm=numeric)
            text = f"{numeric:.0f}"
            if self.telemetry_a_label.text() != text:
                self.telemetry_a_label.setText(text)
            if self._flow_history and self._flow_history[-1][0] == sampled_at:
                self._flow_history[-1] = (sampled_at, numeric)
                self._refresh_plot()
            return
        self._last_telemetry_timestamp = sampled_at
        self._snapshot = replace(self._snapshot, telemetry_a_sccm=numeric)
        text = f"{numeric:.0f}"
        if self.telemetry_a_label.text() != text:
            self.telemetry_a_label.setText(text)
        self._flow_history.append((sampled_at, numeric))
        self._refresh_plot()

    def _refresh_plot(self) -> None:
        now_s = (
            self._flow_history[-1][0]
            if self._flow_history
            else self._clock_ns() / 1_000_000_000
        )
        cutoff_s = now_s - 30.0
        while self._flow_history and self._flow_history[0][0] < cutoff_s:
            self._flow_history.popleft()
        xs = [sampled_at - now_s for sampled_at, _value in self._flow_history]
        values = [value for _sampled_at, value in self._flow_history]
        payload = (tuple(xs), tuple(values))
        if payload != self._last_plot_payload:
            self._flow_curve.setData(xs, values)
            self._last_plot_payload = payload

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
        source: str = "view-event",
        actionable: bool | None = None,
    ) -> None:
        title = user_facing_text(title).strip()
        message = user_facing_text(message).strip()
        if not title and not message:
            self.clear_notice()
            return
        signature = (title, message, severity)
        effective_key = signature if notice_key is None else notice_key
        if actionable is None:
            actionable = self._is_actionable_notice(title, message)
        self._notification_coordinator.publish_event(
            source=source,
            key=effective_key,
            title=title,
            message=message,
            severity=severity,
            actionable=actionable,
        )
        self._sync_notice_output()

    def show_condition_notice(
        self,
        title: str,
        message: str,
        *,
        source: str,
        condition_key: object,
        severity: str = "error",
    ) -> None:
        self._notification_coordinator.publish_condition(
            source=source,
            key=condition_key,
            title=user_facing_text(title).strip(),
            message=user_facing_text(message).strip(),
            severity=severity,
        )
        self._sync_notice_output()

    def resolve_notice_condition(self, *, source: str) -> None:
        self._notification_coordinator.resolve_condition(source=source)
        self._sync_notice_output()

    def clear_notice_event(self, *, source: str = "view-event") -> None:
        self._notification_coordinator.clear_event(source=source)
        self._sync_notice_output()

    def _sync_notice_output(self) -> None:
        current = self._notification_coordinator.current
        if current is None:
            self._remove_notice_frame()
            return
        if (
            current.identity == self._notice_identity
            and self.notice_frame is not None
            and isValid(self.notice_frame)
            and self.notice_frame.isVisible()
        ):
            if self.status_label.text() != current.title:
                self.status_label.setText(current.title)
            if self.detail_label.text() != current.message:
                self.detail_label.setText(current.message)
            title_label = getattr(self.notice_frame, "titleLabel", None)
            if title_label is not None and title_label.text() != current.title:
                title_label.setText(current.title)
            content_label = getattr(self.notice_frame, "contentLabel", None)
            if content_label is not None and content_label.text() != current.message:
                content_label.setText(current.message)
            icon = {
                "error": InfoBarIcon.ERROR,
                "critical": InfoBarIcon.ERROR,
                "success": InfoBarIcon.SUCCESS,
                "info": InfoBarIcon.INFORMATION,
                "warning": InfoBarIcon.WARNING,
            }.get(current.severity, InfoBarIcon.WARNING)
            if current.severity != self._notice_severity:
                self.notice_frame.icon = icon
                self.notice_frame.setProperty("type", icon.value)
                self.notice_frame.iconWidget.icon = icon
                self.notice_frame.iconWidget.update()
                self.notice_frame.style().unpolish(self.notice_frame)
                self.notice_frame.style().polish(self.notice_frame)
            meaningful_update = current.order != self._notice_order
            self.notice_frame.setProperty("managedDurationMs", current.duration_ms)
            if meaningful_update:
                self._notice_timer.stop()
                if current.duration_ms >= 0:
                    self._notice_timer.start(current.duration_ms)
            self._notice_order = current.order
            self._notice_severity = current.severity
            self._notice_actionable = current.actionable
            self.notice_frame.adjustSize()
            self._position_notice_frame()
            return
        old = self.notice_frame
        if old is not None and isValid(old):
            self._notice_timer.stop()
            self._stop_notice_animation(old)
            self.notice_frame = None
            old.close()
        self.status_label.setText(current.title)
        self.detail_label.setText(current.message)
        icon = {
            "error": InfoBarIcon.ERROR,
            "critical": InfoBarIcon.ERROR,
            "success": InfoBarIcon.SUCCESS,
            "info": InfoBarIcon.INFORMATION,
            "warning": InfoBarIcon.WARNING,
        }.get(current.severity, InfoBarIcon.WARNING)
        host = self.window()
        if not isinstance(host, QWidget):
            host = self
        bar = InfoBar(
            icon,
            current.title,
            current.message,
            isClosable=True,
            # QFluentWidgets uses an unowned singleShot + opacity animation
            # internally for duration.  Replacing/tearing down managed bars
            # can leave that animation targeting an invalid QObject.  The
            # view-owned timer below preserves the policy without that race.
            duration=-1,
            # Only one coordinated notice is visible at a time, so a local
            # overlay does not need QFluentWidgets' process-wide stacking
            # manager.  Avoiding that manager also prevents its animations
            # from outliving a replaced or destroyed Manual view.
            position=InfoBarPosition.NONE,
            parent=host,
        )
        bar.setProperty("managedDurationMs", current.duration_ms)
        bar.closedSignal.connect(
            lambda: self._dismiss_notice(current.identity, bar)
        )
        bar.setObjectName("manualExperimentInfoBar")
        self.notice_frame = bar
        self._notice_identity = current.identity
        self._notice_order = current.order
        self._notice_severity = current.severity
        self._notice_actionable = current.actionable
        self.notice_creation_count += 1
        bar.show()
        self._position_notice_frame()
        bar.raise_()
        if current.duration_ms >= 0:
            self._notice_timer.start(current.duration_ms)

    def _position_notice_frame(self) -> None:
        bar = self.notice_frame
        if bar is None or not isValid(bar):
            return
        bar.adjustSize()
        margin = 24
        host = bar.parentWidget() or self
        bar.move(
            max(margin, host.width() - bar.width() - margin),
            max(margin, host.height() - bar.height() - margin),
        )

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt virtual method name
        super().resizeEvent(event)
        self._position_notice_frame()

    def _expire_current_notice(self) -> None:
        bar = self.notice_frame
        if bar is not None and isValid(bar) and self._notice_actionable is False:
            bar.close()

    @staticmethod
    def _stop_notice_animation(bar: InfoBar) -> None:
        """Stop the bar-owned animation before its target is destroyed."""

        bar.opacityAni.stop()

    def _dismiss_notice(
        self,
        identity: tuple[object, ...],
        bar: InfoBar,
    ) -> None:
        if self.notice_frame is not bar:
            return
        actionable = self._notice_actionable is True
        self._notice_timer.stop()
        self._stop_notice_animation(bar)
        self.notice_frame = None
        self._notice_identity = None
        self._notice_order = None
        self._notice_severity = None
        self._notice_actionable = None
        self.status_label.setText("")
        self.detail_label.setText("")
        if actionable:
            self._notification_coordinator.dismiss(identity)
        else:
            self._notification_coordinator.retire_event(identity)
        self._sync_notice_output()

    def _remove_notice_frame(self) -> None:
        self._notice_timer.stop()
        old = self.notice_frame
        self.notice_frame = None
        if old is not None and isValid(old):
            self._stop_notice_animation(old)
            old.close()
        self._notice_identity = None
        self._notice_order = None
        self._notice_severity = None
        self._notice_actionable = None
        self.status_label.setText("")
        self.detail_label.setText("")

    def clear_notice(self) -> None:
        self._notification_coordinator.clear()
        self._remove_notice_frame()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt virtual method name
        """Release managed notice state before Qt destroys animation targets."""

        self.clear_notice()
        for bar in self.findChildren(InfoBar):
            if isValid(bar):
                self._stop_notice_animation(bar)
                bar.close()
                bar.deleteLater()
        super().closeEvent(event)

    def clear_safety_notice(self) -> None:
        """恢复 SAFE 时只清除 safety transition，不覆盖其他操作通知。"""

        self.resolve_notice_condition(source="safety")

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
            text = f"剩余 {remaining_ns / 1_000_000_000:.1f} 秒"
        elif experiment.status is ManualExperimentStatus.COMPLETED:
            text = "本次已完成"
        elif experiment.status is ManualExperimentStatus.RECOVERY_REQUIRED:
            text = "需要恢复"
        else:
            text = "剩余 --"
        if self.countdown_label.text() != text:
            self.countdown_label.setText(text)

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
                sample_a_sccm=self._draft.sample_a_sccm,
                main_b_sccm=self._draft.main_b_sccm,
                vacuum_c_sccm=self._draft.vacuum_c_sccm,
            )
        )

    def _request_release(self) -> None:
        self.release_requested.emit(
            ManualExperimentIntent(
                external_ports=self._draft.selected_external_ports,
                sample_a_sccm=self._draft.sample_a_sccm,
                main_b_sccm=self._draft.main_b_sccm,
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

    def _on_draft_value_changed(self, *_args) -> None:
        if self._rendering:
            return
        self._draft = replace(
            self._draft,
            sample_a_sccm=self.sample_a_input.value(),
            main_b_sccm=self.main_b_input.value(),
            vacuum_c_sccm=self.vacuum_c_input.value(),
            duration_s=self.duration_input.value(),
        )
        self._snapshot = replace(self._snapshot, draft=self._draft)
        self.derived_total_label.setText(
            f"总流量 {self._draft.derived_total_sccm:g} ml/min"
        )
        self.draft_changed.emit(ManualDraftChangedIntent(self._draft))
