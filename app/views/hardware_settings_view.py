from __future__ import annotations

import re
from dataclasses import dataclass, replace

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics, QKeyEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QSizePolicy,
    QStackedWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    ComboBox,
    HeaderCardWidget,
    IconInfoBadge,
    IndeterminateProgressBar,
    InfoBadge,
    InfoLevel,
    LineEdit,
    MessageBox,
    Pivot,
    PrimaryPushButton,
    PushButton,
    ScrollArea,
    StrongBodyLabel,
    SwitchButton,
    TableWidget,
    ToolTipFilter,
    ToolTipPosition,
)
from qfluentwidgets import FluentIcon as FIF

from app.models import (
    ChannelDescriptor,
    ChannelVerification,
    HardwareConnectionConfig,
    HardwareProfile,
    SelectorConfig,
    ValveTargetPreset,
    VerificationStatus,
)
from app.views.product_text import user_facing_text

AMBER = "#E2AD50"


@dataclass(frozen=True, slots=True)
class HardwareChannelDraft:
    external_port: int
    display_name: str
    internal_valve: int | None
    target: str
    active_high: bool
    enabled: bool
    verification: ChannelVerification
    mapping_changed: bool = False

    @classmethod
    def from_descriptor(cls, descriptor: ChannelDescriptor) -> HardwareChannelDraft:
        return cls(
            external_port=descriptor.external_port,
            display_name=descriptor.display_name,
            internal_valve=descriptor.internal_valve,
            target=descriptor.target,
            active_high=descriptor.active_high,
            enabled=descriptor.enabled,
            verification=descriptor.verification,
        )


@dataclass(frozen=True, slots=True)
class HardwareProfileDraft:
    profile_name: str
    channels: tuple[HardwareChannelDraft, ...]
    selector: SelectorConfig | None
    max_total_sccm: float
    max_sample_a_sccm: float
    max_vacuum_c_sccm: float
    serial_port: str = ""
    ni_device_ids_text: str = "Dev1, Dev2"
    alicat_a_unit_id: str = "a"
    alicat_b_unit_id: str = "b"
    alicat_c_unit_id: str = "c"
    revision: int = 0
    target_preset: ValveTargetPreset | None = None

    @classmethod
    def from_profile(
        cls,
        profile: HardwareProfile,
        *,
        revision: int = 0,
    ) -> HardwareProfileDraft:
        return cls(
            profile_name=profile.profile_name,
            channels=tuple(HardwareChannelDraft.from_descriptor(channel) for channel in profile.channels),
            selector=profile.selector,
            max_total_sccm=profile.max_total_sccm,
            max_sample_a_sccm=profile.max_sample_a_sccm,
            max_vacuum_c_sccm=profile.max_vacuum_c_sccm,
            serial_port=profile.connections.serial_port or "",
            ni_device_ids_text=", ".join(profile.connections.ni_device_ids),
            alicat_a_unit_id=profile.connections.alicat_a_unit_id,
            alicat_b_unit_id=profile.connections.alicat_b_unit_id,
            alicat_c_unit_id=profile.connections.alicat_c_unit_id,
            revision=revision,
            target_preset=profile.target_preset,
        )

    def to_profile(self) -> HardwareProfile:
        return HardwareProfile(
            schema_version=1,
            profile_name=self.profile_name,
            channels=tuple(
                ChannelDescriptor(
                    external_port=channel.external_port,
                    internal_valve=channel.internal_valve,
                    target=channel.target,
                    active_high=channel.active_high,
                    enabled=channel.enabled,
                    display_name=channel.display_name,
                    verification=channel.verification,
                )
                for channel in self.channels
            ),
            selector=self.selector,
            connections=HardwareConnectionConfig(
                serial_port=self.serial_port,
                ni_device_ids=tuple(value.strip() for value in self.ni_device_ids_text.split(",") if value.strip()),
                alicat_a_unit_id=self.alicat_a_unit_id,
                alicat_b_unit_id=self.alicat_b_unit_id,
                alicat_c_unit_id=self.alicat_c_unit_id,
            ),
            max_total_sccm=self.max_total_sccm,
            max_sample_a_sccm=self.max_sample_a_sccm,
            max_vacuum_c_sccm=self.max_vacuum_c_sccm,
            target_preset=self.target_preset,
        )


@dataclass(frozen=True, slots=True)
class HardwareSettingsSnapshot:
    profile: HardwareProfile
    revision: int = 0
    can_edit: bool = False
    can_save: bool = False
    can_mock_verify: bool = False
    can_request_physical_verification: bool = False
    rollback_available: bool = False
    save_in_progress: bool = False
    verification_in_progress: bool = False
    verification_port: int | None = None
    simulation_verification_duration_s: float = 20.0
    status_text: str = "设置当前为只读。"
    detail_text: str = "断开设备后可以修改并保存。"

    def __post_init__(self) -> None:
        if self.verification_in_progress and (
            self.verification_port is None
            or not 1 <= self.verification_port <= 20
        ):
            raise ValueError("验证进行中必须指定 1–20 的气口编号。")


def settings_port_text(channel: HardwareChannelDraft) -> str:
    alias = channel.display_name.strip()
    if alias == f"气口 {channel.external_port}":
        alias = ""
    number = f"气口 {channel.external_port:02d}"
    return f"{alias}\n{number}" if alias else number


