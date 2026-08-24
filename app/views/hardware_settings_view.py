from __future__ import annotations

from dataclasses import dataclass, replace

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.models import (
    ChannelDescriptor,
    ChannelVerification,
    HardwareConnectionConfig,
    HardwareProfile,
    SelectorConfig,
    VerificationStatus,
)
from app.views.manual_experiment_view import ConsolePortButton
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
    status_text: str = "设置当前为只读。"
    detail_text: str = "断开设备后可以修改并保存。"


def settings_port_text(channel: HardwareChannelDraft) -> str:
    alias = channel.display_name.strip()
    if alias == f"气口 {channel.external_port}":
        alias = ""
    return f"{alias}\n气口 {channel.external_port}" if alias else f"气口 {channel.external_port}"


class HardwareSettingsView(QWidget):
    """Two-row physical port overview with one-port-at-a-time editing."""

    candidate_changed = Signal(object)
    save_requested = Signal(object, int)
    rollback_requested = Signal(int)
    mock_verify_requested = Signal(int, object)
    physical_verify_requested = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("hardwareSettings")
        self._rendering = False
        self._snapshot: HardwareSettingsSnapshot | None = None
        self._draft: HardwareProfileDraft | None = None
        self._selected_port = 1
        self.overview_buttons: dict[int, QPushButton] = {}
        self.name_inputs: dict[int, QLineEdit] = {}
        self.internal_inputs: dict[int, QSpinBox] = {}
        self.target_inputs: dict[int, QLineEdit] = {}
        self.polarity_inputs: dict[int, QComboBox] = {}
        self.enabled_checks: dict[int, QCheckBox] = {}
        self.verification_labels: dict[int, QLabel] = {}
        self.mock_buttons: dict[int, QPushButton] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 14)
        root.setSpacing(10)
        self.settings_scroll = QScrollArea()
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
        heading = QLabel("气口设置")
        heading.setObjectName("settingsHeading")
        self.profile_name_label = QLabel("配置：-")
        self.profile_name_label.setObjectName("mutedText")
        header.addWidget(heading)
        header.addSpacing(10)
        header.addWidget(self.profile_name_label)
        header.addStretch()
        body.addLayout(header)

        overview = QFrame()
        overview.setObjectName("settingsCard")
        overview_layout = QVBoxLayout(overview)
        overview_layout.setContentsMargins(12, 10, 12, 12)
        overview_title = QHBoxLayout()
        title = QLabel("气口总览")
        title.setObjectName("moduleTitle")
        overview_title.addWidget(title)
        overview_title.addStretch()
        overview_layout.addLayout(overview_title)
        self.overview_layout = QGridLayout()
        self.overview_layout.setHorizontalSpacing(6)
        self.overview_layout.setVerticalSpacing(8)
        for port in range(1, 21):
            button = ConsolePortButton(port)
            button.setObjectName("settingsPortButton")
            button.clicked.connect(lambda _checked=False, p=port: self.select_port(p))
            self.overview_buttons[port] = button
            self.overview_layout.addWidget(button, (port - 1) // 10, (port - 1) % 10)
        overview_layout.addLayout(self.overview_layout)
        body.addWidget(overview)

        editor = QFrame()
        editor.setObjectName("settingsCard")
        editor_layout = QVBoxLayout(editor)
        editor_layout.setContentsMargins(12, 10, 12, 12)
        self.editor_title = QLabel("编辑气口 1")
        self.editor_title.setObjectName("moduleTitle")
        editor_layout.addWidget(self.editor_title)
        self.editor_stack = QStackedWidget()
        self.advanced_stack = QStackedWidget()
        for port in range(1, 21):
            self.editor_stack.addWidget(self._build_port_editor(port))
            self.advanced_stack.addWidget(self._build_port_advanced_editor(port))
        editor_layout.addWidget(self.editor_stack)
        body.addWidget(editor)

        self.advanced_toggle = QToolButton()
        self.advanced_toggle.setObjectName("advancedToggle")
        self.advanced_toggle.setText("高级设置")
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.advanced_toggle.toggled.connect(self._toggle_advanced)
        body.addWidget(self.advanced_toggle)
        self.advanced_panel = self._build_advanced_panel()
        self.advanced_panel.setVisible(False)
        body.addWidget(self.advanced_panel)

        self.validation_label = QLabel("")
        self.validation_label.setObjectName("validationMessage")
        self.validation_label.setWordWrap(True)
        self.status_label = QLabel("")
        self.status_label.setObjectName("pageStatus")
        self.detail_label = QLabel("")
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
        self.save_button = QPushButton("保存设置")
        self.save_button.setObjectName("primaryButton")
        self.rollback_button = QPushButton("恢复上次设置")
        self.close_button = QPushButton("关闭")
        actions.addWidget(self.save_button)
        actions.addWidget(self.rollback_button)
        actions.addStretch()
        actions.addWidget(self.close_button)
        root.addLayout(actions)

        self.save_button.clicked.connect(self._request_save)
        self.rollback_button.clicked.connect(self._request_rollback)
        self.close_button.clicked.connect(self._close_parent_dialog)
        self.select_port(1)

    def _build_port_editor(self, port: int) -> QWidget:
        page = QWidget()
        layout = QGridLayout(page)
        layout.setContentsMargins(0, 2, 0, 0)
        layout.setHorizontalSpacing(10)
        layout.setVerticalSpacing(7)
        enabled = QCheckBox("启用这个气口")
        name_input = QLineEdit()
        name_input.setMaxLength(80)
        name_input.setPlaceholderText("例如：薄荷；留空时显示气口编号")
        internal_input = QSpinBox()
        internal_input.setRange(0, 20)
        internal_input.setSpecialValueText("未配置")
        verification = QLabel("待测试")
        verification.setObjectName("verificationStatus")
        test_button = QPushButton("测试气口")
        test_button.setObjectName("testPortButton")
        self.name_inputs[port] = name_input
        self.internal_inputs[port] = internal_input
        self.enabled_checks[port] = enabled
        self.verification_labels[port] = verification
        self.mock_buttons[port] = test_button
        layout.addWidget(enabled, 0, 0, 1, 2)
        layout.addWidget(QLabel("显示名称"), 1, 0)
        layout.addWidget(name_input, 1, 1)
        layout.addWidget(QLabel("软件逻辑通道"), 2, 0)
        layout.addWidget(internal_input, 2, 1)
        layout.addWidget(QLabel("测试状态"), 3, 0)
        status_row = QHBoxLayout()
        status_row.addWidget(verification, 1)
        status_row.addWidget(test_button)
        layout.addLayout(status_row, 3, 1)
        name_input.textChanged.connect(lambda value, p=port: self._update_channel(p, display_name=value))
        internal_input.valueChanged.connect(
            lambda value, p=port: self._update_channel(
                p,
                internal_valve=value or None,
                mapping_changed=True,
            )
        )
        enabled.toggled.connect(lambda value, p=port: self._update_channel(p, enabled=value))
        test_button.clicked.connect(lambda _checked=False, p=port: self._request_mock_verification(p))
        return page

    def _build_port_advanced_editor(self, port: int) -> QWidget:
        page = QWidget()
        layout = QGridLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(12)
        layout.setVerticalSpacing(9)
        layout.setColumnStretch(1, 1)
        target_input = QLineEdit()
        target_input.setPlaceholderText("例如 Dev1/P0.1")
        polarity_input = QComboBox()
        polarity_input.addItem("高电平开启", True)
        polarity_input.addItem("低电平开启", False)
        self.target_inputs[port] = target_input
        self.polarity_inputs[port] = polarity_input
        layout.addWidget(QLabel("NI 控制目标"), 0, 0)
        layout.addWidget(target_input, 0, 1)
        layout.addWidget(QLabel("极性"), 1, 0)
        layout.addWidget(polarity_input, 1, 1)
        target_input.textChanged.connect(
            lambda value, p=port: self._update_channel(
                p,
                target=value,
                mapping_changed=True,
            )
        )
        polarity_input.currentIndexChanged.connect(
            lambda _index, p=port: self._update_channel(
                p,
                active_high=bool(self.polarity_inputs[p].currentData()),
                mapping_changed=True,
            )
        )
        return page

    def _build_advanced_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("advancedPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 10, 12, 12)
        self.advanced_port_label = QLabel("气口 1 的线路")
        self.advanced_port_label.setObjectName("moduleTitle")
        layout.addWidget(self.advanced_port_label)
        layout.addWidget(self.advanced_stack)
        connection_grid = QGridLayout()
        connection_grid.setHorizontalSpacing(12)
        connection_grid.setVerticalSpacing(9)
        connection_grid.setColumnStretch(1, 1)
        self.serial_port_input = QLineEdit()
        self.serial_port_input.setPlaceholderText("例如 COM6")
        self.ni_device_ids_input = QLineEdit()
        self.ni_device_ids_input.setPlaceholderText("例如 Dev1, Dev2")
        self.alicat_unit_inputs: dict[str, QLineEdit] = {}
        connection_grid.addWidget(QLabel("串口"), 0, 0)
        connection_grid.addWidget(self.serial_port_input, 0, 1)
        connection_grid.addWidget(QLabel("NI 设备名"), 1, 0)
        connection_grid.addWidget(self.ni_device_ids_input, 1, 1)
        for row, channel in enumerate(("A", "B", "C"), start=2):
            unit_input = QLineEdit()
            unit_input.setMaxLength(1)
            self.alicat_unit_inputs[channel] = unit_input
            connection_grid.addWidget(QLabel(f"流量控制器 {channel}"), row, 0)
            connection_grid.addWidget(unit_input, row, 1)
        layout.addLayout(connection_grid)
        self.serial_port_input.textChanged.connect(lambda value: self._update_connections(serial_port=value))
        self.ni_device_ids_input.textChanged.connect(lambda value: self._update_connections(ni_device_ids_text=value))
        for channel, input_control in self.alicat_unit_inputs.items():
            input_control.textChanged.connect(
                lambda value, key=channel: self._update_connections(**{f"alicat_{key.lower()}_unit_id": value})
            )
        return panel

    @property
    def draft(self) -> HardwareProfileDraft | None:
        return self._draft

    def select_port(self, external_port: int) -> None:
        port = max(1, min(20, int(external_port)))
        self._selected_port = port
        self.editor_stack.setCurrentIndex(port - 1)
        self.advanced_stack.setCurrentIndex(port - 1)
        self.editor_title.setText(f"编辑气口 {port}")
        self.advanced_port_label.setText(f"气口 {port} 的线路")
        for number, button in self.overview_buttons.items():
            button.setChecked(number == port)
            button.setProperty("selected", number == port)
            button.setProperty(
                "portState",
                "selected" if number == port else ("available" if button.property("channelEnabled") else "unavailable"),
            )
            button.style().unpolish(button)
            button.style().polish(button)
            button.update()

    def _toggle_advanced(self, expanded: bool) -> None:
        self.advanced_panel.setVisible(expanded)
        self.advanced_toggle.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        if expanded:
            window = self.window()
            screen = window.screen()
            available_height = screen.availableGeometry().height() if screen else 800
            window.resize(max(window.width(), 960), min(780, available_height - 40))

    def render_snapshot(self, snapshot: HardwareSettingsSnapshot) -> None:
        if not isinstance(snapshot, HardwareSettingsSnapshot):
            raise TypeError("气口设置只能显示有效配置。")
        self._snapshot = snapshot
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
                self.verification_labels[port].setText(self._verification_text(channel))
                self._render_overview_button(channel)
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
    ) -> None:
        self.render_snapshot(
            HardwareSettingsSnapshot(
                profile=profile,
                revision=revision,
                can_edit=can_save,
                can_save=can_save,
                can_mock_verify=can_save,
                rollback_available=rollback_available,
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
    ) -> None:
        if self._snapshot is None:
            return
        snapshot = replace(
            self._snapshot,
            can_edit=bool(can_save),
            can_save=bool(can_save),
            can_mock_verify=bool(can_save),
            rollback_available=(
                self._snapshot.rollback_available if rollback_available is None else bool(rollback_available)
            ),
            status_text=message or ("可以编辑并保存。" if can_save else "设置当前为只读。"),
        )
        self._snapshot = snapshot
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
            control.setEnabled(editable)
        for port in range(1, 21):
            for control in (
                self.name_inputs[port],
                self.internal_inputs[port],
                self.target_inputs[port],
                self.polarity_inputs[port],
                self.enabled_checks[port],
            ):
                control.setEnabled(editable)
            self.mock_buttons[port].setEnabled(snapshot.can_mock_verify and not snapshot.save_in_progress)
        self.save_button.setEnabled(snapshot.can_save and not snapshot.save_in_progress)
        self.rollback_button.setEnabled(
            snapshot.can_save and snapshot.rollback_available and not snapshot.save_in_progress
        )
        self.save_button.setText("正在保存…" if snapshot.save_in_progress else "保存设置")

    def _render_overview_button(self, channel: HardwareChannelDraft) -> None:
        button = self.overview_buttons[channel.external_port]
        if isinstance(button, ConsolePortButton):
            button.set_port_content(channel.display_name)
        else:
            button.setText(settings_port_text(channel))
        button.setProperty("channelEnabled", channel.enabled)
        button.setProperty(
            "portState",
            "selected" if button.isChecked() else ("available" if channel.enabled else "unavailable"),
        )
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
        self.verification_labels[external_port].setText(self._verification_text(channels[index]))
        self._render_overview_button(channels[index])
        candidate = self._validate_draft()
        if candidate is not None:
            self.candidate_changed.emit(candidate)

    def _update_connections(self, **changes) -> None:
        if self._rendering or self._draft is None:
            return
        self._draft = replace(self._draft, **changes)
        candidate = self._validate_draft()
        if candidate is not None:
            self.candidate_changed.emit(candidate)

    def _validate_draft(self) -> HardwareProfile | None:
        if self._draft is None:
            return None
        try:
            candidate = self._draft.to_profile()
        except ValueError as exc:
            self.validation_label.setProperty("valid", False)
            self.validation_label.setText(f"配置冲突：{user_facing_text(exc)}")
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
        if candidate is not None:
            self.mock_verify_requested.emit(external_port, candidate)

    def _close_parent_dialog(self) -> None:
        window = self.window()
        if window is not self:
            window.close()

    @staticmethod
    def _verification_text(channel: HardwareChannelDraft) -> str:
        if channel.mapping_changed:
            return "配置已变更，请重新测试"
        verification = channel.verification
        date = f" · {verification.verified_at}" if verification.verified_at else ""
        labels = {
            VerificationStatus.PENDING: "待测试",
            VerificationStatus.MOCK_VERIFIED: f"测试通过{date}",
            VerificationStatus.PHYSICAL_VERIFIED: f"测试通过{date}",
            VerificationStatus.MAPPING_CHANGED: "配置已变更，请重新测试",
            VerificationStatus.INCOMPLETE: "测试未完成",
            VerificationStatus.FAILED: "测试失败，请检查线路",
        }
        return labels[verification.status]
