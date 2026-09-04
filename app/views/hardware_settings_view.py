from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ComboBox,
    ExpandSettingCard,
    IndeterminateProgressBar,
    InfoBadge,
    InfoLevel,
    LineEdit,
    MessageBox,
    PrimaryPushButton,
    PushButton,
    ScrollArea,
    SettingCard,
    SettingCardGroup,
    StrongBodyLabel,
    SwitchButton,
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
from app.views.manual_experiment_view import PortTile
from app.views.product_text import user_facing_text


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
        self.overview_buttons: dict[int, PortTile] = {}
        self.name_inputs: dict[int, LineEdit] = {}
        self.internal_inputs: dict[int, ValveComboBox] = {}
        self.target_inputs: dict[int, LineEdit] = {}
        self.polarity_inputs: dict[int, ComboBox] = {}
        self.enabled_checks: dict[int, SwitchButton] = {}
        self.verification_labels: dict[int, BodyLabel] = {}
        self.custom_mapping_labels: dict[int, CaptionLabel] = {}
        self.preset_target_labels: dict[int, CaptionLabel] = {}
        self.mock_buttons: dict[int, PushButton] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 14)
        root.setSpacing(10)
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

        header = QHBoxLayout()
        heading = StrongBodyLabel("气口与硬件")
        heading.setObjectName("settingsHeading")
        self.profile_name_label = CaptionLabel("配置：-")
        self.profile_name_label.setObjectName("mutedText")
        header.addWidget(heading)
        header.addSpacing(10)
        header.addWidget(self.profile_name_label)
        header.addStretch()
        body.addLayout(header)

        overview_group = SettingCardGroup("气口总览", scroll_content)
        overview = SettingCard(FIF.VIEW, "面板气口 01–20", "固定 2×10 布局", overview_group)
        overview.setMinimumHeight(190)
        self.overview_layout = QGridLayout()
        self.overview_layout.setHorizontalSpacing(6)
        self.overview_layout.setVerticalSpacing(8)
        for port in range(1, 21):
            button = PortTile(port)
            button.setObjectName("settingsPortButton")
            button.clicked.connect(lambda p=port: self.select_port(p))
            self.overview_buttons[port] = button
            self.overview_layout.addWidget(button, (port - 1) // 10, (port - 1) % 10)
        overview.hBoxLayout.addLayout(self.overview_layout)
        overview_group.addSettingCard(overview)
        body.addWidget(overview_group)

        editor_group = SettingCardGroup("单口设置", scroll_content)
        editor = SettingCard(FIF.EDIT, "编辑气口 01", "别名、控制通道和启用状态", editor_group)
        editor.setMinimumHeight(270)
        editor_layout = QVBoxLayout()
        self.editor_title = StrongBodyLabel("编辑气口 01")
        self.editor_title.setObjectName("moduleTitle")
        editor_layout.addWidget(self.editor_title)
        self.editor_stack = QStackedWidget()
        self.advanced_stack = QStackedWidget()
        for port in range(1, 21):
            self.editor_stack.addWidget(self._build_port_editor(port))
            self.advanced_stack.addWidget(self._build_port_advanced_editor(port))
        editor_layout.addWidget(self.editor_stack)
        editor.hBoxLayout.addLayout(editor_layout)
        editor_group.addSettingCard(editor)
        body.addWidget(editor_group)

        self.advanced_card = ExpandSettingCard(
            FIF.DEVELOPER_TOOLS,
            "高级线路信息",
            "标准通道预设与当前 NI 控制目标（只读）",
            scroll_content,
        )
        self.advanced_panel = self._build_advanced_panel()
        self.advanced_card.viewLayout.addWidget(self.advanced_panel)
        self.advanced_toggle = self.advanced_card
        body.addWidget(self.advanced_card)

        self.validation_label = BodyLabel("")
        self.validation_label.setObjectName("validationMessage")
        self.validation_label.setWordWrap(True)
        self.status_label = BodyLabel("")
        self.status_label.setObjectName("pageStatus")
        self.detail_label = CaptionLabel("")
        self.detail_label.setObjectName("pageDetail")
        self.detail_label.setWordWrap(True)
        self.validation_label.setVisible(False)
        self.status_label.setVisible(False)
        self.detail_label.setVisible(False)
        body.addWidget(self.validation_label)
        body.addWidget(self.status_label)
        body.addWidget(self.detail_label)
        body.addStretch()
        root.addWidget(self.settings_scroll, 1)

        actions = QHBoxLayout()
        self.save_button = PrimaryPushButton(FIF.SAVE, "保存设置", self)
        self.save_button.setObjectName("primaryButton")
        self.rollback_button = PushButton("恢复上次设置", self)
        self.rollback_button.hide()
        self.close_button = PushButton("关闭", self)
        self.close_button.hide()
        self.verification_progress = IndeterminateProgressBar(self, start=False)
        self.verification_progress.hide()
        self.verification_stop_button = PushButton(FIF.CANCEL, "立即停止", self)
        self.verification_stop_button.hide()
        actions.addWidget(self.verification_progress)
        actions.addWidget(self.verification_stop_button)
        actions.addWidget(self.save_button)
        actions.addWidget(self.rollback_button)
        actions.addStretch()
        actions.addWidget(self.close_button)
        root.addLayout(actions)

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
        target_input = LineEdit()
        target_input.setReadOnly(True)
        target_input.setPlaceholderText("由标准 20 通道预设自动解析")
        polarity_input = ComboBox()
        polarity_input.addItem("高电平开启", userData=True)
        polarity_input.addItem("低电平开启", userData=False)
        polarity_input.setEnabled(False)
        self.target_inputs[port] = target_input
        self.polarity_inputs[port] = polarity_input
        layout.addWidget(CaptionLabel("NI 控制目标"), 0, 0)
        layout.addWidget(target_input, 0, 1)
        layout.addWidget(CaptionLabel("开启电平"), 1, 0)
        layout.addWidget(polarity_input, 1, 1)
        custom = CaptionLabel("", page)
        custom.setStyleSheet("color: #E2AD50;")
        self.custom_mapping_labels[port] = custom
        layout.addWidget(custom, 2, 0, 1, 2)
        return page

    def _build_advanced_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("advancedPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 10, 12, 12)
        self.advanced_port_label = StrongBodyLabel("气口 01 的线路")
        self.advanced_port_label.setObjectName("moduleTitle")
        layout.addWidget(self.advanced_port_label)
        layout.addWidget(self.advanced_stack)
        preset_title = StrongBodyLabel("控制通道预设（只读）", panel)
        layout.addWidget(preset_title)
        preset_grid = QGridLayout()
        preset_grid.setHorizontalSpacing(18)
        preset_grid.setVerticalSpacing(5)
        for valve in range(1, 21):
            column = 0 if valve <= 10 else 2
            row = (valve - 1) % 10
            preset_grid.addWidget(CaptionLabel(f"控制通道 {valve:02d}"), row, column)
            target_label = CaptionLabel("未配置", panel)
            target_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.preset_target_labels[valve] = target_label
            preset_grid.addWidget(target_label, row, column + 1)
        layout.addLayout(preset_grid)
        connection_grid = QGridLayout()
        connection_grid.setHorizontalSpacing(12)
        connection_grid.setVerticalSpacing(9)
        connection_grid.setColumnStretch(1, 1)
        self.serial_port_input = LineEdit()
        self.serial_port_input.setPlaceholderText("例如 COM6")
        self.ni_device_ids_input = LineEdit()
        self.ni_device_ids_input.setPlaceholderText("例如 Dev1, Dev2")
        self.alicat_unit_inputs: dict[str, LineEdit] = {}
        connection_grid.addWidget(CaptionLabel("串口"), 0, 0)
        connection_grid.addWidget(self.serial_port_input, 0, 1)
        connection_grid.addWidget(CaptionLabel("NI 设备名"), 1, 0)
        connection_grid.addWidget(self.ni_device_ids_input, 1, 1)
        for row, channel in enumerate(("A", "B", "C"), start=2):
            unit_input = LineEdit()
            unit_input.setMaxLength(1)
            self.alicat_unit_inputs[channel] = unit_input
            connection_grid.addWidget(CaptionLabel(f"流量控制器 {channel}"), row, 0)
            connection_grid.addWidget(unit_input, row, 1)
        layout.addLayout(connection_grid)
        for control in (
            self.serial_port_input,
            self.ni_device_ids_input,
            *self.alicat_unit_inputs.values(),
        ):
            control.setReadOnly(True)
        return panel

    @property
    def draft(self) -> HardwareProfileDraft | None:
        return self._draft

    def select_port(self, external_port: int) -> None:
        port = max(1, min(20, int(external_port)))
        self._selected_port = port
        self.editor_stack.setCurrentIndex(port - 1)
        self.advanced_stack.setCurrentIndex(port - 1)
        self.editor_title.setText(f"编辑气口 {port:02d}")
        self.advanced_port_label.setText(f"气口 {port:02d} 的线路")
        for number, button in self.overview_buttons.items():
            button.set_configuration_state(
                display_name=button.alias,
                configured=bool(button.property("channelEnabled")),
                selected=number == port,
            )

    def _toggle_advanced(self, expanded: bool) -> None:
        self.advanced_panel.setVisible(expanded)
        if expanded:
            window = self.window()
            screen = window.screen()
            available_height = screen.availableGeometry().height() if screen else 800
            window.resize(max(window.width(), 960), min(780, available_height - 40))

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
                self.target_inputs[port].setText(channel.target)
                polarity_index = self.polarity_inputs[port].findData(channel.active_high)
                self.polarity_inputs[port].setCurrentIndex(max(0, polarity_index))
                self.enabled_checks[port].setChecked(channel.enabled)
                self._render_verification_badge(port, channel)
                custom = snapshot.profile.channel_uses_custom_target(port)
                self.custom_mapping_labels[port].setText(
                    "历史自定义 NI 映射（普通设置不会覆盖，修改控制通道后恢复标准预设）"
                    if custom
                    else "标准 20 通道预设"
                )
                self._render_overview_button(channel)
            for valve, label in self.preset_target_labels.items():
                preset = snapshot.profile.target_preset
                label.setText("未配置" if preset is None else preset.target_for(valve))
        finally:
            self._rendering = False

        self.profile_name_label.setText(f"配置：{user_facing_text(snapshot.profile.profile_name)}")
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
        self._apply_permissions(snapshot)
        self._validate_draft()

    def _apply_permissions(self, snapshot: HardwareSettingsSnapshot) -> None:
        editable = snapshot.can_edit and not snapshot.save_in_progress
        for control in (
            self.serial_port_input,
            self.ni_device_ids_input,
            *self.alicat_unit_inputs.values(),
        ):
            control.setEnabled(True)
            control.setReadOnly(True)
        for port in range(1, 21):
            for control in (
                self.name_inputs[port],
                self.internal_inputs[port],
                self.enabled_checks[port],
            ):
                control.setEnabled(editable)
            self.target_inputs[port].setEnabled(True)
            self.polarity_inputs[port].setEnabled(False)
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
                "请求现场验证"
                if snapshot.can_request_physical_verification
                else "开始模拟验证"
            )
        self.save_button.setEnabled(snapshot.can_save and not snapshot.save_in_progress)
        self.rollback_button.setEnabled(
            snapshot.can_save and snapshot.rollback_available and not snapshot.save_in_progress
        )
        self.save_button.setText("正在保存…" if snapshot.save_in_progress else "保存设置")
        self.verification_progress.setVisible(snapshot.verification_in_progress)
        self.verification_stop_button.setVisible(snapshot.verification_in_progress)
        if snapshot.verification_in_progress:
            self.verification_progress.start()
            self.status_label.setVisible(True)
        else:
            self.verification_progress.stop()

    def _render_overview_button(self, channel: HardwareChannelDraft) -> None:
        button = self.overview_buttons[channel.external_port]
        if isinstance(button, PortTile):
            button.set_configuration_state(
                display_name=channel.display_name,
                configured=channel.enabled,
                selected=channel.external_port == self._selected_port,
            )
        else:
            button.setText(settings_port_text(channel))
        button.setProperty("channelEnabled", channel.enabled)
        button.setAccessibleDescription(
            f"{'已启用' if channel.enabled else '未启用'}；{self._verification_text(channel)}"
        )
        button.style().unpolish(button)
        button.style().polish(button)
        button.update()

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
        self.custom_mapping_labels[external_port].setText("标准 20 通道预设")

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
            self.save_button.setEnabled(self._snapshot.can_save and not self._snapshot.save_in_progress)
        self.validation_label.style().unpolish(self.validation_label)
        self.validation_label.style().polish(self.validation_label)
        return candidate

    def _render_message_visibility(self, status: str, detail: str) -> None:
        text = f"{status} {detail}"
        actionable = any(marker in text for marker in ("失败", "异常", "冲突", "无法", "未完成", "需要立即"))
        self.status_label.setVisible(actionable and bool(status.strip()))
        self.detail_label.setVisible(actionable and bool(detail.strip()))

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
        if snapshot.can_request_physical_verification:
            self.physical_verify_requested.emit(external_port)
            return
        if not snapshot.can_mock_verify:
            return
        if self.isVisible():
            channel = candidate.registry.by_external_port(external_port)
            duration = snapshot.simulation_verification_duration_s
            duration_text = f"{duration:g} 秒"
            dialog = MessageBox(
                f"验证气口 {external_port:02d}",
                (
                    f"当前映射：控制通道 {int(channel.internal_valve):02d}\n\n"
                    f"测试持续时间：约 {duration_text}\n"
                    "测试流量：2500 ml/min 仅为界面意向；Mock 不驱动 MFC。\n\n"
                    "本次只运行模拟开关回路；验证期间不能执行其他操作。"
                ),
                self.window(),
            )
            dialog.yesButton.setText("开始验证")
            dialog.cancelButton.setText("取消")
            if not dialog.exec():
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
        if channel.mapping_changed:
            return "配置已变更，需要重新验证"
        verification = channel.verification
        date = ""
        if verification.verified_at:
            try:
                parsed = datetime.fromisoformat(verification.verified_at)
                if "T" in verification.verified_at or " " in verification.verified_at:
                    rendered = parsed.astimezone().strftime("%Y-%m-%d %H:%M")
                else:
                    rendered = parsed.strftime("%Y-%m-%d")
                date = f" · {rendered}"
            except ValueError:
                date = f" · {verification.verified_at}"
        labels = {
            VerificationStatus.PENDING: "需要验证",
            VerificationStatus.MOCK_VERIFIED: f"模拟验证完成{date}",
            VerificationStatus.PHYSICAL_VERIFIED: f"已验证{date}",
            VerificationStatus.MAPPING_CHANGED: "配置已变更，需要重新验证",
            VerificationStatus.INCOMPLETE: "验证未完成",
            VerificationStatus.FAILED: "验证失败",
        }
        return labels[verification.status]

    def _render_verification_badge(
        self, external_port: int, channel: HardwareChannelDraft
    ) -> None:
        badge = self.verification_labels[external_port]
        badge.setText(self._verification_text(channel))
        status = (
            VerificationStatus.MAPPING_CHANGED
            if channel.mapping_changed
            else channel.verification.status
        )
        level = {
            VerificationStatus.PENDING: InfoLevel.INFOAMTION,
            VerificationStatus.MOCK_VERIFIED: InfoLevel.SUCCESS,
            VerificationStatus.PHYSICAL_VERIFIED: InfoLevel.SUCCESS,
            VerificationStatus.MAPPING_CHANGED: InfoLevel.WARNING,
            VerificationStatus.INCOMPLETE: InfoLevel.WARNING,
            VerificationStatus.FAILED: InfoLevel.ERROR,
        }[status]
        badge.setLevel(level)