class ValveComboBox(ComboBox):
    """Fluent selector with the legacy numeric API used by view callers."""

    valueChanged = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.addItem("未配置", userData=0)
        for valve in range(1, 21):
            self.addItem(f"控制通道 {valve:02d}", userData=valve)
        self.currentIndexChanged.connect(lambda _index: self.valueChanged.emit(self.value()))

    def value(self) -> int:
        return int(self.currentData() or 0)

    def setValue(self, value: int) -> None:  # noqa: N802 - Qt numeric-control compatibility
        index = self.findData(int(value))
        self.setCurrentIndex(max(0, index))


class SettingsPortTile(CardWidget):
    """设置页专用气口卡片，选择态与验证态相互独立。"""

    def __init__(self, external_port: int, parent=None) -> None:
        self.external_port = int(external_port)
        self._alias = ""
        self._selected = False
        self._state = "unused"
        super().__init__(parent)
        self.setClickEnabled(True)
        self.setFixedHeight(82)
        self.setMinimumWidth(72)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(7, 5, 7, 5)
        layout.setSpacing(2)
        self.selection_accent = QFrame(self)
        self.selection_accent.setFixedHeight(3)
        self.selection_accent.setStyleSheet(
            f"background: {AMBER}; border-radius: 1px;"
        )
        self.selection_accent.hide()
        layout.addWidget(self.selection_accent)
        self.title_label = StrongBodyLabel(self)
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed
        )
        self.title_label.installEventFilter(
            ToolTipFilter(
                self.title_label, showDelay=300, position=ToolTipPosition.TOP
            )
        )
        layout.addWidget(self.title_label)
        self.port_label = CaptionLabel(self)
        self.port_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.port_label.setStyleSheet("color: #858F8B;")
        layout.addWidget(self.port_label)
        self.state_groups = {
            "unused": self._state_group(FIF.REMOVE, "未使用", "#858F8B", "info"),
            "pending": self._state_group(FIF.SYNC, "待验证", AMBER, "warning"),
            "physical": self._state_group(FIF.ACCEPT, "可用", "#77C99D", "success"),
            "failed": self._state_group(FIF.CANCEL, "异常", "#FF918A", "error"),
        }
        for group in self.state_groups.values():
            layout.addWidget(group, 0, Qt.AlignmentFlag.AlignHCenter)
            group.hide()
        self.set_state(display_name="", state="unused", selected=False)

    @staticmethod
    def _state_group(icon, text: str, color: str, level: str) -> QWidget:
        group = QWidget()
        row = QHBoxLayout(group)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2)
        badge = getattr(IconInfoBadge, level)(icon, parent=group)
        badge.setFixedSize(16, 16)
        label = CaptionLabel(text, group)
        label.setStyleSheet(f"color: {color};")
        row.addWidget(badge)
        row.addWidget(label)
        return group

    @property
    def alias(self) -> str:
        return self._alias

    @property
    def status_text(self) -> str:
        return {
            "unused": "未使用",
            "pending": "待验证",
            "physical": "可用",
            "failed": "异常",
        }[self._state]

    def set_state(self, *, display_name: str, state: str, selected: bool) -> None:
        if state not in self.state_groups:
            raise ValueError(f"未知气口状态：{state}")
        alias = " ".join(str(display_name or "").split())
        if alias in {f"气口 {self.external_port}", f"气口 {self.external_port:02d}"}:
            alias = ""
        self._alias = alias
        self._state = state
        self._selected = bool(selected)
        self.selection_accent.setVisible(self._selected)
        for name, group in self.state_groups.items():
            group.setVisible(name == state)
        self.setProperty("portState", state)
        self.setAccessibleDescription(
            f"{self.status_text}；{'已选择' if self._selected else '未选择'}"
        )
        self._refresh_elision()
        self._updateBackgroundColor()

    def _normalBackgroundColor(self) -> QColor:
        if self._selected:
            return QColor(226, 173, 80, 42)
        if self._state == "unused":
            return QColor(255, 255, 255, 9)
        return super()._normalBackgroundColor()

    def _hoverBackgroundColor(self) -> QColor:
        if self._selected:
            return QColor(226, 173, 80, 58)
        return super()._hoverBackgroundColor()

    def _pressedBackgroundColor(self) -> QColor:
        if self._selected:
            return QColor(226, 173, 80, 28)
        return super()._pressedBackgroundColor()

    def _refresh_elision(self) -> None:
        number = f"气口 {self.external_port:02d}"
        self.setAccessibleName(f"{self._alias} {number}".strip())
        self.port_label.setText(number)
        self.port_label.setVisible(bool(self._alias))
        if not self._alias:
            self.title_label.setText(number)
            self.title_label.setToolTip("")
            return
        elided = QFontMetrics(self.title_label.font()).elidedText(
            self._alias,
            Qt.TextElideMode.ElideRight,
            max(20, self.width() - 18),
        )
        self.title_label.setText(elided)
        self.title_label.setToolTip(
            f"{self._alias}\n{number}" if elided != self._alias else ""
        )

    def text(self) -> str:
        number = f"气口 {self.external_port:02d}"
        return f"{self._alias}\n{number}" if self._alias else number

    def isChecked(self) -> bool:  # noqa: N802 - Qt-style compatibility
        return self._selected

    def click(self) -> None:
        if self.isEnabled():
            self.clicked.emit()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt override
        if event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space}:
            if self.isEnabled():
                self.clicked.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._refresh_elision()


