from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, replace

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QKeyEvent
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
    BreadcrumbBar,
    CaptionLabel,
    CardWidget,
    ComboBox,
    HeaderCardWidget,
    IconInfoBadge,
    InfoBadge,
    InfoLevel,
    LineEdit,
    MessageBox,
    Pivot,
    PrimaryPushButton,
    ProgressBar,
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
    HardwareVerificationPhase,
    HardwareVerificationSnapshot,
    SelectorConfig,
    ValveTargetPreset,
    VerificationConfig,
    VerificationStatus,
)
from app.views.port_formatting import (
    format_port_number,
    format_port_text,
    normalize_port_alias,
)
from app.views.product_text import user_facing_text
from app.views.product_theme import COLORS, apply_page_palette, make_viewport_transparent
from app.views.spin_box_rules import (
    ProductNumericSpinBox,
    apply_user_flow_step,
    apply_user_seconds_step,
)

AMBER = COLORS.amber


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
    verification_config: VerificationConfig = VerificationConfig()

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
            verification_config=profile.verification_config,
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
            verification_config=self.verification_config,
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
    verification: HardwareVerificationSnapshot = HardwareVerificationSnapshot()
    status_text: str = "设置当前为只读。"
    detail_text: str = "断开设备后可以修改并保存。"

    @property
    def verification_in_progress(self) -> bool:
        return self.verification.active

    @property
    def verification_port(self) -> int | None:
        return self.verification.external_port

    @property
    def simulation_verification_duration_s(self) -> float:
        return self.verification.duration_s