class HardwareSettingsView(QWidget):
    """Two-row physical port overview with one-port-at-a-time editing."""

    candidate_changed = Signal(object)
    save_requested = Signal(object, int)
    rollback_requested = Signal(int)
    mock_verify_requested = Signal(int, object, int)
    physical_verify_requested = Signal(int)
    verification_stop_requested = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("hardwareSettings")
        self._rendering = False
        self._snapshot: HardwareSettingsSnapshot | None = None
        self._draft: HardwareProfileDraft | None = None
        self._base_can_mock_verify = False
        self._base_can_physical_verify = False
        self._selected_port = 1
        self.overview_buttons: dict[int, SettingsPortTile] = {}
        self.name_inputs: dict[int, LineEdit] = {}
        self.internal_inputs: dict[int, ValveComboBox] = {}
        self.current_channel_labels: dict[int, BodyLabel] = {}
        self.target_inputs: dict[int, BodyLabel] = {}
        self.polarity_inputs: dict[int, BodyLabel] = {}
        self.enabled_checks: dict[int, SwitchButton] = {}
        self.verification_labels: dict[int, BodyLabel] = {}
        self.verification_hints: dict[int, CaptionLabel] = {}
        self.custom_mapping_labels: dict[int, CaptionLabel] = {}
        self.preset_target_labels: dict[int, QTableWidgetItem] = {}
        self.mock_buttons: dict[int, PushButton] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 14)
        root.setSpacing(10)

        heading = StrongBodyLabel("设置")
        heading.setObjectName("settingsHeading")
        root.addWidget(heading)
        self.section_pivot = Pivot(self)
        self.section_stack = QStackedWidget(self)
        self.port_section = QWidget(self.section_stack)
        self.hardware_section = QWidget(self.section_stack)
        self.section_stack.addWidget(self.port_section)
        self.section_stack.addWidget(self.hardware_section)
        self.section_pivot.addItem(
            "ports",
            "气口配置",
            onClick=lambda: self.section_stack.setCurrentWidget(self.port_section),
        )
        self.section_pivot.addItem(
            "hardware",
            "线路与设备",
            onClick=lambda: self.section_stack.setCurrentWidget(self.hardware_section),
        )
        self.section_pivot.setCurrentItem("ports")
        root.addWidget(self.section_pivot)
        root.addWidget(self.section_stack, 1)

        port_section_layout = QVBoxLayout(self.port_section)
        port_section_layout.setContentsMargins(0, 0, 0, 0)
        self.settings_scroll = ScrollArea()
        self.settings_scroll.setObjectName("settingsScroll")
        self.settings_scroll.setWidgetResizable(True)
        self.settings_scroll.viewport().setStyleSheet("background: #1d2322;")
        self.settings_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_content = QWidget()
        scroll_content.setObjectName("settingsScrollContent")
        body = QVBoxLayout(scroll_content)
        body.setContentsMargins(2, 0, 6, 2)
        body.setSpacing(10)
        self.settings_scroll.setWidget(scroll_content)
        port_section_layout.addWidget(self.settings_scroll, 1)

        overview = HeaderCardWidget("气口总览", scroll_content)
        self.overview_card = overview
        overview.setMinimumHeight(190)
        overview.viewLayout.setContentsMargins(12, 10, 12, 12)
        self.overview_layout = QGridLayout()
        self.overview_layout.setHorizontalSpacing(6)
        self.overview_layout.setVerticalSpacing(8)
        for port in range(1, 21):
            button = SettingsPortTile(port)
            button.setObjectName("settingsPortButton")
            button.clicked.connect(lambda p=port: self.select_port(p))
            self.overview_buttons[port] = button
            self.overview_layout.addWidget(button, (port - 1) // 10, (port - 1) % 10)
        overview.viewLayout.addLayout(self.overview_layout)
        body.addWidget(overview)

        editor = HeaderCardWidget("气口 01", scroll_content)
        self.editor_card = editor
        editor.setMinimumHeight(230)
        editor.viewLayout.setContentsMargins(20, 12, 20, 16)
        editor_layout = QVBoxLayout()
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.setSpacing(8)
        self.editor_stack = QStackedWidget()
        self.advanced_stack = QStackedWidget()
        for port in range(1, 21):
            self.editor_stack.addWidget(self._build_port_editor(port))
            self.advanced_stack.addWidget(self._build_port_advanced_editor(port))
        editor_layout.addWidget(self.editor_stack)
        editor.viewLayout.addLayout(editor_layout)
        body.addWidget(editor)

        self.validation_label = BodyLabel("")
        self.validation_label.setObjectName("validationMessage")
        self.validation_label.setWordWrap(True)
        self.status_label = BodyLabel("", self)
        self.status_label.setObjectName("pageStatus")
        self.detail_label = CaptionLabel("", self)
        self.detail_label.setObjectName("pageDetail")
        self.detail_label.setWordWrap(True)
        self.validation_label.setVisible(False)
        self.status_label.setVisible(False)
        self.detail_label.setVisible(False)
        body.addWidget(self.validation_label)
        body.addStretch()

        actions = QHBoxLayout()
        self.save_button = PrimaryPushButton(FIF.SAVE, "保存设置", self)
        self.save_button.setObjectName("primaryButton")
        self.save_feedback_label = CaptionLabel("", self)
        self.save_feedback_label.setObjectName("saveFeedback")
        self.save_feedback_label.hide()
        self.rollback_button = PushButton("恢复上次设置", self)
        self.rollback_button.hide()
        self.close_button = PushButton("关闭", self)
        self.close_button.hide()
        self.verification_panel = QFrame(editor)
        self.verification_panel.setObjectName("verificationTaskPanel")
        verification_layout = QVBoxLayout(self.verification_panel)
        verification_layout.setContentsMargins(0, 6, 0, 0)
        verification_layout.setSpacing(8)
        self.verification_task_title = StrongBodyLabel("正在验证气口 01")
        self.verification_task_detail = BodyLabel("请确认气口 01 是否有气流")
        self.verification_remaining_label = CaptionLabel("剩余约 20 秒")
        verification_layout.addWidget(self.verification_task_title)
        verification_layout.addWidget(self.verification_task_detail)
        verification_layout.addWidget(self.verification_remaining_label)
        self.verification_progress = IndeterminateProgressBar(
            self.verification_panel, start=False
        )
        self.verification_progress.hide()
        self.verification_stop_button = PushButton(
            FIF.CANCEL, "立即停止", self.verification_panel
        )
        self.verification_stop_button.hide()
        verification_actions = QHBoxLayout()
        verification_actions.setContentsMargins(0, 0, 0, 0)
        verification_actions.addWidget(self.verification_progress, 1)
        verification_actions.addWidget(self.verification_stop_button)
        verification_layout.addLayout(verification_actions)
        self.verification_panel.hide()
        editor_layout.addWidget(self.verification_panel)
        actions.addWidget(self.save_button)
        actions.addWidget(self.save_feedback_label)
        actions.addWidget(self.rollback_button)
        actions.addStretch()
        actions.addWidget(self.close_button)
        port_section_layout.addLayout(actions)

        hardware_layout = QVBoxLayout(self.hardware_section)
        hardware_layout.setContentsMargins(0, 0, 0, 0)
        self.hardware_scroll = ScrollArea(self.hardware_section)
        self.hardware_scroll.setWidgetResizable(True)
        self.hardware_scroll.viewport().setStyleSheet("background: #1d2322;")
        self.hardware_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.advanced_panel = self._build_advanced_panel()
        self.hardware_scroll.setWidget(self.advanced_panel)
        hardware_layout.addWidget(self.hardware_scroll)

        self.save_button.clicked.connect(self._request_save)
        self.rollback_button.clicked.connect(self._request_rollback)
        self.close_button.clicked.connect(self._close_parent_dialog)
        self.verification_stop_button.clicked.connect(self._request_verification_stop)
        self.select_port(1)

    def _build_port_editor(self, port: int) -> QWidget:
        page = QWidget()
        layout = QGridLayout(page)
        layout.setContentsMargins(0, 2, 0, 0)
        layout.setHorizontalSpacing(10)
        layout.setVerticalSpacing(7)
        enabled = SwitchButton()
        enabled.setOnText("已启用")
        enabled.setOffText("未启用")
        name_input = LineEdit()
        name_input.setMaxLength(80)
        name_input.setPlaceholderText("例如：薄荷；留空时显示气口编号")
        internal_input = ValveComboBox()
        verification = InfoBadge.info("需要验证", parent=page)
        verification.setObjectName("verificationStatus")
        test_button = PushButton(FIF.PLAY, "验证气口", page)
        test_button.setObjectName("testPortButton")
        self.name_inputs[port] = name_input
        self.internal_inputs[port] = internal_input
        self.enabled_checks[port] = enabled
        self.verification_labels[port] = verification
        self.mock_buttons[port] = test_button
        hint = CaptionLabel("", page)
        hint.setObjectName("verificationHint")
        hint.setStyleSheet("color: #E2AD50;")
        hint.hide()
        self.verification_hints[port] = hint
        layout.addWidget(CaptionLabel("使用这个气口"), 0, 0)
        layout.addWidget(enabled, 0, 1)
        layout.addWidget(CaptionLabel("名称"), 1, 0)
        layout.addWidget(name_input, 1, 1)
        layout.addWidget(CaptionLabel("控制通道"), 2, 0)
        layout.addWidget(internal_input, 2, 1)
        layout.addWidget(CaptionLabel("验证状态"), 3, 0)
        status_row = QHBoxLayout()
        status_row.addWidget(verification, 1)
        status_row.addWidget(test_button)
        layout.addLayout(status_row, 3, 1)
        layout.addWidget(hint, 4, 1)
        name_input.textChanged.connect(lambda value, p=port: self._update_channel(p, display_name=value))
        internal_input.valueChanged.connect(
            lambda value, p=port: self._update_internal_valve(p, value or None)
        )
        enabled.checkedChanged.connect(
            lambda value, p=port: self._update_channel(p, enabled=value)
        )
        test_button.clicked.connect(lambda _checked=False, p=port: self._request_mock_verification(p))
        return page

    def _build_port_advanced_editor(self, port: int) -> QWidget:
        page = QWidget()
        layout = QGridLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(12)
        layout.setVerticalSpacing(9)
        layout.setColumnStretch(1, 1)
        channel_value = BodyLabel("未配置", page)
        target_value = BodyLabel("未配置", page)
        target_value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        polarity_value = BodyLabel("-", page)
        self.current_channel_labels[port] = channel_value
        self.target_inputs[port] = target_value
        self.polarity_inputs[port] = polarity_value
        layout.addWidget(CaptionLabel("控制通道"), 0, 0)
        layout.addWidget(channel_value, 0, 1)
        layout.addWidget(CaptionLabel("输出线路"), 1, 0)
        layout.addWidget(target_value, 1, 1)
        layout.addWidget(CaptionLabel("开启方式"), 2, 0)
        layout.addWidget(polarity_value, 2, 1)
        custom = CaptionLabel("", page)
        self.custom_mapping_labels[port] = custom
        layout.addWidget(custom, 3, 0, 1, 2)
        return page

    def _build_advanced_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("advancedPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 10, 12, 12)
        self.advanced_port_label = StrongBodyLabel("气口 01")
        layout.addWidget(self.advanced_port_label)
        layout.addWidget(self.advanced_stack)
        preset_title = StrongBodyLabel("控制通道表", panel)
        layout.addWidget(preset_title)
        self.preset_table = TableWidget(panel)
        self.preset_table.setColumnCount(2)
        self.preset_table.setRowCount(20)
        self.preset_table.setHorizontalHeaderLabels(("控制通道", "输出线路"))
        self.preset_table.verticalHeader().hide()
        self.preset_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.preset_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.preset_table.setMaximumHeight(520)
        self.preset_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.preset_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        for valve in range(1, 21):
            channel_item = QTableWidgetItem(f"{valve:02d}")
            target_item = QTableWidgetItem("未配置")
            self.preset_table.setItem(valve - 1, 0, channel_item)
            self.preset_table.setItem(valve - 1, 1, target_item)
            self.preset_target_labels[valve] = target_item
        layout.addWidget(self.preset_table)

        connection_title = StrongBodyLabel("设备连接", panel)
        layout.addWidget(connection_title)
        connection_row = QHBoxLayout()
        connection_row.setSpacing(12)
        left_card = QFrame(panel)
        left_card.setMaximumWidth(420)
        left_layout = QGridLayout(left_card)
        right_card = QFrame(panel)
        right_card.setMaximumWidth(420)
        right_layout = QGridLayout(right_card)
        self.connection_cards = (left_card, right_card)
        self.serial_port_input = BodyLabel("-", left_card)
        self.ni_device_ids_input = BodyLabel("-", left_card)
        self.alicat_unit_inputs: dict[str, BodyLabel] = {}
        left_layout.addWidget(CaptionLabel("串口连接"), 0, 0)
        left_layout.addWidget(self.serial_port_input, 0, 1)
        left_layout.addWidget(CaptionLabel("采集设备"), 1, 0)
        left_layout.addWidget(self.ni_device_ids_input, 1, 1)
        for row, channel in enumerate(("A", "B", "C")):
            value = BodyLabel("-", right_card)
            self.alicat_unit_inputs[channel] = value
            right_layout.addWidget(CaptionLabel(f"流量控制器 {channel}"), row, 0)
            right_layout.addWidget(value, row, 1)
        connection_row.addWidget(left_card)
        connection_row.addWidget(right_card)
        connection_row.addStretch()
        layout.addLayout(connection_row)
        layout.addStretch()
        return panel

    @property
    def draft(self) -> HardwareProfileDraft | None:
        return self._draft

    def select_port(self, external_port: int) -> None:
        port = max(1, min(20, int(external_port)))
        if (
            self._snapshot is not None
            and self._snapshot.verification_in_progress
            and self._snapshot.verification_port is not None
        ):
            port = self._snapshot.verification_port
        self._selected_port = port
        self.editor_stack.setCurrentIndex(port - 1)
        self.advanced_stack.setCurrentIndex(port - 1)
        verifying = (
            self._snapshot is not None
            and self._snapshot.verification_in_progress
            and self._snapshot.verification_port == port
        )
        self.editor_card.headerLabel.setText(
            f"正在验证气口 {port:02d}" if verifying else f"气口 {port:02d}"
        )
        self.advanced_port_label.setText(f"气口 {port:02d}")
        for number, button in self.overview_buttons.items():
            button.set_state(
                display_name=button.alias,
                state=str(button.property("portState") or "unused"),
                selected=number == port,
            )

    def render_snapshot(self, snapshot: HardwareSettingsSnapshot) -> None:
        if not isinstance(snapshot, HardwareSettingsSnapshot):
            raise TypeError("气口设置只能显示有效配置。")
        self._snapshot = snapshot
        self._base_can_mock_verify = snapshot.can_mock_verify
        self._base_can_physical_verify = snapshot.can_request_physical_verification
        self._draft = HardwareProfileDraft.from_profile(
            snapshot.profile,
            revision=snapshot.revision,
        )
        self._rendering = True
        try:
            self.serial_port_input.setText(self._draft.serial_port)
            self.ni_device_ids_input.setText(self._draft.ni_device_ids_text)
            self.alicat_unit_inputs["A"].setText(self._draft.alicat_a_unit_id)
            self.alicat_unit_inputs["B"].setText(self._draft.alicat_b_unit_id)
            self.alicat_unit_inputs["C"].setText(self._draft.alicat_c_unit_id)
            for channel in self._draft.channels:
                port = channel.external_port
                self.name_inputs[port].setText(channel.display_name)
                self.internal_inputs[port].setValue(channel.internal_valve or 0)
                self.current_channel_labels[port].setText(
                    "未配置"
                    if channel.internal_valve is None
                    else f"{channel.internal_valve:02d}"
                )
                self.target_inputs[port].setText(channel.target)
                self.polarity_inputs[port].setText(
                    "高电平开启" if channel.active_high else "低电平开启"
                )
                self.enabled_checks[port].setChecked(channel.enabled)
                self._render_verification_badge(port, channel)
                custom = snapshot.profile.channel_uses_custom_target(port)
                self.custom_mapping_labels[port].setText(
                    "已保留本机线路；修改控制通道后使用默认线路"
                    if custom
                    else "默认线路"
                )
                self._render_overview_button(channel)
            for valve, label in self.preset_target_labels.items():
                preset = snapshot.profile.target_preset
                label.setText("未配置" if preset is None else preset.target_for(valve))
        finally:
            self._rendering = False

        if snapshot.verification_port is not None:
            self.select_port(snapshot.verification_port)
        self.status_label.setText(user_facing_text(snapshot.status_text))
        self.detail_label.setText(user_facing_text(snapshot.detail_text))
        self._render_message_visibility(snapshot.status_text, snapshot.detail_text)
        self._apply_permissions(snapshot)
        self._validate_draft()

    def render_profile(
        self,
        profile: HardwareProfile,
        revision: int,
        can_save: bool,
        message: str = "",
        rollback_available: bool = False,
        can_mock_verify: bool | None = None,
        can_request_physical_verification: bool = False,
        verification_port: int | None = None,
        simulation_verification_duration_s: float = 20.0,
    ) -> None:
        self.render_snapshot(
            HardwareSettingsSnapshot(
                profile=profile,
                revision=revision,
                can_edit=can_save,
                can_save=can_save,
                can_mock_verify=(can_save if can_mock_verify is None else can_mock_verify),
                can_request_physical_verification=can_request_physical_verification,
                rollback_available=rollback_available,
                verification_in_progress=verification_port is not None,
                verification_port=verification_port,
                simulation_verification_duration_s=simulation_verification_duration_s,
                status_text=message or ("可以编辑并保存。" if can_save else "设置当前为只读。"),
                detail_text="",
            )
        )

    def render_permissions(
        self,
        *,
        can_save: bool,
        message: str = "",
        rollback_available: bool | None = None,
        can_mock_verify: bool | None = None,
        can_request_physical_verification: bool | None = None,
        verification_port: int | None = None,
        simulation_verification_duration_s: float | None = None,
    ) -> None:
        if self._snapshot is None:
            return
        draft_was_clean = False
        if self._draft is not None:
            try:
                draft_was_clean = (
                    self._draft.to_profile().to_dict()
                    == self._snapshot.profile.to_dict()
                )
            except ValueError:
                pass
        snapshot = replace(
            self._snapshot,
            can_edit=bool(can_save),
            can_save=bool(can_save),
            can_mock_verify=(
                bool(can_save) if can_mock_verify is None else bool(can_mock_verify)
            ),
            can_request_physical_verification=(
                self._snapshot.can_request_physical_verification
                if can_request_physical_verification is None
                else bool(can_request_physical_verification)
            ),
            rollback_available=(
                self._snapshot.rollback_available if rollback_available is None else bool(rollback_available)
            ),
            status_text=message or ("可以编辑并保存。" if can_save else "设置当前为只读。"),
            verification_in_progress=verification_port is not None,
            verification_port=verification_port,
            simulation_verification_duration_s=(
                self._snapshot.simulation_verification_duration_s
                if simulation_verification_duration_s is None
                else float(simulation_verification_duration_s)
            ),
        )
        self._snapshot = snapshot
        if draft_was_clean:
            self._base_can_mock_verify = snapshot.can_mock_verify
            self._base_can_physical_verify = snapshot.can_request_physical_verification
        self.status_label.setText(user_facing_text(snapshot.status_text))
        self.detail_label.setText(user_facing_text(snapshot.detail_text))
        self._render_message_visibility(snapshot.status_text, snapshot.detail_text)
        if snapshot.verification_port is not None:
            self.select_port(snapshot.verification_port)
        self._apply_permissions(snapshot)
        self._validate_draft()

    def _apply_permissions(self, snapshot: HardwareSettingsSnapshot) -> None:
        if snapshot.verification_in_progress:
            assert snapshot.verification_port is not None
            self.section_stack.setCurrentWidget(self.port_section)
            self.section_pivot.setCurrentItem("ports")
            self.select_port(snapshot.verification_port)
        self.section_pivot.setEnabled(not snapshot.verification_in_progress)
        editable = snapshot.can_edit and not snapshot.save_in_progress
        dirty = self._draft_is_dirty()
        for port in range(1, 21):
            self.overview_buttons[port].setEnabled(
                not snapshot.verification_in_progress
            )
            for control in (
                self.name_inputs[port],
                self.internal_inputs[port],
                self.enabled_checks[port],
            ):
                control.setEnabled(editable)
            channel = None if self._draft is None else self._draft.channels[port - 1]
            self.mock_buttons[port].setEnabled(
                (snapshot.can_mock_verify or snapshot.can_request_physical_verification)
                and not snapshot.save_in_progress
                and not snapshot.verification_in_progress
                and channel is not None
                and channel.enabled
                and channel.internal_valve is not None
            )
            self.mock_buttons[port].setText(
                "验证气口"
            )
            hint = ""
            if dirty:
                hint = "保存后验证"
            elif (
                port == self._selected_port
                and not snapshot.verification_in_progress
                and "验证未完成" in snapshot.status_text
            ):
                hint = "验证已停止"
            elif (
                port == self._selected_port
                and not snapshot.verification_in_progress
                and "现场验证未开放" in snapshot.status_text
            ):
                hint = "现场验证暂不可用"
            self.verification_hints[port].setText(hint)
            self.verification_hints[port].setVisible(bool(hint))
        self.save_button.setEnabled(snapshot.can_save and not snapshot.save_in_progress)
        self.rollback_button.setEnabled(
            snapshot.can_save and snapshot.rollback_available and not snapshot.save_in_progress
        )
        self.save_button.setText("正在保存…" if snapshot.save_in_progress else "保存设置")
        save_feedback = ""
        if not dirty and not snapshot.save_in_progress and not snapshot.verification_in_progress:
            if "已保存连接设置" in snapshot.status_text:
                save_feedback = "已保存，请重新启动"
            elif "保存成功" in snapshot.status_text:
                save_feedback = "设置已保存"
        self.save_feedback_label.setText(save_feedback)
        self.save_feedback_label.setVisible(bool(save_feedback))
        if snapshot.verification_in_progress:
            for port in range(1, 21):
                for control in (
                    self.name_inputs[port],
                    self.internal_inputs[port],
                    self.enabled_checks[port],
                ):
                    control.setEnabled(False)
            self.save_button.setEnabled(False)
        self.verification_progress.setVisible(snapshot.verification_in_progress)
        self.verification_stop_button.setVisible(snapshot.verification_in_progress)
        self.verification_panel.setVisible(snapshot.verification_in_progress)
        self.editor_stack.setVisible(not snapshot.verification_in_progress)
        if snapshot.verification_in_progress:
            port = snapshot.verification_port or self._selected_port
            self.editor_card.headerLabel.setText(f"正在验证气口 {port:02d}")
            self.verification_task_title.setText(f"正在验证气口 {port:02d}")
            self.verification_task_detail.setText(f"请确认气口 {port:02d} 是否有气流")
            match = re.search(r"剩余\s*(\d+)\s*秒", snapshot.status_text)
            remaining = (
                f"剩余 {match.group(1)} 秒"
                if match
                else f"剩余约 {snapshot.simulation_verification_duration_s:g} 秒"
            )
            self.verification_remaining_label.setText(remaining)
            self.verification_progress.start()
        else:
            self.editor_card.headerLabel.setText(f"气口 {self._selected_port:02d}")
            self.verification_progress.stop()

    def _render_overview_button(self, channel: HardwareChannelDraft) -> None:
        button = self.overview_buttons[channel.external_port]
        button.set_state(
            display_name=channel.display_name,
            state=self._channel_state(channel),
            selected=channel.external_port == self._selected_port,
        )
        button.setProperty("channelEnabled", channel.enabled)
        button.setAccessibleDescription(
            f"{'已启用' if channel.enabled else '未启用'}；"
            f"{self._verification_text(channel)}；"
            f"{'已选择' if button.isChecked() else '未选择'}"
        )
        button.style().unpolish(button)
        button.style().polish(button)
        button.update()

    def _draft_is_dirty(self) -> bool:
        if self._draft is None or self._snapshot is None:
            return False
        try:
            return self._draft.to_profile().to_dict() != self._snapshot.profile.to_dict()
        except ValueError:
            return True

    @staticmethod
    def _channel_state(channel: HardwareChannelDraft) -> str:
        if not channel.enabled:
            return "unused"
        if channel.mapping_changed:
            return "pending"
        if channel.verification.status is VerificationStatus.FAILED:
            return "failed"
        descriptor = ChannelDescriptor(
            external_port=channel.external_port,
            internal_valve=channel.internal_valve,
            target=channel.target,
            active_high=channel.active_high,
            enabled=channel.enabled,
            display_name=channel.display_name,
            verification=channel.verification,
        )
        return "physical" if descriptor.verification_valid_for() else "pending"

    def _update_channel(self, external_port: int, **changes) -> None:
        if self._rendering or self._draft is None:
            return
        channels = list(self._draft.channels)
        index = external_port - 1
        channels[index] = replace(channels[index], **changes)
        self._draft = replace(self._draft, channels=tuple(channels))
        self._render_verification_badge(external_port, channels[index])
        self._render_overview_button(channels[index])
        candidate = self._validate_draft()
        if self._snapshot is not None:
            clean = (
                candidate is not None
                and candidate.to_dict() == self._snapshot.profile.to_dict()
            )
            self._snapshot = replace(
                self._snapshot,
                can_mock_verify=self._base_can_mock_verify if clean else False,
                can_request_physical_verification=(
                    self._base_can_physical_verify if clean else False
                ),
            )
            self._apply_permissions(self._snapshot)
            if candidate is None:
                self.save_button.setEnabled(False)
        if candidate is not None:
            self.candidate_changed.emit(candidate)

    def _update_internal_valve(
        self, external_port: int, internal_valve: int | None
    ) -> None:
        if self._rendering or self._draft is None:
            return
        preset = self._draft.target_preset
        if preset is None:
            previous = self._draft.channels[external_port - 1].internal_valve or 0
            self._rendering = True
            try:
                self.internal_inputs[external_port].setValue(previous)
            finally:
                self._rendering = False
            self.validation_label.setProperty("valid", False)
            self.validation_label.setText(
                "配置冲突：缺少标准 20 通道映射，无法修改控制通道。"
            )
            self.validation_label.setVisible(True)
            self.save_button.setEnabled(False)
            return
        self._update_channel(
            external_port,
            internal_valve=internal_valve,
            target=preset.target_for(internal_valve),
            mapping_changed=True,
        )
        self.target_inputs[external_port].setText(preset.target_for(internal_valve))
        self.current_channel_labels[external_port].setText(
            "未配置" if internal_valve is None else f"{internal_valve:02d}"
        )
        self.custom_mapping_labels[external_port].setText("默认线路")

    def _validate_draft(self) -> HardwareProfile | None:
        if self._draft is None:
            return None
        try:
            candidate = self._draft.to_profile()
        except ValueError as exc:
            message = user_facing_text(exc)
            if "内部阀位不得重复映射" in message:
                owners: dict[int, int] = {}
                for channel in self._draft.channels:
                    valve = channel.internal_valve
                    if valve is None:
                        continue
                    owner = owners.get(valve)
                    if owner is not None:
                        message = (
                            f"控制通道 {valve:02d} 已被气口 {owner:02d} 使用。"
                        )
                        break
                    owners[valve] = channel.external_port
            self.validation_label.setProperty("valid", False)
            self.validation_label.setText(f"配置冲突：{message}")
            self.validation_label.setVisible(True)
            self.save_button.setEnabled(False)
            self.validation_label.style().unpolish(self.validation_label)
            self.validation_label.style().polish(self.validation_label)
            return None
        self.validation_label.setProperty("valid", True)
        self.validation_label.setText("")
        self.validation_label.setVisible(False)
        if self._snapshot is not None:
            self.save_button.setEnabled(
                self._snapshot.can_save
                and not self._snapshot.save_in_progress
                and not self._snapshot.verification_in_progress
            )
        self.validation_label.style().unpolish(self.validation_label)
        self.validation_label.style().polish(self.validation_label)
        return candidate

    def _render_message_visibility(self, status: str, detail: str) -> None:
        del status, detail
        self.status_label.hide()
        self.detail_label.hide()

    def _request_save(self) -> None:
        candidate = self._validate_draft()
        if candidate is not None and self._draft is not None:
            self.save_requested.emit(candidate, self._draft.revision)

    def _request_rollback(self) -> None:
        if self._snapshot is not None:
            self.rollback_requested.emit(self._snapshot.revision)

    def _request_mock_verification(self, external_port: int) -> None:
        candidate = self._validate_draft()
        if candidate is None:
            return
        snapshot = self._snapshot
        if snapshot is None:
            return
        if not (
            snapshot.can_mock_verify or snapshot.can_request_physical_verification
        ):
            return
        if self.isVisible():
            duration = snapshot.simulation_verification_duration_s
            duration_text = f"{duration:g} 秒"
            dialog = MessageBox(
                f"验证气口 {external_port:02d}",
                f"开始后，请确认气口 {external_port:02d} 是否出气。\n\n约 {duration_text}",
                self.window(),
            )
            dialog.yesButton.setText("开始验证")
            dialog.cancelButton.setText("取消")
            if not dialog.exec():
                return
        if snapshot.can_request_physical_verification:
            self.physical_verify_requested.emit(external_port)
            return
        self.mock_verify_requested.emit(external_port, candidate, self._draft.revision)

    def _request_verification_stop(self) -> None:
        if self._snapshot is not None and self._snapshot.verification_port is not None:
            self.verification_stop_requested.emit(self._snapshot.verification_port)

    def _close_parent_dialog(self) -> None:
        window = self.window()
        if window is not self:
            window.close()

    @staticmethod
    def _verification_text(channel: HardwareChannelDraft) -> str:
        return {
            "unused": "未使用",
            "pending": "待验证",
            "physical": "可用",
            "failed": "异常",
        }[HardwareSettingsView._channel_state(channel)]

    def _render_verification_badge(
        self, external_port: int, channel: HardwareChannelDraft
    ) -> None:
        badge = self.verification_labels[external_port]
        badge.setText(self._verification_text(channel))
        level = {
            "unused": InfoLevel.INFOAMTION,
            "pending": InfoLevel.WARNING,
            "physical": InfoLevel.SUCCESS,
            "failed": InfoLevel.ERROR,
        }[self._channel_state(channel)]
        badge.setLevel(level)