def settings_port_text(channel: HardwareChannelDraft) -> str:
    alias = normalize_port_alias(channel.display_name, channel.external_port)
    number = format_port_number(channel.external_port)
    return f"{number}\n{alias}" if alias else number


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

    def set_occupied(self, owners: dict[int, int], *, own_port: int) -> None:
        current = self.value()
        for valve in range(1, 21):
            index = self.findData(valve)
            owner = owners.get(valve)
            occupied = owner is not None and owner != own_port and valve != current
            text = f"控制通道 {valve:02d}"
            if occupied:
                text += f"（已用于气口 {owner:02d}）"
            self.setItemText(index, text)
            self.setItemEnabled(index, not occupied)


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
            "unused": self._state_group(FIF.REMOVE, "未启用", COLORS.secondary, "info"),
            "pending": self._state_group(FIF.SYNC, "待验证", AMBER, "warning"),
            "mock": self._state_group(FIF.INFO, "待现场确认", COLORS.warning, "warning"),
            "physical": self._state_group(FIF.ACCEPT, "可用", COLORS.success, "success"),
            "failed": self._state_group(FIF.CANCEL, "需检查", COLORS.error, "error"),
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
            "unused": "未启用",
            "pending": "待验证",
            "mock": "待现场确认",
            "physical": "可用",
            "failed": "需检查",
        }[self._state]

    def set_state(self, *, display_name: str, state: str, selected: bool) -> None:
        if state not in self.state_groups:
            raise ValueError(f"未知气口状态：{state}")
        alias = normalize_port_alias(display_name, self.external_port)
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
        number = format_port_number(self.external_port)
        self.setAccessibleName(f"{number} {self._alias}".strip())
        self.title_label.setText(number)
        self.port_label.setVisible(bool(self._alias))
        if not self._alias:
            self.port_label.setText("")
            self.title_label.setToolTip("")
            return
        formatted = format_port_text(
            self.external_port,
            self._alias,
            font=self.port_label.font(),
            width=max(20, self.width() - 18),
        )
        self.port_label.setText(formatted.elided_alias)
        self.title_label.setToolTip(formatted.tooltip)

    def text(self) -> str:
        number = format_port_number(self.external_port)
        return f"{number}\n{self._alias}" if self._alias else number

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
    verification_result_requested = Signal(int, bool)

    def __init__(
        self,
        parent=None,
        *,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("hardwareSettings")
        apply_page_palette(self)
        self._rendering = False
        self._verification_config_values: dict[str, float] = {}
        self._invalid_verification_config_reason = ""
        self._snapshot: HardwareSettingsSnapshot | None = None
        self._draft: HardwareProfileDraft | None = None
        self._base_can_mock_verify = False
        self._base_can_physical_verify = False
        self._selected_port = 1
        self._monotonic_ns = monotonic_ns
        self._invalid_preset_targets: dict[int, str] = {}
        self.overview_buttons: dict[int, SettingsPortTile] = {}
        self.name_inputs: dict[int, LineEdit] = {}
        self.internal_inputs: dict[int, ValveComboBox] = {}
        self.current_channel_labels: dict[int, BodyLabel] = {}
        self.target_inputs: dict[int, BodyLabel] = {}
        self.polarity_inputs: dict[int, SwitchButton] = {}
        self.enabled_checks: dict[int, SwitchButton] = {}
        self.verification_labels: dict[int, BodyLabel] = {}
        self.verification_hints: dict[int, CaptionLabel] = {}
        self.custom_mapping_labels: dict[int, CaptionLabel] = {}
        self.preset_target_labels: dict[int, BodyLabel] = {}
        self.mock_buttons: dict[int, PushButton] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 14)
        root.setSpacing(10)

        heading = StrongBodyLabel("设置")
        heading.setObjectName("settingsHeading")
        root.addWidget(heading)
        self.breadcrumb = BreadcrumbBar(self)
        self.breadcrumb.addItem("home", "设置")
        self.breadcrumb.addItem("section", "气口配置")
        self.breadcrumb.currentItemChanged.connect(self._breadcrumb_changed)
        self.breadcrumb.hide()
        root.addWidget(self.breadcrumb)
        self.page_stack = QStackedWidget(self)
        self.settings_home = self._build_settings_home()
        self.section_stack = self.page_stack
        self.port_section = QWidget(self.page_stack)
        self.hardware_section = QWidget(self.page_stack)
        self.page_stack.addWidget(self.settings_home)
        self.page_stack.addWidget(self.port_section)
        self.page_stack.addWidget(self.hardware_section)
        self.section_pivot = Pivot(self)
        self.section_pivot.hide()
        root.addWidget(self.page_stack, 1)

        port_section_layout = QVBoxLayout(self.port_section)
        port_section_layout.setContentsMargins(0, 0, 0, 0)
        self.settings_scroll = ScrollArea()
        self.settings_scroll.setObjectName("settingsScroll")
        self.settings_scroll.setWidgetResizable(True)
        make_viewport_transparent(self.settings_scroll)
        make_viewport_transparent(self.settings_scroll.viewport())
        self.settings_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_content = QWidget()
        scroll_content.setObjectName("settingsScrollContent")
        make_viewport_transparent(scroll_content)
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

        verification_config_card = HeaderCardWidget("验证参数", scroll_content)
        verification_config_card.viewLayout.setContentsMargins(20, 10, 20, 14)
        verification_config_layout = QGridLayout()
        verification_config_layout.setHorizontalSpacing(12)
        verification_config_layout.setVerticalSpacing(8)
        self.verification_flow_input = ProductNumericSpinBox(verification_config_card)
        self.verification_flow_input.setRange(0.0, 1500.0)
        apply_user_flow_step(self.verification_flow_input)
        self.verification_flow_input.setSuffix(" ml/min")
        self.verification_flow_input.setMaximumWidth(260)
        self.verification_duration_input = ProductNumericSpinBox(
            verification_config_card
        )
        self.verification_duration_input.setRange(1.0, 60.0)
        apply_user_seconds_step(self.verification_duration_input)
        self.verification_duration_input.setSuffix(" 秒")
        self.verification_duration_input.setMaximumWidth(180)
        verification_config_layout.addWidget(CaptionLabel("验证流量"), 0, 0)
        verification_config_layout.addWidget(self.verification_flow_input, 0, 1)
        verification_config_layout.addWidget(CaptionLabel("最长验证时间"), 1, 0)
        verification_config_layout.addWidget(self.verification_duration_input, 1, 1)
        verification_config_card.viewLayout.addLayout(verification_config_layout)
        body.addWidget(verification_config_card)

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
        self._save_feedback_timer = QTimer(self)
        self._save_feedback_timer.setSingleShot(True)
        self._save_feedback_timer.timeout.connect(self.save_feedback_label.hide)
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
        self.verification_progress = ProgressBar(self.verification_panel)
        self.verification_progress.setRange(0, 100)
        self.verification_progress.setValue(0)
        self.verification_progress.hide()
        self.verification_stop_button = PushButton(
            FIF.CANCEL, "立即停止", self.verification_panel
        )
        self.verification_stop_button.hide()
        self.verification_negative_button = PushButton(
            FIF.CANCEL, "没有或位置不对", self.verification_panel
        )
        self.verification_positive_button = PrimaryPushButton(
            FIF.ACCEPT, "出气正确", self.verification_panel
        )
        self.verification_negative_button.hide()
        self.verification_positive_button.hide()
        verification_actions = QHBoxLayout()
        verification_actions.setContentsMargins(0, 0, 0, 0)
        verification_actions.addWidget(self.verification_progress, 1)
        verification_actions.addWidget(self.verification_stop_button)
        verification_actions.addWidget(self.verification_negative_button)
        verification_actions.addWidget(self.verification_positive_button)
        verification_layout.addLayout(verification_actions)
        self.verification_panel.hide()
        editor_layout.addWidget(self.verification_panel)
        actions.addWidget(self.save_button)
        actions.addWidget(self.save_feedback_label)
        actions.addWidget(self.rollback_button)
        actions.addStretch()
        actions.addWidget(self.close_button)
        self.port_actions = QWidget(self.port_section)
        self.port_actions.setLayout(actions)
        port_section_layout.addWidget(self.port_actions)

        hardware_layout = QVBoxLayout(self.hardware_section)
        hardware_layout.setContentsMargins(0, 0, 0, 0)
        self.hardware_scroll = ScrollArea(self.hardware_section)
        self.hardware_scroll.setWidgetResizable(True)
        make_viewport_transparent(self.hardware_scroll)
        make_viewport_transparent(self.hardware_scroll.viewport())
        self.hardware_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.advanced_panel = self._build_advanced_panel()
        self.hardware_scroll.setWidget(self.advanced_panel)
        hardware_layout.addWidget(self.hardware_scroll)

        self.port_actions.setParent(self)
        root.addWidget(self.port_actions)
        self.page_stack.currentChanged.connect(
            lambda _index: self.port_actions.setVisible(
                self.page_stack.currentWidget() is not self.settings_home
            )
        )

        self.save_button.clicked.connect(self._request_save)
        self.rollback_button.clicked.connect(self._request_rollback)
        self.close_button.clicked.connect(self._close_parent_dialog)
        self.verification_stop_button.clicked.connect(self._request_verification_stop)
        self.verification_negative_button.clicked.connect(
            lambda: self._request_verification_result(False)
        )
        self.verification_positive_button.clicked.connect(
            lambda: self._request_verification_result(True)
        )
        self.verification_flow_input.valueChanged.connect(
            lambda value: self._update_verification_config(flow_sccm=float(value))
        )
        self.verification_duration_input.valueChanged.connect(
            lambda value: self._update_verification_config(duration_s=float(value))
        )
        self.verification_flow_input.outOfRangeCommitAttempted.connect(
            lambda value: self._update_verification_config(flow_sccm=float(value))
        )
        self.verification_duration_input.outOfRangeCommitAttempted.connect(
            lambda value: self._update_verification_config(duration_s=float(value))
        )
        self.page_stack.setCurrentWidget(self.settings_home)
        self.port_actions.hide()
        self.select_port(1)

    def _build_settings_home(self) -> QWidget:
        page = QWidget(self)
        layout = QGridLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setHorizontalSpacing(16)
        layout.setVerticalSpacing(16)
        self.port_entry_card = CardWidget(page)
        self.port_entry_card.setClickEnabled(True)
        port_layout = QVBoxLayout(self.port_entry_card)
        port_layout.setContentsMargins(24, 22, 24, 22)
        port_layout.addWidget(StrongBodyLabel("气口配置", self.port_entry_card))
        port_detail = BodyLabel("设置 20 个面板气口的名称、通道与验证状态", self.port_entry_card)
        port_detail.setWordWrap(True)
        port_layout.addWidget(port_detail)
        port_layout.addStretch()
        port_layout.addWidget(CaptionLabel("进入气口配置  ›", self.port_entry_card))
        self.line_entry_card = CardWidget(page)
        self.line_entry_card.setClickEnabled(True)
        line_layout = QVBoxLayout(self.line_entry_card)
        line_layout.setContentsMargins(24, 22, 24, 22)
        line_layout.addWidget(StrongBodyLabel("线路与设备", self.line_entry_card))
        line_detail = BodyLabel("查看或编辑控制线路和本机连接参数", self.line_entry_card)
        line_detail.setWordWrap(True)
        line_layout.addWidget(line_detail)
        line_layout.addStretch()
        line_layout.addWidget(CaptionLabel("进入线路与设备  ›", self.line_entry_card))
        layout.addWidget(self.port_entry_card, 0, 0)
        layout.addWidget(self.line_entry_card, 0, 1)
        layout.setColumnStretch(0, 1)
        layout.setColumnStretch(1, 1)
        layout.setRowStretch(1, 1)
        self.port_entry_card.clicked.connect(self.open_port_settings)
        self.line_entry_card.clicked.connect(self.open_hardware_settings)
        return page

    def _breadcrumb_changed(self, route_key: str) -> None:
        if route_key == "home":
            self.open_home()

    def open_home(self) -> None:
        if self._snapshot is not None and self._snapshot.verification.active:
            return
        self.page_stack.setCurrentWidget(self.settings_home)
        self.breadcrumb.hide()

    def open_port_settings(self) -> None:
        self.page_stack.setCurrentWidget(self.port_section)
        self.breadcrumb.setItemText("section", "气口配置")
        self.breadcrumb.setCurrentItem("section")
        self.breadcrumb.show()

    def open_hardware_settings(self) -> None:
        if self._snapshot is not None and self._snapshot.verification.active:
            return
        self.page_stack.setCurrentWidget(self.hardware_section)
        self.breadcrumb.setItemText("section", "线路与设备")
        self.breadcrumb.setCurrentItem("section")
        self.breadcrumb.show()

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
        name_input.setMaximumWidth(420)
        name_input.setPlaceholderText("例如：薄荷；留空时显示气口编号")
        internal_input = ValveComboBox()
        internal_input.setMaximumWidth(280)
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
        self.current_channel_labels[port] = channel_value
        self.target_inputs[port] = target_value
        layout.addWidget(CaptionLabel("控制通道"), 0, 0)
        layout.addWidget(channel_value, 0, 1)
        layout.addWidget(CaptionLabel("输出线路"), 1, 0)
        layout.addWidget(target_value, 1, 1)
        polarity = SwitchButton(page)
        polarity.setOnText("高电平开启")
        polarity.setOffText("低电平开启")
        polarity.setEnabled(False)
        polarity.checkedChanged.connect(
            lambda value, p=port: self._update_channel(
                p, active_high=value, mapping_changed=True
            )
        )
        self.polarity_inputs[port] = polarity
        layout.addWidget(CaptionLabel("开启方式"), 2, 0)
        layout.addWidget(polarity, 2, 1)
        custom = CaptionLabel("", page)
        self.custom_mapping_labels[port] = custom
        layout.addWidget(custom, 3, 0, 1, 2)
        return page

    def _build_advanced_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("advancedPanel")
        make_viewport_transparent(panel)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 10, 12, 12)
        self.advanced_port_label = StrongBodyLabel("气口 01")
        layout.addWidget(self.advanced_port_label)
        layout.addWidget(self.advanced_stack)
        preset_header = QHBoxLayout()
        preset_title = StrongBodyLabel("控制线路", panel)
        self.edit_lines_button = PushButton(FIF.EDIT, "编辑线路", panel)
        self.edit_lines_button.setCheckable(True)
        self.edit_lines_button.toggled.connect(self._set_line_editing)
        preset_header.addWidget(preset_title)
        preset_header.addStretch()
        preset_header.addWidget(self.edit_lines_button)
        layout.addLayout(preset_header)
        self.preset_table = TableWidget(panel)
        self.preset_table.setColumnCount(4)
        self.preset_table.setRowCount(10)
        self.preset_table.setHorizontalHeaderLabels(
            ("控制通道 01–10", "输出线路", "控制通道 11–20", "输出线路")
        )
        self.preset_table.verticalHeader().hide()
        self.preset_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.preset_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.preset_table.setMaximumHeight(370)
        for column in (0, 2):
            self.preset_table.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents
            )
        for column in (1, 3):
            self.preset_table.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeMode.Stretch
            )
        self.preset_target_inputs: dict[int, LineEdit] = {}
        for valve in range(1, 21):
            channel_item = QTableWidgetItem(f"{valve:02d}")
            target_container = QWidget(self.preset_table)
            target_layout = QHBoxLayout(target_container)
            target_layout.setContentsMargins(4, 0, 4, 0)
            target_label = BodyLabel("未配置", target_container)
            target_label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            target_input = LineEdit(target_container)
            target_input.setMaximumWidth(280)
            target_input.hide()
            target_input.textChanged.connect(
                lambda value, v=valve: self._update_preset_target(v, value)
            )
            target_layout.addWidget(target_label)
            target_layout.addWidget(target_input)
            row = (valve - 1) % 10
            column = 0 if valve <= 10 else 2
            self.preset_table.setItem(row, column, channel_item)
            self.preset_table.setCellWidget(row, column + 1, target_container)
            self.preset_target_labels[valve] = target_label
            self.preset_target_inputs[valve] = target_input
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
        self.profile_name_input = LineEdit(left_card)
        self.profile_name_input.setMaximumWidth(420)
        self.serial_port_input = LineEdit(left_card)
        self.serial_port_input.setMaximumWidth(320)
        self.ni_device_ids_input = LineEdit(left_card)
        self.ni_device_ids_input.setMaximumWidth(420)
        self.alicat_unit_inputs: dict[str, LineEdit] = {}
        left_layout.addWidget(CaptionLabel("配置名称"), 0, 0)
        left_layout.addWidget(self.profile_name_input, 0, 1)
        left_layout.addWidget(CaptionLabel("串口连接"), 1, 0)
        left_layout.addWidget(self.serial_port_input, 1, 1)
        left_layout.addWidget(CaptionLabel("采集设备"), 2, 0)
        left_layout.addWidget(self.ni_device_ids_input, 2, 1)
        for row, channel in enumerate(("A", "B", "C")):
            value = LineEdit(right_card)
            value.setMaxLength(1)
            value.setMaximumWidth(280)
            self.alicat_unit_inputs[channel] = value
            right_layout.addWidget(CaptionLabel(f"流量控制器 {channel}"), row, 0)
            right_layout.addWidget(value, row, 1)
        connection_row.addWidget(left_card)
        connection_row.addWidget(right_card)
        connection_row.addStretch()
        layout.addLayout(connection_row)
        self.profile_name_input.textChanged.connect(
            lambda value: self._update_connections(profile_name=value)
        )
        self.serial_port_input.textChanged.connect(
            lambda value: self._update_connections(serial_port=value)
        )
        self.ni_device_ids_input.textChanged.connect(
            lambda value: self._update_connections(ni_device_ids_text=value)
        )
        for channel, value in self.alicat_unit_inputs.items():
            value.textChanged.connect(
                lambda text, c=channel: self._update_connections(
                    **{f"alicat_{c.lower()}_unit_id": text}
                )
            )
        layout.addStretch()
        return panel

    def _set_line_editing(self, enabled: bool) -> None:
        editable = bool(
            enabled
            and self._snapshot is not None
            and self._snapshot.can_edit
            and not self._snapshot.save_in_progress
            and not self._snapshot.verification.active
        )
        if enabled and not editable:
            self.edit_lines_button.setChecked(False)
        for valve, field in self.preset_target_inputs.items():
            field.setVisible(editable)
            self.preset_target_labels[valve].setVisible(not editable)
        self.edit_lines_button.setText("完成编辑" if editable else "编辑线路")
        for field in self.polarity_inputs.values():
            field.setEnabled(editable)

    def _update_verification_config(self, **changes) -> None:
        if self._rendering or self._draft is None:
            return
        if not self._verification_config_values:
            self._verification_config_values = self._draft.verification_config.to_dict()
        self._verification_config_values.update(changes)
        try:
            verification_config = VerificationConfig.from_value(
                self._verification_config_values
            )
        except ValueError as exc:
            self._invalid_verification_config_reason = str(exc)
            self.status_label.setText(f"验证参数无效：{exc}")
            self.save_button.setEnabled(False)
            for button in self.mock_buttons.values():
                button.setEnabled(False)
            return
        self._invalid_verification_config_reason = ""
        self._draft = replace(self._draft, verification_config=verification_config)
        candidate = self._validate_draft()
        if candidate is not None:
            self.candidate_changed.emit(candidate)
        if self._snapshot is not None:
            self._apply_permissions(self._snapshot)

    def _update_connections(self, **changes) -> None:
        if self._rendering or self._draft is None:
            return
        self._draft = replace(self._draft, **changes)
        candidate = self._validate_draft()
        if candidate is not None:
            self.candidate_changed.emit(candidate)
        if self._snapshot is not None:
            self._apply_permissions(self._snapshot)

    def _update_preset_target(self, internal_valve: int, target: str) -> None:
        if self._rendering or self._draft is None or self._draft.target_preset is None:
            return
        try:
            preset = self._draft.target_preset.with_target(internal_valve, target)
        except ValueError as exc:
            self._invalid_preset_targets[internal_valve] = user_facing_text(exc)
            self.validation_label.setProperty("valid", False)
            self.validation_label.setText(
                f"配置冲突：{self._invalid_preset_targets[internal_valve]}"
            )
            self.validation_label.show()
            self.save_button.setEnabled(False)
            return
        self._invalid_preset_targets.pop(internal_valve, None)
        channels = tuple(
            replace(
                channel,
                target=preset.target_for(internal_valve),
                mapping_changed=self._mapping_changed(
                    channel.external_port,
                    replace(channel, target=preset.target_for(internal_valve)),
                ),
            )
            if channel.internal_valve == internal_valve
            else channel
            for channel in self._draft.channels
        )
        self._draft = replace(self._draft, target_preset=preset, channels=channels)
        self.preset_target_labels[internal_valve].setText(
            preset.target_for(internal_valve)
        )
        for channel in channels:
            if channel.internal_valve == internal_valve:
                self.target_inputs[channel.external_port].setText(channel.target)
                self._render_verification_badge(channel.external_port, channel)
                self._render_overview_button(channel)
        candidate = self._validate_draft()
        if candidate is not None:
            self.candidate_changed.emit(candidate)

    def _refresh_occupied_options(self) -> None:
        if self._draft is None:
            return
        owners = {
            int(channel.internal_valve): channel.external_port
            for channel in self._draft.channels
            if channel.internal_valve is not None
        }
        for port, selector in self.internal_inputs.items():
            selector.set_occupied(owners, own_port=port)

    @property
    def draft(self) -> HardwareProfileDraft | None:
        return self._draft

    @property
    def snapshot(self) -> HardwareSettingsSnapshot | None:
        return self._snapshot

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
        self._invalid_preset_targets.clear()
        self._rendering = True
        try:
            self.profile_name_input.setText(self._draft.profile_name)
            self.serial_port_input.setText(self._draft.serial_port)
            self.ni_device_ids_input.setText(self._draft.ni_device_ids_text)
            self.alicat_unit_inputs["A"].setText(self._draft.alicat_a_unit_id)
            self.alicat_unit_inputs["B"].setText(self._draft.alicat_b_unit_id)
            self.alicat_unit_inputs["C"].setText(self._draft.alicat_c_unit_id)
            self._verification_config_values = (
                self._draft.verification_config.to_dict()
            )
            self._invalid_verification_config_reason = ""
            maximum_verification_flow = min(
                self._draft.verification_config.max_approved_flow_sccm,
                self._draft.max_sample_a_sccm,
            )
            self.verification_flow_input.setRange(
                0.0,
                maximum_verification_flow,
            )
            self.verification_duration_input.setRange(1.0, 60.0)
            self.verification_flow_input.setValue(
                self._draft.verification_config.flow_sccm
            )
            self.verification_duration_input.setValue(
                self._draft.verification_config.duration_s
            )
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
                self.polarity_inputs[port].setChecked(channel.active_high)
                self.enabled_checks[port].setChecked(channel.enabled)
                self._render_verification_badge(port, channel)
                custom = snapshot.profile.channel_uses_custom_target(port)
                self.custom_mapping_labels[port].setText(
                    "已保留本机线路；修改控制通道后使用默认线路"
                    if custom
                    else "默认线路"
                )
                self._render_overview_button(channel)
            for valve, field in self.preset_target_inputs.items():
                preset = snapshot.profile.target_preset
                target = "" if preset is None else preset.target_for(valve)
                field.setText(target)
                self.preset_target_labels[valve].setText(target or "未配置")
            self._refresh_occupied_options()
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
        verification: HardwareVerificationSnapshot | None = None,
    ) -> None:
        if verification is None:
            verification = self._legacy_verification_snapshot(
                profile,
                revision=revision,
                external_port=verification_port,
                duration_s=simulation_verification_duration_s,
            )
        self.render_snapshot(
            HardwareSettingsSnapshot(
                profile=profile,
                revision=revision,
                can_edit=can_save,
                can_save=can_save,
                can_mock_verify=(can_save if can_mock_verify is None else can_mock_verify),
                can_request_physical_verification=can_request_physical_verification,
                rollback_available=rollback_available,
                verification=verification,
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
        verification: HardwareVerificationSnapshot | None = None,
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
        if verification is None and verification_port is not None:
            verification = self._legacy_verification_snapshot(
                self._snapshot.profile,
                revision=self._snapshot.revision,
                external_port=verification_port,
                duration_s=(
                    self._snapshot.simulation_verification_duration_s
                    if simulation_verification_duration_s is None
                    else float(simulation_verification_duration_s)
                ),
            )
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
            verification=(
                self._snapshot.verification if verification is None else verification
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

    @staticmethod
    def _legacy_verification_snapshot(
        profile: HardwareProfile,
        *,
        revision: int,
        external_port: int | None,
        duration_s: float,
    ) -> HardwareVerificationSnapshot:
        if external_port is None:
            return HardwareVerificationSnapshot(duration_s=duration_s)
        channel = profile.registry.by_external_port(external_port)
        started = time.monotonic_ns()
        return HardwareVerificationSnapshot(
            phase=HardwareVerificationPhase.RUNNING,
            external_port=external_port,
            started_ns=started,
            deadline_ns=started + int(float(duration_s) * 1_000_000_000),
            duration_s=duration_s,
            can_stop=True,
            awaiting_user_confirmation=False,
            run_identity="legacy-view-state",
            revision=revision,
            fingerprint=channel.mapping_fingerprint or "0" * 64,
        )

    def _apply_permissions(self, snapshot: HardwareSettingsSnapshot) -> None:
        if snapshot.verification_in_progress:
            assert snapshot.verification_port is not None
            self.section_stack.setCurrentWidget(self.port_section)
            self.breadcrumb.setItemText("section", "气口配置")
            self.breadcrumb.show()
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
                and not self._invalid_verification_config_reason
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
        for control in (
            self.profile_name_input,
            self.serial_port_input,
            self.ni_device_ids_input,
            self.verification_flow_input,
            self.verification_duration_input,
            *self.alicat_unit_inputs.values(),
        ):
            control.setEnabled(editable)
        self.edit_lines_button.setEnabled(editable)
        self._set_line_editing(self.edit_lines_button.isChecked())
        self.save_button.setEnabled(
            snapshot.can_save
            and dirty
            and not snapshot.save_in_progress
            and not self._invalid_verification_config_reason
        )
        self.rollback_button.setEnabled(
            snapshot.can_save and snapshot.rollback_available and not snapshot.save_in_progress
        )
        self.save_button.setText("正在保存…" if snapshot.save_in_progress else "保存设置")
        save_feedback = ""
        if not dirty and not snapshot.save_in_progress and not snapshot.verification_in_progress:
            if "重启后生效" in snapshot.status_text:
                save_feedback = "设置已保存，重启后生效"
            elif "设置已保存" in snapshot.status_text:
                save_feedback = "设置已保存"
        self.save_feedback_label.setText(save_feedback)
        self.save_feedback_label.setVisible(bool(save_feedback))
        if save_feedback:
            self._save_feedback_timer.stop()
            self._save_feedback_timer.start(3000)
        else:
            self._save_feedback_timer.stop()
        if snapshot.verification_in_progress:
            for port in range(1, 21):
                for control in (
                    self.name_inputs[port],
                    self.internal_inputs[port],
                    self.polarity_inputs[port],
                    self.enabled_checks[port],
                ):
                    control.setEnabled(False)
            self.save_button.setEnabled(False)
        verification = snapshot.verification
        running = verification.phase is HardwareVerificationPhase.RUNNING
        awaiting = (
            verification.phase is HardwareVerificationPhase.AWAITING_CONFIRMATION
        )
        self.verification_progress.setVisible(running)
        self.verification_stop_button.setVisible(verification.can_stop)
        self.verification_negative_button.setVisible(running or awaiting)
        self.verification_positive_button.setVisible(running or awaiting)
        self.verification_panel.setVisible(snapshot.verification_in_progress)
        self.editor_stack.setVisible(not snapshot.verification_in_progress)
        if snapshot.verification_in_progress:
            port = snapshot.verification_port or self._selected_port
            self.editor_card.headerLabel.setText(f"正在验证气口 {port:02d}")
            if running:
                self.verification_task_title.setText(f"正在验证气口 {port:02d}")
                self.verification_task_detail.setText(
                    f"请确认气口 {port:02d} 是否有气流"
                )
                now_ns = self._monotonic_ns()
                remaining = verification.remaining_seconds(now_ns)
                self.verification_remaining_label.setText(
                    f"剩余 {remaining} 秒" if remaining else "正在安全收口"
                )
                duration_ns = max(1, int(verification.duration_s * 1_000_000_000))
                elapsed_ns = max(0, now_ns - int(verification.started_ns or 0))
                self.verification_progress.setValue(
                    min(100, int(elapsed_ns * 100 / duration_ns))
                )
            elif verification.phase is HardwareVerificationPhase.PREPARING:
                self.verification_task_title.setText(
                    f"正在准备验证气口 {port:02d}"
                )
                self.verification_task_detail.setText(
                    "正在确认全关、验证流量与安全路由，请等待准备完成"
                )
                self.verification_remaining_label.setText("准备中")
            else:
                self.verification_task_title.setText(
                    f"气口 {port:02d} 是否正确出气？"
                )
                self.verification_task_detail.setText("请选择刚才观察到的结果")
                remaining = verification.remaining_seconds(self._monotonic_ns())
                self.verification_remaining_label.setText(
                    f"请在 {remaining} 秒内选择检查结果"
                    if remaining
                    else "请选择检查结果"
                )
        else:
            self.editor_card.headerLabel.setText(f"气口 {self._selected_port:02d}")
            self.verification_progress.setValue(0)

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
        if descriptor.verification_valid_for():
            return "physical"
        if descriptor.verification_valid_for(allow_mock=True):
            return "mock"
        return "pending"

    def _update_channel(self, external_port: int, **changes) -> None:
        if self._rendering or self._draft is None:
            return
        channels = list(self._draft.channels)
        index = external_port - 1
        updated = replace(channels[index], **changes)
        updated = replace(
            updated,
            mapping_changed=self._mapping_changed(external_port, updated),
        )
        channels[index] = updated
        self._draft = replace(self._draft, channels=tuple(channels))
        self._refresh_occupied_options()
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
        if self._invalid_preset_targets:
            valve = next(iter(self._invalid_preset_targets))
            self.validation_label.setProperty("valid", False)
            self.validation_label.setText(
                f"配置冲突：控制通道 {valve:02d}："
                f"{self._invalid_preset_targets[valve]}"
            )
            self.validation_label.setVisible(True)
            self.save_button.setEnabled(False)
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
                and self._draft_is_dirty()
                and not self._snapshot.save_in_progress
                and not self._snapshot.verification_in_progress
            )
        self.validation_label.style().unpolish(self.validation_label)
        self.validation_label.style().polish(self.validation_label)
        return candidate

    def _mapping_changed(
        self, external_port: int, channel: HardwareChannelDraft
    ) -> bool:
        if self._snapshot is None:
            return channel.mapping_changed
        base = self._snapshot.profile.registry.by_external_port(external_port)
        return (
            channel.internal_valve != base.internal_valve
            or channel.target != base.target
            or channel.active_high != base.active_high
        )

    def _render_message_visibility(self, status: str, detail: str) -> None:
        del status, detail
        self.status_label.hide()
        self.detail_label.hide()

    def _request_save(self) -> None:
        candidate = self._validate_draft()
        if (
            candidate is not None
            and self._draft is not None
            and self._snapshot is not None
            and self._snapshot.can_save
            and not self._snapshot.save_in_progress
            and not self._snapshot.verification_in_progress
            and self._draft_is_dirty()
        ):
            self.save_requested.emit(candidate, self._draft.revision)

    def _request_rollback(self) -> None:
        if self._snapshot is not None:
            self.rollback_requested.emit(self._snapshot.revision)

    def _request_mock_verification(self, external_port: int) -> None:
        if self._invalid_verification_config_reason:
            return
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
            flow_text = f"{candidate.verification_config.flow_sccm:g} ml/min"
            dialog = MessageBox(
                f"验证气口 {external_port:02d}",
                (
                    f"开始后，请确认气口 {external_port:02d} 是否出气。\n\n"
                    f"验证流量：{flow_text}\n最长时间：{duration_text}"
                ),
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

    def _request_verification_result(self, positive: bool) -> None:
        if (
            self._snapshot is not None
            and self._snapshot.verification.phase
            in {
                HardwareVerificationPhase.RUNNING,
                HardwareVerificationPhase.AWAITING_CONFIRMATION,
            }
            and self._snapshot.verification_port is not None
        ):
            self.verification_result_requested.emit(
                self._snapshot.verification_port, bool(positive)
            )

    def _close_parent_dialog(self) -> None:
        window = self.window()
        if window is not self:
            window.close()

    @staticmethod
    def _verification_text(channel: HardwareChannelDraft) -> str:
        return {
            "unused": "未启用",
            "pending": "待验证",
            "mock": "待现场确认",
            "physical": "可用",
            "failed": "需检查",
        }[HardwareSettingsView._channel_state(channel)]

    def _render_verification_badge(
        self, external_port: int, channel: HardwareChannelDraft
    ) -> None:
        badge = self.verification_labels[external_port]
        badge.setText(self._verification_text(channel))
        level = {
            "unused": InfoLevel.INFOAMTION,
            "pending": InfoLevel.WARNING,
            "mock": InfoLevel.WARNING,
            "physical": InfoLevel.SUCCESS,
            "failed": InfoLevel.ERROR,
        }[self._channel_state(channel)]
        badge.setLevel(level)
